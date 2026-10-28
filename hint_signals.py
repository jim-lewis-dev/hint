"""Persistent signal definitions, scheduling and optional RTC wakeups.

Callers hold the table lock while changing a signal or starting a timed action.
The JSON table is replaced atomically; the lock has its own stable inode.
"""

from __future__ import annotations

from datetime import datetime
import fcntl
import json
import math
import os
import re
import subprocess
import sys
import tempfile

from hint_config import (
    TABLE, LOCK, NAME, TIME_FORMAT, RTC_HELPER, HintError, config, read_json,
)
from hint_playback import launch

RTC_TIMEOUT = 5
_CALENDAR_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}")


def locked():
    """Return a context-managed, process-wide advisory lock for the table."""
    LOCK.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd = os.open(LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    handle = os.fdopen(fd, "a+")
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
    except BaseException:
        handle.close()
        raise
    return handle


def read_table() -> dict:
    table = read_json(TABLE, {})
    if not isinstance(table, dict):
        raise HintError("signals.json must contain a JSON object")
    for name, entry in table.items():
        validate(name, entry)
    return table


def write_table(table: dict) -> None:
    """Persist a whole table; the caller is responsible for holding its lock."""
    TABLE.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=TABLE.parent, prefix=".signals.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(table, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, TABLE)
    except OSError as exc:
        raise HintError(f"could not save signals.json: {exc}") from exc
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

    # The rename has committed the visible table. A directory flush failure
    # must not make callers restore the old RTC alarm for the new table.
    try:
        directory = os.open(TABLE.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        print(f"HINT storage: signals saved but directory sync failed: {exc}",
              file=sys.stderr, flush=True)


def parse_time(value: str) -> datetime:
    if not isinstance(value, str) or not _CALENDAR_TIME.fullmatch(value):
        raise HintError(f"invalid time: expected {TIME_FORMAT}")
    try:
        return datetime.strptime(value, TIME_FORMAT)
    except ValueError as exc:
        raise HintError(f"invalid time: expected {TIME_FORMAT}") from exc


def validate_name(name: str) -> None:
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise HintError("signal name may contain only letters, digits, '_' and '-'")


def validate(name: str, entry: object) -> None:
    validate_name(name)
    if not isinstance(entry, dict) or set(entry) - {"time", "wake", "action"}:
        raise HintError(f"invalid signal entry: {name}")
    if not isinstance(entry.get("action"), str) or not entry["action"].strip():
        raise HintError(f"signal {name!r} requires an action")
    if "time" in entry:
        parse_time(entry["time"])
    if "wake" in entry and not isinstance(entry["wake"], bool):
        raise HintError(f"signal {name!r} wake must be a boolean")
    if entry.get("wake") and "time" not in entry:
        raise HintError(f"signal {name!r} cannot wake without a time")


def _next_wake(table: dict, now: datetime) -> datetime | None:
    times = [parse_time(entry["time"]) for entry in table.values() if entry.get("wake")]
    return min((when for when in times if when > now), default=None)


def _wake_argument(when: datetime | None, now: datetime) -> str:
    argument = "disable"
    if when is not None:
        lead = config().get("wake_lead_seconds", 10)
        if type(lead) is not int or lead < 0:
            raise HintError("wake_lead_seconds must be a nonnegative integer")
        # Convert each local calendar time to an instant before subtracting:
        # a DST change can make elapsed time differ from wall-clock time.
        # Subtract the integer lead after rounding to avoid large-lead overflow.
        argument = str(max(5, math.ceil(when.timestamp() - now.timestamp()) - lead))
    return argument


def _run_wake(argument: str) -> None:
    try:
        result = subprocess.run(
            ["sudo", "-n", RTC_HELPER, argument],
            text=True, capture_output=True, timeout=RTC_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise HintError("RTC wake helper timed out") from exc
    except OSError as exc:
        raise HintError(f"could not run RTC wake helper: {exc}") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip()
        raise HintError((detail or f"could not arm RTC wake ({argument})") +
                        "; install wake support with ./install.sh --enable-wake")


def arm_wake(table: dict | None = None) -> None:
    """Rearm a future HINT wake; an empty table never requires RTC access.

    Passing a table is intended for callers already holding the table lock.
    Calling without one keeps that lock through the actual RTC update.
    """
    if table is None:
        with locked():
            arm_wake(read_table())
        return
    now = datetime.now()
    when = _next_wake(table, now)
    if when is not None:
        _run_wake(_wake_argument(when, now))


def _restore_wake(argument: str) -> None:
    try:
        _run_wake(argument)
    except HintError as exc:
        print(f"HINT RTC: could not restore previous alarm: {exc}", file=sys.stderr, flush=True)


def _commit_table(previous: dict, updated: dict) -> None:
    """Commit a user change, keeping the previous alarm if the change fails."""
    now = datetime.now()
    old_wake = _next_wake(previous, now)
    new_wake = _next_wake(updated, now)
    clearing_last_wake = (any(entry.get("wake") for entry in previous.values())
                          and not any(entry.get("wake") for entry in updated.values()))
    # A near-due event can leave an alarm pending for the helper's minimum
    # five-second delay even though its signal time has just passed.
    if old_wake == new_wake and not clearing_last_wake:
        write_table(updated)
        return
    # Validate configuration before running the privileged helper, including
    # before attempting any rollback. Invalid input must not touch the RTC.
    new_argument = _wake_argument(new_wake, now)
    old_argument = _wake_argument(old_wake, now)
    try:
        _run_wake(new_argument)
    except HintError:
        # The helper may have cleared the old alarm before a failed arm.
        _restore_wake(old_argument)
        raise
    try:
        write_table(updated)
    except Exception:
        _restore_wake(old_argument)
        raise


def fire(name: str, scheduled_time: str | None = None) -> dict:
    validate_name(name)
    with locked():
        table = read_table()
        entry = table.get(name)
        if entry is None:
            return {"signal": name, "fired": False, "error": "signal not found"}
        if scheduled_time is not None and entry.get("time") != scheduled_time:
            return {"signal": name, "fired": False, "error": "stale time"}
        try:
            # Starting a child returns promptly. Keep the lock until a timed
            # entry is removed so a second process cannot start it as well.
            launch(entry["action"])
        except (OSError, HintError) as exc:
            return {"signal": name, "fired": False, "error": str(exc)}
        if scheduled_time is not None:
            updated = dict(table)
            del updated[name]
            write_table(updated)
            now = datetime.now()
            # An already-started action must stay removed if RTC repair fails.
            # Do not treat alarm trouble as a reason to launch it again.
            if entry.get("wake"):
                try:
                    _run_wake(_wake_argument(_next_wake(updated, now), now))
                except HintError as exc:
                    print(f"HINT RTC: {exc}", file=sys.stderr, flush=True)
    return {"signal": name, "fired": True}


def get_signal(name: str) -> dict:
    validate_name(name)
    with locked():
        return {name: read_table().get(name)}


def list_signals() -> dict:
    with locked():
        return read_table()


def set_signal(name: str, action: str, when: str | None, wake: bool) -> dict:
    if not isinstance(wake, bool):
        raise HintError("wake must be a boolean")
    entry = {"action": action}
    if when is not None:
        if parse_time(when) <= datetime.now():
            raise HintError("time must be in the future")
        entry["time"] = when
    if wake:
        entry["wake"] = True
    validate(name, entry)
    with locked():
        table = read_table()
        updated = dict(table)
        updated[name] = entry
        _commit_table(table, updated)
    return {name: entry}


def remove_signal(name: str) -> dict:
    validate_name(name)
    with locked():
        table = read_table()
        removed = name in table
        if removed:
            updated = dict(table)
            del updated[name]
            _commit_table(table, updated)
    return {"signal": name, "removed": removed}


def fire_due() -> None:
    now = datetime.now()
    with locked():
        table = read_table()
        due = sorted((parse_time(entry["time"]), name, entry["time"])
                     for name, entry in table.items()
                     if "time" in entry and parse_time(entry["time"]) <= now)
    for _, name, when in due:
        result = fire(name, when)
        if not result.get("fired") and result.get("error") not in {"signal not found", "stale time"}:
            print(json.dumps(result), file=sys.stderr, flush=True)
