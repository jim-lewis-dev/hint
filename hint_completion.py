"""Read-only Bash completion and live HINT peer discovery."""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys


NAME = re.compile(r"^[A-Za-z0-9_-]+$")
NAME_OPTIONS = frozenset(("--set", "--fire", "--get", "--remove"))
VALUE_OPTIONS = NAME_OPTIONS | {"--remote", "--time", "--action"}
OPERATIONS = NAME_OPTIONS | {"--list", "--discover"}
OPTIONS = (
    "--set", "--fire", "--get", "--remove", "--list", "--discover",
    "--remote", "--action", "--time", "--wake", "--help",
)


def _read_object(path: Path) -> dict:
    if not path.exists():
        return {}
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def _names(value: object) -> list[str]:
    if not isinstance(value, dict):
        raise ValueError("signal list must be a JSON object")
    return sorted(name for name, entry in value.items()
                  if isinstance(name, str) and NAME.fullmatch(name) and isinstance(entry, dict))


def _context(words: list[str], cword: int):
    pending = None
    remote = None
    used = set()
    for word in words[1:cword]:
        if pending:
            if word == "=":
                # Bash splits --option=value at its default '=' word break.
                continue
            if pending == "--remote":
                remote = word
            pending = None
            continue
        option, equals, value = word.partition("=")
        if option not in OPTIONS:
            continue
        used.add(option)
        if option in VALUE_OPTIONS:
            if equals:
                if option == "--remote":
                    remote = value
            else:
                pending = option
    return pending, remote, used


def _whole_context(words: list[str], cword: int):
    """Find a selected remote even when the cursor edits an earlier argument."""
    pending = None
    remote = None
    used = set()
    for index, word in enumerate(words[1:], 1):
        if pending:
            if word == "=":
                continue
            if pending == "--remote" and index != cword:
                remote = word
            pending = None
            continue
        if index == cword:
            continue
        option, equals, value = word.partition("=")
        if option not in OPTIONS:
            continue
        used.add(option)
        if option in VALUE_OPTIONS:
            if equals:
                if option == "--remote":
                    remote = value
            else:
                pending = option
    return remote, used


def complete(words: list[str], cword: int) -> list[str]:
    """Return candidates for Bash's full COMP_WORDS and COMP_CWORD values."""
    if cword < 1 or cword > len(words):
        return []
    prefix = words[cword] if cword < len(words) else ""
    pending, _, _ = _context(words, cword)
    remote, used = _whole_context(words, cword)
    if pending and prefix == "=":
        prefix = ""
    option, equals, value = prefix.partition("=")
    attached = equals and option in VALUE_OPTIONS and pending is None
    if attached:
        pending, prefix = option, value

    if pending == "--remote":
        if "--discover" in used:
            return []
        from hint_network import discover
        candidates = discover()
    elif pending in NAME_OPTIONS:
        if remote:
            from hint_network import remote as request
            candidates = _names(request(remote, ["--list"]))
        else:
            from hint_config import TABLE
            candidates = _names(_read_object(Path(TABLE)))
    elif pending:
        # Action strings and calendar times have no finite candidate list.
        return []
    else:
        candidates = [candidate for candidate in OPTIONS if candidate not in used]
        if "--set" not in used:
            candidates = [candidate for candidate in candidates
                          if candidate not in {"--action", "--time", "--wake"}]
        if used & OPERATIONS:
            candidates = [candidate for candidate in candidates if candidate not in OPERATIONS]
        if remote:
            candidates = [candidate for candidate in candidates if candidate != "--discover"]
        if "--discover" in used:
            candidates = [candidate for candidate in candidates if candidate != "--remote"]

    matches = [candidate for candidate in candidates if candidate.startswith(prefix)]
    return [f"{option}={candidate}" for candidate in matches] if attached else matches


def main(argv: list[str]) -> int:
    """Hidden CLI: completion errors and unavailable peers stay quiet."""
    try:
        cword = int(argv[0])
        for candidate in complete(argv[1:], cword):
            print(candidate)
    except (Exception, KeyboardInterrupt):
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
