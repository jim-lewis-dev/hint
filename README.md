# HINT — Human INTerrupt

HINT gives a name to an action: play a sound, show a picture or slideshow,
open a page, or start a command. Fire it immediately or schedule it once.
Computers connected to the same NATS broker can discover each other and set,
read, fire, or remove each other's signals.

Each computer owns its own signals. Messaging uses live Core NATS through
[Agent Messenger](https://github.com/jim-lewis-dev/agent_messenger) and its
[NATS transport](https://github.com/jim-lewis-dev/nats_transport) dependency.
There is no JetStream or offline queue.

## Install on each computer

Supported: Ubuntu 22.04 or newer, Python 3.10 or newer, Bash, and a systemd
user session.

Copy or unpack the HINT source folder to `~/projects/hint` on the new computer.
The folder must contain `install.sh`, `pyproject.toml`, and the Python source
files. A local source copy is sufficient; HINT does not need to be in a Git
repository. Open a terminal in your desktop session and run as your normal user:

```bash
cd "$HOME/projects/hint"
./install.sh --server nats://192.168.1.10:4222
```

Replace the example server URL with your NATS broker's address. Repeat on each
computer, using the **same reachable broker**. `127.0.0.1` means the computer
running the command, so it cannot identify another computer's broker.

The installer installs HINT, MPV, desktop-opening tools, Bash completion, Git,
and a background user service. Agent Messenger, NATS transport, and the NATS
Python client install automatically as dependencies. Internet access is needed
to download packages and the dependencies hosted on GitHub. The **NATS broker
is separate**; use one already running on your network or configure one before testing remote
operations. You do not need a broker on every computer.

The installer requests sudo for system packages. Do not run the entire script
with sudo. Once installed, HINT starts automatically when you log in and stays
available for schedules and remote requests. It resumes with your session after
suspend. Desktop actions require a logged-in desktop session; startup before
login is not promised.

Open a new terminal for the `hint` command and completion. If `hint` is not
found, ensure `~/.local/bin` is on your PATH. Verify installation with:

```bash
hint --list
systemctl --user status hint.service --no-pager
hint --discover
```

Running `./install.sh` without options asks for a broker address on the first
interactive installation. For a local-only computer, replace the installation
command with:

```bash
./install.sh --local
```

For optional hardware wake support, add `--enable-wake` to the first installation
command, or run this later from the project directory:

```bash
./install.sh --enable-wake
```

Wake support installs a restricted privileged RTC helper. It still depends on
the computer's firmware and Linux support; test it on each target computer.

## Update an installed computer

Get the current HINT source on **each computer** you want to update, then run:

```bash
cd "$HOME/projects/hint"
./install.sh
```

The installer updates the installed application and restarts the service while
preserving that computer's settings and signals. It also refreshes completion
and any existing wake support. Open a new terminal afterward.

Use the same source-distribution method you used for installation: replace the
source folder with the current copy, or, once HINT is published and you have
cloned that repository, run `git pull --ff-only` before `./install.sh`.
Publishing a change with `git push` does not update installed computers.
Keep communicating computers on the same HINT version. On an existing
installation, omit `--server` and `--local`; to change the broker, edit the [configuration](#configuration-and-files)
and restart the service.

## First useful test

```bash
hint --set example --action https://example.com
hint --get example
hint --fire example
hint --remove example
```

The set command saves the named signal; the fire command opens your browser
and returns JSON promptly. It does not borrow terminal input or wait for the
browser to close. `--get example` shows the stored action:

```json
{
  "example": {
    "action": "https://example.com"
  }
}
```

## Commands

```text
hint --list
hint --get NAME
hint --fire NAME
hint --set NAME --action STRING [--time "YYYY-MM-DD HH:MM:SS"] [--wake]
hint --remove NAME
hint --discover
hint --remote SERVER <operation>
```

Names contain letters, digits, `_`, and `-`. Options must be spelled in full.
Responses are JSON. Exit status is 0 for a completed operation, 1 for an
operation failure, and 2 for invalid input/configuration or a reported HINT
error. `--get` returns `{ "NAME": null }` for a missing name; removing a missing
name returns `"removed": false` without failing.

`--set` replaces the entire definition. Manual `--fire` keeps its definition and
scheduled time. `"fired": true` means an action process started, not that it
finished successfully. A timed signal is removed after its action starts.
Removing a definition does not stop an already running action.

## Remote use

Each service is named `hint_server_<hostname>`. Characters outside letters,
digits, `_`, and `-` become underscores. Give participating computers distinct
hostnames, and run one HINT service per hostname on a shared broker.

```bash
hint --discover
hint --remote hint_server_beacon --list
hint --remote hint_server_beacon --set example --action https://example.com
hint --remote hint_server_beacon --get example
hint --remote hint_server_beacon --fire example
hint --remote hint_server_beacon --remove example
```

Every installed computer can both send and receive. The recipient must be awake,
connected to the broker, and running its service. Paths and times refer to the
**receiving computer**. Quote a path such as `'~/Sounds/ding.wav'` so its `~` is
expanded there. `$HOME` in double quotes is expanded by the sender's shell.

`--discover` lists peers that respond during the collection window, one second
by default. It is a live poll, not a complete inventory: busy, reconnecting, or
slow computers can miss the deadline. Run it again if a peer is missing, or
increase `discovery_timeout_seconds` for a slower network. Late responses are
not retained. Remote-name completion performs the same fresh poll; it does not
register peers or remember earlier results.

A timeout does not prove that a remote action did not execute. Check its state
before repeating a mutation. HINT does not automatically retry those requests.
Messages sent while a computer is offline or asleep are not saved for later.

Remote commands execute as the receiving desktop user. Allow broker access
only to trusted users/computers; use NATS authentication and network controls.
For a shared LAN broker, configure its listening address and authentication,
allow its client port (normally 4222), and use the matching URL on each HINT
computer. A URL can include NATS credentials, for example
`nats://USER:PASSWORD@HOST:4222`; percent-encode reserved characters in credentials.
A `tls://HOST:PORT` URL uses the client's normal TLS trust configuration.
Do not put real credentials into the repository.

## Configuration and files

| File | Purpose |
| --- | --- |
| `~/.config/hint/config.json` | Broker address and application settings |
| `~/.local/share/hint/signals.json` | This computer's signal definitions |
| `~/.local/share/hint/.hint.lock` | Coordination between HINT processes |
| `~/.local/share/hint/venv/` | Installed Python application |
| `~/.local/bin/hint` | Command |
| `~/.local/share/bash-completion/completions/hint` | Bash completion |
| `~/.config/systemd/user/hint.service` | Background service |

Absolute `XDG_CONFIG_HOME` and `XDG_DATA_HOME` values replace the corresponding
base directories. Relative values are ignored. The source directory can live
anywhere and can be moved after installation. Keep personal settings and signals
on each computer. On a new computer, use a clean source copy or archive and
select its broker address. A Git checkout works too when a repository is
available. Copying a `signals.json` also copies its scheduled actions.

All configuration settings are shown in `config.example.json`:

```json
{
  "nats_server": "nats://127.0.0.1:4222",
  "wake_lead_seconds": 10,
  "request_timeout_seconds": 10,
  "discovery_timeout_seconds": 1.0
}
```

Set `nats_server` to JSON `null` for local-only operation. The request deadline
bounds the whole remote operation, including connecting and closing. Discovery
uses its configured response-collection window plus a short bounded allowance
for connection and cleanup. Timeout settings accept numbers greater than zero
and at most 300 seconds. Wake lead accepts integer seconds from 0 to 86400.
Unknown settings and invalid values are rejected.

After editing configuration, restart the service:

```bash
systemctl --user restart hint.service
```

Settings and signals are private user files. The lock contains no events; leave
it alone while HINT is running. Deleting an active lock can break coordination.

## Scheduling and wakeup

For a signal two minutes from now on the local computer:

```bash
hint --set reminder --time "$(date -d '+2 minutes' '+%Y-%m-%d %H:%M:%S')" --action '~/Sounds/ding.wav'
```

Times use the receiving computer's local wall clock, without a timezone field.
Use valid, unambiguous local times around daylight-saving transitions. Signals
are one-shot; overdue entries fire when the service next runs or resumes.
A failed action start keeps its timed entry for retry; failures are logged.
Concurrent HINT processes coordinate starts using the table lock. This is
best-effort scheduling: a crash or storage failure between starting an action
and saving its removal can cause a repeated action.

Add `--wake` to a timed signal after installing wake support. HINT arms the
earliest future wake signal with a relative `rtcwake` delay, normally ten seconds
early. The computer's firmware and Linux RTC must support waking from its
chosen sleep state. HINT does not suspend the computer itself. Ordinary signals
need no RTC helper. A wake-helper failure does not stop ordinary scheduling or
remote service operation.

The user service starts at login and resumes with your session after suspend.
It does not promise graphical playback before you log in after a reboot.
GUI actions need the receiving user's desktop session. Installation imports
available display/session variables; rerun installation from the desktop if
those were unavailable during setup.

## Actions and playback

| Action | Behavior |
| --- | --- |
| Audio file | Plays once with MPV, without a video window |
| Video file | Plays once fullscreen with MPV |
| Image file | Displays fullscreen until closed |
| Directory | Loops immediate files in lexical order, fullscreen; images display for eight seconds |
| URL or ordinary document | Opens with `xdg-open` |
| Executable path | Starts that file directly |
| Command string | Runs through `/bin/sh`; supports redirects, pipes, variables, and command chaining |

Use absolute paths or quoted `~` paths. Service actions run from the user's
home directory; manually fired actions use the command's working directory.
Existing file paths and URLs keep the behavior above. Other action strings
run as noninteractive `/bin/sh` commands when fired:

```bash
hint --set task --action 'echo booyah > file' --remote hint_server_beacon
hint --fire task --remote hint_server_beacon
```

This writes `file` in the receiving user's home directory, overwriting any
existing contents. `--set` only saves the command. Wrap the whole action in
single quotes to prevent your local shell from expanding it. Inside those
quotes, double quotes protect spaces in paths while allowing variables to
expand on the receiver: `'printf "%s\n" "$HOME" > "home path.txt"'`.

Commands support `>`, `>>`, pipes, `&&`, variables, wildcards, and shell builtins.
They do not load interactive aliases or `.bashrc`; use an executable script
for complex actions, or explicitly invoke `bash -c` for Bash-specific syntax.
Shell syntax is checked before launch. `"fired": true` still means the process
started: a missing command or later failure is reported by the shell, not by
the fire response. Remote/scheduled command output goes to the service journal;
manually launched command output is discarded unless redirected to a file.

Directories are nonrecursive and include all immediate files, including hidden
and non-media files; use a media-only folder. HTTP(S) media links open through
the desktop/browser. File routing uses MIME types inferred from file names;
actual playback depends on the installed MPV.

See [action behavior and the practical test checklist](docs/actions.md) for
file types, paths, commands, expected lifetimes, and local/remote checks.

Close an image/slideshow window, or press `q` in it, to stop looping playback.
Single audio and video files exit when finished. CLI-launched actions are
detached from the terminal. Service-launched actions send diagnostics to the
service journal; finished children are reaped. Restarting/stopping the service
also stops its remaining child actions through systemd's normal control group.

## Bash completion

Press Tab after an option, signal name, or remote name. Completion supports
existing names for `--set`, `--get`, `--fire`, and `--remove`; `--set` also accepts
new names. `--remote` discovers live HINT servers, and a remotely targeted
operation completes names from that computer. Offline peers produce no
suggestions after a short deadline. Local completion needs no network.
Remote-name completion normally finishes after the one-second discovery window
plus connection and process overhead. Bash caps any completion attempt at two
seconds to keep the shell responsive; this cap does not add a wait.
Long custom discovery windows can therefore produce no completion suggestions;
use `hint --discover` when deliberately waiting longer.

For an already open Bash terminal, load the installed completion:

```bash
source "${XDG_DATA_HOME:-$HOME/.local/share}/bash-completion/completions/hint"
```

## Diagnose or uninstall

```bash
hint --list
systemctl --user status hint.service --no-pager
journalctl --user -u hint.service -n 30 --no-pager
```

If local operations work but remote ones fail, check the configured broker,
recipient name, recipient service, firewall, and VPN LAN settings. A successful
fire followed by no visible action calls for checking the receiver's journal,
action path, and desktop environment.

To uninstall from the project directory:

```bash
./uninstall.sh
```

Uninstallation preserves personal settings and signal data. It removes HINT's
wake access rule when installed; shared helper removal waits until no HINT
user still has an access rule.

## Development and tests

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
sudo apt-get install nats-server
REQUIRE_NATS_SERVER=1 .venv/bin/python -m unittest discover -s tests -v
```

Tests use isolated user directories, harmless commands, a fake player, and an
isolated loopback NATS broker. Installer tests stub privileged operations.
They do not change real services or RTC alarms. Without `nats-server`, live
network tests skip unless `REQUIRE_NATS_SERVER=1` is set. Real display/audio,
suspend, and firmware wake support still need a test on the target computer.

HINT depends on [Agent Messenger](https://github.com/jim-lewis-dev/agent_messenger),
pinned to tested commit `ec34a8cb25623a6b7675830d739fa82c4dcbf157`. Messenger
installs its pinned NATS transport dependency, which installs `nats-py`. HINT
contains no copies of either library. Installation requires Git and access to
GitHub. Dependency upgrades are explicit changes followed by HINT's
compatibility tests.

MIT licensed; see [LICENSE](LICENSE).
