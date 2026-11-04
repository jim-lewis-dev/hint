"""Owned action lifecycle and media/display dispatch without personal applications."""
import json
import errno
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import hint_playback as playback
from hint_config import HintError


def eventually(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


class PlaybackTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home with spaces"
        self.home.mkdir()
        environment = patch.dict(os.environ, {"HOME": str(self.home), "DISPLAY": ":99"})
        environment.start()
        self.addCleanup(environment.stop)
        mode = patch.object(playback, "SERVICE_MODE", False)
        mode.start()
        self.addCleanup(mode.stop)

    def launch(self, action):
        run = playback.launch(action)
        self.addCleanup(run.stop)
        return run

    def test_command_detaches_stdin_and_runs_in_home(self):
        run = self.launch("read value || :; printf '%s' booyah > file")
        self.assertTrue(eventually(lambda: run.poll() is not None))
        self.assertEqual((self.home / "file").read_text(), "booyah")
        self.assertEqual(run.poll(), 0)
        self.assertEqual(run.kind, "command")

    def test_shell_builtins_expansion_redirection_and_long_commands(self):
        (self.home / "one.txt").touch()
        (self.home / "two.txt").touch()
        run = self.launch('word=booyah; printf "%s\\n" "$word" *.txt > result')
        self.assertTrue(eventually(lambda: run.poll() is not None))
        self.assertEqual((self.home / "result").read_text(), "booyah\none.txt\ntwo.txt\n")
        long_run = self.launch("printf %s " + "x" * 1000 + " > long")
        self.assertTrue(eventually(lambda: long_run.poll() is not None))
        self.assertEqual((self.home / "long").read_text(), "x" * 1000)

    def test_invalid_syntax_runs_nothing(self):
        with self.assertRaisesRegex(HintError, "Invalid command syntax"):
            playback.launch('touch unexpected; echo "unterminated')
        self.assertFalse((self.home / "unexpected").exists())

    def test_finished_child_is_reaped_by_poll_without_next_launch(self):
        run = self.launch("exit 17")
        self.assertTrue(eventually(lambda: run.poll() == 17))
        self.assertEqual(run.process.returncode, 17)
        with self.assertRaises(ChildProcessError):
            os.waitpid(run.pid, os.WNOHANG)

    def test_pause_resume_and_stop_paused_process_group(self):
        script = self.root / "heartbeat.py"
        script.write_text("import pathlib,time\np=pathlib.Path('heartbeat')\nwhile True:\n with p.open('a') as f: f.write('.')\n time.sleep(.02)\n")
        run = self.launch(shlex.join([sys.executable, str(script)]))
        marker = self.home / "heartbeat"
        self.assertTrue(eventually(marker.exists))
        run.pause()
        time.sleep(0.05)
        paused_size = marker.stat().st_size
        time.sleep(0.12)
        self.assertEqual(marker.stat().st_size, paused_size)
        run.resume()
        self.assertTrue(eventually(lambda: marker.stat().st_size > paused_size))
        run.pause()
        run.stop()
        self.assertTrue(eventually(lambda: run.poll() is not None))
        self.assertFalse(playback._group_alive(run.pid))

    def test_background_child_remains_owned_after_shell_exits(self):
        run = self.launch("sleep 20 &")
        self.assertTrue(eventually(lambda: run.process.poll() is not None))
        self.assertIsNone(run.poll())
        run.stop()
        self.assertTrue(eventually(lambda: run.poll() is not None))
        self.assertFalse(playback._group_alive(run.pid))

    def test_stop_does_not_touch_unrelated_process(self):
        unrelated = subprocess.Popen(["sleep", "20"], start_new_session=True)
        self.addCleanup(lambda: unrelated.poll() is None and unrelated.kill())
        self.addCleanup(lambda: None)
        run = self.launch("sleep 20")
        run.stop()
        self.assertIsNone(unrelated.poll())
        unrelated.terminate()
        unrelated.wait(timeout=2)

    def test_existing_executable_requires_execution_bits(self):
        script = self.root / "script"
        script.write_text("#!/bin/sh\nprintf executed > result\n")
        script.chmod(0o755)
        run = self.launch(str(script))
        self.assertTrue(eventually(lambda: run.poll() is not None))
        self.assertEqual((self.home / "result").read_text(), "executed")
        script.chmod(0o644)
        with patch.object(playback, "_document", return_value="document") as document:
            self.assertEqual(playback.launch(str(script)), "document")
            document.assert_called_once()

    def test_empty_nul_and_empty_popup_are_rejected(self):
        for action in ("", " ", "a\0b"):
            with self.subTest(action=action), self.assertRaises(HintError):
                playback.launch(action)
        for message in ("", " ", "a\0b", 3):
            with self.subTest(popup=message), self.assertRaises(HintError):
                playback.launch(popup=message)
        with self.assertRaises(HintError):
            playback.launch("true", popup="hello")

    def test_popup_option_keeps_all_text_literal(self):
        message = ' $HOME <b>hello!</b>: "yes"\n$(touch unexpected) '
        with patch.object(playback, "_popup", return_value="popup") as popup:
            self.assertEqual(playback.launch(popup=message), "popup")
            popup.assert_called_once_with(message)
        self.assertFalse((self.home / "unexpected").exists())

    def test_old_popup_prefix_is_an_ordinary_shell_command(self):
        with patch.object(playback, "_popup", side_effect=AssertionError("popup opened")), \
                patch.object(playback, "_spawn", return_value="command") as spawn:
            self.assertEqual(playback.launch("POPUP:hello"), "command")
            spawn.assert_called_once_with([playback.SHELL, "-c", "POPUP:hello"], detail="POPUP:hello")

    def test_popup_uses_distro_python_and_its_own_module(self):
        with patch.dict(os.environ, {"DISPLAY": ":99"}), patch.object(playback, "_spawn", return_value="window") as spawn:
            self.assertEqual(playback.launch(popup="--help"), "window")
            argv = spawn.call_args.args[0]
            self.assertEqual(argv[0], "/usr/bin/python3")
            self.assertTrue(argv[1].endswith("hint_popup.py"))
            self.assertEqual(argv[2], "--text=--help")
            self.assertFalse(spawn.call_args.kwargs["pausable"])

    def test_visual_action_without_desktop_fails_before_spawn(self):
        with patch.dict(os.environ, {"DISPLAY": "", "WAYLAND_DISPLAY": ""}), patch.object(playback, "_spawn") as spawn:
            with self.assertRaisesRegex(HintError, "no usable desktop"):
                playback.launch(popup="hello")
            with self.assertRaisesRegex(HintError, "no usable desktop"):
                playback.launch("https://example.com")
            spawn.assert_not_called()

    def test_visual_media_requires_desktop_but_audio_does_not(self):
        video = self.root / "movie.mp4"
        image = self.root / "image.png"
        audio = self.root / "song.mp3"
        directory = self.root / "slides"
        for path in (video, image, audio):
            path.touch()
        directory.mkdir()
        (directory / "a.png").touch()
        with patch.dict(os.environ, {"DISPLAY": "", "WAYLAND_DISPLAY": ""}), \
                patch.object(playback, "_spawn", return_value="player") as spawn:
            for path in (video, image, directory):
                with self.subTest(path=path), self.assertRaisesRegex(HintError, "no usable desktop"):
                    playback.launch(str(path))
            spawn.assert_not_called()
            self.assertEqual(playback.launch(str(audio)), "player")
            __import__("shutil").rmtree(spawn.call_args.kwargs["temporary"])

    def test_directory_all_regular_files_filename_order_and_existing_modes(self):
        directory = self.root / "mixed"
        directory.mkdir()
        names = ["z.jpg", "b.mp3", "a.mp4", ".hidden.png", "nonsense.bin"]
        for name in names:
            (directory / name).touch()
        (directory / "subdirectory").mkdir()
        with patch.object(playback, "_spawn", return_value="player") as spawn:
            playback.launch(str(directory))
            argv = spawn.call_args.args[0]
            self.assertEqual(argv[-len(names):], [str(directory / name) for name in sorted(names)])
            self.assertIn("--loop-playlist=inf", argv)
            self.assertIn("--image-display-duration=8", argv)
            self.assertIn("--input-terminal=no", argv)
            self.assertIn("--keep-open=no", argv)
            self.assertIn("--idle=no", argv)
            self.assertTrue(any(value.startswith("--input-ipc-server=") for value in argv))
            # Mocked spawning did not consume ownership of its temporary path.
            import shutil
            shutil.rmtree(spawn.call_args.kwargs["temporary"])

    def test_single_media_types_keep_loop_and_pause_behavior(self):
        for suffix, kind, expected, pausable in (
            (".mp3", "audio", "--loop-file=no", True),
            (".mp4", "video", "--loop-file=no", True),
            (".png", "image", "--loop-file=inf", True),
            (".gif", "image", "--loop-file=inf", True)
        ):
            path = self.root / ("item" + suffix)
            path.touch()
            with self.subTest(kind=kind), patch.object(playback, "_play", return_value="player") as play:
                playback.launch(str(path))
                self.assertIn(expected, play.call_args.args)
                self.assertEqual(play.call_args.kwargs["kind"], kind)
                self.assertEqual(play.call_args.kwargs.get("pausable", True), pausable)

    def test_media_keeps_symlink_path_without_resolving_it(self):
        target = self.root / "mounted share" / "song.mp3"
        target.parent.mkdir()
        target.touch()
        alias = self.root / "song.mp3"
        alias.symlink_to(target)
        with patch.object(Path, "resolve", side_effect=OSError(errno.EINVAL, "Invalid argument")):
            inspected = playback._inspect_path(alias)
        self.assertEqual(inspected["mime"], "audio/mpeg")
        with patch.object(playback, "_play", return_value="player") as play:
            self.assertEqual(playback.launch(str(alias)), "player")
        self.assertEqual(play.call_args.args[-1], str(alias))

    def test_extensionless_symlink_inherits_target_media_type(self):
        target = self.root / "song.mp3"
        target.touch()
        alias = self.root / "song"
        alias.symlink_to(target)
        with patch.object(playback, "_play", return_value="player") as play:
            self.assertEqual(playback.launch(str(alias)), "player")
        self.assertEqual(play.call_args.kwargs["kind"], "audio")
        self.assertEqual(play.call_args.args[-1], str(alias))

    def test_parent_traversal_through_symlink_preserves_filesystem_meaning(self):
        mounted = self.root / "mount" / "folder"
        mounted.mkdir(parents=True)
        (mounted.parent / "song.mp3").touch()
        alias = self.root / "alias"
        alias.symlink_to(mounted, target_is_directory=True)
        path = alias / ".." / "song.mp3"
        with patch.object(playback, "_play", return_value="player") as play:
            self.assertEqual(playback.launch(str(path)), "player")
        self.assertEqual(play.call_args.args[-1], str(path))

    def test_command_ending_in_media_extension_still_runs_as_command(self):
        run = self.launch("printf booyah > result.mp3")
        self.assertTrue(eventually(lambda: run.poll() is not None))
        self.assertEqual((self.home / "result.mp3").read_text(), "booyah")

    def test_stalled_share_probe_is_killed_and_never_launches_later(self):
        worker = self.root / "blocked_probe.py"
        marker, late = self.root / "probe-pid", self.root / "too-late"
        worker.write_text(
            "import os,pathlib,time\n"
            f"pathlib.Path({str(marker)!r}).write_text(str(os.getpid()))\n"
            "time.sleep(20)\n"
            f"pathlib.Path({str(late)!r}).touch()\n"
        )
        started = time.monotonic()
        with patch.object(playback, "__file__", str(worker)), \
                patch.object(playback, "PATH_PROBE_SECONDS", 0.3), \
                patch.object(playback, "_spawn") as spawn:
            with self.assertRaisesRegex(HintError, "network share; nothing was launched"):
                playback.launch(str(self.root / "mounted-song.mp3"))
            spawn.assert_not_called()
        self.assertLess(time.monotonic() - started, 2)
        self.assertTrue(marker.exists(), "the isolated probe must actually have started")
        pid = int(marker.read_text())

        def reaped():
            try:
                os.kill(pid, 0)
                return False
            except ProcessLookupError:
                return True

        self.assertTrue(eventually(reaped))
        self.assertFalse(late.exists())
        run = self.launch("printf recovered > recovered")
        self.assertTrue(eventually(lambda: run.poll() is not None))
        self.assertEqual((self.home / "recovered").read_text(), "recovered")

    def test_path_error_is_reported_without_poisoning_the_next_launch(self):
        worker = self.root / "failed_probe.py"
        worker.write_text(
            "import json\n"
            "print(json.dumps({'kind':'error','message':'[Errno 22] Invalid argument'}))\n"
        )
        with patch.object(playback, "__file__", str(worker)), patch.object(playback, "_spawn") as spawn:
            with self.assertRaisesRegex(HintError, r"Cannot inspect .*Errno 22"):
                playback.launch(str(self.root / "song.mp3"))
            spawn.assert_not_called()
        path = self.root / "song.mp3"
        path.touch()
        with patch.object(playback, "_play", return_value="player"):
            self.assertEqual(playback.launch(str(path)), "player")

    def test_repeated_file_launches_have_independent_player_controls(self):
        path = self.root / "song.mp3"
        path.touch()
        with patch.object(playback, "_spawn", return_value="player") as spawn:
            playback.launch(str(path))
            playback.launch(str(path))
        self.assertEqual(spawn.call_count, 2)
        first, second = spawn.call_args_list
        for call in (first, second):
            self.addCleanup(playback.shutil.rmtree, call.kwargs["temporary"], True)
            self.assertEqual(call.args[0][-1], str(path))
        self.assertNotEqual(first.kwargs["ipc"], second.kwargs["ipc"])

    def test_launch_uses_its_session_snapshot(self):
        environment = dict(os.environ, DISPLAY=":22")
        run = playback.launch('printf "%s" "$DISPLAY" > display', environment=environment)
        self.addCleanup(run.stop)
        self.assertTrue(eventually(lambda: run.poll() is not None))
        self.assertEqual((self.home / "display").read_text(), ":22")
        environment["DISPLAY"] = ""
        environment["WAYLAND_DISPLAY"] = ""
        with self.assertRaisesRegex(HintError, "no usable desktop"):
            playback.launch(popup="hello", environment=environment)

    def test_cancelled_launch_starts_no_child_and_cleans_player_resources(self):
        path = self.root / "song.mp3"
        path.touch()
        cancelled = threading.Event()
        original = playback._temporary
        temporary = []

        def cancel_before_spawn(*args, **kwargs):
            directory = original(*args, **kwargs)
            temporary.append(directory)
            cancelled.set()
            return directory

        with patch.object(playback, "_temporary", side_effect=cancel_before_spawn):
            with self.assertRaisesRegex(HintError, "launch was cancelled"):
                playback.launch(str(path), cancelled=cancelled.is_set)
        self.assertEqual(len(temporary), 1)
        self.assertFalse(temporary[0].exists())

    def test_browser_uses_independent_profile_and_fullscreen(self):
        with patch.dict(os.environ, {"DISPLAY": ":99"}), \
                patch.object(playback.shutil, "which", side_effect=lambda name: "/usr/bin/firefox" if name == "firefox" else None), \
                patch.object(playback, "_spawn", return_value="browser") as spawn:
            url = "https://example.com/?q=$HOME&x=two"
            self.assertEqual(playback.launch(url), "browser")
            argv = spawn.call_args.args[0]
            self.assertIn("--no-remote", argv)
            self.assertIn("--kiosk", argv)
            profile = Path(argv[argv.index("--profile") + 1])
            self.assertEqual(profile.stat().st_mode & 0o777, 0o700)
            self.assertEqual(argv[-1], url)
            self.assertFalse(spawn.call_args.kwargs["pausable"])
            import shutil
            shutil.rmtree(profile)

    def test_pdf_uses_isolated_browser_and_text_own_viewer(self):
        pdf, text = self.root / "a.pdf", self.root / "a.txt"
        pdf.touch()
        text.write_text("literal <html> $HOME")
        with patch.dict(os.environ, {"DISPLAY": ":99"}), patch.object(playback, "_browser", return_value="pdf") as browser:
            self.assertEqual(playback.launch(str(pdf)), "pdf")
            browser.assert_called_once_with(pdf.as_uri())
        with patch.dict(os.environ, {"DISPLAY": ":99"}), patch.object(playback, "_popup", return_value="text") as popup:
            self.assertEqual(playback.launch(str(text)), "text")
            popup.assert_called_once_with(str(text), file=True)

    def test_unknown_document_fallback_is_untracked(self):
        path = self.root / "opaque.unknown"
        path.touch()
        with patch.dict(os.environ, {"DISPLAY": ":99"}), patch.object(playback, "_spawn", return_value="document") as spawn:
            playback.launch(str(path))
            self.assertFalse(spawn.call_args.kwargs["controllable"])
            self.assertFalse(spawn.call_args.kwargs["pausable"])

    def test_mpv_ipc_pause_resume_and_resource_cleanup(self):
        try:
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.close()
        except PermissionError:
            if os.environ.get("REQUIRE_LOCAL_IPC") == "1":
                raise
            self.skipTest("AF_UNIX sockets are restricted in this environment")
        executable = self.root / "mpv"
        executable.write_text(
            f"#!{sys.executable}\n"
            "import json,pathlib,socket,sys\n"
            "path=next(x.split('=',1)[1] for x in sys.argv if x.startswith('--input-ipc-server='))\n"
            "s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);s.bind(path);s.listen()\n"
            "while True:\n"
            " c,_=s.accept()\n"
            " with c:\n"
            "  request=json.loads(c.makefile().readline())\n"
            "  with pathlib.Path('ipc-commands').open('a') as f:f.write(json.dumps(request['command'])+'\\n')\n"
            "  c.sendall((json.dumps({'request_id':request['request_id'],'error':'success','data':'next.jpg'})+'\\n').encode())\n"
            "  if request['command'][0]=='quit':break\n"
        )
        executable.chmod(0o755)
        audio = self.root / "song.mp3"
        audio.touch()
        with patch.dict(os.environ, {"PATH": str(self.root) + os.pathsep + os.environ.get("PATH", "")}):
            run = self.launch(str(audio))
            temporary = run.temporary
            run.pause()
            self.assertTrue(run.paused)
            run.resume()
            self.assertFalse(run.paused)
            self.assertEqual(run.detail, "next.jpg")
            run.stop()
            self.assertTrue(eventually(lambda: run.poll() is not None))
            self.assertFalse(temporary.exists())
            commands = [json.loads(line) for line in (self.home / "ipc-commands").read_text().splitlines()]
            self.assertIn(["set_property", "pause", True], commands)
            self.assertIn(["set_property", "pause", False], commands)
            self.assertIn(["quit"], commands)

    def test_ipc_protocol_handles_events_and_fragmented_acknowledgement(self):
        process = MagicMock(pid=123)
        process.poll.return_value = None
        run = playback.ManagedAction(process, kind="audio", ipc=self.root / "control.sock")
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.recv.side_effect = [b'{"event":"pause"}\n{"request_id":', b'1,"error":"success"}\n']
        with patch.object(playback.socket, "socket", return_value=connection):
            run.pause()
        self.assertTrue(run.paused)
        request = json.loads(connection.sendall.call_args.args[0])
        self.assertEqual(request["command"], ["set_property", "pause", True])
        self.assertEqual(request["request_id"], 1)

    def test_failed_ipc_acknowledgement_does_not_claim_paused(self):
        process = MagicMock(pid=123)
        process.poll.return_value = None
        run = playback.ManagedAction(process, kind="audio", ipc=self.root / "control.sock")
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.recv.return_value = b'{"request_id":1,"error":"property unavailable"}\n'
        with patch.object(playback.socket, "socket", return_value=connection):
            with self.assertRaisesRegex(HintError, "Media control failed"):
                run.pause()
        self.assertFalse(run.paused)

    def test_snap_browser_profile_uses_its_accessible_common_directory(self):
        temporary = playback._temporary("browser", browser="/snap/bin/firefox")
        self.addCleanup(lambda: __import__("shutil").rmtree(temporary, ignore_errors=True))
        self.assertEqual(temporary.parent, self.home / "snap/firefox/common")
        self.assertEqual(temporary.stat().st_mode & 0o777, 0o700)

    def test_office_uses_separate_read_only_profile(self):
        path = self.root / "sheet.xlsx"
        path.touch()
        with patch.dict(os.environ, {"DISPLAY": ":99"}), \
                patch.object(playback.shutil, "which", return_value="/usr/bin/libreoffice"), \
                patch.object(playback, "_spawn", return_value="office") as spawn, \
                patch.object(playback, "_fullscreen_owned") as fullscreen:
            self.assertEqual(playback.launch(str(path)), "office")
            argv = spawn.call_args.args[0]
            self.assertIn("--view", argv)
            self.assertTrue(any(arg.startswith("-env:UserInstallation=file:") for arg in argv))
            self.assertEqual(argv[-1], str(path))
            fullscreen.assert_called_once_with("office")
            __import__("shutil").rmtree(spawn.call_args.kwargs["temporary"])

    def test_extensionless_office_symlink_uses_isolated_libreoffice(self):
        target = self.root / "sheet.xlsx"
        target.touch()
        alias = self.root / "sheet"
        alias.symlink_to(target)
        with patch.object(playback.shutil, "which", return_value="/usr/bin/libreoffice"), \
                patch.object(playback, "_spawn", return_value="office") as spawn, \
                patch.object(playback, "_fullscreen_owned"):
            self.assertEqual(playback.launch(str(alias)), "office")
        self.addCleanup(playback.shutil.rmtree, spawn.call_args.kwargs["temporary"], True)
        argv = spawn.call_args.args[0]
        self.assertEqual(argv[0], "/usr/bin/libreoffice")
        self.assertTrue(any(arg.startswith("-env:UserInstallation=file:") for arg in argv))
        self.assertEqual(argv[-1], str(alias))

    def test_missing_player_cleans_private_socket_directory(self):
        path = self.root / "song.mp3"
        path.touch()
        temporary = self.root / "private"
        temporary.mkdir()
        with patch.object(playback, "_temporary", return_value=temporary), patch.dict(os.environ, {"PATH": ""}):
            with self.assertRaisesRegex(HintError, "not available"):
                playback.launch(str(path))
        self.assertFalse(temporary.exists())


if __name__ == "__main__":
    unittest.main()
