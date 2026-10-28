"""HINT's single configuration format and standard per-user paths."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit


def xdg_path(variable: str, fallback: Path) -> Path:
    value = os.environ.get(variable)
    return Path(value) if value and Path(value).is_absolute() else fallback


CONFIG_DIR = xdg_path("XDG_CONFIG_HOME", Path.home() / ".config") / "hint"
DATA_DIR = xdg_path("XDG_DATA_HOME", Path.home() / ".local/share") / "hint"
CONFIG = CONFIG_DIR / "config.json"
TABLE = DATA_DIR / "signals.json"
LOCK = DATA_DIR / ".hint.lock"
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
NAME = re.compile(r"^[A-Za-z0-9_-]+$")
RTC_HELPER = "/usr/local/sbin/hint-rtc"
DEFAULT_NATS_SERVER = "nats://127.0.0.1:4222"
DEFAULTS = {
    "nats_server": DEFAULT_NATS_SERVER,
    "wake_lead_seconds": 10,
    "request_timeout_seconds": 10,
    "discovery_timeout_seconds": 1.0,
}


class HintError(Exception):
    """An actionable HINT configuration or operation error."""


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HintError(f"cannot read valid JSON from {path}") from exc


def validate_config(value: object) -> dict:
    if not isinstance(value, dict):
        raise HintError("config.json must contain a JSON object")
    unknown = set(value) - set(DEFAULTS)
    if unknown:
        raise HintError("unknown configuration setting: " + ", ".join(sorted(unknown)))
    result = {**DEFAULTS, **value}
    server = result["nats_server"]
    if server is not None:
        if not isinstance(server, str):
            raise HintError("nats_server must be a NATS URL or null for local-only use")
        try:
            url = urlsplit(server)
            valid = (url.scheme in {"nats", "tls"} and url.hostname and
                     not url.query and not url.fragment and url.path in {"", "/"})
            port = url.port
            if not valid or (port is not None and not 1 <= port <= 65535) or any(c.isspace() for c in server):
                raise ValueError
        except ValueError:
            raise HintError("nats_server must be a full nats://host:port or tls://host:port URL") from None
    lead = result["wake_lead_seconds"]
    if type(lead) is not int or not 0 <= lead <= 86400:
        raise HintError("wake_lead_seconds must be an integer from 0 to 86400")
    for key in ("request_timeout_seconds", "discovery_timeout_seconds"):
        timeout = result[key]
        if (type(timeout) not in (int, float) or not math.isfinite(timeout)
                or not 0 < timeout <= 300):
            raise HintError(f"{key} must be a number greater than 0 and at most 300")
    return result


def config() -> dict:
    return validate_config(read_json(CONFIG, {}))


def sanitize_error(error: object) -> str:
    """Remove broker credentials before reporting dependency exceptions."""
    message = str(error) or type(error).__name__
    message = re.sub(r"(?i)\b(nats|tls|ws|wss)://[^\s/]*@", r"\1://[redacted]@", message)
    try:
        raw = read_json(CONFIG, {})
        server = raw.get("nats_server") if isinstance(raw, dict) else None
        if isinstance(server, str):
            password = urlsplit(server).password
            if password:
                for secret in {password, unquote(password)}:
                    message = message.replace(secret, "[redacted]")
    except (HintError, ValueError):
        pass
    return message
