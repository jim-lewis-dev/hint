"""Bound HINT's live NATS operations without changing its transport protocol."""

from __future__ import annotations

import json
import os
import select
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid

from hint_config import DEFAULTS


SERVER_START_TIMEOUT = 10.0
SERVER_WATCHDOG_SECONDS = 3.0
SERVER_RETRY_SECONDS = 1.0
_OUTPUT_LOCK = threading.Lock()


def _hint():
    import hint
    return hint


def _error(message: str):
    return _hint().HintError(message)


def _safe(error: object) -> str:
    return _hint().sanitize_error(error)


def _configured() -> dict:
    config = _hint().config()
    if config.get("nats_server") is None:
        raise _error("Remote commands are disabled. Configure a NATS server first.")
    return config


def hostname() -> str:
    import re
    return re.sub(r"[^A-Za-z0-9_-]", "_", socket.gethostname()) or "computer"


def server_name() -> str:
    return f"hint_server_{hostname()}"


def _recipient(host: str) -> str:
    if not _hint().NAME.fullmatch(host or ""):
        raise _error("invalid remote name")
    return f"hint_server_{host}"


def _timeout_message(arguments: dict) -> str:
    host = arguments.get("recipient", "remote computer")
    host = host[len("hint_server_"):] if host.startswith("hint_server_") else host
    argv = arguments.get("argv", [])
    operation = argv[0].partition("=")[0] if argv else ""
    if operation in {"--get", "--list", "--status"}:
        return f"No reply from '{host}'. Check HINT on that computer."
    return f"No reply from '{host}'. The request may have taken effect; it was not retried."


def _client_name() -> str:
    return f"hint_client_{uuid.uuid4().hex}"


def _decode_worker(output: str):
    try:
        envelope = json.loads(output)
    except (ValueError, TypeError):
        raise _error("invalid response from the HINT network worker") from None
    if not isinstance(envelope, dict) or not isinstance(envelope.get("ok"), bool):
        raise _error("invalid response from the HINT network worker")
    if not envelope["ok"]:
        raise _error(_safe(envelope.get("error", "HINT network operation failed")))
    return envelope.get("value")


def _run_client(kind: str, *, timeout: float | None = None, **arguments):
    config = _configured()
    if kind == "remote":
        allowance = float(config.get("request_timeout_seconds", 10))
    else:
        allowance = max(1.0, float(config.get("discovery_timeout_seconds", DEFAULTS["discovery_timeout_seconds"])) + 0.7)
    if timeout is not None:
        allowance = min(allowance, max(0.05, timeout))
        config = {**config, "discovery_timeout_seconds": min(
            float(config.get("discovery_timeout_seconds", DEFAULTS["discovery_timeout_seconds"])),
            max(0.05, allowance - 0.5)
        )}
    body = {"kind": kind, "config": config,
            "deadline": time.monotonic() + allowance, **arguments}
    try:
        result = subprocess.run(
            [sys.executable, "-m", "hint_network", "--client-worker"],
            input=json.dumps(body), capture_output=True, text=True, timeout=allowance,
        )
    except subprocess.TimeoutExpired as error:
        # A complete reply remains useful if transport cleanup stalls
        # after the worker printed it. subprocess.run has already killed/reaped it.
        output = error.stdout or ""
        if isinstance(output, bytes):
            output = output.decode(errors="replace")
        if output.strip():
            return _decode_worker(output)
        if kind == "remote":
            raise _error(_timeout_message(arguments)) from None
        raise _error("HINT discovery timed out; check the configured NATS server") from None
    if not result.stdout.strip():
        raise _error("HINT network operation failed; check the configured NATS server")
    return _decode_worker(result.stdout)


def remote(recipient: str, argv: list[str], *, timeout: float | None = None) -> dict:
    """Execute one request, with no retry, and return its existing JSON reply."""
    target = _recipient(recipient)
    operation = argv[0].partition("=")[0] if argv else ""
    if operation == "--set":
        # An older peer accepts --set --action but replaces omitted settings.
        # Verify partial-update support before sending any mutation.
        allowance = float(_configured().get("request_timeout_seconds", 10))
        if timeout is not None:
            allowance = min(allowance, timeout)
        deadline = time.monotonic() + allowance
        status = _run_client("remote", recipient=target, argv=["--status"], timeout=allowance)
        if (not isinstance(status, dict) or status.get("op") != "status"
                or not isinstance(status.get("capabilities"), list)
                or "partial-set" not in status["capabilities"]):
            host = recipient
            return {"error": f"'{host}' does not support partial updates. Run hint --ready on that computer."}
        timeout = deadline - time.monotonic()
        if timeout <= 0:
            raise _error("The remote check timed out. No changes were sent.")
    value = _run_client("remote", recipient=target, argv=argv, timeout=timeout)
    if not isinstance(value, dict):
        raise _error("invalid response from the remote HINT server")
    if isinstance(value.get("error"), str):
        message = _safe(value["error"])
        unsupported = ("unrecognized arguments" in message.lower()
                       or "unrecognized argument" in message.lower()
                       or (operation == "--set" and "--set requires --action" in message)
                       or (operation == "--fire" and "--fire" in message and "expected one argument" in message))
        if unsupported:
            operation = (operation[2:] if operation.startswith("--") else operation) or "this operation"
            host = recipient
            message = f"'{host}' does not support {operation}. Run hint --ready on that computer."
        value = {**value, "error": message}
    return value


def discover(*, timeout: float | None = None) -> list[str]:
    """Collect responding HINT names within a bounded worker process."""
    value = _run_client("discover", timeout=timeout)
    if not isinstance(value, list) or any(
        not isinstance(name, str) or not name.startswith("hint_server_")
        or not _hint().NAME.fullmatch(name[len("hint_server_"):]) for name in value
    ):
        raise _error("invalid HINT discovery response")
    return sorted({name[len("hint_server_"):] for name in value})


def _emit(value: dict) -> None:
    with _OUTPUT_LOCK:
        sys.stdout.write(json.dumps(value) + "\n")
        sys.stdout.flush()


def _client_worker(body: dict) -> int:
    import shlex
    from agent_messenger import AgentMessenger
    from nats_transport import NatsTransport

    messenger = None
    status = 1
    sent = False
    connected = False
    try:
        config = body["config"]
        remaining = body["deadline"] - time.monotonic()
        if remaining <= 0:
            raise _error("HINT network operation timed out")
        messenger = AgentMessenger(_client_name(), NatsTransport(
            config["nats_server"], connect_timeout=min(3.0, remaining),
            operation_timeout=min(3.0, remaining), close_timeout=min(1.0, remaining),
        ))
        connected = True
        remaining = body["deadline"] - time.monotonic()
        if remaining <= 0:
            raise _error("HINT network operation timed out")
        if body["kind"] == "discover":
            window = min(float(config.get("discovery_timeout_seconds", DEFAULTS["discovery_timeout_seconds"])), remaining)
            value = sorted(name for name in messenger.discover(timeout=window)
                           if name.startswith("hint_server_") and _hint().NAME.fullmatch(name))
        else:
            sent = True
            reply = messenger.request(body["recipient"],
                                      shlex.join(["hint", *body["argv"]]), timeout=remaining)
            value = json.loads(reply)
        _emit({"ok": True, "value": value})
        status = 0
    except Exception as error:
        if not connected:
            message = f"Could not connect to the NATS server: {_safe(error)}. Nothing was sent."
        elif sent and isinstance(error, TimeoutError):
            message = _timeout_message(body)
        else:
            message = _safe(error)
        _emit({"ok": False, "error": message})
    finally:
        if messenger is not None:
            try:
                messenger.close()
            except Exception:
                pass
    return status


def _native_connection_closed(transport) -> bool:
    # The pinned synchronous wrapper has no public health API. Keep its one
    # implementation-specific check here; the supervising process also handles
    # hangs or crashes independently of this check.
    connection = getattr(transport, "_nc", None)
    return bool(connection is not None and connection.is_closed)


def _server_worker(config: dict) -> int:
    import hint_playback
    from agent_messenger import AgentMessenger
    from nats_transport import NatsTransport

    hint_playback.SERVICE_MODE = True
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    messenger = None
    try:
        allowance = float(config.get("request_timeout_seconds", 10))
        transport = NatsTransport(config["nats_server"],
                                  connect_timeout=min(3.0, allowance),
                                  operation_timeout=min(3.0, allowance),
                                  close_timeout=min(1.0, allowance))
        messenger = AgentMessenger(server_name(), transport)

        def receive(event: dict) -> None:
            if event.get("type") != "in_request":
                return
            try:
                reply = _hint().command_text(str(event.get("payload", "")))
            except Exception as error:
                reply = json.dumps({"error": _safe(error)})
            try:
                messenger.respond(str(event["request_id"]), reply)
            except Exception as error:
                print(f"HINT response: {_safe(error)}", file=sys.stderr, flush=True)

        messenger.listen(receive)
        _emit({"event": "ready", "name": server_name()})
        while not stop.wait(0.4):
            if _native_connection_closed(transport):
                raise _error("NATS connection closed; reconnecting")
            _emit({"event": "heartbeat"})
        return 0
    except Exception as error:
        _emit({"event": "error", "error": _safe(error)})
        return 1
    finally:
        if messenger is not None:
            try:
                messenger.close()
            except Exception:
                pass


def _stop_process(process) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)
    if process.stdout is not None:
        process.stdout.close()


def _supervise(process, stop: threading.Event, state_callback=None) -> str:
    ready = False
    deadline = time.monotonic() + SERVER_START_TIMEOUT
    buffer = b""
    while not stop.is_set():
        if process.poll() is not None:
            return "NATS worker stopped; reconnecting"
        if time.monotonic() >= deadline:
            return "NATS worker timed out; reconnecting"
        readable, _, _ = select.select([process.stdout], [], [], 0.1)
        if not readable:
            continue
        chunk = os.read(process.stdout.fileno(), 65536)
        if not chunk:
            return "NATS worker stopped; reconnecting"
        buffer += chunk
        if len(buffer) > 1024 * 1024:
            return "invalid NATS worker output; reconnecting"
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            text = line.decode(errors="replace")
            try:
                event = json.loads(text)
            except ValueError:
                print(f"HINT NATS: {_safe(text)}", file=sys.stderr, flush=True)
                continue
            if not isinstance(event, dict):
                continue
            if event.get("event") == "ready":
                ready = True
                deadline = time.monotonic() + SERVER_WATCHDOG_SECONDS
                print(f"HINT listening on {hostname()}", flush=True)
                if state_callback is not None:
                    state_callback("connected")
            elif event.get("event") == "heartbeat" and ready:
                deadline = time.monotonic() + SERVER_WATCHDOG_SECONDS
            elif event.get("event") == "error":
                return _safe(event.get("error", "NATS worker failed"))
    return ""


def serve(stop: threading.Event, state_callback=None) -> None:
    """Supervise the installed server; child requests call hint.command_text."""
    while not stop.is_set():
        process = None
        try:
            config = _hint().config()
            if config.get("nats_server") is None:
                if state_callback is not None:
                    state_callback("disabled")
                return
            if state_callback is not None:
                state_callback("connecting")
            process = subprocess.Popen(
                [sys.executable, "-u", "-m", "hint_network", "--server-worker"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            )
            process.stdin.write((json.dumps(config) + "\n").encode())
            process.stdin.close()
            reason = _supervise(process, stop, state_callback)
            if reason and not stop.is_set():
                print(f"HINT NATS: {_safe(reason)}", file=sys.stderr, flush=True)
        except Exception as error:
            if not stop.is_set():
                print(f"HINT NATS: {_safe(error)}", file=sys.stderr, flush=True)
        finally:
            if state_callback is not None:
                state_callback("unavailable")
            if process is not None:
                _stop_process(process)
        stop.wait(SERVER_RETRY_SECONDS)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["--client-worker"]:
        return _client_worker(json.load(sys.stdin))
    if argv == ["--server-worker"]:
        return _server_worker(json.load(sys.stdin))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
