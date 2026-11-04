"""Persistent signal definitions, scheduling and optional RTC wakeups.

Callers hold the table lock while changing a signal or starting a timed action.
The JSON table is replaced atomically; the lock has its own stable inode.
"""

from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from hint_config import (
    TABLE, LOCK, NAME, TIME_FORMAT, RTC_HELPER, HintError, config, read_json, sanitize_error,
)

RTC_TIMEOUT = 5
_CALENDAR_TIME = re.compile(
    r"(?P<year>[0-9]{4})(?:-(?P<month>[0-9]{2})(?:-(?P<day>[0-9]{2}))?)?"
    r"(?: (?P<hour>[0-9]{2})(?::(?P<minute>[0-9]{2})(?::(?P<second>[0-9]{2}))?)?)?"
)
_GENERATED_NAME = re.compile(r"HINT-[0-9]+", re.IGNORECASE)
# Failed post-launch persistence must not launch again on the next tick. This
# state belongs only to the service session; no extra history file is created.
_PENDING_CLEARS = set()
_REPORTED_ERRORS = {}


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


def local_zone():
    """Read the receiving computer's timezone, including its future DST rules."""
    setting = os.environ.get("TZ")
    if setting:
        setting = setting.removeprefix(":")
        try:
            return ZoneInfo(setting)
        except (ZoneInfoNotFoundError, ValueError):
            path = Path(setting)
            if path.is_absolute() and path.is_file():
                with path.open("rb") as stream:
                    return ZoneInfo.from_file(stream)
            # UTC0 is a common POSIX spelling that has no IANA zone file.
            if setting in {"UTC0", "GMT0"}:
                return timezone.utc
            raise HintError(f"Unknown local timezone '{setting}'.") from None
    try:
        path = Path("/etc/localtime")
        resolved = str(path.resolve())
        label = resolved.split("/zoneinfo/", 1)[-1] if "/zoneinfo/" in resolved else "local timezone"
        with path.open("rb") as stream:
            return ZoneInfo.from_file(stream, key=label)
    except (OSError, ValueError):
        return datetime.now().astimezone().tzinfo


def _local_instant(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value
    zone = local_zone()
    possible = {}
    for fold in (0, 1):
        aware = value.replace(tzinfo=zone, fold=fold)
        try:
            restored = aware.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
        except (OverflowError, ValueError):
            raise HintError("Time is outside the supported calendar range in the local timezone.") from None
        if restored == value:
            possible[aware.timestamp()] = aware
    zone_name = getattr(zone, "key", None) or str(zone)
    if not possible:
        raise HintError(f"'{value.strftime(TIME_FORMAT)}' does not exist in {zone_name} because the clocks change.")
    if len(possible) > 1:
        raise HintError(f"'{value.strftime(TIME_FORMAT)}' occurs twice in {zone_name}. Choose an unambiguous time.")
    return next(iter(possible.values()))


def parse_time(value: str) -> datetime:
    """Parse a local calendar date with a required year and omitted-field defaults."""
    match = _CALENDAR_TIME.fullmatch(value.strip()) if isinstance(value, str) else None
    if match is None:
        raise HintError(f"Invalid time '{value}'. Use YYYY[-MM[-DD]][ HH[:MM[:SS]]].")
    fields = {name: int(part) if part is not None else (1 if name in {"month", "day"} else 0)
              for name, part in match.groupdict().items()}
    try:
        parsed = datetime(**fields)
    except ValueError:
        raise HintError(f"Invalid time '{value}'. Use YYYY[-MM[-DD]][ HH[:MM[:SS]]].") from None
    _local_instant(parsed)
    return parsed


def display_time(value: str) -> str:
    return _local_instant(parse_time(value)).strftime(TIME_FORMAT + " %Z")


def _future(value: datetime, now: datetime) -> bool:
    # now() can be within the repeated fall-back hour; its fold is already
    # meaningful, so only the user's scheduled time needs ambiguity checking.
    now_aware = now if now.tzinfo is not None else now.replace(tzinfo=local_zone())
    return _local_instant(value).timestamp() > now_aware.timestamp()


def validate_name(name: str) -> None:
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise HintError(f"Invalid name '{name}': use letters, digits, '_' or '-'.")


def validate(name: str, entry: object) -> None:
    validate_name(name)
    if _GENERATED_NAME.fullmatch(name):
        raise HintError(f"'{name}' is reserved for temporary runs. Choose another saved name.")
    if not isinstance(entry, dict) or set(entry) - {"time", "wake", "action", "popup"}:
        raise HintError(f"invalid signal entry: {name}")
    if ("action" in entry) == ("popup" in entry):
        raise HintError(f"signal {name!r} requires exactly one of action or popup")
    field = "popup" if "popup" in entry else "action"
    label = "Popup text" if field == "popup" else "Action"
    if not isinstance(entry[field], str) or not entry[field].strip():
        raise HintError(label + " must be nonempty text.")
    if "\0" in entry[field]:
        raise HintError(label + " cannot contain a NUL character.")
    if "time" in entry:
        parse_time(entry["time"])
    if "wake" in entry and not isinstance(entry["wake"], bool):
        raise HintError(f"signal {name!r} wake must be a boolean")


def _next_wake(table: dict, now: datetime) -> datetime | None:
    times = [parse_time(entry["time"]) for entry in table.values() if entry.get("wake") and "time" in entry]
    return min((when for when in times if _future(when, now)), default=None)


def _wake_argument(when: datetime | None, now: datetime) -> str:
    argument = "disable"
    if when is not None:
        lead = config().get("wake_lead_seconds", 10)
        if type(lead) is not int or lead < 0:
            raise HintError("wake_lead_seconds must be a nonnegative integer")
        # Convert each local calendar time to an instant before subtracting:
        # a DST change can make elapsed time differ from wall-clock time.
        # Subtract the integer lead after rounding to avoid large-lead overflow.
        now_aware = now if now.tzinfo is not None else now.replace(tzinfo=local_zone())
        argument = str(max(5, math.ceil(_local_instant(when).timestamp() - now_aware.timestamp()) - lead))
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
    clearing_last_wake = (any(entry.get("wake") and "time" in entry for entry in previous.values())
                          and not any(entry.get("wake") and "time" in entry for entry in updated.values()))
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


def _schedule_key(name: str, when: str):
    return (str(TABLE), name, when)


def fire(name: str, scheduled_time: str | None = None, launcher=None) -> dict:
    """Launch through the service and retain scheduled definitions after firing."""
    validate_name(name)
    if launcher is None:
        if scheduled_time is not None:
            raise HintError("Scheduled actions must be launched by the running HINT service.")
        from hint_runtime import request
        return request({"op": "fire", "name": name})
    with locked():
        table = read_table()
        entry = table.get(name)
        if entry is None:
            return {"signal": name, "fired": False, "error": "signal not found"}
        if scheduled_time is not None and entry.get("time") != scheduled_time:
            return {"signal": name, "fired": False, "error": "stale time"}
        key = _schedule_key(name, scheduled_time) if scheduled_time is not None else None
        already_fired = key in _PENDING_CLEARS if key else False
        if not already_fired:
            try:
                result = (launcher(name, popup=entry["popup"]) if "popup" in entry
                          else launcher(name, entry["action"]))
                if isinstance(result, dict) and result.get("error"):
                    return result
            except (OSError, HintError) as exc:
                return {"signal": name, "fired": False, "error": sanitize_error(exc)}
            if key:
                _PENDING_CLEARS.add(key)
        result = {"op": "fire", "signal": name, "fired": not already_fired}
        if already_fired:
            result["already_fired"] = True
        if scheduled_time is not None:
            updated = dict(table)
            updated[name] = {field: value for field, value in entry.items() if field != "time"}
            try:
                write_table(updated)
            except Exception as exc:
                result["error"] = (f"Fired '{name}', but could not clear its schedule: "
                                   f"{sanitize_error(exc)}. Check its status before firing again.")
                return result
            _PENDING_CLEARS.discard(key)
            _REPORTED_ERRORS.pop(key, None)
            result["schedule_cleared"] = True
            # Launch and schedule clearing succeeded even if RTC rearming fails.
            if entry.get("wake"):
                try:
                    now = datetime.now()
                    _run_wake(_wake_argument(_next_wake(updated, now), now))
                except HintError as exc:
                    result["warning"] = (f"Fired '{name}' and cleared its schedule; "
                                         f"the next wake alarm could not be set: {sanitize_error(exc)}.")
    return result


def get_signal(name: str) -> dict:
    validate_name(name)
    with locked():
        table = read_table()
        if name not in table:
            raise HintError(f"No action named '{name}'. Run hint --list.")
        return {name: table[name]}


def list_signals() -> dict:
    with locked():
        return read_table()


def set_signal(name: str, action: str | None = None, when: str | None = None,
               wake: bool | None = None, no_time: bool = False, *, popup: str | None = None) -> dict:
    """Change only supplied fields; clearing a schedule retains wake preference."""
    validate_name(name)
    if _GENERATED_NAME.fullmatch(name):
        raise HintError(f"'{name}' is reserved for temporary runs. Choose another saved name.")
    if wake is not None and not isinstance(wake, bool):
        raise HintError("wake must be a boolean")
    if not isinstance(no_time, bool):
        raise HintError("no_time must be a boolean")
    if when is not None and no_time:
        raise HintError("'--time' and '--no-time' cannot be used together.")
    if action is not None and popup is not None:
        raise HintError("Supply either --action or --popup.")
    if action is None and popup is None and when is None and wake is None and not no_time:
        raise HintError(f"No changes supplied for '{name}'.")
    resolved_time = None
    if when is not None:
        parsed = parse_time(when)
        if not _future(parsed, datetime.now()):
            raise HintError(f"Time must be in the future: {display_time(when)}.")
        resolved_time = parsed.strftime(TIME_FORMAT)
    with locked():
        table = read_table()
        created = name not in table
        if created and action is None and popup is None:
            raise HintError(f"Creating '{name}' requires --action or --popup.")
        previous = table.get(name, {})
        entry = dict(previous)
        if action is not None:
            entry["action"] = action
            entry.pop("popup", None)
        if popup is not None:
            entry["popup"] = popup
            entry.pop("action", None)
        if resolved_time is not None:
            entry["time"] = resolved_time
        if no_time:
            entry.pop("time", None)
        if wake is True:
            entry["wake"] = True
        elif wake is False:
            entry.pop("wake", None)
        validate(name, entry)
        changed = [field for field in ("action", "popup", "time", "wake")
                   if previous.get(field, False if field == "wake" else None)
                   != entry.get(field, False if field == "wake" else None)]
        if created or changed:
            updated = dict(table)
            updated[name] = entry
            _commit_table(table, updated)
        return {"op": "set", "name": name, "created": created,
                "changed": changed, "definition": entry,
                "time_display": display_time(entry["time"]) if "time" in entry else None}


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


def fire_due(launcher=None) -> None:
    if launcher is None:
        raise HintError("Scheduled actions must be launched by the running HINT service.")
    now = datetime.now()
    with locked():
        table = read_table()
        due = sorted((parse_time(entry["time"]), name, entry["time"])
                     for name, entry in table.items()
                     if "time" in entry and not _future(parse_time(entry["time"]), now))
        current_keys = {_schedule_key(name, entry["time"])
                        for name, entry in table.items() if "time" in entry}
        for key in set(_PENDING_CLEARS) | set(_REPORTED_ERRORS):
            if key[0] == str(TABLE) and key not in current_keys:
                _PENDING_CLEARS.discard(key)
                _REPORTED_ERRORS.pop(key, None)
    for _, name, when in due:
        result = fire(name, when, launcher=launcher)
        message = result.get("warning") or result.get("error")
        if message and message not in {"signal not found", "stale time"}:
            key = _schedule_key(name, when)
            if _REPORTED_ERRORS.get(key) != message:
                print("HINT clock: " + message, file=sys.stderr, flush=True)
                _REPORTED_ERRORS[key] = message
