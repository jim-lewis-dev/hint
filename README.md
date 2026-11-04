# HINT - Human INTerrupt

[![Tests](https://github.com/jim-lewis-dev/hint/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/jim-lewis-dev/hint/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**An alarm clock with unreasonable ambition.**

Launch reminders, media, messages, and commands locally, remotely, or on a
schedule.

```bash
hint --fire --popup 'Dinner is ready!' --remote beacon
```

HINT can play a song, fill a screen with a message, loop a folder of media,
open a web page, or run a shell command. Save useful actions under names,
schedule them, and pause, resume, or stop what you launched.

Each computer owns its definitions and runs. Remote communication uses live
Core NATS through [Agent Messenger](https://github.com/jim-lewis-dev/agent_messenger)
and [NATS transport](https://github.com/jim-lewis-dev/nats_transport). There is
no JetStream or offline message queue. Local use needs no NATS broker.

[Install](#install-once-on-each-computer) | [Examples](#ready-set-fire) |
[Remote use](#remote-use) | [Actions](#actions-and-controls) |
[Settings](#configuration-and-files) | [Troubleshooting](#troubleshooting-and-removal)

## Install once on each computer

HINT targets Ubuntu 20.04 or newer, Python 3.8 or newer, Bash, and a systemd
user session. Open a terminal in your desktop and run as your normal user:

```bash
sudo apt-get update
sudo apt-get install -y git
mkdir -p "$HOME/projects"
git clone https://github.com/jim-lewis-dev/hint.git "$HOME/projects/hint"
cd "$HOME/projects/hint"
./hint --ready
```

When no saved configuration exists, setup asks for a NATS server hostname,
IP address, or URL, or `local` for this computer only. You must choose
explicitly; empty input does not silently select a server. Updates reuse
your saved choice.

You can supply the choice directly. Without an interactive terminal, setup
requires `--server` or `--local`:

```bash
./hint --ready --server server
```

Replace `server` with your broker's hostname or IP address. Use the same
reachable broker on participating computers. `127.0.0.1` always means the
computer running HINT; it does not identify a broker on another computer.
For a computer that needs no remote communication, use `./hint --ready --local`.
Bare addresses use `nats://` and port 4222. Supply `server:PORT` for another
port, or a full `nats://` or `tls://` URL for credentials or TLS. IPv6 addresses
require a full bracketed URL, such as `nats://[::1]:4222`.

Setup installs HINT and its Python dependencies, MPV, desktop-opening tools,
GTK, Bash completion, and a background user service. System package
installation may request sudo; do not run the whole command with sudo.
Ubuntu 20.04 uses its existing Python 3.8; setup installs timezone support
inside HINT's own environment without replacing the system Python.
The NATS broker itself is separate: use an existing broker or follow the
[NATS getting-started guide](https://docs.nats.io/concepts/getting-started/)
before testing remote operations. HINT uses Core NATS; enabling JetStream is
unnecessary.

For several computers, choose one reliably available broker and use its URL
on every computer. Use a hostname that all of them can resolve, or reserve the
broker's IP address in your router's DHCP settings so it stays stable.
`hint --discover` finds HINT computers through that broker; it does not scan
your network for NATS servers or choose one automatically.

HINT starts immediately and automatically with your user session on later
logins. After suspend, it continues with that session. Displaying windows
requires a usable desktop session; booting to a login screen is not sufficient.

Open a new terminal, then try:

```bash
hint --status
hint --fire --popup 'HINT is ready.'
```

Dismiss the popup with its button or Escape. If `hint` is not found, ensure
`~/.local/bin` is on your PATH. The project-local `./hint` launcher also works.

## Ready, set, fire

```bash
hint --set stretch --popup 'Get up and stretch.'
hint --fire stretch
hint --status stretch
hint --stop stretch
```

`--set` saves a definition; it does not launch it. `--fire` launches immediately
and returns without taking over your terminal. Firing the same name again
starts another run. A fire acknowledgement means **launched**, not successfully
finished; use status for its progress or outcome.

Schedule the same action a minute from now:

```bash
hint --set stretch --time "$(date -d '+1 minute' '+%Y-%m-%d %H:%M:%S')"
```

After its scheduled launch, the definition remains and its schedule is cleared.
Set another time to schedule it again. Existing attributes stay unchanged when
you omit them:

```bash
hint --set stretch --popup 'Walk around for five minutes.'
hint --set stretch --no-time
hint --set stretch --no-wake
```

`--no-time` removes the schedule. `--no-wake` disables hardware waking without
removing the schedule. Neither command stops an active run.

## Everyday commands

| Command | Purpose |
| --- | --- |
| `hint --ready` | Install or update HINT and ensure its service is running |
| `hint --set NAME --action STRING` | Save or replace a command, path, or URL |
| `hint --set NAME --popup TEXT` | Save or replace a popup message |
| `hint --set NAME --time TIME` | Add or change an existing action's schedule |
| `hint --set NAME --no-time` | Clear its schedule |
| `hint --set NAME --wake` / `--no-wake` | Enable or disable its wake preference |
| `hint --get NAME` | Show one saved definition |
| `hint --list` | Show all saved definitions |
| `hint --list --remote` | Show local and discovered computers' definitions, grouped by hostname |
| `hint --fire NAME` | Launch another run of a saved action |
| `hint --fire --action STRING` | Launch an action without saving a definition |
| `hint --fire --popup TEXT` | Display a popup without saving a definition |
| `hint --pause NAME` / `--resume NAME` | Pause or resume the active runs that support it |
| `hint --stop NAME` | Stop the active runs HINT can control |
| `hint --status [NAME]` | Show service, schedule, and run information |
| `hint --remove NAME` | Remove a saved definition and its schedule |
| `hint --discover` | List responding HINT hostnames |

Names use letters, digits, `_`, and `-`, and are case-sensitive. Spell options
in full. Names of the form `HINT-` followed by digits, ignoring case, are reserved for
temporary runs. Creating a new definition requires either `--action` or
`--popup`; modifying an existing one requires only the attributes you want to
change. Setting `--popup` replaces its previous action, and setting `--action`
replaces its previous popup. Schedule and wake settings stay unchanged unless
you change them. Conflicting switches such as `--action ... --popup ...` or
`--time ... --no-time` are rejected.

Pause, resume, and stop by a saved name apply to **all its active runs**.
Removing a definition does not stop those runs; they remain visible in status
and selectable for control. Stopping a run does not remove its definition or
cancel a future schedule.

Nameless fires receive temporary names such as `HINT-001`, then `HINT-002`.
Use the name printed by the fire response to control that run:

```bash
hint --status HINT-001
hint --stop HINT-001
```

Temporary names are not saved definitions. Numbers are not recycled during a
service session, but numbering restarts with the service. Always use current
status rather than an old remembered temporary name after a restart.

The terminal shows readable results, tables, and errors. Messages between HINT
computers remain JSON. Completion is quiet when no candidates are available.

## Remote use

Give computers distinct hostnames and run one HINT service per hostname on a
shared broker. Use the hostname in commands and completion:

```bash
hint --discover
hint --list --remote beacon
hint --set stretch --popup 'Take a break.' --remote beacon
hint --fire stretch --remote beacon
hint --status stretch --remote beacon
hint --stop stretch --remote beacon
hint --fire --popup 'Dinner is ready!' --remote beacon
```

Replace `beacon` with a hostname returned by discovery. Every installed computer
can send and receive. The receiver must be awake, connected, and running HINT.
You can type a known hostname directly even if discovery did not list it.
`--ready` prepares the local computer; it does not remotely upgrade peers.
All participating computers must already be configured to use the same broker.

To see saved actions across your computers in one command:

```bash
hint --list --remote
```

`hint --remote --list` does the same thing. The result includes this computer
and responding peers, grouped by hostname. It cannot include offline computers
that discovery cannot find. If discovery or an individual peer fails, HINT
keeps the available results, reports the error, and exits with a nonzero status.
Only `--list` accepts `--remote` without a hostname; firing or changing actions
always requires a specific receiver.

Paths, command execution, and scheduled times belong to the **receiving
computer**. For example, `'~/Music/a song.mp3'` refers to its user's music
folder. Single quotes keep your local shell from expanding the action first.
All managed actions, including local ones, run from the receiving user's home
directory. Use absolute paths or explicit `cd` commands when another working
directory is needed.

Discovery is a bounded live poll, not an inventory. A busy, reconnecting, or
unreachable computer may not answer in time. Messages sent while a receiver
is offline or asleep are not stored. A remote timeout may occur after an action
has already launched; check status before repeating it. HINT does not blindly
retry mutating requests.

Remote commands run as the receiving desktop user. Use a broker accessible only
to trusted users and computers, with appropriate NATS authentication and network
controls: a client permitted to send HINT requests can launch commands on the
receiving computer. Configure broker listening, authentication, and the client port
(normally 4222) separately. Credentials belong in the private configuration,
not in the repository. A NATS URL can contain percent-encoded credentials;
`tls://HOST:PORT` uses the client's TLS trust configuration.

If the broker computer uses UFW, allow its NATS port from your trusted LAN
subnet. For example, on the **broker computer**:

```bash
sudo ufw allow proto tcp from 192.168.1.0/24 to any port 4222
```

Replace `192.168.1.0/24` with your LAN subnet and use the configured NATS port.
A rule allowing only one client IP will block your other computers. This rule
does not configure NATS authentication or router port forwarding.

VPNs on **every participating computer**, including the broker, must permit
LAN traffic. For NordVPN on Linux, check `nordvpn settings`; on your trusted
LAN, enable LAN discovery if it is disabled:

```bash
nordvpn set lan-discovery enable
```

This permits local-network traffic while the VPN stays connected. See
[NordVPN's LAN settings](https://support.nordvpn.com/hc/en-us/articles/20196094470929-How-to-install-the-NordVPN-app-on-Linux-distributions).
HINT's installer does not change your firewall, VPN, or router settings.

## Actions and controls

| Action | What happens | Pause/resume |
| --- | --- | --- |
| `--popup 'Your message'` | HINT opens its own fullscreen text window | Not applicable |
| Audio file | MPV plays once, without a video window | Supported |
| Video file | MPV plays once fullscreen | Supported |
| Image file | MPV shows it fullscreen until closed | Pauses animations; static images remain displayed |
| Directory | MPV loops all immediate files in filename order; images last eight seconds | Supported |
| HTTP(S) URL | Open an isolated HINT browser instance and request fullscreen | Not supported |
| PDF, text, or office document | Open a separate viewer where supported; request fullscreen where available | Not supported |
| Executable file | Run the file directly | Suspend/continue its owned processes |
| Shell command | Run through noninteractive `/bin/sh` | Suspend/continue its owned processes |

`--popup` displays literal text, not HTML or a shell command. Use it with
`--set NAME` to save a message or with `--fire` to display one immediately.
`--action` continues to open paths and URLs and treats other text as a shell
command; it does not recognize popup prefixes. Use single quotes in Bash to
keep `!`, `$`, and command substitutions literal:

```bash
hint --fire --popup 'Dinner is ready!'
hint --set dinner --popup 'Dinner is ready!' --time '2027-01-01 18:00'
```

Directory playback remains deliberately broad: every immediate regular file,
including hidden and non-media files, is passed to MPV. It is nonrecursive and
uses lexical filename order (`10` before `2`). MPV determines what it can play;
unsupported entries can report errors or be skipped. Names such as `01`, `02`,
and `03` make the order obvious. A directory containing only subdirectories
has no files to submit and is rejected.

HINT requests fullscreen where the application supports it and continues
quietly otherwise. Its browser uses separate profile data, so your personal
browser's logins and cookies are not automatically present. HINT controls its
own instances; it does not kill unrelated browser or document windows.
URLs and PDFs require an available Firefox or Chromium-family browser. Text
files use HINT's own read-only viewer; supported office documents use an
isolated LibreOffice profile in read-only mode when LibreOffice is available.
Some other desktop associations hand off to an existing application. Those launches
are explicitly untracked rather than pretending HINT can safely control them.

X11 and Wayland desktops are supported through the chosen applications.
HINT's own GTK text windows support both display protocols.
Desktop integration, installed viewers, and graphics/audio drivers vary;
verify the [practical checks](docs/actions.md) on each target computer.
Raspberry Pi OS and other non-Ubuntu installations need separate validation;
this installer is designed around Ubuntu's packages and user services.

For a command with redirects or arguments, quote the whole action once:

```bash
hint --set task --action 'printf "%s\n" booyah > hint-example.txt'
hint --fire task
```

This creates `hint-example.txt` in the receiving user's home directory.
`>` replaces existing contents; `>>` appends. Double quotes inside the outer
single quotes can protect paths while allowing expansion when the action runs.
Commands do not load `.bashrc` or interactive aliases and receive empty stdin.
For complex work, use an executable script. See [actions and practical checks](docs/actions.md)
for routing order, executable permissions, process control, and examples.

## Scheduling and optional wake

A year is required. Missing month/day values become January/the first; missing
hour, minute, and second values become zero:

| Input | Meaning |
| --- | --- |
| `2027` | `2027-01-01 00:00:00` |
| `2027-02` | `2027-02-01 00:00:00` |
| `2027-02-10 09` | `2027-02-10 09:00:00` |
| `2027-02-10 09:30` | `2027-02-10 09:30:00` |

Times use the receiving computer's local timezone. Invalid dates, times in the
past, and ambiguous or nonexistent local times at daylight-saving changes are
rejected. Scheduling output shows the resolved time and timezone.

Schedules are one-shot. Overdue actions are considered when the service next
runs or resumes. A launch failure keeps the schedule for retry. Once launch
succeeds, HINT clears the schedule and retains the definition. If saving that
change fails, the running service suppresses another launch while retrying the
save. This is not an exactly-once guarantee across a crash or power loss.
Manual firing leaves a future schedule intact.

To enable optional RTC wake support, run from the project directory:

```bash
./hint --ready --enable-wake
```

Then set a time and wake preference:

```bash
hint --set stretch --time "$(date -d '+5 minutes' '+%Y-%m-%d %H:%M:%S')" --wake
```

HINT arms the earliest scheduled wake, normally ten seconds early. The computer
must support RTC waking from the selected sleep state. Waking from shutdown is
hardware/firmware-dependent and does not create a logged-in desktop session.
HINT does not suspend the computer or send Wake-on-LAN packets. Test waking on
each computer separately. Clearing a schedule retains its wake preference for
a later schedule but leaves no alarm for that unscheduled action.

## Keep each installation current

```bash
hint --ready
```

The command updates a clean Git checkout by fast-forward, prepares the new
installation, and activates it when there are no active runs. It preserves
settings and definitions. It does not overwrite local source edits or resolve
divergent Git history. If active runs prevent an update, finish or stop them
and run it again.

The new environment is prepared before stopping the working service. Activation
is checked, and failed activation attempts restore the previous installation.
HINT installs the dependency versions declared by the tested application
revision, including its tested NATS Python client version. It does not upgrade
the operating system or shared NATS server. Run `--ready` on each computer;
pushing to GitHub does not update other installations automatically.

Keep the source checkout available: installation remembers its location. If it
moves, run `./hint --ready` from the new location. For a source archive without
Git history, replace it with current source and run `./hint --ready` there.
An older peer may require updating before it supports a newly added operation.

## Configuration and files

| File | Purpose |
| --- | --- |
| `~/.config/hint/config.json` | Broker and timeout settings |
| `~/.local/share/hint/signals.json` | Saved actions and schedules |
| `~/.local/share/hint/.hint.lock` | Coordinates changes to those definitions |
| `~/.local/share/hint/venv` | Points to the active installed Python environment |
| `~/.local/share/hint/source` | Points to the source checkout used for updates |
| `~/.local/bin/hint` | Installed command |
| `~/.local/share/bash-completion/completions/hint` | Bash completion |
| `~/.config/systemd/user/hint.service` | User service |
| `~/.config/autostart/hint.desktop` | Refreshes the running service with the graphical login session |

Absolute `XDG_CONFIG_HOME` and `XDG_DATA_HOME` replace the corresponding base
paths. Private temporary sockets live under the user runtime directory, with a
private temporary-directory fallback when necessary. Run status stays in
memory: there is no `status.json`. The service keeps active runs and a bounded
recent history; restarting it clears that history and stops its controlled
runs. Untracked external windows may remain open. A graphical-login entry refreshes
the service with the current display session without restarting active runs.

Your live settings are in **`~/.config/hint/config.json`**. To change the
broker, edit the `nats_server` entry in that private file, keeping its other
settings. For example:

```json
"nats_server": "nats://192.168.1.10:4222"
```

Use your broker's address, or JSON `null` for local-only operation. Then restart
the service; this ends its active runs:

```bash
systemctl --user restart hint.service
```

[config.example.json](config.example.json) documents the available settings.
Editing that example or the source file `hint_config.py` does not change the
installed service's saved configuration. Setup flags choose the initial
configuration; later `--ready` updates preserve it.

`request_timeout_seconds` bounds a remote operation. Discovery uses its response
collection window plus a short connection/cleanup allowance. Timeouts must be
positive and at most 300 seconds. `wake_lead_seconds` is an integer from 0 to
86400. Unknown settings are rejected. Settings and definitions are private user
files; the lock contains no actions and should be left alone while HINT runs.

## Bash completion

Press Tab to complete options, saved action names, active temporary names, and
remote hostnames. Name completion uses fresh local data or a fresh query to the
selected remote computer. `--set` completes existing names and still accepts
new ones. Controls include active names even if their saved definitions were
removed. A missing or unreachable remote never falls back to local names.

Completion has its own short network budget and an outer two-second limit.
It does not wait for a long configured discovery timeout. No answers means no
candidates, not proof that a computer is absent. Completion never launches an
action or changes a schedule. Path completion within an arbitrary action string
is not provided. `--popup` accepts free text and does not suggest filenames
or action names for the message. This integration is for Bash, not Fish or Zsh.

To enable it in an already open Bash terminal:

```bash
source "${XDG_DATA_HOME:-$HOME/.local/share}/bash-completion/completions/hint"
```

## Troubleshooting and removal

For a remote connection failure, check these in order:

1. **Saved settings:** confirm `nats_server` in `~/.config/hint/config.json`
   points to the intended broker. `127.0.0.1` means this computer.
2. **TCP reachability:** on the failing computer, test the broker's host and
   port. Replace the example address below with yours:

   ```bash
   python3 -c 'import socket; socket.create_connection(("192.168.1.10", 4222), timeout=3).close(); print("TCP connection succeeded")'
   ```

   A timeout or refusal requires checking the broker, route, firewall, and VPN
   LAN settings. Reinstalling HINT does not repair a blocked TCP connection.
   A successful connection confirms reachability, not NATS authentication or TLS.
3. **Service diagnostics:** inspect HINT's status and journal on the affected
   computer for connection, authentication, or action errors:

   ```bash
   hint --status
   systemctl --user status hint.service --no-pager
   journalctl --user -u hint.service -b -n 30 --no-pager
   ```

The journal is the existing system service log. It holds launch diagnostics and
errors, including a shell command's later failure. You do not need to watch it
during normal use. If a fire is acknowledged but nothing appears, check the
receiver's status and journal, its action path, and its desktop session.

From the project directory, `./uninstall.sh` removes the managed installation
and keeps your settings and saved definitions.

## Development

Install a `nats-server` binary for the live network tests. Ubuntu 24.04 provides
it with `sudo apt-get install nats-server`; on older Ubuntu releases, use an
[official NATS server release](https://github.com/nats-io/nats-server/releases).
The application itself can use a broker on another computer.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e .
REQUIRE_NATS_SERVER=1 REQUIRE_LOCAL_IPC=1 .venv/bin/python -m unittest discover -s tests -v
```

Tests use isolated user directories, harmless commands, controlled player
substitutes, and a loopback NATS broker. Privileged installer operations are
stubbed. Without `nats-server`, live network tests skip unless
`REQUIRE_NATS_SERVER=1` is set. Real display/audio and hardware wake behavior
still need the [target-computer checks](docs/actions.md).

The [test workflow](https://github.com/jim-lewis-dev/hint/actions/workflows/test.yml)
runs Python 3.8, 3.10, 3.12, and 3.14, plus Ubuntu 20.04's stock Python and GTK
packages. The modern Ubuntu jobs also test popup startup with both GTK 3 and
GTK 4 installed. These checks do not prove that a particular computer's audio,
desktop session, or firmware wake support works.

HINT depends on Agent Messenger, which installs NATS transport and `nats-py`.
It contains no copied implementations of those libraries. Dependency changes
are reviewed with the application tests. See [pyproject.toml](pyproject.toml)
for the exact dependency revision.

MIT licensed; see [LICENSE](LICENSE).
