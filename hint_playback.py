"""Background media and command actions with service journal diagnostics."""

from __future__ import annotations

import errno
import mimetypes
import os
from pathlib import Path
import subprocess
import sys
import threading
from urllib.parse import urlparse

from hint_config import HintError


SERVICE_MODE = False
SHELL = "/bin/sh"


def _spawn(argv: list[str]) -> None:
    """Detach CLI actions; preserve a service action's journal diagnostics."""
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        # Service stdout is reserved for its private control messages. Send
        # both action streams to the service's journal/error destination.
        stdout=sys.stderr if SERVICE_MODE else subprocess.DEVNULL,
        stderr=None if SERVICE_MODE else subprocess.DEVNULL,
        start_new_session=True,
        cwd=Path.home() if SERVICE_MODE else None,
    )

    def reap() -> None:
        status = process.wait()
        if status:
            print(f"HINT action {Path(argv[0]).name}: exited with status {status}",
                  file=sys.stderr, flush=True)

    # A persistent HINT service must collect finished children itself. Waiting
    # on another thread also keeps firing and incoming requests responsive.
    threading.Thread(target=reap, name="hint-action-wait", daemon=True).start()


def _play(*options: str) -> None:
    # --terminal=no silences MPV's errors too. A service may write useful
    # warnings to its journal while still never reading keyboard input.
    terminal = (
        ["--terminal=yes", "--msg-level=all=warn", "--msg-color=no", "--term-osd=no"]
        if SERVICE_MODE else ["--terminal=no"]
    )
    _spawn(["mpv", *terminal, "--input-terminal=no", "--idle=no", "--keep-open=no", *options])


def launch(action: str) -> None:
    action = action.strip()
    if not action:
        raise HintError("empty action")
    if "\0" in action:
        raise HintError("action cannot contain a NUL character")
    parsed = urlparse(action)
    if parsed.scheme in {"http", "https"}:
        _spawn(["xdg-open", action])
        return

    path = Path(os.path.expanduser(action))
    try:
        exists = path.exists()
    except OSError as exc:
        # A perfectly valid shell command can exceed a filesystem component's
        # length limit. Only that error means to continue as command text.
        if exc.errno != errno.ENAMETOOLONG:
            raise
        exists = False
    if exists:
        path = path.resolve()
        if path.is_dir():
            files = [str(p) for p in sorted(path.iterdir()) if p.is_file()]
            if not files:
                raise HintError(f"empty directory: {path}")
            _play("--fullscreen", "--loop-file=no", "--loop-playlist=inf",
                  "--image-display-duration=8", *files)
            return
        mime = mimetypes.guess_type(str(path))[0] or ""
        if mime.startswith("audio/"):
            _play("--no-video", "--loop-file=no", "--loop-playlist=no", str(path))
        elif mime.startswith("video/"):
            _play("--fullscreen", "--loop-file=no", "--loop-playlist=no", str(path))
        elif mime.startswith("image/"):
            _play("--fullscreen", "--loop-file=inf", "--loop-playlist=no", str(path))
        elif os.access(path, os.X_OK):
            _spawn([str(path)])
        else:
            _spawn(["xdg-open", str(path)])
        return

    # Parse without executing: a failed check must not consume a scheduled
    # signal. Command lookup and runtime expansion still belong to the shell.
    try:
        syntax = subprocess.run(
            [SHELL, "-n", "-c", action], stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=2,
        )
    except subprocess.TimeoutExpired as exc:
        raise HintError("shell syntax check timed out") from exc
    if syntax.returncode:
        raise HintError("invalid shell syntax: " + syntax.stderr.strip())
    _spawn([SHELL, "-c", action])
