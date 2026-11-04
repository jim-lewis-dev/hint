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
from unittest.mock import Mock, patch

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
        self.launch = Mock()
        signals._PENDING_CLEARS.clear()
        signals._REPORTED_ERRORS.clear()

    def launched(self, name, action=None, *, popup=None):
        return self.launch(popup=popup) if popup is not None else self.launch(action)

    def fire(self, name, when=None):
        return signals.fire(name, when, launcher=self.launched)

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
        self.assertTrue(self.fire("bell")["fired"])
        self.assertIn("bell", signals.list_signals())
        self.launch.side_effect = signals.HintError("missing executable")
        self.assertFalse(self.fire("bell", "2030-01-01 07:01:00")["fired"])
        self.assertIn("bell", signals.list_signals())
        self.rtc.assert_not_called()

    def test_scheduled_fire_retains_definition_even_if_next_alarm_cannot_be_armed(self):
        when = "2030-01-01 06:59:00"
        signals.write_table({"bell": {"action": "fake", "time": when, "wake": True}})
        self.rtc.side_effect = subprocess.TimeoutExpired("sudo", signals.RTC_TIMEOUT)
        errors = io.StringIO()
        with redirect_stderr(errors):
            result = self.fire("bell", when)
        self.assertTrue(result["fired"])
        self.assertEqual(signals.list_signals(), {"bell": {"action": "fake", "wake": True}})
        self.assertIn("timed out", result["warning"])
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
                results.append(self.fire("bell", when))
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
        self.assertEqual(signals.list_signals(), {"bell": {"action": "fake"}})

    def test_due_signals_fire_in_order_and_keep_definitions(self):
        signals.write_table({
            "later": {"action": "later", "time": "2030-01-01 06:59:00"},
            "early": {"action": "early", "time": "2030-01-01 06:58:00"},
            "future": {"action": "future", "time": "2030-01-01 07:01:00"},
        })
        signals.fire_due(launcher=self.launched)
        self.assertEqual([call.args[0] for call in self.launch.call_args_list], ["early", "later"])
        self.assertEqual(set(signals.list_signals()), {"early", "later", "future"})
        self.assertNotIn("time", signals.list_signals()["early"])
        self.assertNotIn("time", signals.list_signals()["later"])
        self.rtc.assert_not_called()

    def test_table_validation_accepts_false_wake_and_rejects_malformed_data(self):
        signals.validate("bell", {"action": "fake", "wake": False})
        bad_entries = [None, {"action": ""}, {"action": "fake", "wake": 1},
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

    def test_stored_signals_require_exactly_one_action_or_popup(self):
        self.table.parent.mkdir(parents=True)
        for entry in ({"handler": "/bin/true"},
                      {"action": "/bin/true", "handler": "/bin/false"},
                      {"action": "true", "popup": "hello"}, {}, {"popup": " "}):
            with self.subTest(entry=entry):
                self.table.write_text(json.dumps({"bell": entry}))
                with self.assertRaises(signals.HintError):
                    signals.read_table()
        self.rtc.assert_not_called()

    def test_popup_definition_and_partial_updates_keep_one_content_field(self):
        message = 'Dinner! "Now"\n$HOME; $(touch unexpected)'
        created = signals.set_signal("dinner", popup=message, when="2030-01-01 07:01", wake=True)
        self.assertTrue(created["created"])
        self.assertEqual(created["definition"], {
            "popup": message, "time": "2030-01-01 07:01:00", "wake": True
        })
        self.assertEqual(signals.get_signal("dinner"), {"dinner": created["definition"]})
        self.assertEqual(signals.set_signal("dinner", popup=message)["changed"], [])
        changed = signals.set_signal("dinner", action="printf hello")
        self.assertNotIn("popup", changed["definition"])
        self.assertEqual(changed["definition"]["action"], "printf hello")
        self.assertEqual(changed["definition"]["time"], "2030-01-01 07:01:00")
        self.assertTrue(changed["definition"]["wake"])
        changed = signals.set_signal("dinner", popup="Now!")
        self.assertNotIn("action", changed["definition"])
        self.assertEqual(changed["definition"]["popup"], "Now!")
        changed = signals.set_signal("dinner", no_time=True, wake=False)
        self.assertEqual(changed["definition"], {"popup": "Now!"})

    def test_scheduled_popup_launches_literal_text_and_can_be_rescheduled(self):
        message = 'Dinner!\n$(touch unexpected)'
        when = "2030-01-01 06:59:00"
        signals.write_table({"dinner": {"popup": message, "time": when}})
        signals.fire_due(launcher=self.launched)
        self.launch.assert_called_once_with(popup=message)
        self.assertEqual(signals.list_signals(), {"dinner": {"popup": message}})
        signals.fire_due(launcher=self.launched)
        self.assertEqual(self.launch.call_count, 1)
        updated = signals.set_signal("dinner", when="2031")
        self.assertEqual(updated["definition"], {"popup": message, "time": "2031-01-01 00:00:00"})

    def test_manual_popup_fire_preserves_schedule_and_failed_launch_does_too(self):
        signals.set_signal("dinner", popup="Dinner!", when="2030-01-01 07:01")
        self.assertTrue(self.fire("dinner")["fired"])
        self.launch.assert_called_once_with(popup="Dinner!")
        self.launch.side_effect = signals.HintError("no usable desktop")
        self.assertFalse(self.fire("dinner", "2030-01-01 07:01:00")["fired"])
        self.assertEqual(signals.get_signal("dinner")["dinner"]["time"], "2030-01-01 07:01:00")


    def test_partial_set_keeps_unspecified_fields_and_clears_independently(self):
        created = signals.set_signal("bell", "old", "2030-01-01 07:01", True)
        self.assertTrue(created["created"])
        self.assertEqual(created["changed"], ["action", "time", "wake"])
        changed = signals.set_signal("bell", action="new")
        self.assertEqual(changed["changed"], ["action"])
        self.assertEqual(changed["definition"], {"action": "new", "time": "2030-01-01 07:01:00", "wake": True})
        cleared = signals.set_signal("bell", no_time=True)
        self.assertEqual(cleared["definition"], {"action": "new", "wake": True})
        self.assertEqual(self.rtc_arguments()[-1], "disable")
        rescheduled = signals.set_signal("bell", when="2030-01-01 08")
        self.assertEqual(rescheduled["definition"]["time"], "2030-01-01 08:00:00")
        self.assertTrue(rescheduled["definition"]["wake"])
        disabled = signals.set_signal("bell", wake=False)
        self.assertNotIn("wake", disabled["definition"])
        self.assertIn("time", disabled["definition"])
        self.assertEqual(signals.set_signal("bell", wake=False)["changed"], [])

    def test_clearing_last_schedule_disarms_even_when_wake_preference_retained(self):
        signals.set_signal("bell", "action", "2030-01-01 07:01", True)
        self.rtc.reset_mock()
        signals.set_signal("bell", no_time=True)
        self.assertEqual(self.rtc_arguments(), ["disable"])
        self.rtc.reset_mock()
        signals.arm_wake()
        signals.remove_signal("bell")
        self.rtc.assert_not_called()

    def test_invalid_partial_edits_do_not_change_saved_data(self):
        signals.set_signal("bell", "action")
        original = self.table.read_bytes()
        cases = [
            {"action": ""}, {"popup": " "}, {"action": "bad\0action"},
            {"popup": "bad\0text"}, {"action": "true", "popup": "hello"},
            {"when": "2030-01-01 07:01", "no_time": True}, {"wake": 1},
            {"when": "2029"}, {}
        ]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(signals.HintError):
                signals.set_signal("bell", **kwargs)
            self.assertEqual(self.table.read_bytes(), original)
        with self.assertRaisesRegex(signals.HintError, "requires --action"):
            signals.set_signal("missing", when="2031")
        with self.assertRaisesRegex(signals.HintError, "No action named"):
            signals.get_signal("missing")
        for name in ("HINT-001", "hint-01", "HiNt-2000"):
            with self.subTest(name=name), self.assertRaisesRegex(signals.HintError, "reserved"):
                signals.set_signal(name, "fake")

    def test_calendar_defaults_and_invalid_components(self):
        cases = {
            "2031": "2031-01-01 00:00:00",
            "2031-06": "2031-06-01 00:00:00",
            "2031-06-12": "2031-06-12 00:00:00",
            "2031-06-12 09": "2031-06-12 09:00:00",
            "2031-06-12 09:42": "2031-06-12 09:42:00",
            "2031-06-12 09:42:51": "2031-06-12 09:42:51",
            "2031 09": "2031-01-01 09:00:00"
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(signals.parse_time(value).strftime(signals.TIME_FORMAT), expected)
        for value in ("", "09:00", "2031-00", "2031-13", "2031-02-29", "2031-06-00", "2031-06-12 24", "2031-06-12 09:60", "2031-06-12 09:42:60", "2031-1-1", "2031-01-01T09:00", "0000"):
            with self.subTest(value=value), self.assertRaises(signals.HintError):
                signals.parse_time(value)

    def test_dst_gaps_and_repeated_times_are_rejected_on_receiver(self):
        with patch.dict(os.environ, {"TZ": "America/Chicago"}):
            with self.assertRaisesRegex(signals.HintError, "does not exist"):
                signals.parse_time("2030-03-10 02:30")
            with self.assertRaisesRegex(signals.HintError, "occurs twice"):
                signals.parse_time("2030-11-03 01:30")
            self.assertEqual(signals.display_time("2030-06-12 09"), "2030-06-12 09:00:00 CDT")
            self.assertEqual(signals.display_time("2030-12-12 09"), "2030-12-12 09:00:00 CST")

    def test_schedule_clear_failure_retries_save_without_relaunch(self):
        when = "2030-01-01 06:59:00"
        signals.write_table({"bell": {"action": "fake", "time": when}})
        original = self.table.read_bytes()
        with patch.object(signals, "write_table", side_effect=signals.HintError("disk full")):
            with redirect_stderr(io.StringIO()) as errors:
                signals.fire_due(launcher=self.launched)
                signals.fire_due(launcher=self.launched)
            self.assertEqual(errors.getvalue().count("could not clear its schedule"), 1)
        self.assertEqual(self.table.read_bytes(), original)
        self.launch.assert_called_once_with("fake")
        signals.fire_due(launcher=self.launched)
        signals.fire_due(launcher=self.launched)
        self.launch.assert_called_once_with("fake")
        self.assertEqual(signals.list_signals(), {"bell": {"action": "fake"}})
        self.assertEqual(signals._PENDING_CLEARS, set())

    def test_scheduler_skips_deleted_or_rescheduled_snapshot(self):
        when = "2030-01-01 06:59:00"
        signals.write_table({"bell": {"action": "fake", "time": when}})
        signals.set_signal("bell", when="2031")
        self.assertEqual(self.fire("bell", when)["error"], "stale time")
        self.launch.assert_not_called()
        signals.remove_signal("bell")
        self.assertEqual(self.fire("bell", when)["error"], "signal not found")


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
