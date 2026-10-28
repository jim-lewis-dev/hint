#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
usage() {
  printf 'Usage: ./install.sh [--server URL | --local] [--enable-wake]\n'
  printf 'Existing configuration and signals are preserved. Run as your normal user.\n'
}
MODE=auto
NATS_SERVER=""
ENABLE_WAKE=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --server)
      [[ $# -ge 2 && "$MODE" == auto && -n "$2" ]] || { usage >&2; exit 2; }
      MODE=server; NATS_SERVER="$2"; shift 2 ;;
    --local)
      [[ "$MODE" == auto ]] || { usage >&2; exit 2; }
      MODE=local; shift ;;
    --enable-wake) ENABLE_WAKE=true; shift ;;
    *) usage >&2; exit 2 ;;
  esac
done
if [[ "$EUID" -eq 0 ]]; then
  printf 'Run ./install.sh as your normal desktop user, without sudo.\n' >&2
  exit 1
fi
umask 077

xdg_base() {
  case "$1" in /*) printf '%s\n' "$1" ;; *) printf '%s\n' "$2" ;; esac
}
CONFIG_HOME="$(xdg_base "${XDG_CONFIG_HOME:-}" "$HOME/.config")"
DATA_HOME="$(xdg_base "${XDG_DATA_HOME:-}" "$HOME/.local/share")"
APP_DIR="$DATA_HOME/hint"
ENV_DIR="$APP_DIR/venv"
CONFIG_FILE="$CONFIG_HOME/hint/config.json"
SIGNALS_FILE="$APP_DIR/signals.json"
COMMAND="$HOME/.local/bin/hint"
SERVICE="$CONFIG_HOME/systemd/user/hint.service"
COMPLETION="$DATA_HOME/bash-completion/completions/hint"
USER_NAME="$(id -un)"

# Replace only a command installed by this project.
if [[ -e "$COMMAND" || -L "$COMMAND" ]]; then
  if [[ ! -L "$COMMAND" ]]; then
    printf 'Refusing to replace an existing command: %s\n' "$COMMAND" >&2
    exit 1
  fi
  if [[ "$(readlink -f -- "$COMMAND")" != "$ENV_DIR/bin/hint" ]]; then
    printf 'Refusing to replace a command not installed by HINT: %s\n' "$COMMAND" >&2
    exit 1
  fi
fi
if [[ -e "$SERVICE" ]] && ! grep -Fq 'HINT managed user service' "$SERVICE"; then
  printf 'Refusing to replace a service not installed by HINT: %s\n' "$SERVICE" >&2
  exit 1
fi
if [[ -e "$COMPLETION" ]] && ! grep -Fq 'Bash completion for the installed HINT command' "$COMPLETION"; then
  printf 'Refusing to replace an existing completion: %s\n' "$COMPLETION" >&2
  exit 1
fi
if [[ -e "$ENV_DIR" ]] && { [[ -L "$ENV_DIR" ]] ||
   [[ ! -f "$ENV_DIR/.hint-owned" ]] ||
   ! grep -Fxq 'HINT managed environment' "$ENV_DIR/.hint-owned"; }; then
  printf 'Refusing to modify an environment not installed by HINT: %s\n' "$ENV_DIR" >&2
  exit 1
fi
for ASSET in pyproject.toml hint.service rtc_helper.py completions/hint.bash; do
  [[ -f "$HERE/$ASSET" ]] || { printf 'Missing installation file: %s\n' "$ASSET" >&2; exit 1; }
done

if [[ -f "$CONFIG_FILE" ]]; then
  if [[ "$MODE" != auto ]]; then
    printf 'Existing configuration was preserved. Edit %s to change its server.\n' "$CONFIG_FILE" >&2
    exit 2
  fi
  printf 'Keeping existing configuration and signal data.\n'
elif [[ "$MODE" == auto ]]; then
  if [[ -t 0 ]]; then
    read -r -p 'NATS server URL [nats://127.0.0.1:4222]: ' NATS_SERVER
  fi
  NATS_SERVER="${NATS_SERVER:-nats://127.0.0.1:4222}"
fi

PLAN="$(mktemp)"
UNIT="$(mktemp)"
RULE="$(mktemp)"
PREVIOUS_UNIT="$(mktemp)"
SERVICE_STOPPED=false
cleanup() {
  RESULT=$?
  if [[ "$RESULT" -ne 0 && "$SERVICE_STOPPED" == true ]]; then
    printf 'Installation failed; attempting to restart the previous HINT service.\n' >&2
    if [[ -s "$PREVIOUS_UNIT" ]]; then
      install -m 0644 "$PREVIOUS_UNIT" "$SERVICE"
      timeout 10s systemctl --user daemon-reload || true
    fi
    timeout 10s systemctl --user start hint.service || printf 'Could not restart HINT; inspect systemctl --user status hint.service.\n' >&2
  fi
  rm -f "$PLAN" "$UNIT" "$RULE" "$PREVIOUS_UNIT"
  return "$RESULT"
}
trap cleanup EXIT

# Validate before installing packages or changing system files.
python3 - "$HERE" "$CONFIG_FILE" "$SIGNALS_FILE" "$MODE" "$NATS_SERVER" > "$PLAN" <<'PYCONFIG'
import json
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
from hint_config import DEFAULTS, HintError, validate_config
from hint_signals import validate
try:
    config_path, signals_path = map(Path, sys.argv[2:4])
    if config_path.is_file():
        configuration = json.loads(config_path.read_text())
    else:
        configuration = {**DEFAULTS, "nats_server": None if sys.argv[4] == "local" else sys.argv[5]}
    validate_config(configuration)
    table = json.loads(signals_path.read_text()) if signals_path.is_file() else {}
    if not isinstance(table, dict):
        raise HintError("signals.json must contain a JSON object")
    for name, entry in table.items():
        validate(name, entry)
    json.dump(configuration, sys.stdout)
except (HintError, OSError, ValueError) as error:
    raise SystemExit(f"HINT installation: {error}")
PYCONFIG

sudo apt-get update
PACKAGES=(python3-venv mpv xdg-utils bash-completion git)
sudo apt-get install -y "${PACKAGES[@]}"
mkdir -p "$APP_DIR"
if [[ ! -x "$ENV_DIR/bin/python" ]]; then
  mkdir -p "$ENV_DIR"
  printf 'HINT managed environment\n' > "$ENV_DIR/.hint-owned"
  python3 -m venv "$ENV_DIR"
fi

# Stop the running service before updating its installed application.
if timeout 10s systemctl --user is-active --quiet hint.service; then
  if [[ -f "$SERVICE" ]]; then cp "$SERVICE" "$PREVIOUS_UNIT"; fi
  timeout 10s systemctl --user stop hint.service
  SERVICE_STOPPED=true
fi
"$ENV_DIR/bin/python" -m pip install --upgrade "$HERE"
"$ENV_DIR/bin/hint" --help >/dev/null
AUTO_WAKE="$(python3 - "$HERE" "$PLAN" "$CONFIG_FILE" "$SIGNALS_FILE" <<'PYSTATE'
import json
import os
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
from hint_config import validate_config
from hint_signals import validate
plan, config_path, signals_path = map(Path, sys.argv[2:])
configuration = json.loads(config_path.read_text()) if config_path.is_file() else json.loads(plan.read_text())
validate_config(configuration)
table = json.loads(signals_path.read_text()) if signals_path.is_file() else {}
if not isinstance(table, dict):
    raise SystemExit("signals.json must contain a JSON object")
for name, entry in table.items():
    validate(name, entry)
for destination, value in ((config_path, configuration), (signals_path, table)):
    destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if not destination.exists():
        contents = (json.dumps(value, indent=2) + "\n").encode()
        with os.fdopen(os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
    destination.chmod(0o600)
print("true" if any(entry.get("wake") is True for entry in table.values()) else "false")
PYSTATE
)"
if [[ "$AUTO_WAKE" == true ]] || { [[ -f "$APP_DIR/wake-access" ]] &&
   grep -Fxq "HINT managed RTC access for $USER_NAME" "$APP_DIR/wake-access"; }; then
  ENABLE_WAKE=true
fi
if [[ "$ENABLE_WAKE" == true ]]; then
  sudo apt-get install -y util-linux
  sudo install -o root -g root -m 0755 "$HERE/rtc_helper.py" /usr/local/sbin/hint-rtc
  printf '# HINT managed RTC access\n%s ALL=(root) NOPASSWD: /usr/local/sbin/hint-rtc *\n' "$USER_NAME" > "$RULE"
  sudo visudo -cf "$RULE"
  sudo install -o root -g root -m 0440 "$RULE" "/etc/sudoers.d/hint-rtc-$USER_NAME"
  printf 'HINT managed RTC access for %s\n' "$USER_NAME" > "$APP_DIR/wake-access"
  chmod 0600 "$APP_DIR/wake-access"
fi

install -d -m 0700 "$(dirname "$COMMAND")" "$(dirname "$SERVICE")" "$(dirname "$COMPLETION")"
ln -snf "$ENV_DIR/bin/hint" "$COMMAND"
install -m 0644 "$HERE/completions/hint.bash" "$COMPLETION"
python3 - "$HERE/hint.service" "$ENV_DIR/bin/hint" "$CONFIG_HOME" "$DATA_HOME" > "$UNIT" <<'PY'
from pathlib import Path
import sys
def quote(value):
    if "\n" in value or "\r" in value:
        raise SystemExit("Installation paths cannot contain line breaks")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'
text = Path(sys.argv[1]).read_text()
lines = []
for line in text.splitlines():
    if line.startswith("ExecStart="):
        lines.extend(("ExecStart=" + quote(sys.argv[2]) + " --serve",
                      "Environment=" + quote("XDG_CONFIG_HOME=" + sys.argv[3]),
                      "Environment=" + quote("XDG_DATA_HOME=" + sys.argv[4])))
    else:
        lines.append(line)
print("\n".join(lines))
PY
install -m 0644 "$UNIT" "$SERVICE"
timeout 10s systemctl --user daemon-reload
DESKTOP_VARIABLES=()
for VARIABLE in DISPLAY WAYLAND_DISPLAY XAUTHORITY DBUS_SESSION_BUS_ADDRESS; do
  if [[ -v "$VARIABLE" && -n "${!VARIABLE}" ]]; then DESKTOP_VARIABLES+=("$VARIABLE"); fi
done
if [[ ${#DESKTOP_VARIABLES[@]} -gt 0 ]]; then
  timeout 10s systemctl --user import-environment "${DESKTOP_VARIABLES[@]}"
fi
timeout 10s systemctl --user enable hint.service
timeout 10s systemctl --user restart hint.service
for CHECK in 1 2 3 4; do
  sleep 0.25
  timeout 5s systemctl --user is-active --quiet hint.service
done
(cd "$HOME" && "$ENV_DIR/bin/hint" --list >/dev/null)
SERVICE_STOPPED=false
printf '\nHINT installed. Settings: %s\nSignals: %s\n' "$CONFIG_FILE" "$SIGNALS_FILE"
printf 'Try: hint --list\nCompletion in this shell: source %q\n' "$COMPLETION"
