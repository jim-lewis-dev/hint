# Actions and practical checks

[Back to the README](../README.md)

A signal stores one `action` string. `--set` checks that it is nonempty; files,
executables, and desktop associations are checked when the signal fires, on the
computer receiving the operation.

## How an action is chosen

The first matching row wins:

| Action value | Behavior | Expected lifetime |
| --- | --- | --- |
| HTTP or HTTPS URL | Open with `xdg-open`, normally in a browser | The browser controls its window size and lifetime; HINT does not request fullscreen. This includes URLs ending in `.mp3` or `.mp4`. |
| Existing directory | Send its sorted immediate files to fullscreen MPV | Repeat the playlist until closed; images last eight seconds each. |
| Existing audio file | Play with MPV and no video window | Play once and exit. |
| Existing video file | Play fullscreen with MPV | Play once and exit. |
| Existing image file | Show fullscreen with MPV | Remain until closed. |
| Other existing executable file | Run the exact path | The program controls its lifetime. |
| Other existing file | Open with `xdg-open` | The associated desktop application controls its lifetime. |
| Command string | Run with noninteractive `/bin/sh -c` | The command controls its lifetime. |

Python's MIME tables classify file names. Common examples are MP3/WAV/FLAC for
audio, MP4/MKV/WebM for video, and JPEG/PNG/GIF for images. This is not a fixed
extension whitelist: mappings vary with the operating system and Python, and
actual playback depends on the installed MPV's decoder support. Unknown files
need a usable desktop association.

For a non-media file, HINT checks the user's execute permission with
`os.access(path, os.X_OK)`. A script launched directly also needs a valid
interpreter line, such as `#!/usr/bin/env python3`; its filename extension
alone does not make it executable. An explicit command such as
`python3 /absolute/path/task.py` needs execute permission on Python, not the
script.

A directory is **not recursive**. Its playlist includes all immediate files,
including hidden and non-media files. Sorting is lexical, so `10` comes before
`2`; names such as `01`, `02`, `03` give an explicit order. Use a media-only
folder. An empty folder, including one containing only subdirectories, fails
when fired.

`file://` URLs and playlist formats such as M3U/M3U8 have no dedicated action
contract. Use a local path for direct media playback. Other URL schemes are
not handled specially.

## Paths and commands

Pass a whole action as one shell argument:

```bash
hint --set example_audio --action "$HOME/Music/a song.mp3"
```

For remote use, quote a tilde path so it is expanded by the receiver:

```bash
hint --remote hint_server_beacon --set example_audio --action '~/Music/a song.mp3'
```

Use a real receiver name returned by `hint --discover`. The files and commands
must exist on that receiver. Do not put an extra pair of quotes inside a bare
media-path string.

After URL and existing-path handling, HINT runs the text with `/bin/sh -c`.
This supports redirects (`>`, `>>`, `2>`), pipes, `&&`, `||`, variables,
wildcards, command substitution, and shell builtins. Existing paths remain
literal, even when their names contain shell characters. To use a variable in
a media path, explicitly call the player, for example
`'mpv --fullscreen "$HOME/Videos/movie.mp4"'`.

Single quotes around the whole action preserve its contents until firing.
Double quotes *inside* them protect spaces while allowing the receiving shell
to expand variables:

```bash
hint --remote hint_server_beacon --set example_command \
  --action 'printf "%s\n" "$HOME" | tr a-z A-Z > "home path.txt" && echo done >> "home path.txt"'
hint --remote hint_server_beacon --fire example_command
```

This creates `home path.txt` on the receiver. `>` overwrites a file; `>>`
appends. `--set` only stores the text. It does not execute it or expand variables.
Do not add another pair of quotes around the *entire* command inside the outer
quotes: that would turn it into one executable name. Single quotes can also
be escaped using your invoking shell's normal quoting rules; for complex
commands, an executable script is easier to read and maintain.

The command shell is POSIX `/bin/sh`, not an interactive terminal. It does not
load `.bashrc` or your aliases. Its environment and PATH come from the launching
HINT process, which may differ from your terminal. Use absolute executable paths
when needed; explicitly invoke `bash -c` for Bash-specific syntax. Commands get
empty stdin, so programs that require keyboard interaction are unsuitable.

Manual local firing uses the command's working directory. Scheduled and remote
actions start in the receiving user's home directory. Absolute paths avoid
that difference.

## What a successful fire means

`"fired": true` means a process started. It does not confirm playback, rendering,
or successful program completion. Shell commands are syntax-checked before
launch, but command lookup and execution happen asynchronously. A nonexistent
command can therefore return `"fired": true` and then exit with status 127.
CLI-launched actions have empty stdin and discarded output so they do not take over the shell prompt. Scheduled and remote
actions write diagnostics to the service journal:

```bash
journalctl --user -u hint.service -n 30 --no-pager
```

Every fire can start another process. HINT currently has no stop, pause, resume,
or per-action status command. Close the media window or press `q` in it.
Removing a signal definition does not stop an already running action.

Manual firing preserves its definition and scheduled time. Scheduled signals
are removed after their action process starts. Immediate launch failure keeps
the timed signal for retry; a failed shell syntax check also keeps it for retry.
The check cannot detect every runtime error. Once a shell starts, a missing
command or later nonzero exit does not restore the signal.

Stopping or restarting the user service also stops its remaining child actions.
It does not stop actions launched by a separate manual CLI command. Graphical
and audio actions need a working desktop/audio session on the receiving computer.

## Test one computer first

Use unused signal names and real, existing files. For each file or directory
below, use the same pattern, replacing the path:

```bash
hint --set probe_audio --action '/absolute/path/to/a-short-song.mp3'
hint --fire probe_audio
hint --get probe_audio
```

| Check | Supply | Pass condition |
| --- | --- | --- |
| Audio | Short MP3 or WAV | Plays once, no video window, immediate shell prompt, player exits when finished. |
| Video | Short MP4 | Fullscreen, plays once, exits. |
| Image | JPEG or PNG | Fullscreen, stays visible, `q` or closing the window stops it. |
| Slideshow | Folder of two or three images named in order | Each image stays about eight seconds, playlist wraps, `q` closes it. |
| URL | `https://example.com` | Browser opens; prompt is immediately usable. |
| Document | Existing PDF or text file | Expected desktop application opens. |
| Executable | Harmless executable script | Its expected result appears once. |
| Command arguments | Harmless command with arguments | Arguments reach the program correctly. |
| Shell syntax failure | `'echo "unfinished'` | Fire returns an error without starting the command. |
| Command failure | Nonexistent command, fired remotely | Fire acknowledges shell launch; the service journal reports command-not-found and exit status 127. |

For an exact command-arguments check, create a temporary marker:

```bash
hint_probe_dir="$(mktemp -d /tmp/hint-probe.XXXXXX)"
hint --set probe_command --action "touch $hint_probe_dir/ran"
hint --fire probe_command
ls "$hint_probe_dir/ran"
```

The file may take a moment to appear because launch is asynchronous. After it
appears, remove the signal and temporary directory:

```bash
hint --remove probe_command
rm "$hint_probe_dir/ran"
rmdir "$hint_probe_dir"
```

For scheduling, use an action already proven to work, one minute ahead:

```bash
hint --set probe_scheduled --time "$(date -d '+1 minute' '+%Y-%m-%d %H:%M:%S')" --action https://example.com
hint --get probe_scheduled
```

Wait for the browser to open, then use `hint --list`: the scheduled signal
should have disappeared. Test hardware wake separately if wake support is
installed and wanted.

Remove your other temporary signal definitions with `hint --remove NAME`, and
close active media windows separately. If a check fails, record the command,
the file type, the result, and whether it was local, remote, or scheduled.

## Then test two computers

1. Run `hint --discover` and confirm the receiver's name.
2. Remotely set, get, and list a test signal using a receiver-local action.
3. Fire it remotely and confirm that only the receiving computer acts.
4. Remove it remotely and check that it disappears.
5. Try Tab completion for the remote name and its signal names.
6. Repeat in the other direction, then try one remote scheduled action.

Use the command forms in the [remote-use instructions](../README.md#remote-use).
After both directions work, check that the service starts after login/reboot.
