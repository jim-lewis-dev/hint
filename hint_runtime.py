"""The local service owns action lifetimes and a private, live control socket.

Run information is intentionally held in memory. The socket and its lock are
runtime coordination, not another database or status file.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import socketserver
import stat
import struct
import tempfile
import threading
import time
import uuid

from hint_config import DATA_DIR, HintError, NAME, sanitize_error


MAX_MESSAGE = 1024 * 1024
RECENT_LIMIT = 100
TEMPORARY_NAME = re.compile(r"HINT-[0-9]+$")
SESSION_VARIABLES = frozenset({
    "DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS", "XDG_SESSION_TYPE"
})


def _now() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def runtime_path() -> Path:
    """Select the normal user runtime directory, with an isolated fallback."""
    configured = os.environ.get("XDG_RUNTIME_DIR", "")
    if configured and Path(configured).is_absolute():
        return Path(configured) / "hint" / "control.sock"
    key = hashlib.sha256(str(DATA_DIR.absolute()).encode()).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / f"hint-{os.getuid()}-{key}" / "control.sock"


class RunManager:
    """Track every owned launch, without imposing a one-run-per-name limit."""

    def __init__(self, launcher=None, *, recent_limit=RECENT_LIMIT):
        self._launcher = launcher
        self._lock = threading.RLock()
        self._active = []
        self._recent = deque(maxlen=recent_limit)
        self._next_temporary = 1
        self._closed = False
        self._update_token = None
        self._update_deadline = 0.0
        self._network_state = "connecting"

    def set_network_state(self, value: str) -> None:
        with self._lock:
            self._network_state = value

    @property
    def network(self):
        with self._lock:
            return self._network_state

    @network.setter
    def network(self, value):
        self.set_network_state(value)

    def _updating(self) -> bool:
        if self._update_token and time.monotonic() >= self._update_deadline:
            self._update_token = None
        return self._update_token is not None

    @staticmethod
    def _public(run: dict) -> dict:
        result = {key: value for key, value in run.items() if key != "_action"}
        if run["active"] and "_action" in run:
            try:
                result["detail"] = run["_action"].detail
            except (HintError, OSError):
                pass
        return result

    def _refresh(self) -> None:
        remaining = []
        for run in self._active:
            action = run["_action"]
            code = action.poll()
            if code is None:
                remaining.append(run)
                continue
            run["active"] = False
            run["ended_at"] = _now()
            run["exit_code"] = code
            if getattr(action, "untracked", False):
                run["state"] = "launched" if code == 0 else "failed"
                run["tracking"] = "unavailable"
            elif run["state"] == "stopping":
                run["state"] = "stopped"
            else:
                run["state"] = "finished" if code == 0 else "failed"
            dispose = getattr(action, "dispose", None)
            if dispose is not None:
                try:
                    dispose()
                except OSError:
                    pass
            self._recent.append(self._public(run))
        self._active = remaining

    def start(self, name: str | None, action: str) -> dict:
        with self._lock:
            self._refresh()
            if self._closed:
                raise HintError("HINT service is stopping. Run hint --ready.")
            if self._updating():
                raise HintError("HINT is preparing an update. Try again shortly.")
            if name is not None:
                if not isinstance(name, str) or not NAME.fullmatch(name):
                    raise HintError("Invalid action name.")
                if TEMPORARY_NAME.fullmatch(name):
                    raise HintError("Names beginning HINT- followed by digits are reserved for temporary runs.")
            if not isinstance(action, str) or not action.strip():
                raise HintError("Action cannot be empty.")
            temporary = name is None
            target = name if name is not None else f"HINT-{self._next_temporary:03d}"
            if self._launcher is None:
                from hint_playback import launch
                process = launch(action)
            else:
                process = self._launcher(action)
            if process is None:
                raise HintError("Action launcher did not return a managed run.")
            if temporary:
                self._next_temporary += 1
            self._active.append({
                "name": target,
                "signal": target,
                "temporary": temporary,
                "action": action,
                "kind": process.kind,
                "detail": process.detail,
                "pid": process.pid,
                "state": "running",
                "active": True,
                "started_at": _now(),
                "ended_at": None,
                "exit_code": None,
                "pausable": bool(process.pausable),
                "controllable": bool(process.controllable),
                "tracking": "unavailable" if getattr(process, "untracked", False) else "owned",
                "_action": process
            })
            return {"op": "fire", "signal": target, "run": target,
                    "temporary": temporary, "fired": True}

    @staticmethod
    def _definitions() -> dict:
        from hint_signals import list_signals
        return list_signals()

    def status(self, name=None) -> dict:
        if name is not None:
            self._validate_target(name)
        # Reading definitions must never hold the run-registry lock: a slow or
        # broken storage path must not prevent stopping an existing process.
        definitions_error = None
        try:
            definitions = self._definitions()
        except (HintError, OSError) as error:
            definitions = {}
            definitions_error = sanitize_error(error)
        from hint_signals import display_time
        display_times = {}
        try:
            display_times = {entry["time"]: display_time(entry["time"])
                             for entry in definitions.values() if "time" in entry}
        except (HintError, OSError) as error:
            definitions_error = sanitize_error(error)
        with self._lock:
            self._refresh()
            runs = [*self._recent, *(self._public(run) for run in self._active)]
            if name is not None:
                if (definitions_error is None and name not in definitions
                        and not any(run["name"] == name for run in runs)):
                    self._missing(name)
                definitions = {name: definitions[name]} if name in definitions else {}
                runs = [run for run in runs if run["name"] == name]
                display_times = {entry["time"]: display_times[entry["time"]]
                                 for entry in definitions.values() if entry.get("time") in display_times}
            result = {
                "op": "status",
                "host": socket.gethostname(),
                "capabilities": ["partial-set", "inline-fire", "controls", "popup"],
                "service": "running",
                "network": self._network_state,
                "definitions": definitions,
                "display_times": display_times,
                "runs": runs,
                "scheduled_count": sum("time" in entry for entry in definitions.values()),
                "active_count": sum(run["active"] for run in runs),
                "updating": self._updating()
            }
            if definitions_error is not None:
                result["definitions_error"] = definitions_error
            return result

    @staticmethod
    def _validate_target(name) -> None:
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise HintError("Invalid action name: use letters, digits, '_' or '-'.")

    @staticmethod
    def _missing(name) -> None:
        if TEMPORARY_NAME.fullmatch(name):
            raise HintError(f"Run {name} is no longer available. Run hint --status.")
        raise HintError(f"No action named '{name}'. Run hint --list.")

    def control(self, operation: str, name: str) -> dict:
        self._validate_target(name)
        definitions = {}
        with self._lock:
            self._refresh()
            runtime_target = any(run["name"] == name for run in self._active) or any(
                run["name"] == name for run in self._recent
            )
        if not runtime_target:
            if TEMPORARY_NAME.fullmatch(name):
                self._missing(name)
            # Existing runs remain controllable even if definitions are corrupt.
            # A missing runtime target still needs a valid definition lookup to
            # distinguish an idle saved action from a typo.
            definitions = self._definitions()
        with self._lock:
            self._refresh()
            selected = [run for run in self._active if run["name"] == name]
            if not selected and name not in definitions and not any(
                run["name"] == name for run in self._recent
            ):
                self._missing(name)
            result = {"op": operation, "signal": name, "name": name, "count": 0,
                      "already": 0, "unsupported": 0, "failed": [], "active": len(selected)}
            for run in selected:
                action = run["_action"]
                if not run["controllable"] or (operation != "stop" and not run["pausable"]):
                    result["unsupported"] += 1
                    continue
                if (operation == "pause" and run["state"] == "paused"
                        or operation == "resume" and run["state"] != "paused"
                        or operation == "stop" and run["state"] == "stopping"):
                    result["already"] += 1
                    continue
                try:
                    getattr(action, operation)()
                    run["state"] = {"pause": "paused", "resume": "running", "stop": "stopping"}[operation]
                    result["count"] += 1
                except (HintError, OSError) as error:
                    result["failed"].append({"name": name, "reason": sanitize_error(error)})
            self._refresh()
            result["changed"] = result["count"]
            if operation == "stop":
                # Finished entries were updated in place during _refresh.
                result["pending"] = sum(run["active"] and run["state"] == "stopping" for run in selected)
            return result

    def dispatch(self, body: dict) -> dict:
        if not isinstance(body, dict) or not isinstance(body.get("op"), str):
            raise HintError("Invalid HINT control request.")
        operation = body["op"]
        allowed = {
            "fire": {"op", "name", "action"},
            "pause": {"op", "name"}, "resume": {"op", "name"}, "stop": {"op", "name"},
            "status": {"op", "name"},
            "session": {"op", "environment"},
            "prepare_update": {"op", "lease_seconds"}, "cancel_update": {"op", "token"}
        }
        if operation not in allowed or set(body) - allowed[operation]:
            raise HintError("Invalid HINT control operation or arguments.")
        if operation == "status":
            return self.status(body.get("name"))
        if operation in {"pause", "resume", "stop"}:
            return self.control(operation, body.get("name"))
        if operation == "fire":
            name = body.get("name")
            action = body.get("action")
            if name is not None:
                self._validate_target(name)
                if action is not None:
                    raise HintError("Supply either a saved name or --action.")
                definitions = self._definitions()
                if name not in definitions:
                    self._missing(name)
                action = definitions[name]["action"]
            return self.start(name, action)
        if operation == "session":
            environment = body.get("environment")
            if (not isinstance(environment, dict) or set(environment) - SESSION_VARIABLES
                    or any(not isinstance(value, str) or "\0" in value for value in environment.values())):
                raise HintError("Invalid desktop session environment.")
            if not (environment.get("DISPLAY") or environment.get("WAYLAND_DISPLAY")):
                raise HintError("No usable desktop session was supplied.")
            with self._lock:
                # Session refresh and launching share a lock, so a new child
                # receives one complete session environment. Existing children
                # retain their original environment and are never restarted.
                for key in SESSION_VARIABLES - {"DBUS_SESSION_BUS_ADDRESS"}:
                    if key not in environment:
                        os.environ.pop(key, None)
                for key, value in environment.items():
                    if value:
                        os.environ[key] = value
                    else:
                        os.environ.pop(key, None)
            return {"op": "session", "updated": True}
        with self._lock:
            self._refresh()
            if operation == "prepare_update":
                seconds = body.get("lease_seconds", 60)
                if type(seconds) not in (float, int) or not 1 <= seconds <= 300:
                    raise HintError("Invalid update lease duration.")
                if self._active or self._updating():
                    return {"op": operation, "ready": False, "active": len(self._active),
                            "updating": self._updating()}
                self._update_token = uuid.uuid4().hex
                self._update_deadline = time.monotonic() + seconds
                return {"op": operation, "ready": True, "active": 0,
                        "token": self._update_token, "expires_in": seconds}
            if not self._updating() or body.get("token") != self._update_token:
                raise HintError("Invalid or expired update lease.")
            self._update_token = None
            return {"op": operation, "cancelled": True}

    def tick(self) -> None:
        from hint_signals import fire_due
        with self._lock:
            self._refresh()
            allowed = not self._closed and not self._updating()
        if allowed:
            # The callback checks the update lease again under the registry
            # lock. This avoids holding that lock while waiting for file I/O.
            fire_due(launcher=self.start)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for run in self._active:
                if run["controllable"]:
                    try:
                        run["_action"].stop()
                    except (HintError, OSError):
                        pass
            self._refresh()
            self._active.clear()
            self._recent.clear()


def _private_directory(directory: Path) -> None:
    if directory.is_symlink():
        raise HintError(f"Runtime directory must not be a symlink: {directory}.")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.stat()
    if info.st_uid != os.getuid() or not stat.S_ISDIR(info.st_mode):
        raise HintError(f"Runtime directory is not owned by this user: {directory}.")
    directory.chmod(0o700)


class _ControlServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 16

    def __init__(self, path, manager):
        self.manager = manager
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(str(path), _ControlHandler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class _ControlHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(3)
        try:
            if hasattr(socket, "SO_PEERCRED"):
                credentials = self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
                if struct.unpack("3i", credentials)[1] != os.getuid():
                    raise HintError("HINT control requests must come from the same user.")
            raw = self.rfile.readline(MAX_MESSAGE + 1)
            if len(raw) > MAX_MESSAGE or not raw.endswith(b"\n"):
                raise HintError("Invalid or oversized HINT control request.")
            body = json.loads(raw)
            response = {"ok": True, "value": self.server.manager.dispatch(body)}
        except Exception as error:
            response = {"ok": False, "error": sanitize_error(error)}
        try:
            raw = json.dumps(response, separators=(",", ":")).encode() + b"\n"
            if len(raw) > MAX_MESSAGE:
                raw = b'{"ok":false,"error":"HINT response is too large."}\n'
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionError, TimeoutError):
            pass


class RuntimeServer:
    def __init__(self, manager=None, path=None):
        self.manager = manager if manager is not None else RunManager()
        self.path = Path(path) if path is not None else runtime_path()
        self._server = None
        self._thread = None
        self._lock_fd = None
        self._socket_inode = None

    def start(self):
        if self._server is not None:
            return self
        _private_directory(self.path.parent)
        lock = self.path.parent / ".control.lock"
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        self._lock_fd = os.open(lock, flags, 0o600)
        try:
            info = os.fstat(self._lock_fd)
            if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode):
                raise HintError("Invalid HINT runtime lock.")
            os.fchmod(self._lock_fd, 0o600)
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise HintError("HINT service is already running.") from None
            if self.path.exists() or self.path.is_symlink():
                info = self.path.lstat()
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                    raise HintError(f"Refusing to replace unrelated runtime path: {self.path}.")
                self.path.unlink()
            self._server = _ControlServer(self.path, self.manager)
            self.path.chmod(0o600)
            self._socket_inode = self.path.stat().st_ino
            self._thread = threading.Thread(target=self._server.serve_forever,
                                            kwargs={"poll_interval": 0.1},
                                            name="hint-control", daemon=True)
            self._thread.start()
            return self
        except Exception:
            if self._server is not None:
                self._server.server_close()
                self._server = None
            os.close(self._lock_fd)
            self._lock_fd = None
            raise

    def close(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            if self._thread is not None:
                self._thread.join(timeout=2)
            self._server = None
        self.manager.close()
        if self._socket_inode is not None:
            try:
                if self.path.lstat().st_ino == self._socket_inode:
                    self.path.unlink()
            except FileNotFoundError:
                pass
            self._socket_inode = None
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *args):
        self.close()


def request(body: dict, timeout=10, *, path=None) -> dict:
    """One local request. A lost mutation reply is never automatically retried."""
    address = Path(path) if path is not None else runtime_path()
    if type(timeout) not in (int, float) or not 0 < timeout <= 300:
        raise HintError("Invalid HINT request timeout.")
    raw = json.dumps(body, separators=(",", ":")).encode() + b"\n"
    if len(raw) > MAX_MESSAGE:
        raise HintError("HINT control request is too large.")
    sent = False
    deadline = time.monotonic() + timeout
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout)
            connection.connect(str(address))
            sent = True
            connection.sendall(raw)
            received = bytearray()
            while b"\n" not in received:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                connection.settimeout(remaining)
                chunk = connection.recv(min(65536, MAX_MESSAGE + 1 - len(received)))
                if not chunk:
                    raise ConnectionError("HINT service closed its response.")
                received.extend(chunk)
                if len(received) > MAX_MESSAGE:
                    raise HintError("HINT response is too large.")
        response = json.loads(received.split(b"\n", 1)[0])
        if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
            raise ValueError
        if not response["ok"]:
            raise HintError(str(response.get("error", "HINT operation failed.")))
        value = response.get("value")
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (FileNotFoundError, ConnectionRefusedError):
        raise HintError("HINT service is not running. Run hint --ready.") from None
    except (TimeoutError, ConnectionError, BrokenPipeError):
        if sent and body.get("op") not in {"status"}:
            raise HintError("No reply from the HINT service. The request may have taken effect; it was not retried.") from None
        raise HintError("No reply from the HINT service. Run hint --ready.") from None
    except (ValueError, UnicodeError):
        raise HintError("Invalid response from the HINT service.") from None
