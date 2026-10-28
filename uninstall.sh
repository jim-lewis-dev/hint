#!/usr/bin/env bash
set -euo pipefail

if [[ $# -gt 0 ]]; then
  if [[ $# -eq 1 && ( "$1" == --help || "$1" == -h ) ]]; then
    printf 'Usage: ./uninstall.sh\nRemoves HINT installation; preserves configuration and signals.\n'
    exit 0
  fi
  printf 'Usage: ./uninstall.sh\n' >&2
  exit 2
fi
if [[ "$EUID" -eq 0 ]]; then
  printf 'Run ./uninstall.sh as the user who installed HINT, without sudo.\n' >&2
  exit 1
fi
xdg_base() {
  case "$1" in /*) printf '%s\n' "$1" ;; *) printf '%s\n' "$2" ;; esac
}
CONFIG_HOME="$(xdg_base "${XDG_CONFIG_HOME:-}" "$HOME/.config")"
DATA_HOME="$(xdg_base "${XDG_DATA_HOME:-}" "$HOME/.local/share")"
APP_DIR="$DATA_HOME/hint"
ENV_DIR="$APP_DIR/venv"
COMMAND="$HOME/.local/bin/hint"
SERVICE="$CONFIG_HOME/systemd/user/hint.service"
COMPLETION="$DATA_HOME/bash-completion/completions/hint"
USER_NAME="$(id -un)"
OWN_COMMAND=false
OWN_SERVICE=false
if [[ -e "$COMMAND" || -L "$COMMAND" ]]; then
  if [[ -L "$COMMAND" && "$(readlink -f -- "$COMMAND")" == "$ENV_DIR/bin/hint" ]]; then
    OWN_COMMAND=true
  else
    printf 'Refusing to remove a command not installed by HINT: %s\n' "$COMMAND" >&2
    exit 1
  fi
fi
if [[ -e "$SERVICE" ]]; then
  if grep -Fq 'HINT managed user service' "$SERVICE"; then OWN_SERVICE=true
  else
    printf 'Refusing to remove a service not installed by HINT: %s\n' "$SERVICE" >&2
    exit 1
  fi
fi
if [[ -e "$ENV_DIR" ]]; then
  if [[ -L "$ENV_DIR" || ! -f "$ENV_DIR/.hint-owned" ]] ||
     ! grep -Fxq 'HINT managed environment' "$ENV_DIR/.hint-owned"; then
    printf 'Refusing to remove an environment not identified as HINT: %s\n' "$ENV_DIR" >&2
    exit 1
  fi
fi

if [[ "$OWN_SERVICE" == true ]]; then
  timeout 10s systemctl --user disable --now hint.service
fi
if [[ -f "$APP_DIR/wake-access" ]] &&
   grep -Fxq "HINT managed RTC access for $USER_NAME" "$APP_DIR/wake-access"; then
  WAKE_RULE="/etc/sudoers.d/hint-rtc-$USER_NAME"
  if sudo test -f "$WAKE_RULE"; then
    if ! sudo grep -Fq '# HINT managed RTC access' "$WAKE_RULE"; then
      printf 'Refusing to remove an unrecognized RTC access rule: %s\n' "$WAKE_RULE" >&2
      exit 1
    fi
    # A retained wake-enabled event must not leave an unattended HINT alarm.
    if [[ -f "$APP_DIR/signals.json" ]] && python3 - "$APP_DIR/signals.json" <<'PY'
import json
from pathlib import Path
import sys
try:
    table = json.loads(Path(sys.argv[1]).read_text())
    raise SystemExit(0 if isinstance(table, dict) and any(isinstance(v, dict) and v.get("wake") is True for v in table.values()) else 1)
except (OSError, ValueError):
    raise SystemExit(1)
PY
    then
      sudo -n /usr/local/sbin/hint-rtc disable || printf 'Could not clear the RTC alarm; inspect it before leaving the computer unattended.\n' >&2
    fi
    sudo rm -f "$WAKE_RULE"
  fi
  if [[ -f /usr/local/sbin/hint-rtc ]] && grep -Fq '# HINT managed RTC helper' /usr/local/sbin/hint-rtc; then
    if [[ -z "$(sudo find /etc/sudoers.d -maxdepth 1 -name 'hint-rtc-*' -print -quit)" ]]; then
      sudo rm -f /usr/local/sbin/hint-rtc
    fi
  fi
  rm -f "$APP_DIR/wake-access"
fi
if [[ "$OWN_COMMAND" == true ]]; then rm -f -- "$COMMAND"; fi
if [[ "$OWN_SERVICE" == true ]]; then
  rm -f -- "$SERVICE"
  timeout 10s systemctl --user daemon-reload
fi
if [[ -f "$COMPLETION" ]] && grep -Fq 'Bash completion for the installed HINT command' "$COMPLETION"; then
  rm -f -- "$COMPLETION"
fi
if [[ -d "$ENV_DIR" ]]; then rm -rf -- "$ENV_DIR"; fi
printf 'HINT uninstalled. Preserved settings: %s\nPreserved signals: %s\n' "$CONFIG_HOME/hint/config.json" "$APP_DIR/signals.json"
