"""HINT - Human INTerrupt. Readable CLI, structured remote commands, one run owner."""
from __future__ import annotations

import argparse
import json
import re
import shlex
import signal
import socket
import sys
import threading
from datetime import datetime

from hint_config import HintError, NAME, config, sanitize_error
from hint_signals import arm_wake, get_signal, list_signals, remove_signal, set_signal



class CommandParser(argparse.ArgumentParser):
    def error(self, message):
        raise HintError(message + ". Run hint --help.")


class OnceAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        seen = getattr(namespace, "_seen", set())
        if self.dest in seen:
            raise HintError(f"'{option_string}' may be supplied only once.")
        setattr(namespace, "_seen", seen | {self.dest})
        setattr(namespace, self.dest, self.const if self.nargs == 0 else values)


class RemoteAction(OnceAction):
    def __call__(self, parser, namespace, values, option_string=None):
        if values is not True and not NAME.fullmatch(values or ""):
            raise HintError("Invalid hostname: use letters, digits, '_' or '-'.")
        super().__call__(parser, namespace, values, option_string)


def parser(*, remote_command=False) -> argparse.ArgumentParser:
    p = CommandParser(
        prog="hint", description="HINT - Human INTerrupt. An alarm clock with unreasonable ambition.",
        epilog="Examples:\n  hint --set stretch --popup 'Time to stretch!'\n"
               "  hint --fire stretch --remote beacon\n"
               "  hint --list --remote\n"
               "  hint --fire --popup 'Dinner is ready!'\n"
               "  hint --set stretch --time '2027-02-10 09:00'\n"
               "  hint --status\n  hint --ready",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False, add_help=not remote_command
    )
    op = p.add_mutually_exclusive_group(required=True)
    op.add_argument("--fire", nargs="?", const="", metavar="NAME", action=OnceAction,
                    help="launch a saved action, or use --action/--popup for a temporary run")
    for flag, help_text in (
        ("get", "show one saved action"), ("remove", "remove a saved definition"),
        ("pause", "pause every active run of a name"),
        ("resume", "resume paused runs of a name"), ("stop", "stop active runs of a name")
    ):
        op.add_argument("--" + flag, metavar="NAME", action=OnceAction, help=help_text)
    op.add_argument("--status", nargs="?", const="", metavar="NAME", action=OnceAction,
                    help="show service, schedules and active/recent runs")
    op.add_argument("--set", dest="set_name", metavar="NAME", action=OnceAction,
                    help="create an action or change only the supplied attributes")
    for flag, help_text in (
        ("list", "list saved actions"), ("discover", "list responding HINT hostnames"),
        ("ready", "install/update HINT and ensure its local service is running"),
        ("serve", argparse.SUPPRESS)
    ):
        op.add_argument("--" + flag, nargs=0, const=True, default=False,
                        action=OnceAction, help=help_text)
    times = p.add_mutually_exclusive_group()
    times.add_argument("--time", metavar="TIME", action=OnceAction,
                       help="YYYY[-MM[-DD]][ HH[:MM[:SS]]], interpreted on the target computer")
    times.add_argument("--no-time", nargs=0, const=True, default=False, action=OnceAction,
                       help="clear the schedule and retain the saved action")
    wake = p.add_mutually_exclusive_group()
    wake.add_argument("--wake", dest="wake", nargs=0, const=True, default=None,
                      action=OnceAction, help="enable waking for this action's schedule")
    wake.add_argument("--no-wake", dest="wake", nargs=0, const=False,
                      action=OnceAction, help="disable waking without clearing the schedule")
    content = p.add_mutually_exclusive_group()
    content.add_argument("--action", metavar="STRING", action=OnceAction,
                         help="URL, existing file/directory, or /bin/sh command")
    content.add_argument("--popup", metavar="TEXT", action=OnceAction,
                         help="show literal text in HINT's own fullscreen window")
    p.add_argument("--remote", nargs="?", const=True, metavar="HOST", action=RemoteAction,
                   help="target HOST; with --list, omit HOST to include all responding computers")
    setup = p.add_mutually_exclusive_group()
    setup.add_argument("--server", metavar="ADDRESS", action=OnceAction,
                       help="initial --ready setup: NATS hostname, IP[:port], or URL")
    setup.add_argument("--local", nargs=0, const=True, default=False, action=OnceAction,
                       help="initial --ready setup: local-only operation")
    p.add_argument("--enable-wake", nargs=0, const=True, default=False, action=OnceAction,
                   help="with --ready: install optional hardware wake support")
    return p


def validate_args(args) -> None:
    if args.remote is True and not args.list:
        raise HintError("Supply a hostname after --remote; only --list can use --remote without a hostname.")
    for value in (args.set_name, args.fire, args.get, args.remove, args.pause, args.resume,
                  args.stop, args.status):
        if value not in (None, "") and not NAME.fullmatch(value):
            raise HintError(f"Invalid name '{value}': use letters, digits, '_' or '-'.")
    attributes = args.time is not None or args.no_time or args.wake is not None
    content = args.action is not None or args.popup is not None
    if args.set_name is not None:
        if not content and not attributes:
            raise HintError(f"No changes supplied for '{args.set_name}'.")
    elif args.fire is not None:
        if args.fire and content:
            raise HintError("Supply either a saved name, --action or --popup.")
        if not args.fire and not content:
            raise HintError("Supply a name, --action or --popup to fire.")
        if attributes:
            raise HintError("--time, --no-time, --wake and --no-wake can only be used with --set.")
    elif content or attributes:
        raise HintError("Use --action or --popup with --set or nameless --fire; schedule and wake options require --set.")
    for label, text in (("Action", args.action), ("Popup text", args.popup)):
        if text is not None:
            if not text.strip():
                raise HintError(label + " cannot be empty.")
            if "\0" in text:
                raise HintError(label + " cannot contain a NUL character.")
    if args.remote and (args.serve or args.discover or args.ready):
        raise HintError("--serve, --discover and --ready are local operations.")
    if (args.server is not None or args.local or args.enable_wake) and not args.ready:
        raise HintError("--server, --local and --enable-wake can only be used with --ready.")


def operation_args(args) -> list[str]:
    """One shell-quoted command format for Messenger; values stay literal."""
    def value(option, text):
        return [option + "=" + text] if text.startswith("-") else [option, text]

    if args.set_name is not None:
        result = value("--set", args.set_name)
        if args.action is not None:
            result += value("--action", args.action)
        if args.popup is not None:
            result += value("--popup", args.popup)
        if args.time is not None:
            result += value("--time", args.time)
        if args.no_time:
            result.append("--no-time")
        if args.wake is not None:
            result.append("--wake" if args.wake else "--no-wake")
        return result
    if args.fire is not None:
        result = value("--fire", args.fire) if args.fire else ["--fire"]
        if args.action is not None:
            result += value("--action", args.action)
        if args.popup is not None:
            result += value("--popup", args.popup)
        return result
    for option in ("get", "remove", "pause", "resume", "stop", "status"):
        selected = getattr(args, option)
        if selected is not None:
            return value("--" + option, selected) if selected else ["--" + option]
    return ["--list"]


def _offline_status(name=None):
    definitions = list_signals()
    if name is not None and name not in definitions:
        raise HintError(f"No action named '{name}'. Run hint --list.")
    return {"op": "status", "host": socket.gethostname(),
            "service": "stopped", "network": "unavailable", "capabilities": ["partial-set", "inline-fire", "controls", "popup"], "definitions": definitions if name is None else {name: definitions[name]},
            "runs": [], "scheduled_count": sum("time" in entry for entry in definitions.values()),
            "message": "HINT service is not running. Run hint --ready."}


def execute(args):
    if args.remote is True:
        from hint_network import list_all
        return list_all()
    if args.remote:
        from hint_network import remote
        return remote(args.remote, operation_args(args))
    if args.set_name is not None:
        return set_signal(args.set_name, action=args.action, popup=args.popup,
                          when=args.time, wake=args.wake, no_time=args.no_time)
    if args.get is not None:
        return get_signal(args.get)
    if args.list:
        return list_signals()
    if args.remove is not None:
        result = remove_signal(args.remove)
        try:
            from hint_runtime import request
            status = request({"op": "status"}, timeout=0.3)
            result["active_count"] = sum(run.get("name") == args.remove and run.get("state") in {"starting", "running", "paused", "stopping"}
                                         for run in status.get("runs", []))
        except HintError:
            pass
        return result
    if args.discover:
        from hint_network import discover
        return discover()
    from hint_runtime import request
    if args.fire is not None:
        body = {"op": "fire", "name": args.fire or None}
        if args.action is not None:
            body["action"] = args.action
        if args.popup is not None:
            body["popup"] = args.popup
        return request(body)
    for option in ("pause", "resume", "stop", "status"):
        target = getattr(args, option)
        if target is not None:
            try:
                return request({"op": option, "name": target or None})
            except HintError as exc:
                if option == "status" and ("not running" in str(exc).lower() or "not available" in str(exc).lower()):
                    return _offline_status(target or None)
                raise
    raise HintError("No operation selected.")


def run(argv: list[str]):
    """Structured internal interface; terminal presentation belongs to main()."""
    args = parser().parse_args(argv)
    validate_args(args)
    if args.serve or args.ready:
        raise HintError("--serve and --ready are reserved for local lifecycle management.")
    return execute(args)


def command_text(text: str) -> str:
    """Messenger requests always receive JSON, independently of terminal output."""
    try:
        argv = shlex.split(text)
        if not argv or argv[0] != "hint":
            raise HintError("Invalid remote HINT command.")
        args = parser(remote_command=True).parse_args(argv[1:])
        validate_args(args)
        if args.serve or args.discover or args.ready or args.remote:
            raise HintError("Invalid remote HINT command.")
        result = execute(args)
    except (Exception, SystemExit) as exc:
        result = {"error": sanitize_error(exc) if not isinstance(exc, SystemExit) else "Invalid remote HINT command."}
    return json.dumps(result, sort_keys=True)


def _plain(value) -> str:
    # Values may come from a remote action or a filename; don't inject terminal controls.
    return "".join(character if character.isprintable() else " " for character in str(value))


def _time(value) -> str:
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    except (ValueError, TypeError):
        return str(value)


def _table(headers, rows) -> str:
    rows = [[_plain(value) for value in row] for row in rows]
    if not rows:
        return ""
    widths = [max(len(header), *(len(row[i]) for row in rows)) for i, header in enumerate(headers)]
    return "\n".join("  ".join(value.ljust(widths[i]) for i, value in enumerate(row)).rstrip()
                     for row in [headers, *rows])


def _definitions(value, *, remote=False, display_times=None) -> str:
    if not value:
        return "No saved actions."
    display_times = display_times or {}
    def scheduled(entry):
        if "time" not in entry:
            return "none"
        value = entry["time"]
        return display_times.get(value) or (value + " (target local)" if remote else _time(value))
    return _table(["NAME", "SCHEDULE", "WAKE", "ACTION"], [
        [name, scheduled(entry),
         "enabled" if entry.get("wake") else "disabled",
         "Popup: " + entry["popup"] if "popup" in entry else entry["action"]]
        for name, entry in sorted(value.items())
    ])


def render(result, args) -> str:
    if args.list and args.remote is True:
        return "\n\n".join(
            _plain(host) + "\n" + _definitions(value["definitions"], remote=host != result["local_host"])
            for host, value in sorted(result["hosts"].items()) if "definitions" in value
        )
    host = args.remote or socket.gethostname()
    prefix = _plain(host) + ": "
    if isinstance(result, dict) and isinstance(result.get("error"), str):
        return prefix + _plain(sanitize_error(result["error"]))
    if args.discover:
        return "\n".join(_plain(name) for name in result) if result else prefix + "No HINT computers answered."
    if args.list or args.get is not None:
        return _plain(host) + "\n" + _definitions(result, remote=bool(args.remote))
    if args.set_name is not None:
        name = args.set_name
        entry = result["definition"]
        changed = result.get("changed", [])
        if not changed and not result.get("created"):
            if args.no_time:
                text = f"'{name}' has no schedule."
            elif args.wake is not None:
                text = f"Wake is already {'enabled' if args.wake else 'disabled'} for '{name}'."
            else:
                text = f"'{name}' is already set that way."
            return prefix + text
        fragments = []
        if ("action" in changed or "popup" in changed) and not result.get("created"):
            fragments.append("action changed")
        if "time" in changed:
            fragments.append("scheduled for " + (result.get("time_display") or _time(entry["time"])) if "time" in entry else "schedule cleared")
        if "wake" in changed:
            fragments.append("wake enabled" if entry.get("wake") else "wake disabled")
        text = ("Created" if result.get("created") else "Updated") + f" '{name}'"
        return prefix + text + (": " + "; ".join(fragments) if fragments else "") + "."
    if args.remove is not None:
        text = f"Removed '{args.remove}'." if result.get("removed") else f"'{args.remove}' is already absent."
        count = result.get("active_count", 0)
        if count:
            text += f" Its {count} active {'run continues' if count == 1 else 'runs continue'}."
        return prefix + text
    if args.fire is not None:
        name = result.get("signal") or result.get("name") or result.get("run")
        return prefix + (f"Fired '{args.fire}'." if args.fire else f"Fired {name}.")
    if args.status is not None:
        lines = [_plain(result.get("host", host)), "Service: " + str(result.get("service", "running")),
                 "Network: " + str(result.get("network", "unknown"))]
        definitions = result.get("definitions", {})
        if definitions:
            lines += ["", _definitions(definitions, remote=bool(args.remote), display_times=result.get("display_times"))]
        runs = result.get("runs", [])
        if runs:
            lines += ["", _table(["NAME", "STATE", "DETAIL"], [
                [item.get("name", item.get("signal", "")), ("launched (tracking unavailable)" if item.get("state") == "launched" and item.get("tracking") == "unavailable" else item.get("state", "unknown")),
                 ("exit code " + str(item["exit_code"])) if item.get("exit_code") is not None else item.get("detail", item.get("action", ""))]
                for item in runs
            ])]
        else:
            lines.append("No active runs.")
        if result.get("definitions_error"):
            lines.append("Saved actions unavailable: " + _plain(result["definitions_error"]))
        if result.get("message"):
            lines.append(str(result["message"]))
        return "\n".join(lines)
    for operation, past in (("pause", "Paused"), ("resume", "Resumed"), ("stop", "Stopped")):
        name = getattr(args, operation)
        if name is None:
            continue
        if result.get("message"):
            return prefix + _plain(result["message"])
        count = result.get("changed", result.get("count", 0))
        if operation == "stop" and result.get("pending"):
            past = "Stopping"
        failures = result.get("failed", [])
        if isinstance(failures, int):
            failures = ["operation failed"] * failures
        already = result.get("already", 0)
        unsupported = result.get("unsupported", 0)
        if count and not (already or unsupported or failures):
            text = f"{past} {name}." if re.fullmatch(r"HINT-[0-9]+", name) else f"{past} {count} {'run' if count == 1 else 'runs'} of '{name}'."
        elif count or already or unsupported or failures:
            parts = [f"{past} {count} {'run' if count == 1 else 'runs'}"] if count else []
            if already:
                state = {"pause": "paused", "resume": "running", "stop": "stopping"}[operation]
                parts.append(f"{already} already {state}")
            if unsupported:
                parts.append(f"{unsupported} unsupported")
            if failures:
                parts.append(f"{len(failures)} failed")
            text = "; ".join(parts) + "."
        else:
            text = f"No {'paused' if operation == 'resume' else 'active'} runs of '{name}'."
        for failure in failures:
            text += "\n" + prefix + _plain(failure.get("reason", failure.get("error", failure)) if isinstance(failure, dict) else failure)
        return prefix + text
    return prefix + "Operation completed."


def serve() -> int:
    import hint_playback
    from hint_network import serve as serve_network
    from hint_runtime import RuntimeServer

    hint_playback.SERVICE_MODE = True
    cfg = config()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    with RuntimeServer() as controller:
        try:
            arm_wake()
        except Exception as exc:
            print(f"HINT wake: {sanitize_error(exc)}", file=sys.stderr, flush=True)
        thread = None
        if cfg["nats_server"] is not None:
            def network_state(state):
                controller.manager.set_network_state(state)
            thread = threading.Thread(target=serve_network, args=(stop,), kwargs={"state_callback": network_state},
                                      name="hint-network", daemon=True)
            thread.start()
        else:
            controller.manager.set_network_state("local-only")
        while not stop.is_set():
            try:
                controller.manager.tick()
            except Exception as exc:
                print(f"HINT clock: {sanitize_error(exc)}", file=sys.stderr, flush=True)
            stop.wait(0.2)
        if thread is not None:
            thread.join(timeout=3)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["--session"]:
        from hint_ready import session
        try:
            session()
            return 0
        except Exception as exc:
            print("HINT session: " + _plain(sanitize_error(exc)), file=sys.stderr)
            return 1
    if argv[:1] == ["--complete"]:
        from hint_completion import main as complete
        return complete(argv[1:])
    if not argv:
        parser().print_help()
        return 0
    args = None
    try:
        args = parser().parse_args(argv)
        validate_args(args)
        if args.serve:
            return serve()
        if args.ready:
            from hint_ready import main as ready
            options = []
            if args.server is not None:
                options += ["--server", args.server]
            if args.local:
                options.append("--local")
            if args.enable_wake:
                options.append("--enable-wake")
            return ready(options)
        result = execute(args)
        if args.list and args.remote is True:
            output = render(result, args)
            if output:
                print(output)
            failures = [f"{_plain(host)}: {_plain(sanitize_error(value['error']))}"
                        for host, value in sorted(result["hosts"].items()) if "error" in value]
            if result.get("discovery_error"):
                failures.append("Discovery: " + _plain(sanitize_error(result["discovery_error"])))
            for message in failures:
                print(message, file=sys.stderr)
            return 1 if failures else 0
        failed = isinstance(result, dict) and (isinstance(result.get("error"), str) or bool(result.get("failed"))
                    or (bool(result.get("unsupported")) and not result.get("changed", result.get("count", 0))))
        print(render(result, args), file=sys.stderr if failed else sys.stdout)
        return 1 if failed else 0
    except HintError as exc:
        host = getattr(args, "remote", None) or socket.gethostname()
        if host is True:
            host = socket.gethostname()
        print(f"{_plain(host)}: {_plain(sanitize_error(exc))}", file=sys.stderr)
        return 2
    except Exception as exc:
        host = getattr(args, "remote", None) or socket.gethostname()
        if host is True:
            host = socket.gethostname()
        print(f"{_plain(host)}: Could not complete the operation: {_plain(sanitize_error(exc))}. Check status before retrying.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
