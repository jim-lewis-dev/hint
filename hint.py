"""HINT — Human INTerrupt. Command line and background clock."""
from __future__ import annotations

import argparse
import json
import shlex
import signal
import sys
import threading

from hint_config import HintError, NAME, config, sanitize_error
from hint_signals import (
    arm_wake, fire, fire_due, get_signal, list_signals, remove_signal, set_signal,
)


class CommandParser(argparse.ArgumentParser):
    def error(self, message):
        raise HintError(message)


class RemoteAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        if namespace.remote is not None:
            raise HintError("--remote may be supplied only once")
        if not NAME.fullmatch(values or ""):
            raise HintError("invalid remote name")
        setattr(namespace, self.dest, values)


def parser(*, remote_command=False) -> argparse.ArgumentParser:
    p = CommandParser(prog="hint", description="HINT — Human INTerrupt", allow_abbrev=False,
                      add_help=not remote_command)
    op = p.add_mutually_exclusive_group(required=True)
    op.add_argument("--fire", metavar="NAME", help="start a signal's action")
    op.add_argument("--get", metavar="NAME", help="read one signal")
    op.add_argument("--list", action="store_true", help="list signal definitions")
    op.add_argument("--set", dest="set_name", metavar="NAME", help="create or replace a signal")
    op.add_argument("--remove", metavar="NAME", help="remove a signal definition")
    op.add_argument("--discover", action="store_true", help="list reachable HINT servers")
    op.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--time", metavar='"YYYY-MM-DD HH:MM:SS"')
    p.add_argument("--wake", action="store_true")
    p.add_argument("--action", metavar="STRING",
                   help="URL, existing file/directory, or /bin/sh command")
    p.add_argument("--remote", metavar="SERVER", action=RemoteAction,
                   help="run an operation on another HINT server")
    return p


def validate_args(args) -> None:
    modifiers = args.time is not None or args.wake or args.action is not None
    if args.set_name is not None:
        if args.action is None:
            raise HintError("--set requires --action")
    elif modifiers:
        raise HintError("--time, --wake and --action are only valid with --set")
    if args.remote and (args.serve or args.discover):
        raise HintError("--serve and --discover cannot be remote")
    for value in (args.set_name, args.fire, args.get, args.remove):
        if value is not None and not NAME.fullmatch(value or ""):
            raise HintError("signal name may contain only letters, digits, '_' and '-'")


def operation_args(args) -> list[str]:
    """Build one canonical command for the existing Messenger wire format."""
    def option_value(option, text):
        # Preserve values beginning with a dash through both CLI parsers.
        return [option + "=" + text] if text.startswith("-") else [option, text]

    if args.set_name is not None:
        result = option_value("--set", args.set_name) + option_value("--action", args.action)
        if args.time is not None:
            result += option_value("--time", args.time)
        if args.wake:
            result.append("--wake")
        return result
    for option in ("fire", "get", "remove"):
        value = getattr(args, option)
        if value is not None:
            return option_value("--" + option, value)
    return ["--list"]


def execute(args):
    if args.remote:
        from hint_network import remote
        return remote(args.remote, operation_args(args))
    if args.set_name is not None:
        return set_signal(args.set_name, args.action, args.time, args.wake)
    if args.fire is not None:
        return fire(args.fire)
    if args.get is not None:
        return get_signal(args.get)
    if args.list:
        return list_signals()
    if args.remove is not None:
        return remove_signal(args.remove)
    if args.discover:
        from hint_network import discover
        return discover()
    raise HintError("no operation selected")


def run(argv: list[str]):
    args = parser().parse_args(argv)
    validate_args(args)
    if args.serve:
        raise HintError("--serve is reserved for the local service")
    return execute(args)


def command_text(text: str) -> str:
    """Process the existing live Messenger request, always returning JSON."""
    try:
        argv = shlex.split(text)
        if not argv or argv[0] != "hint":
            raise HintError("invalid remote HINT command")
        args = parser(remote_command=True).parse_args(argv[1:])
        validate_args(args)
        if args.serve or args.discover or args.remote:
            raise HintError("invalid remote HINT command")
        result = execute(args)
    except (Exception, SystemExit) as exc:
        result = {"error": sanitize_error(exc) if not isinstance(exc, SystemExit) else "invalid remote HINT command"}
    return json.dumps(result, indent=2, sort_keys=True)


def serve() -> int:
    import hint_playback
    from hint_network import serve as serve_network

    hint_playback.SERVICE_MODE = True
    cfg = config()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        arm_wake()
    except Exception as exc:
        print(f"HINT wake: {sanitize_error(exc)}", file=sys.stderr, flush=True)
    thread = None
    if cfg["nats_server"] is not None:
        thread = threading.Thread(target=serve_network, args=(stop,),
                                  name="hint-network", daemon=True)
        thread.start()
    while not stop.is_set():
        try:
            fire_due()
        except Exception as exc:
            print(f"HINT clock: {sanitize_error(exc)}", file=sys.stderr, flush=True)
        stop.wait(1)
    if thread is not None:
        thread.join(timeout=3)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--complete"]:
        from hint_completion import main as complete
        return complete(argv[1:])
    try:
        args = parser().parse_args(argv)
        validate_args(args)
        if args.serve:
            return serve()
        result = execute(args)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 1 if isinstance(result, dict) and isinstance(result.get("error"), str) else 0
    except HintError as exc:
        print(json.dumps({"error": sanitize_error(exc)}, indent=2))
        return 2
    except Exception as exc:
        print(json.dumps({"error": sanitize_error(exc)}, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
