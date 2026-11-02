"""Owned media, window and command processes for the HINT service."""
from __future__ import annotations

import errno
import json
import mimetypes
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlparse

from hint_config import HintError

SERVICE_MODE = False
SHELL = "/bin/sh"
GUI_PYTHON = "/usr/bin/python3"
OFFICE_SUFFIXES = {
    ".odt", ".ods", ".odp", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".rtf", ".csv"
}


def _group_alive(group: int) -> bool:
    """Ignore exited grandchildren awaiting collection by their new parent."""
    proc = Path("/proc")
    try:
        matching_namespace = int(os.readlink("/proc/self")) == os.getpid()
    except (OSError, ValueError):
        matching_namespace = False
    if proc.is_dir() and matching_namespace:
        for entry in proc.iterdir():
            if not entry.name.isdecimal():
                continue
            try:
                fields = (entry / "stat").read_text().rsplit(") ", 1)[1].split()
                if int(fields[2]) == group and fields[0] not in {"Z", "X"}:
                    return True
            except (OSError, ValueError, IndexError):
                continue
        return False
    try:
        os.killpg(group, 0)
        return True
    except ProcessLookupError:
        return False


def _temporary(kind: str, browser: str | None = None) -> Path:
    base = None
    if browser:
        snap = ("firefox" if "firefox" in browser else
                "libreoffice" if "libreoffice" in browser else "chromium")
        if Path(f"/snap/{snap}/current").exists() or str(browser).startswith("/snap/"):
            # Snap browsers have a different /tmp and cannot read arbitrary
            # hidden home directories. Their own common directory is accessible.
            base = Path.home() / "snap" / snap / "common"
            base.mkdir(parents=True, exist_ok=True, mode=0o700)
        else:
            cache = Path(os.environ.get("XDG_CACHE_HOME", ""))
            if not cache.is_absolute():
                cache = Path.home() / ".cache"
            base = cache / "hint"
            base.mkdir(parents=True, exist_ok=True, mode=0o700)
    else:
        runtime = Path(os.environ.get("XDG_RUNTIME_DIR", ""))
        if runtime.is_absolute() and runtime.is_dir() and runtime.stat().st_uid == os.getuid():
            base = runtime
    if not browser and base is not None and len(os.fsencode(base)) > 65:
        base = Path("/tmp")
    return Path(tempfile.mkdtemp(prefix=f"hint-{kind}-", dir=base))


class ManagedAction:
    """One owned process group; the service polls and reaps its child."""

    def __init__(self, process, *, kind="command", detail="", pausable=True,
                 controllable=True, temporary=None, ipc=None):
        self.process = process
        self.pid = process.pid
        self.kind = kind
        self._detail = detail
        self.pausable = pausable
        self.controllable = controllable
        self.untracked = not controllable
        self.paused = False
        self.temporary = temporary
        self.ipc = ipc
        self._disposed = False
        self._reported = False
        self._ipc_lock = threading.Lock()

    @property
    def detail(self):
        if self.ipc and self.process.poll() is None:
            try:
                current = self._command(["get_property", "path"], timeout=0.08)
                if isinstance(current, str):
                    return Path(current).name
            except (HintError, OSError):
                pass
        return self._detail

    def poll(self):
        code = self.process.poll()
        if code is None:
            return None
        if self.controllable and _group_alive(self.pid):
            return None
        self.dispose()
        if code and SERVICE_MODE and not self._reported:
            print(f"HINT action: exited with status {code}", file=sys.stderr, flush=True)
        self._reported = True
        return code

    def dispose(self):
        if not self._disposed:
            self._disposed = True
            if self.temporary is not None:
                shutil.rmtree(self.temporary, ignore_errors=True)

    def _command(self, command, timeout=1.0):
        if not self.ipc:
            raise HintError("This action does not support playback control.")
        deadline = time.monotonic() + timeout
        with self._ipc_lock:
            while True:
                try:
                    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                except OSError as exc:
                    raise HintError("Cannot open media control: " + str(exc)) from exc
                connection.settimeout(max(0.01, deadline - time.monotonic()))
                try:
                    connection.connect(str(self.ipc))
                    break
                except (FileNotFoundError, ConnectionRefusedError):
                    connection.close()
                    if self.process.poll() is not None:
                        raise HintError("This run has already finished.") from None
                    if time.monotonic() >= deadline:
                        raise HintError("The media player did not answer.") from None
                    time.sleep(0.01)
                except OSError as exc:
                    connection.close()
                    raise HintError("Cannot connect to media control: " + str(exc)) from exc
            with connection:
                try:
                    connection.sendall(json.dumps({"command": command, "request_id": 1}).encode() + b"\n")
                    data = b""
                    while time.monotonic() < deadline:
                        connection.settimeout(max(0.01, deadline - time.monotonic()))
                        chunk = connection.recv(65536)
                        if not chunk:
                            break
                        data += chunk
                        if len(data) > 1024 * 1024:
                            raise HintError("Invalid response from the media player.")
                        while b"\n" in data:
                            line, data = data.split(b"\n", 1)
                            response = json.loads(line)
                            if not isinstance(response, dict):
                                raise HintError("Invalid response from the media player.")
                            if response.get("request_id") == 1:
                                if response.get("error") != "success":
                                    raise HintError("Media control failed: " + str(response.get("error")))
                                return response.get("data")
                except (OSError, ValueError) as exc:
                    raise HintError("The media player did not answer.") from exc
        raise HintError("The media player did not answer.")

    def _signal(self, value):
        try:
            os.killpg(self.pid, value)
        except ProcessLookupError:
            raise HintError("This run has already finished.") from None

    def pause(self):
        if not self.pausable:
            raise HintError("This action does not support pausing.")
        if self.poll() is not None:
            raise HintError("This run has already finished.")
        if self.ipc:
            self._command(["set_property", "pause", True])
        else:
            self._signal(signal.SIGSTOP)
        self.paused = True

    def resume(self):
        if not self.pausable:
            raise HintError("This action does not support pausing.")
        if self.poll() is not None:
            raise HintError("This run has already finished.")
        if self.ipc:
            self._command(["set_property", "pause", False])
        else:
            self._signal(signal.SIGCONT)
        self.paused = False

    def stop(self):
        if not self.controllable:
            raise HintError("HINT cannot control this application's window.")
        if self.poll() is not None:
            return
        if self.ipc:
            try:
                self._command(["quit"], timeout=0.2)
            except HintError:
                pass
        try:
            # SIGTERM cannot be handled while a command is stopped.
            os.killpg(self.pid, signal.SIGCONT)
            os.killpg(self.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        self.paused = False
        deadline = time.monotonic() + 0.8
        while self.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        if self.poll() is None:
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                raise HintError("The action has not stopped yet.") from None
        self.poll()


def _spawn(argv, *, kind="command", detail="", pausable=True,
           controllable=True, temporary=None, ipc=None):
    try:
        process = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL,
            stdout=2 if SERVICE_MODE else subprocess.DEVNULL,
            stderr=2 if SERVICE_MODE else subprocess.DEVNULL,
            start_new_session=True, cwd=Path.home()
        )
    except OSError as exc:
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)
        if isinstance(exc, FileNotFoundError):
            raise HintError(f"Cannot launch action: '{argv[0]}' is not available.") from exc
        raise HintError(f"Cannot launch action: {exc}") from exc
    return ManagedAction(process, kind=kind, detail=detail, pausable=pausable,
                         controllable=controllable, temporary=temporary, ipc=ipc)


def _desktop():
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise HintError("Cannot display this action: no usable desktop session.")


def _play(*options, kind="media", detail="", pausable=True):
    if "--no-video" not in options:
        _desktop()
    temporary = _temporary("mpv")
    ipc = temporary / "control.sock"
    terminal = (["--terminal=yes", "--msg-level=all=warn", "--msg-color=no", "--term-osd=no"]
                if SERVICE_MODE else ["--terminal=no"])
    return _spawn(["mpv", *terminal, "--input-terminal=no", "--idle=no", "--keep-open=no",
                   f"--input-ipc-server={ipc}", *options], kind=kind, detail=detail,
                  pausable=pausable, temporary=temporary, ipc=ipc)


def _browser(target):
    _desktop()
    program = next((shutil.which(name) for name in
                    ("firefox", "firefox-esr", "chromium", "chromium-browser", "google-chrome")
                    if shutil.which(name)), None)
    if program is None:
        raise HintError("Cannot launch action: Firefox or Chromium is not available.")
    temporary = _temporary("browser", browser=program)
    if "firefox" in Path(program).name:
        argv = [program, "--no-remote", "--profile", str(temporary), "--kiosk", target]
    else:
        argv = [program, f"--user-data-dir={temporary}", "--no-first-run",
                "--no-default-browser-check", "--start-fullscreen", "--new-window", target]
    return _spawn(argv, kind="browser", detail=target, pausable=False, temporary=temporary)


def _popup(value, *, file=False):
    _desktop()
    helper = Path(__file__).with_name("hint_popup.py")
    option = "--file=" if file else "--text="
    return _spawn([GUI_PYTHON, str(helper), option + value],
                  kind="document" if file else "popup", detail=value, pausable=False)


def _fullscreen_owned(action):
    """Optional X11 fallback, targeting only windows in this process group."""
    if not os.environ.get("DISPLAY") or not shutil.which("wmctrl"):
        return

    def attempt():
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and action.poll() is None:
            try:
                result = subprocess.run(["wmctrl", "-lp"], text=True, capture_output=True, timeout=0.3)
                for line in result.stdout.splitlines():
                    fields = line.split(None, 4)
                    if len(fields) >= 3 and fields[2].isdigit() and os.getpgid(int(fields[2])) == action.pid:
                        subprocess.run(["wmctrl", "-ir", fields[0], "-b", "add,fullscreen"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=0.3)
                        return
            except (OSError, subprocess.TimeoutExpired):
                pass
            time.sleep(0.1)
    threading.Thread(target=attempt, name="hint-fullscreen", daemon=True).start()


def _document(path, mime):
    _desktop()
    if mime == "application/pdf":
        return _browser(path.as_uri())
    if path.suffix.lower() in OFFICE_SUFFIXES and shutil.which("libreoffice"):
        program = shutil.which("libreoffice")
        temporary = _temporary("office", browser=program)
        action = _spawn([program, "-env:UserInstallation=" + temporary.as_uri(),
                         "--norestore", "--view", str(path)], kind="document", detail=str(path),
                        pausable=False, temporary=temporary)
        _fullscreen_owned(action)
        return action
    if mime.startswith("text/"):
        return _popup(str(path), file=True)
    return _spawn(["xdg-open", str(path)], kind="document", detail=str(path),
                  pausable=False, controllable=False)


def launch(action: str) -> ManagedAction:
    """Interpret an action once and launch it without terminal interaction."""
    if not isinstance(action, str) or not action.strip():
        raise HintError("Action cannot be empty.")
    action = action.lstrip()
    if "\0" in action:
        raise HintError("Action cannot contain a NUL character.")
    if action[:6].lower() == "popup:":
        text = action[6:]
        if not text.strip():
            raise HintError("Popup text cannot be empty.")
        return _popup(text)
    action = action.strip()
    if urlparse(action).scheme.lower() in {"http", "https"}:
        return _browser(action)
    path = Path(os.path.expanduser(action))
    try:
        exists = path.exists()
    except OSError as exc:
        if exc.errno != errno.ENAMETOOLONG:
            raise
        exists = False
    if exists:
        path = path.resolve()
        if path.is_dir():
            files = [str(p) for p in sorted(path.iterdir()) if p.is_file()]
            if not files:
                raise HintError(f"Directory contains no files: {path}.")
            return _play("--fullscreen", "--loop-file=no", "--loop-playlist=inf",
                         "--image-display-duration=8", *files, kind="directory", detail=str(path))
        mime = mimetypes.guess_type(str(path))[0] or ""
        if mime.startswith("audio/"):
            return _play("--no-video", "--loop-file=no", "--loop-playlist=no", str(path),
                         kind="audio", detail=str(path))
        if mime.startswith("video/"):
            return _play("--fullscreen", "--loop-file=no", "--loop-playlist=no", str(path),
                         kind="video", detail=str(path))
        if mime.startswith("image/"):
            return _play("--fullscreen", "--loop-file=inf", "--loop-playlist=no", str(path),
                         kind="image", detail=str(path))
        if os.access(path, os.X_OK):
            return _spawn([str(path)], detail=str(path))
        return _document(path, mime)
    try:
        syntax = subprocess.run([SHELL, "-n", "-c", action], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=2)
    except subprocess.TimeoutExpired as exc:
        raise HintError("Shell syntax check timed out.") from exc
    if syntax.returncode:
        raise HintError("Invalid command syntax: " + syntax.stderr.strip())
    return _spawn([SHELL, "-c", action], detail=action)
