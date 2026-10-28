"""Playback lifecycle checks using harmless actions and a fake MPV executable."""

import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import hint_playback


def eventually(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def finish_process(process):
    """Bounded cleanup, including when a lifecycle assertion fails."""
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


class PlaybackTests(unittest.TestCase):
    def setUp(self):
        mode = patch.object(hint_playback, "SERVICE_MODE", False)
        mode.start()
        self.addCleanup(mode.stop)

    def test_fire_returns_json_while_action_keeps_running_without_terminal_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = os.environ.copy()
            environment.update({
                "HOME": str(root / "home"),
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_DATA_HOME": str(root / "data"),
                "XDG_STATE_HOME": str(root / "state"),
                "PYTHONPATH": str(Path(hint_playback.__file__).parent)
                + os.pathsep + environment.get("PYTHONPATH", ""),
            })
            (root / "home").mkdir()
            release = root / "release"
            ready = root / "ready.json"
            finished = root / "finished"
            heartbeat = root / "heartbeat"
            action = root / "safe_action.py"
            action.write_text(
                f"#!{sys.executable}\n"
                "import json, pathlib, sys, time\n"
                f"root = pathlib.Path({str(root)!r})\n"
                "print('action stdout must not enter the CLI response', flush=True)\n"
                "print('action stderr must not enter the CLI response', file=sys.stderr, flush=True)\n"
                "received = sys.stdin.read()\n"
                "(root / 'ready.tmp').write_text(json.dumps({'stdin': received}))\n"
                "(root / 'ready.tmp').replace(root / 'ready.json')\n"
                "deadline = time.monotonic() + 8\n"
                "while not (root / 'release').exists() and time.monotonic() < deadline:\n"
                "    with (root / 'heartbeat').open('a') as stream:\n"
                "        stream.write('.')\n"
                "    time.sleep(0.02)\n"
                "(root / 'finished').write_text('finished')\n"
            )
            action.chmod(0o755)
            table = root / "data" / "hint" / "signals.json"
            table.parent.mkdir(parents=True)
            table.write_text(
                json.dumps({"probe": {"action": str(action)}})
            )

            # If stdout is accidentally inherited, subprocess.run can wait for
            # the action after killing a timed-out CLI. Release it regardless.
            watchdog = threading.Timer(5, release.touch)
            watchdog.daemon = True
            watchdog.start()
            try:
                result = subprocess.run(
                    [sys.executable, "-m", "hint", "--fire", "probe"],
                    input="input belonging to the calling terminal\n",
                    capture_output=True,
                    text=True,
                    timeout=3,
                    env=environment,
                    cwd=root,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), {"signal": "probe", "fired": True})
                self.assertEqual(result.stderr, "")
                self.assertTrue(eventually(ready.exists), "action did not start")
                self.assertEqual(json.loads(ready.read_text()), {"stdin": ""})
                self.assertFalse(finished.exists(), "CLI waited for action completion")
                self.assertTrue(eventually(heartbeat.exists))
                before = heartbeat.stat().st_size
                self.assertTrue(
                    eventually(lambda: heartbeat.stat().st_size > before),
                    "action stopped when the CLI exited",
                )
            finally:
                release.touch()
                watchdog.cancel()
                watchdog.join(timeout=1)
                if ready.exists():
                    self.assertTrue(eventually(finished.exists), "test action did not finish")

    def test_finished_action_is_reaped_without_another_launch(self):
        created = []
        real_popen = subprocess.Popen

        def record_process(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            if kwargs.get("start_new_session"):
                created.append(process)
                self.addCleanup(finish_process, process)
            return process

        with patch.object(hint_playback.subprocess, "Popen", record_process):
            hint_playback.launch(shlex.join([sys.executable, "-c", "raise SystemExit(0)"]))
            self.assertEqual(len(created), 1)
            process = created[0]
            # Reading returncode does not poll or reap. Only HINT's own waiter
            # can collect this exit status before the cleanup registered above.
            self.assertTrue(
                eventually(lambda: process.returncode is not None),
                "HINT left the exited action uncollected",
            )
            self.assertEqual(process.returncode, 0)

    def test_failed_action_exit_is_reported(self):
        created = []
        real_popen = subprocess.Popen

        def record_process(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            if kwargs.get("start_new_session"):
                created.append(process)
                self.addCleanup(finish_process, process)
            return process

        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), patch.object(hint_playback.subprocess, "Popen", record_process):
            hint_playback.launch(shlex.join([sys.executable, "-c", "raise SystemExit(17)"]))
            self.assertTrue(eventually(lambda: "17" in errors.getvalue()))
            self.assertEqual(created[0].returncode, 17)

    def test_service_keeps_player_diagnostics_without_reading_terminal_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            home.mkdir()
            details = root / "player.json"
            player = root / "mpv"
            player.write_text(
                f"#!{sys.executable}\n"
                "import json, os, pathlib, sys\n"
                "pathlib.Path(os.environ['HINT_TEST_MPV_ARGS']).write_text(json.dumps({\n"
                "    'argv': sys.argv[1:], 'stdin': sys.stdin.read(), 'cwd': os.getcwd(),\n"
                "}))\n"
                "if '--terminal=no' not in sys.argv:\n"
                "    print('service action output', flush=True)\n"
                "    print('MPV diagnostic: failed to open audio device', file=sys.stderr, flush=True)\n"
                "raise SystemExit(23)\n"
            )
            player.chmod(0o755)
            audio = root / "song.mp3"
            audio.touch()
            environment = os.environ.copy()
            environment.update({
                "HOME": str(home),
                "PATH": str(root) + os.pathsep + environment.get("PATH", ""),
                "PYTHONPATH": str(Path(hint_playback.__file__).parent)
                + os.pathsep + environment.get("PYTHONPATH", ""),
                "HINT_TEST_MPV_ARGS": str(details),
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_DATA_HOME": str(root / "data"),
                "XDG_STATE_HOME": str(root / "state"),
            })
            script = (
                "import sys, threading, hint_playback\n"
                "hint_playback.SERVICE_MODE = True\n"
                "hint_playback.launch(sys.argv[1])\n"
                "for thread in threading.enumerate():\n"
                "    if thread.name == 'hint-action-wait':\n"
                "        thread.join(timeout=3)\n"
                "        if thread.is_alive():\n"
                "            raise SystemExit('action waiter did not finish')\n"
            )
            result = subprocess.run(
                [sys.executable, "-c", script, str(audio)],
                input="input that belongs to the service caller\n",
                capture_output=True, text=True, timeout=5, env=environment, cwd=root,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertIn("service action output", result.stderr)
            self.assertIn("failed to open audio device", result.stderr)
            self.assertIn("status 23", result.stderr)
            invocation = json.loads(details.read_text())
            self.assertEqual(invocation["stdin"], "")
            self.assertEqual(invocation["cwd"], str(home))
            self.assertIn("--terminal=yes", invocation["argv"])
            self.assertIn("--input-terminal=no", invocation["argv"])

    def test_mpv_modes_do_not_take_over_terminal_and_keep_intended_loops(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "mpv"
            executable.write_text(
                f"#!{sys.executable}\n"
                "import json, os, pathlib, sys\n"
                "target = pathlib.Path(os.environ['HINT_TEST_MPV_ARGS'])\n"
                "temporary = target.with_suffix('.tmp')\n"
                "temporary.write_text(json.dumps(sys.argv[1:]))\n"
                "temporary.replace(target)\n"
            )
            executable.chmod(0o755)
            audio, video, image, slides = (
                root / "song.mp3", root / "movie.mp4", root / "photo.png", root / "slides"
            )
            for path in (audio, video, image):
                path.touch()
            slides.mkdir()
            for filename in ("b.png", "a.png"):
                (slides / filename).touch()

            for kind, target in (("audio", audio), ("video", video), ("image", image), ("directory", slides)):
                with self.subTest(kind=kind):
                    arguments = root / f"{kind}.json"
                    environment = {
                        "PATH": str(root) + os.pathsep + os.environ.get("PATH", ""),
                        "HINT_TEST_MPV_ARGS": str(arguments),
                    }
                    with patch.dict(os.environ, environment):
                        hint_playback.launch(str(target))
                        self.assertTrue(eventually(arguments.exists), "fake MPV did not start")
                    argv = json.loads(arguments.read_text())
                    for option in ("--terminal=no", "--input-terminal=no", "--idle=no", "--keep-open=no"):
                        self.assertIn(option, argv)
                    # Last assignment wins in MPV; common defaults must not
                    # accidentally override the intentional image/slideshow loop.
                    options = {
                        item.split("=", 1)[0]: item.split("=", 1)[1]
                        for item in argv if item.startswith("--") and "=" in item
                    }
                    if kind in ("audio", "video"):
                        self.assertEqual(options.get("--loop-file"), "no")
                        self.assertEqual(options.get("--loop-playlist"), "no")
                    if kind == "audio":
                        self.assertIn("--no-video", argv)
                    elif kind == "video":
                        self.assertIn("--fullscreen", argv)
                    elif kind == "image":
                        self.assertEqual(options.get("--loop-file"), "inf")
                    else:
                        self.assertEqual(options.get("--loop-playlist"), "inf")
                        self.assertEqual(options.get("--image-display-duration"), "8")
                        self.assertLess(argv.index(str(slides / "a.png")), argv.index(str(slides / "b.png")))


class ShellPlaybackTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home with spaces"
        self.cwd = self.root / "working directory"
        self.home.mkdir()
        self.cwd.mkdir()

    def run_action(self, command, service=False):
        environment = os.environ.copy()
        environment.update({
            "HOME": str(self.home),
            "PYTHONPATH": str(Path(hint_playback.__file__).parent),
        })
        script = (
            "import sys, threading, hint_playback\n"
            "hint_playback.SERVICE_MODE = sys.argv[2] == 'service'\n"
            "hint_playback.launch(sys.argv[1])\n"
            "for thread in threading.enumerate():\n"
            "    if thread.name == 'hint-action-wait':\n"
            "        thread.join(timeout=3)\n"
            "        if thread.is_alive():\n"
            "            raise SystemExit('action waiter did not finish')\n"
        )
        return subprocess.run(
            [sys.executable, "-c", script, command, "service" if service else "local"],
            capture_output=True, text=True, timeout=5, env=environment, cwd=self.cwd,
        )

    def test_local_redirection_uses_callers_directory_and_fire_is_quiet(self):
        result = self.run_action("echo booyah > file")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual((self.cwd / "file").read_text(), "booyah\n")
        self.assertFalse((self.home / "file").exists())

    def test_shell_builtins_globs_variables_and_short_circuit(self):
        (self.home / "one.txt").touch()
        (self.home / "two.txt").touch()
        result = self.run_action(
            'cd "$HOME" && word=booyah && '
            'printf "%s\\n" "$word" *.txt > result && '
            'false && touch unexpected || printf "%s\\n" recovered >> result',
            service=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.home / "result").read_text(), "booyah\none.txt\ntwo.txt\nrecovered\n")
        self.assertFalse((self.home / "unexpected").exists())

    def test_long_shell_command_is_not_rejected_as_a_long_filename(self):
        value = "x" * 1000
        result = self.run_action(f"printf %s {value} > result")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.cwd / "result").read_text(), value)

    def test_invalid_syntax_does_not_execute_an_earlier_command(self):
        result = self.run_action('touch unexpected; echo "unterminated')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid shell syntax", result.stderr)
        self.assertFalse((self.cwd / "unexpected").exists())

    def test_missing_command_is_an_async_failure_in_service_diagnostics(self):
        result = self.run_action("hint_nonexistent_command_9f74a2", service=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("hint_nonexistent_command_9f74a2", result.stderr)
        self.assertIn("status 127", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_failed_shell_syntax_check_keeps_scheduled_signal_without_side_effects(self):
        import hint_signals as signals

        table = self.root / "signals.json"
        marker = self.root / "unexpected"
        when = "2030-01-01 06:59:00"
        with patch.object(signals, "TABLE", table), \
                patch.object(signals, "LOCK", self.root / ".hint.lock"):
            signals.write_table({"bad_shell": {
                "action": f'touch "{marker}"; echo "unterminated', "time": when,
            }})
            result = signals.fire("bad_shell", when)
            self.assertFalse(result["fired"])
            self.assertIn("invalid shell syntax", result["error"])
            self.assertIn("bad_shell", signals.list_signals())
            self.assertFalse(marker.exists())

    def test_existing_media_paths_and_urls_are_not_interpreted_as_shell(self):
        media = self.home / "song; echo $HOME > unexpected.mp3"
        media.touch()
        url = "https://example.com/?q=$HOME&v=two"
        with patch.object(hint_playback, "_spawn") as spawn:
            hint_playback.launch(str(media))
            self.assertEqual(spawn.call_args.args[0][-1], str(media))
            self.assertEqual(spawn.call_args.args[0][0], "mpv")
            hint_playback.launch(url)
            spawn.assert_called_with(["xdg-open", url])
        self.assertFalse((self.home / "unexpected.mp3").exists())


if __name__ == "__main__":
    unittest.main()
