"""Signal scheduling and RTC contracts, without launching actions or hardware."""

from contextlib import ExitStack, redirect_stderr
from datetime import datetime
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import hint_signals as signals
import rtc_helper


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2030, 1, 1, 7, 0, 0)


def success(*args, **kwargs):
    return SimpleNamespace(returncode=0, stdout="", stderr="")


class SignalTests(unittest.TestCase):
    def setUp(self):
        self.context = ExitStack()
        self.addCleanup(self.context.close)
        root = Path(self.context.enter_context(tempfile.TemporaryDirectory()))
        self.table = root / "state" / "signals.json"
        self.context.enter_context(patch.object(signals, "TABLE", self.table))
        self.context.enter_context(patch.object(signals, "LOCK", self.table.with_name(".hint.lock")))
        self.context.enter_context(patch.object(signals, "datetime", FrozenDateTime))
        self.context.enter_context(patch.object(signals, "config", return_value={"wake_lead_seconds": 10}))
        self.rtc = self.context.enter_context(patch.object(signals.subprocess, "run", side_effect=success))
        self.launch = self.context.enter_context(patch.object(signals, "launch"))

    def rtc_arguments(self):
        return [call.args[0][-1] for call in self.rtc.call_args_list]

    def test_ordinary_signals_and_empty_startup_never_require_rtc(self):
        signals.arm_wake()
        signals.set_signal("bell", "fake-action", None, False)
        signals.set_signal("timer", "fake-action", "2030-01-01 07:01:00", False)
        signals.remove_signal("timer")
        signals.remove_signal("bell")
        signals.arm_wake()
        self.rtc.assert_not_called()
        self.assertEqual(signals.list_signals(), {})

    def test_atomic_private_table_and_lock_live_in_the_data_directory(self):
        signals.set_signal("bell", "fake-action", None, False)
        self.assertEqual(json.loads(self.table.read_text()), {"bell": {"action": "fake-action"}})
        self.assertEqual(self.table.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.table.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(signals.LOCK.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.table.parent.glob(".signals.*")), [])

    def test_only_changes_to_the_earliest_wake_touch_rtc(self):
        signals.set_signal("late", "fake", "2030-01-01 07:02:00", True)
        signals.set_signal("plain", "fake", None, False)
        signals.set_signal("later", "fake", "2030-01-01 07:03:00", True)
        signals.set_signal("early", "fake", "2030-01-01 07:01:00", True)
        signals.remove_signal("early")
        signals.remove_signal("later")
        signals.remove_signal("late")
        self.assertEqual(self.rtc_arguments(), ["110", "50", "110", "disable"])
        for call in self.rtc.call_args_list:
            self.assertEqual(call.kwargs["timeout"], signals.RTC_TIMEOUT)

    @unittest.skipUnless(hasattr(time, "tzset"), "requires POSIX timezone support")
    def test_wake_delay_uses_elapsed_seconds_across_both_dst_changes(self):
        original_tz = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "America/New_York"
            time.tzset()
            cases = [
                # The spring clock jumps from 01:59 to 03:00: one real hour.
                (datetime(2026, 3, 8, 1, 30), datetime(2026, 3, 8, 3, 30), "3590"),
                # The fall clock repeats the 01:00 hour: three real hours.
                (datetime(2026, 11, 1, 0, 30), datetime(2026, 11, 1, 2, 30), "10790"),
            ]
            for now, when, expected in cases:
                with self.subTest(now=now, when=when):
                    self.assertEqual(signals._wake_argument(when, now), expected)
        finally:
            if original_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = original_tz
            time.tzset()
        self.rtc.assert_not_called()

    def test_failed_arm_leaves_previous_table_and_restores_previous_alarm(self):
        signals.set_signal("old", "fake", "2030-01-01 07:02:00", True)
        original = self.table.read_bytes()
        self.rtc.reset_mock()
        self.rtc.side_effect = [SimpleNamespace(returncode=1, stderr="no RTC", stdout=""), success()]
        with self.assertRaisesRegex(signals.HintError, "no RTC"), redirect_stderr(io.StringIO()):
            signals.set_signal("new", "fake", "2030-01-01 07:01:00", True)
        self.assertEqual(self.table.read_bytes(), original)
        self.assertEqual(self.rtc_arguments(), ["50", "110"])

    def test_failed_save_restores_previous_alarm(self):
        signals.set_signal("old", "fake", "2030-01-01 07:02:00", True)
        original = self.table.read_bytes()
        self.rtc.reset_mock()
        with patch.object(signals.os, "replace", side_effect=OSError("read-only")):
            with self.assertRaisesRegex(signals.HintError, "read-only"):
                signals.set_signal("new", "fake", "2030-01-01 07:01:00", True)
        self.assertEqual(self.table.read_bytes(), original)
        self.assertEqual(self.rtc_arguments(), ["50", "110"])
        self.assertEqual(list(self.table.parent.glob(".signals.*")), [])

    def test_removing_last_past_wake_clears_a_still_pending_alarm(self):
        signals.write_table({"bell": {"action": "fake", "time": "2030-01-01 06:59:59", "wake": True}})
        signals.remove_signal("bell")
        self.assertEqual(self.rtc_arguments(), ["disable"])
        self.assertEqual(signals.list_signals(), {})

    def test_directory_sync_failure_keeps_the_committed_table_and_new_alarm(self):
        signals.set_signal("old", "fake", "2030-01-01 07:02:00", True)
        self.rtc.reset_mock()
        real_fsync = signals.os.fsync
        sync_calls = 0

        def fail_directory(fd):
            nonlocal sync_calls
            sync_calls += 1
            if sync_calls == 2:
                raise OSError("directory sync unavailable")
            return real_fsync(fd)

        with patch.object(signals.os, "fsync", side_effect=fail_directory):
            with redirect_stderr(io.StringIO()) as errors:
                signals.set_signal("new", "fake", "2030-01-01 07:01:00", True)
        self.assertIn("new", signals.list_signals())
        self.assertEqual(self.rtc_arguments(), ["50"])
        self.assertIn("signals saved but directory sync failed", errors.getvalue())

    def test_invalid_wake_configuration_does_not_touch_rtc_or_table(self):
        with patch.object(signals, "config", return_value={"wake_lead_seconds": True}):
            with self.assertRaises(signals.HintError):
                signals.set_signal("bad", "fake", "2030-01-01 07:01:00", True)
        self.rtc.assert_not_called()
        self.assertFalse(self.table.exists())

    def test_manual_fire_preserves_a_schedule_and_launch_failure_preserves_it(self):
        signals.set_signal("bell", "fake", "2030-01-01 07:01:00", False)
        self.assertTrue(signals.fire("bell")["fired"])
        self.assertIn("bell", signals.list_signals())
        self.launch.side_effect = signals.HintError("missing executable")
        self.assertFalse(signals.fire("bell", "2030-01-01 07:01:00")["fired"])
        self.assertIn("bell", signals.list_signals())
        self.rtc.assert_not_called()

    def test_scheduled_fire_is_removed_even_if_next_alarm_cannot_be_armed(self):
        when = "2030-01-01 06:59:00"
        signals.write_table({"bell": {"action": "fake", "time": when, "wake": True}})
        self.rtc.side_effect = subprocess.TimeoutExpired("sudo", signals.RTC_TIMEOUT)
        errors = io.StringIO()
        with redirect_stderr(errors):
            self.assertTrue(signals.fire("bell", when)["fired"])
        self.assertEqual(signals.list_signals(), {})
        self.assertIn("timed out", errors.getvalue())
        self.launch.assert_called_once_with("fake")

    def test_simultaneous_schedulers_start_one_action(self):
        when = "2030-01-01 06:59:00"
        signals.write_table({"bell": {"action": "fake", "time": when}})
        started = threading.Event()
        release = threading.Event()
        second_started = threading.Event()
        second_read = threading.Event()
        real_read = signals.read_table
        results = []
        errors = []

        def launch(action):
            started.set()
            if not release.wait(3):
                raise AssertionError("test did not release action launch")

        def fire(second=False):
            try:
                if second:
                    second_started.set()
                results.append(signals.fire("bell", when))
            except BaseException as exc:
                errors.append(exc)

        def observed_read():
            if threading.current_thread().name == "second-scheduler":
                second_read.set()
            return real_read()

        self.launch.side_effect = launch
        first = threading.Thread(target=fire)
        second = threading.Thread(target=fire, args=(True,), name="second-scheduler")
        with patch.object(signals, "read_table", side_effect=observed_read):
            first.start()
            try:
                self.assertTrue(started.wait(2))
                second.start()
                self.assertTrue(second_started.wait(2))
                # The second scheduler must not even read the entry while the
                # first owns the launch-and-removal critical section.
                self.assertFalse(second_read.wait(0.1))
            finally:
                release.set()
                first.join(3)
                if second.ident is not None:
                    second.join(3)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.launch.assert_called_once_with("fake")
        self.assertEqual(sum(result["fired"] for result in results), 1)
        self.assertEqual(signals.list_signals(), {})

    def test_due_signals_fire_in_order_and_are_removed(self):
        signals.write_table({
            "later": {"action": "later", "time": "2030-01-01 06:59:00"},
            "early": {"action": "early", "time": "2030-01-01 06:58:00"},
            "future": {"action": "future", "time": "2030-01-01 07:01:00"},
        })
        signals.fire_due()
        self.assertEqual([call.args[0] for call in self.launch.call_args_list], ["early", "later"])
        self.assertEqual(set(signals.list_signals()), {"future"})
        self.rtc.assert_not_called()

    def test_table_validation_accepts_false_wake_and_rejects_malformed_data(self):
        signals.validate("bell", {"action": "fake", "wake": False})
        bad_entries = [None, {"action": ""}, {"action": "fake", "wake": 1},
                       {"action": "fake", "wake": True},
                       {"action": "fake", "time": 7},
                       {"action": "fake", "time": "2030-1-1 07:00:00"},
                       {"action": "fake", "time": "2030-02-30 07:00:00"}]
        for entry in bad_entries:
            with self.subTest(entry=entry), self.assertRaises(signals.HintError):
                signals.validate("bell", entry)
        for name in (None, 12, "", "bad.name", "../bell"):
            for operation in (signals.get_signal, signals.remove_signal, signals.fire):
                with self.subTest(name=name, operation=operation), self.assertRaises(signals.HintError):
                    operation(name)
        self.rtc.assert_not_called()

    def test_stored_signals_require_the_action_field(self):
        self.table.parent.mkdir(parents=True)
        for entry in ({"handler": "/bin/true"},
                      {"action": "/bin/true", "handler": "/bin/false"}):
            with self.subTest(entry=entry):
                self.table.write_text(json.dumps({"bell": entry}))
                with self.assertRaises(signals.HintError):
                    signals.read_table()
        self.rtc.assert_not_called()


class RtcHelperTests(unittest.TestCase):
    def test_invalid_input_is_rejected_before_the_alarm_is_touched(self):
        for argv in ([], ["1", "2"], ["invalid"], ["-1"], ["4"], ["1.5"]):
            with self.subTest(argv=argv), patch.object(rtc_helper.subprocess, "run") as run:
                with redirect_stderr(io.StringIO()):
                    self.assertEqual(rtc_helper.main(argv), 2)
                run.assert_not_called()

    def test_valid_delays_are_normalized_and_calls_are_bounded(self):
        with patch.object(rtc_helper.subprocess, "run", side_effect=success) as run:
            self.assertEqual(rtc_helper.main(["00015"]), 0)
            self.assertEqual(run.call_args_list[0].args[0], [rtc_helper.RTCWAKE, "-m", "disable"])
            self.assertEqual(run.call_args_list[1].args[0][-1], "15")
            self.assertTrue(all(call.kwargs["timeout"] == rtc_helper.RTC_TIMEOUT for call in run.call_args_list))
        with patch.object(rtc_helper.subprocess, "run", side_effect=success) as run:
            self.assertEqual(rtc_helper.main(["disable"]), 0)
            self.assertEqual(run.call_count, 1)

    def test_hardware_failure_and_timeout_are_reported(self):
        with patch.object(rtc_helper.subprocess, "run", return_value=SimpleNamespace(returncode=3)) as run:
            self.assertEqual(rtc_helper.main(["15"]), 3)
            self.assertEqual(run.call_count, 1)
        with patch.object(rtc_helper.subprocess, "run", side_effect=subprocess.TimeoutExpired("rtcwake", 2)):
            with redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(rtc_helper.main(["15"]), 1)
            self.assertIn("timed out", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
