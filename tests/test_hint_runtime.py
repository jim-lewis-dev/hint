"""Managed action lifetimes and private IPC, isolated from desktop services."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from hint_config import HintError
from hint_runtime import RunManager, RuntimeServer, request


class FakeAction:
    def __init__(self, action):
        self.pid = 123
        self.kind = "command"
        self.detail = action
        self.pausable = True
        self.controllable = True
        self.untracked = False
        self.code = None
        self.events = []

    def poll(self):
        return self.code

    def pause(self):
        self.events.append("pause")

    def resume(self):
        self.events.append("resume")

    def stop(self):
        self.events.append("stop")
        self.code = -15


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.definitions = {"stretch": {"action": "original action"}}
        self.patch = patch("hint_signals.list_signals", side_effect=lambda: self.definitions.copy())
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.actions = []

        def launch(action):
            value = FakeAction(action)
            self.actions.append(value)
            return value

        self.manager = RunManager(launch, recent_limit=3)
        self.addCleanup(self.manager.close)

    def require_ipc(self):
        try:
            with socket.socket(socket.AF_UNIX):
                pass
        except PermissionError:
            if os.environ.get("REQUIRE_LOCAL_IPC") == "1":
                raise
            self.skipTest("this environment blocks Unix sockets; REQUIRE_LOCAL_IPC=1 makes this a required gate")

    def test_repeated_name_controls_all_runs_and_preserves_launch_snapshot(self):
        for _ in range(2):
            self.manager.dispatch({"op": "fire", "name": "stretch"})
        self.definitions["stretch"]["action"] = "changed action"
        self.manager.dispatch({"op": "fire", "name": "stretch"})
        runs = self.manager.status()["runs"]
        self.assertEqual([run["action"] for run in runs], ["original action", "original action", "changed action"])
        self.assertEqual(self.manager.control("pause", "stretch")["count"], 3)
        self.assertEqual(self.manager.control("pause", "stretch")["already"], 3)
        self.assertEqual(self.manager.control("resume", "stretch")["count"], 3)
        self.assertEqual(self.manager.control("stop", "stretch")["count"], 3)
        self.assertTrue(all(run["state"] == "stopped" for run in self.manager.status()["runs"]))
        self.assertIn("stretch", self.definitions)
        self.assertEqual(self.manager.control("stop", "stretch")["active"], 0)

    def test_temporary_numbers_advance_without_definitions_or_reuse(self):
        first = self.manager.dispatch({"op": "fire", "action": "first"})
        self.assertEqual(first["signal"], "HINT-001")
        self.manager.control("stop", "HINT-001")
        second = self.manager.dispatch({"op": "fire", "action": "second"})
        self.assertEqual(second["signal"], "HINT-002")
        self.assertEqual(set(self.definitions), {"stretch"})
        with self.assertRaisesRegex(HintError, "reserved"):
            self.manager.start("HINT-003", "third")

    def test_simultaneous_fires_receive_distinct_temporary_names(self):
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _: self.manager.start(None, "anything"), range(20)))
        self.assertEqual({result["signal"] for result in results},
                         {f"HINT-{number:03d}" for number in range(1, 21)})
        self.assertEqual(self.manager.status()["active_count"], 20)

    def test_deleted_definition_does_not_hide_or_orphan_running_action(self):
        self.manager.dispatch({"op": "fire", "name": "stretch"})
        self.definitions.clear()
        self.assertEqual(len(self.manager.status("stretch")["runs"]), 1)
        self.assertEqual(self.manager.control("stop", "stretch")["count"], 1)

    def test_partial_control_reports_unsupported_and_failures(self):
        for _ in range(3):
            self.manager.start("stretch", "anything")
        self.actions[1].pausable = False
        self.manager._active[1]["pausable"] = False
        self.actions[2].pause = lambda: (_ for _ in ()).throw(HintError("unavailable"))
        result = self.manager.control("pause", "stretch")
        self.assertEqual((result["count"], result["unsupported"], len(result["failed"])), (1, 1, 1))
        self.assertEqual(result["failed"][0]["reason"], "unavailable")

    def test_terminal_history_is_bounded_and_preserves_exit_status(self):
        for code in range(5):
            self.manager.start(None, "anything")
            self.actions[-1].code = code
            self.manager.status()
        runs = self.manager.status()["runs"]
        self.assertEqual(len(runs), 3)
        self.assertEqual([run["exit_code"] for run in runs], [2, 3, 4])
        self.assertTrue(all(run["state"] == "failed" for run in runs))
        self.manager.start(None, "anything")
        self.actions[-1].code = 0
        self.assertEqual(self.manager.status()["runs"][-1]["state"], "finished")

    def test_untracked_launcher_exit_is_not_claimed_as_closed_window(self):
        self.manager.start(None, "external document")
        self.actions[-1].untracked = True
        self.actions[-1].code = 0
        result = self.manager.status()["runs"][-1]
        self.assertEqual(result["state"], "launched")
        self.assertEqual(result["tracking"], "unavailable")

    def test_untracked_launcher_failure_is_failed(self):
        self.manager.start(None, "external document")
        self.actions[-1].untracked = True
        self.actions[-1].code = 3
        result = self.manager.status()["runs"][-1]
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["exit_code"], 3)

    def test_corrupt_definitions_do_not_hide_or_prevent_control_of_live_runs(self):
        self.manager.start("stretch", "anything")
        self.manager.start(None, "anything")
        with patch("hint_signals.list_signals", side_effect=HintError("invalid signals.json")):
            result = self.manager.status()
            self.assertEqual(result["definitions"], {})
            self.assertEqual(result["display_times"], {})
            self.assertEqual(result["definitions_error"], "invalid signals.json")
            self.assertEqual(result["active_count"], 2)
            self.assertEqual(len(self.manager.status("stretch")["runs"]), 1)
            self.assertEqual(self.manager.control("pause", "stretch")["count"], 1)
            self.assertEqual(self.manager.control("stop", "stretch")["count"], 1)
            self.assertEqual(self.manager.control("stop", "HINT-001")["count"], 1)
            self.assertEqual(self.manager.control("stop", "stretch")["active"], 0)

    def test_status_includes_server_formatted_schedule_times(self):
        self.definitions["stretch"]["time"] = "2027-02-10 09:00:00"
        with patch("hint_signals.display_time", return_value="2027-02-10 09:00:00 CST"):
            result = self.manager.status()
        self.assertEqual(result["display_times"], {"2027-02-10 09:00:00": "2027-02-10 09:00:00 CST"})

    def test_slow_definition_read_does_not_hold_the_run_registry_lock(self):
        self.manager.start("stretch", "anything")
        reading = threading.Event()
        release = threading.Event()
        stopped = threading.Event()

        def read():
            reading.set()
            release.wait(timeout=2)
            return self.definitions.copy()

        def stop():
            self.manager.control("stop", "stretch")
            stopped.set()

        with patch("hint_signals.list_signals", side_effect=read):
            status_thread = threading.Thread(target=self.manager.status)
            status_thread.start()
            self.assertTrue(reading.wait(timeout=1))
            stop_thread = threading.Thread(target=stop)
            stop_thread.start()
            try:
                self.assertTrue(stopped.wait(timeout=0.5), "storage read blocked stop")
            finally:
                release.set()
                status_thread.join(timeout=2)
                stop_thread.join(timeout=2)

    def test_busy_update_does_not_interrupt_runs_and_idle_lease_blocks_new_work(self):
        self.manager.start("stretch", "anything")
        self.assertFalse(self.manager.dispatch({"op": "prepare_update"})["ready"])
        self.assertEqual(self.actions[-1].events, [])
        self.manager.control("stop", "stretch")
        lease = self.manager.dispatch({"op": "prepare_update"})
        self.assertTrue(lease["ready"])
        with self.assertRaisesRegex(HintError, "preparing an update"):
            self.manager.start(None, "anything")
        with patch("hint_signals.fire_due") as due:
            self.manager.tick()
            due.assert_not_called()
        with self.assertRaisesRegex(HintError, "Invalid"):
            self.manager.dispatch({"op": "cancel_update", "token": "other"})
        self.manager.dispatch({"op": "cancel_update", "token": lease["token"]})
        self.assertTrue(self.manager.start(None, "anything")["fired"])

    def test_expired_update_lease_does_not_leave_service_stuck(self):
        self.manager.dispatch({"op": "prepare_update"})
        self.manager._update_deadline = time.monotonic() - 1
        self.assertTrue(self.manager.start(None, "anything")["fired"])

    def test_session_refresh_is_whitelisted_and_does_not_restart_active_runs(self):
        with patch.dict(os.environ, {"DISPLAY": ":old", "WAYLAND_DISPLAY": "wayland-old",
                                     "XAUTHORITY": "/old/auth", "XDG_SESSION_TYPE": "x11",
                                     "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/bus"}):
            self.manager.start("stretch", "anything")
            result = self.manager.dispatch({"op": "session", "environment": {
                "WAYLAND_DISPLAY": "wayland-new", "XDG_SESSION_TYPE": "wayland"
            }})
            self.assertTrue(result["updated"])
            self.assertNotIn("DISPLAY", os.environ)
            self.assertNotIn("XAUTHORITY", os.environ)
            self.assertEqual(os.environ["WAYLAND_DISPLAY"], "wayland-new")
            self.assertEqual(os.environ["XDG_SESSION_TYPE"], "wayland")
            self.assertEqual(os.environ["DBUS_SESSION_BUS_ADDRESS"], "unix:path=/run/user/bus")
            self.assertEqual(self.manager.status()["active_count"], 1)
            self.assertEqual(self.actions[0].events, [])
            self.manager.dispatch({"op": "session", "environment": {"DISPLAY": ":new"}})
            self.assertNotIn("WAYLAND_DISPLAY", os.environ)
            self.assertNotIn("XDG_SESSION_TYPE", os.environ)
            self.assertEqual(os.environ["DISPLAY"], ":new")

    def test_session_rejects_other_environment_keys_and_invalid_values_atomically(self):
        before = dict(os.environ)
        for environment in ({"DISPLAY": ":1", "PATH": "/untrusted"},
                            {"DISPLAY": ":1", "HOME": "/untrusted"},
                            {"DISPLAY": 1}, {"DISPLAY": ":1\0"},
                            {"XAUTHORITY": "/auth"}, {"DISPLAY": ""}, None):
            with self.subTest(environment=environment), self.assertRaises(HintError):
                self.manager.dispatch({"op": "session", "environment": environment})
            self.assertEqual(dict(os.environ), before)

    def test_tick_passes_owned_launcher_and_close_stops_owned_runs(self):
        with patch("hint_signals.fire_due") as due:
            self.manager.tick()
            due.assert_called_once_with(launcher=self.manager.start)
        self.manager.start(None, "anything")
        self.manager.close()
        self.assertIn("stop", self.actions[-1].events)
        with self.assertRaisesRegex(HintError, "stopping"):
            self.manager.start(None, "anything")

    @unittest.skipUnless(all(hasattr(os, name) for name in ("waitid", "P_PID", "WEXITED", "WNOHANG", "WNOWAIT")),
                         "requires non-reaping child-exit observation")
    def test_tick_reaps_finished_child_without_status_or_another_launch(self):
        from hint_playback import launch
        actions = []

        def owned_launch(action):
            run = launch(action)
            actions.append(run)
            return run

        manager = RunManager(owned_launch)
        try:
            manager.start("clock", "exit 0")
            pid = actions[0].pid
            deadline = time.monotonic() + 3
            event = None
            while event is None and time.monotonic() < deadline:
                # Observe completion without collecting the child ourselves.
                event = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                if event is None:
                    time.sleep(0.005)
            self.assertIsNotNone(event, "owned child did not finish")
            self.assertEqual(event.si_status, 0)
            with patch("hint_signals.fire_due"):
                manager.tick()
            with self.assertRaises(ChildProcessError):
                os.waitpid(pid, os.WNOHANG)
            self.assertEqual(manager.status("clock")["runs"][0]["state"], "finished")
        finally:
            manager.close()

    def test_real_commands_pause_resume_stop_without_touching_unrelated_process(self):
        manager = RunManager()
        unrelated = subprocess.Popen(["sleep", "30"], start_new_session=True)
        try:
            manager.start("stretch", "exec sleep 30")
            manager.start("stretch", "exec sleep 30")
            children = [run["pid"] for run in manager.status()["runs"]]
            self.assertEqual(manager.control("pause", "stretch")["count"], 2)
            for pid in children:
                deadline = time.monotonic() + 1
                event = None
                while event is None and time.monotonic() < deadline:
                    event = os.waitid(os.P_PID, pid, os.WSTOPPED | os.WNOHANG | os.WNOWAIT)
                    if event is None:
                        time.sleep(0.005)
                self.assertIsNotNone(event, "owned child did not actually suspend")
            self.assertEqual(manager.control("resume", "stretch")["count"], 2)
            manager.control("pause", "stretch")
            stopped = manager.control("stop", "stretch")
            self.assertEqual(stopped["count"], 2)
            self.assertEqual(stopped["pending"], 0)
            self.assertEqual(manager.status()["active_count"], 0)
            self.assertIsNone(unrelated.poll())
        finally:
            manager.close()
            unrelated.terminate()
            unrelated.wait(timeout=2)

    def test_ipc_roundtrip_permissions_duplicate_service_and_cleanup(self):
        self.require_ipc()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime/control.sock"
            with RuntimeServer(self.manager, path):
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
                result = request({"op": "fire", "name": "stretch"}, path=path)
                self.assertTrue(result["fired"])
                with self.assertRaisesRegex(HintError, "already running"):
                    RuntimeServer(path=path).start()
                self.assertEqual(request({"op": "status"}, path=path)["active_count"], 1)
                self.assertEqual(request({"op": "stop", "name": "stretch"}, path=path)["count"], 1)
            self.assertFalse(path.exists())
            self.assertFalse(any(path.parent.glob("*.json")))
            with self.assertRaisesRegex(HintError, "not running"):
                request({"op": "status"}, path=path)

    def test_bad_requests_do_not_launch_anything(self):
        self.require_ipc()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "control.sock"
            with RuntimeServer(self.manager, path):
                for body in ({"op": "unknown"}, {"op": "fire", "name": "stretch", "extra": 1},
                             {"op": "fire", "name": "stretch", "action": "inline"}):
                    with self.subTest(body=body), self.assertRaises(HintError):
                        request(body, path=path)
                with socket.socket(socket.AF_UNIX) as connection:
                    connection.connect(str(path))
                    connection.sendall(b"not JSON\n")
                    reply = json.loads(connection.recv(65536))
                    self.assertFalse(reply["ok"])
                self.assertEqual(self.actions, [])

    def test_no_retry_after_lost_fire_reply(self):
        self.require_ipc()
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "stall.sock")
            with socket.socket(socket.AF_UNIX) as server:
                server.bind(path)
                server.listen(2)
                seen = []

                def stall():
                    connection, _ = server.accept()
                    with connection:
                        seen.append(connection.recv(65536))
                        time.sleep(0.15)

                thread = threading.Thread(target=stall)
                thread.start()
                with self.assertRaisesRegex(HintError, "may have taken effect; it was not retried"):
                    request({"op": "fire", "action": "anything"}, timeout=0.05, path=path)
                thread.join(timeout=1)
                self.assertEqual(len(seen), 1)


if __name__ == "__main__":
    unittest.main()
