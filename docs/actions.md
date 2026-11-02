# Actions and practical checks

[Back to the README](../README.md)

An action is one string, interpreted on the receiving computer when it fires.
`--set` saves it; `--fire` launches it. All managed launches belong to the local
HINT service and start in that user's home directory, including fires issued
from a local terminal.

## Routing and lifetime

The first matching row wins:

| Value | Behavior | Lifetime and control |
| --- | --- | --- |
| `popup:` prefix, any capitalization | Display the remaining literal text in HINT's own window; request fullscreen | Stays until dismissed or stopped; no meaningful pause |
| HTTP or HTTPS URL | Start an isolated browser instance; request fullscreen | HINT can stop its own instance; no webpage pause/resume |
| Existing directory | Submit sorted immediate regular files to fullscreen MPV | Playlist loops; images last eight seconds; pause/resume/stop |
| Existing audio file | MPV with no video window | Plays once; pause/resume/stop |
| Existing video file | Fullscreen MPV | Plays once; pause/resume/stop |
| Existing image file | Fullscreen MPV | Stays until closed/stopped |
| Other existing executable file | Execute the path directly | Program controls completion; owned processes can be suspended/resumed/stopped |
| Other existing file | Use an isolated supported viewer or its desktop association | Fullscreen requested where supported; generic handoffs may be untracked |
| Remaining command text | Noninteractive `/bin/sh -c` | Shell controls completion; owned processes can be suspended/resumed/stopped |

There is no `--type` switch. An empty popup is an error. Popup windows use native GTK and close with Escape, Enter, or their Dismiss
button. Popup content is literal:
`popup:echo hello > file` displays those words and creates no file. In a shell
command, the same redirect executes normally. Quotes surrounding the whole
argument are interpreted by your invoking shell, not used as a type marker.

Media classification uses Python's filename MIME tables; decoder support comes
from the installed MPV. MP3/WAV/FLAC, MP4/MKV/WebM, and JPEG/PNG are typical
examples, not a fixed whitelist. HTTP(S) media URLs open in the browser, including
URLs ending in `.mp3` or `.mp4`. `file://` and playlist formats have no dedicated
routing contract; use a local path for direct media playback.

Directories are **nonrecursive** and include hidden and unsupported files.
MPV decides which entries it can play. Sorting is lexical: `10` precedes `2`;
use names such as `01`, `02`, `03` for deliberate ordering. Empty folders and
folders containing only subdirectories are rejected. HINT does not filter a
mixed directory into a slideshow or reorder it by media type.

URLs and PDFs use a separate Firefox/Firefox ESR or Chromium-family browser
profile. Those sessions do not borrow your personal browser's cookies or logins.
Readable text files use HINT's own read-only window. Supported office formats
(ODT/ODS/ODP, DOC/DOCX, XLS/XLSX, PPT/PPTX, RTF, and CSV) use LibreOffice with
an isolated profile in read-only mode when available. Fullscreen support for
these external applications varies by application and desktop; HINT does not
report whether the request was honored.

Executable permission is checked with `os.access(path, os.X_OK)` after media
routing. Direct scripts also need a valid interpreter line, such as
`#!/usr/bin/env python3`. An extension alone does not make a script executable.
An explicit `python3 /path/task.py` command requires executable Python, not an
executable script file.

## Shell quoting and paths

Use a whole action as one argument. For receiver-side home expansion:

```bash
hint --set music --action '~/Music/a song.mp3' --remote beacon
hint --fire music --remote beacon
```

Replace `beacon` with the actual receiver. Its files must exist there; HINT does
not upload the sender's files. Do not add an extra pair of quotes inside a bare
media-path string.

Single quotes around a command defer expansion until it runs. Double quotes
inside them protect spaces while permitting receiver-side variable expansion:

```bash
hint --set task --remote beacon \
  --action 'printf "%s\n" "$HOME" > "hint-home-path.txt"'
hint --fire task --remote beacon
```

That writes `hint-home-path.txt` in the receiver's home directory. `>` replaces
contents; `>>` appends. Other POSIX shell features include pipes, `&&`, `||`,
variables, wildcards, and command substitution. Existing paths remain literal
even when their filenames contain shell characters.

HINT uses `/bin/sh`, not an interactive terminal. It does not load `.bashrc`
or aliases. Its PATH comes from the service environment. Use absolute executable
paths when needed, a script for complicated work, or explicitly invoke Bash for
Bash-specific syntax. Commands receive empty stdin. For another working
directory, include `cd /path && ...` in the command.

## Runs, definitions, and schedules

A saved name is a definition. Every fire creates a run, and repeated fires are
allowed. Controls by a saved name affect all active runs with that name. Edits
to the definition affect future launches, not the already running copies.
Removing the definition does not stop its active runs.

A nameless fire gets a name such as `HINT-001`. The response, status, and
completion expose it so you can use the same control commands. It creates no
saved definition. Temporary numbering resets with the service; do not use old
remembered names after restarting it.

`--pause` holds MPV playback and slideshow advancement. Suspending a command
freezes its owned processes; external servers and real-world timeouts keep
running. Resuming continues those processes. Stop terminates the owned run;
it cannot undo files already written or requests already sent. Commands that
deliberately hand work to other services are outside that process ownership.

Static popups have nothing to advance. MPV images accept pause: animations freeze
and static images remain displayed. Web pages and document
viewers do not have a general pause operation. HINT does not freeze a shared
browser or kill unrelated applications to simulate control. If the associated
application cannot be isolated/tracked, status says so and controls report the
limitation. Fullscreen is requested where available without success warnings.

A fire acknowledgement means the process launched. A shell command that does
not exist can launch a shell and then fail with exit code 127. Use status or
the journal for the later outcome:

```bash
hint --status task --remote beacon
```

On the receiving computer:

```bash
journalctl --user -u hint.service -n 30 --no-pager
```

All managed action output goes to the service's journal unless the action
redirects it. It never borrows terminal input or waits for Enter to restore a
prompt. Status history is bounded and in memory; service restart clears it and
ends controlled runs. An untracked external application may remain open.

Manual fire preserves a schedule. A successful scheduled launch clears its
time and retains its definition and wake preference. A launch failure keeps
the schedule for retry. A save failure after launch is tracked in memory to
prevent firing again on each scheduler tick; a crash at that boundary still
prevents an exactly-once guarantee. A timeout on a remote mutation likewise
does not prove it failed: check before issuing it again.

## Test the installed computer

Start with named actions so controls are easy to repeat:

```bash
hint --set probe --action 'popup:HINT display test'
hint --fire probe
hint --status probe
hint --stop probe
```

Then use actual files on that computer:

| Check | Action | Expected result |
| --- | --- | --- |
| Popup | `popup:HINT display test` | Readable own window; fullscreen requested; dismissal and stop work |
| Case-insensitive popup | `POPUP:Hello` | Same popup behavior; message capitalization preserved |
| Literal popup | `popup:echo hello > file` | Literal message; no file created |
| Audio | Short MP3/WAV path | Plays once; pause/resume work; player exits after completion |
| Video | Short MP4 path | Own playback window; fullscreen requested; pause/resume/stop work |
| Image | JPEG/PNG path | Remains until closed or stopped |
| Mixed directory | Images, audio, video in a named order | Filename order; eight-second images; playlist repeats; pause holds position |
| Browser | `https://example.com` | Separate browser session; stopping it leaves your existing browser intact |
| PDF/text/office file | A small existing document | Separate viewer where supported; otherwise honest untracked status |
| Repeated fire | Fire one named popup twice | Two windows; stop by name closes both |
| Nameless fire | `hint --fire --action 'popup:Temporary test'` | Generated HINT name appears in status/completion and can be stopped |
| Invalid shell syntax | Action `echo "unfinished` | Launch rejected |
| Later command failure | Action `exit 7` | Launch acknowledged, then failed exit code visible in status |

For a harmless command and an explicit working-directory check:

```bash
hint --set probe_command --action 'pwd > hint-probe-cwd.txt'
hint --fire probe_command
hint --status probe_command
cat "$HOME/hint-probe-cwd.txt"
```

The result appears asynchronously. It should contain the receiving user's home
directory. Remove the marker afterward with `rm "$HOME/hint-probe-cwd.txt"`.

For scheduling, use the known-working popup:

```bash
hint --set probe --time "$(date -d '+1 minute' '+%Y-%m-%d %H:%M:%S')"
hint --get probe
```

After it fires, `hint --get probe` should still show the action with no schedule.
Set a new time and confirm it fires again. Test `--no-time` before a later
schedule is due. Test wake separately only after enabling wake support.

Run a pause/stop check on a harmless long command:

```bash
hint --set probe_sleep --action 'sleep 120'
hint --fire probe_sleep
hint --pause probe_sleep
hint --status probe_sleep
hint --resume probe_sleep
hint --stop probe_sleep
```

Clean up with `hint --stop NAME` and `hint --remove NAME` for your test names.
If a test fails, record the command, action type, status, journal detail, and
whether it was local, remote, or scheduled.

## Test two computers

1. Configure the same reachable NATS broker and run `hint --discover`.
2. Use an actual returned hostname to remotely set, get, and list `probe`.
3. Fire the popup remotely; only the receiving computer should display it.
4. Check remote status, fire it again, and stop both by the saved name.
5. Fire a nameless popup and stop it using its generated name on that receiver.
6. Add and remove definitions remotely, verifying the next Tab sees the change.
7. Check completion with `--remote` before and after the action name. An
   unfinished or unreachable remote must not produce local action suggestions.
8. Repeat in the opposite direction, then test a remotely scheduled popup.
9. Reboot/log in and verify service startup. Test suspend/wake separately.

HINT's network transport does not require the same display protocol on both
computers. X11, Wayland, and Raspberry Pi
desktops still need their own real display/audio and hardware wake checks.
