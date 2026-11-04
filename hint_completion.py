"""Fresh, read-only Bash completion for local and remote HINT actions."""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys

NAME = re.compile(r"^[A-Za-z0-9_-]+$")
DEFINITION_OPTIONS = frozenset(("--set", "--fire", "--get", "--remove"))
CONTROL_OPTIONS = frozenset(("--pause", "--resume", "--stop"))
NAME_OPTIONS = DEFINITION_OPTIONS | CONTROL_OPTIONS | {"--status"}
VALUE_OPTIONS = NAME_OPTIONS | {"--remote", "--time", "--action", "--popup", "--server"}
OPTIONAL_VALUES = {"--fire", "--status"}
OPERATIONS = NAME_OPTIONS | {"--list", "--discover", "--ready"}
OPTIONS = (
    "--set", "--fire", "--pause", "--resume", "--stop", "--status",
    "--get", "--remove", "--list", "--discover", "--ready",
    "--remote", "--action", "--popup", "--time", "--no-time", "--wake", "--no-wake",
    "--server", "--local", "--enable-wake", "--help"
)
COMPLETION_TIMEOUT = 1.6


def _read_object(path: Path) -> dict:
    if not path.exists():
        return {}
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def _names(value: object) -> list[str]:
    if not isinstance(value, dict):
        raise ValueError("action list must contain an object")
    return sorted(name for name, entry in value.items()
                  if isinstance(name, str) and NAME.fullmatch(name) and isinstance(entry, dict))


def _scan(words: list[str], cword: int):
    """Classify words without interpreting an action's contents as switches."""
    pending = None
    roles = {}
    values = {}
    used = set()
    remote = None
    remote_present = False
    for index, word in enumerate(words[1:], 1):
        if pending:
            if word == "=":
                roles[index] = pending
                continue
            is_option = word.partition("=")[0] in OPTIONS
            if is_option and (pending in OPTIONAL_VALUES or pending == "--remote"):
                pending = None
            else:
                roles[index] = pending
                if index != cword:
                    values[pending] = word
                    if pending == "--remote":
                        remote = word or None
                pending = None
                continue
        option, equals, value = word.partition("=")
        if option not in OPTIONS:
            continue
        used.add(option)
        if option == "--remote":
            remote_present = True
        if option in VALUE_OPTIONS:
            if equals:
                roles[index] = option
                if index != cword:
                    values[option] = value
                    if option == "--remote":
                        remote = value or None
            else:
                pending = option
    if pending:
        roles[len(words)] = pending
    return roles, values, used, remote_present, remote


def _status_names(value: object, operation: str) -> list[str]:
    if not isinstance(value, dict) or not isinstance(value.get("runs"), list):
        raise ValueError("invalid HINT status")
    names = set(_names(value.get("definitions", {}))) if operation == "--status" else set()
    for run in value["runs"]:
        if not isinstance(run, dict):
            continue
        if operation in CONTROL_OPTIONS and not run.get("active"):
            continue
        name = run.get("name", run.get("signal"))
        if isinstance(name, str) and NAME.fullmatch(name):
            names.add(name)
    return sorted(names)


def complete(words: list[str], cword: int) -> list[str]:
    if cword < 1 or cword > len(words):
        return []
    prefix = words[cword] if cword < len(words) else ""
    roles, values, used, remote_present, remote = _scan(words, cword)
    pending = roles.get(cword)
    option, equals, value = prefix.partition("=")
    attached = bool(equals and option in VALUE_OPTIONS and pending == option)
    if attached:
        prefix = value
    elif pending and prefix == "=":
        prefix = ""
    if (pending in OPTIONAL_VALUES or pending == "--remote") and prefix.startswith("--") and not attached:
        pending = None
    if pending == "--remote":
        if used & {"--discover", "--ready"}:
            return []
        from hint_network import discover
        candidates = discover(timeout=COMPLETION_TIMEOUT)
    elif pending in NAME_OPTIONS:
        if pending == "--fire" and used & {"--action", "--popup"}:
            return []
        if remote_present and (not remote or not NAME.fullmatch(remote)):
            return []
        if pending in CONTROL_OPTIONS or pending == "--status":
            if remote_present:
                from hint_network import remote as request
                status = request(remote, ["--status"], timeout=COMPLETION_TIMEOUT)
            else:
                from hint_runtime import request
                from hint_config import HintError, TABLE
                try:
                    status = request({"op": "status"}, timeout=COMPLETION_TIMEOUT)
                except HintError as error:
                    if pending != "--status" or "not running" not in str(error).lower():
                        raise
                    status = {"definitions": _read_object(Path(TABLE)), "runs": []}
            candidates = _status_names(status, pending)
        elif remote_present:
            from hint_network import remote as request
            candidates = _names(request(remote, ["--list"], timeout=COMPLETION_TIMEOUT))
        else:
            from hint_config import TABLE
            candidates = _names(_read_object(Path(TABLE)))
        if pending == "--fire" and not prefix and not attached and not used & {"--action", "--popup"}:
            candidates.extend(("--action", "--popup"))
        if pending == "--status" and not prefix and not attached and not remote_present:
            candidates.append("--remote")
    elif pending:
        # Arbitrary command text/calendar values are never executed to complete them.
        return []
    else:
        candidates = [candidate for candidate in OPTIONS if candidate not in used]
        if "--set" not in used:
            forbidden = {"--time", "--no-time", "--wake", "--no-wake"}
            if "--fire" not in used or "--fire" in values:
                forbidden.update(("--action", "--popup"))
            candidates = [c for c in candidates if c not in forbidden]
        if used & OPERATIONS:
            candidates = [c for c in candidates if c not in OPERATIONS]
        if remote_present:
            candidates = [c for c in candidates if c not in {"--discover", "--ready"}]
            if remote is None and not used & OPERATIONS:
                candidates = [c for c in candidates if c in {"--list", "--help"}]
        if used & {"--discover", "--ready"}:
            candidates = [c for c in candidates if c != "--remote"]
        if "--ready" not in used:
            candidates = [c for c in candidates if c not in {"--server", "--local", "--enable-wake"}]
        for first, second in (("--time", "--no-time"), ("--wake", "--no-wake"), ("--server", "--local"),
                              ("--action", "--popup")):
            if first in used:
                candidates = [c for c in candidates if c != second]
            if second in used:
                candidates = [c for c in candidates if c != first]
    matches = [candidate for candidate in candidates if candidate.startswith(prefix)]
    return [f"{option}={candidate}" for candidate in matches] if attached else matches


def main(argv: list[str]) -> int:
    """Completion failures leave the prompt usable without stderr chatter."""
    try:
        cword = int(argv[0])
        for candidate in complete(argv[1:], cword):
            print(candidate)
    except (Exception, KeyboardInterrupt):
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
