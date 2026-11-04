"""Completion uses the installed CLI and reads the correct local/remote table."""

import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import hint_config
import hint_completion as completion
import hint_network as network


ROOT = Path(__file__).resolve().parents[1]


class CompletionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.table = self.root / "signals.json"
        self.table.write_text(json.dumps({
            "breakfast": {"action": "breakfast.txt"},
            "bedtime": {"action": "bedtime.txt"},
            "stretch": {"action": "stretch.txt"},
        }))
        patcher = patch.object(hint_config, "TABLE", self.table)
        patcher.start()
        self.addCleanup(patcher.stop)

    def candidates(self, *words):
        return completion.complete(list(words), len(words) - 1)

    def test_existing_names_for_all_name_operations_without_network(self):
        with patch.object(network, "_run_client", side_effect=AssertionError("network used")):
            for operation in ("--set", "--fire", "--get", "--remove"):
                with self.subTest(operation=operation):
                    self.assertEqual(self.candidates("hint", operation, "b"), ["bedtime", "breakfast"])
            self.assertEqual(self.candidates("hint", "--set", "new_signal"), [])

    def test_options_only_offer_modifiers_for_set(self):
        self.assertEqual(self.candidates("hint", "--r"), ["--resume", "--remove", "--ready", "--remote"])
        self.assertEqual(self.candidates("hint", "--set", "new_name", "--a"), ["--action"])
        self.assertEqual(self.candidates("hint", "--set", "new_name", "--h"), ["--help"])
        self.assertNotIn("--handler", self.candidates("hint", "--set", "new_name", ""))
        for operation in ("--get", "--fire", "--remove"):
            candidates = self.candidates("hint", operation, "name", "")
            self.assertFalse(set(candidates) & {"--action", "--popup", "--time", "--wake"})
        self.assertEqual(self.candidates("hint", "--set", "new_name", "--action", "--f"), [])
        self.assertEqual(self.candidates("hint", "--set", "new_name", "--time", "2026-"), [])
        self.assertNotIn("--get", self.candidates("hint", "--set", "new_name", ""))

    def test_remote_discovery_and_remote_names(self):
        before = self.table.read_bytes()
        with patch.object(network, "discover", return_value=["alder", "birch"]):
            self.assertEqual(self.candidates("hint", "--remote", "b"), ["birch"])
        with patch.object(network, "remote", return_value={"remote_alarm": {"action": "unused"}}) as request:
            self.assertEqual(self.candidates("hint", "--remote", "birch", "--get", "r"), ["remote_alarm"])
            request.assert_called_once_with("birch", ["--list"], timeout=completion.COMPLETION_TIMEOUT)
        self.assertEqual(self.table.read_bytes(), before)

    def test_remote_after_cursor_still_selects_remote_names(self):
        with patch.object(network, "remote", return_value={"remote_alarm": {"action": "unused"}}) as request:
            words = ["hint", "--get", "r", "--remote=birch"]
            self.assertEqual(completion.complete(words, 2), ["remote_alarm"])
            request.assert_called_once_with("birch", ["--list"], timeout=completion.COMPLETION_TIMEOUT)

    def test_payload_values_are_not_mistaken_for_remote_options(self):
        with patch.object(network, "remote", side_effect=AssertionError("network used")):
            for option in ("--action", "--popup"):
                words = ["hint", "--set", "br", option, "--remote=other"]
                self.assertEqual(completion.complete(words, 2), ["breakfast"])

    def test_popup_completion_accepts_literal_text_without_suggestions(self):
        with patch.object(network, "remote", side_effect=AssertionError("network used")), \
                patch.object(network, "discover", side_effect=AssertionError("discovery used")):
            for suffix in (("--popup", ""), ("--popup", "--remote=other"),
                           ("--popup=",), ("--popup=hello",), ("--popup", "=", "")):
                with self.subTest(suffix=suffix):
                    self.assertEqual(self.candidates("hint", "--fire", *suffix), [])

    def test_popup_and_action_options_are_mutually_exclusive(self):
        for first, second in (("--popup", "--action"), ("--action", "--popup")):
            for operation in (("--set", "probe"), ("--fire",)):
                with self.subTest(first=first, operation=operation):
                    self.assertNotIn(second, self.candidates("hint", *operation, first, "text", ""))
                    self.assertNotIn(second, self.candidates("hint", *operation, first + "=text", ""))
        self.assertEqual(self.candidates("hint", "--set", "probe", "--p"), ["--popup"])
        self.assertEqual(self.candidates("hint", "--fire", "--p"), ["--popup"])

    def test_supplied_payload_does_not_offer_saved_fire_names(self):
        for option in ("--action", "--popup"):
            self.assertEqual(self.candidates("hint", option, "text", "--fire", ""), [])
            self.assertEqual(self.candidates("hint", option + "=text", "--fire="), [])

    def test_discovery_and_remote_are_not_combined(self):
        self.assertNotIn("--remote", self.candidates("hint", "--discover", ""))
        self.assertNotIn("--discover", self.candidates("hint", "--remote", "birch", ""))

    def test_attached_values_and_bash_equals_wordbreaks(self):
        self.assertEqual(self.candidates("hint", "--get=st"), ["--get=stretch"])
        self.assertEqual(self.candidates("hint", "--get", "=", "st"), ["stretch"])
        self.assertEqual(self.candidates("hint", "--get", "="), ["bedtime", "breakfast", "stretch"])
        with patch.object(network, "discover", return_value=["birch"]):
            self.assertEqual(self.candidates("hint", "--remote", "=", "b"), ["birch"])
        with patch.object(network, "remote", return_value={"remote_alarm": {"action": "unused"}}):
            words = ["hint", "--get", "=", "r", "--remote", "=", "birch"]
            self.assertEqual(completion.complete(words, 3), ["remote_alarm"])

    def test_incomplete_or_failed_remote_never_uses_local_names(self):
        for tail in (["--remote"], ["--remote", ""], ["--remote="], ["--remote", "=", ""]):
            with self.subTest(tail=tail):
                self.assertEqual(completion.complete(["hint", "--fire", "st", *tail], 2), [])
        with patch.object(network, "remote", side_effect=RuntimeError("offline")):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                completion.main(["2", "hint", "--fire", "st", "--remote", "missing"])
            self.assertEqual(output.getvalue(), "")

    def test_local_and_remote_changes_appear_on_next_tab(self):
        self.assertEqual(self.candidates("hint", "--get", "st"), ["stretch"])
        self.table.write_text('{"fresh":{"action":"/bin/true"}}')
        self.assertEqual(self.candidates("hint", "--get", "st"), [])
        self.assertEqual(self.candidates("hint", "--get", "fr"), ["fresh"])
        with patch.object(network, "remote", side_effect=[{"old": {}}, {"new": {}}, {}]) as remote:
            self.assertEqual(self.candidates("hint", "--remote", "birch", "--get", ""), ["old"])
            self.assertEqual(self.candidates("hint", "--remote", "birch", "--get", ""), ["new"])
            self.assertEqual(self.candidates("hint", "--remote", "birch", "--get", ""), [])
            self.assertEqual(remote.call_count, 3)

    def test_controls_include_live_runs_without_saved_definitions(self):
        status = {"definitions": {"stretch": {}}, "runs": [
            {"name": "removed_definition", "active": True},
            {"name": "HINT-001", "active": True},
            {"name": "finished", "active": False}
        ]}
        with patch("hint_runtime.request", return_value=status) as request:
            for operation in ("--pause", "--resume", "--stop"):
                self.assertEqual(self.candidates("hint", operation, ""), ["HINT-001", "removed_definition"])
            self.assertEqual(self.candidates("hint", "--status", ""),
                             ["HINT-001", "finished", "removed_definition", "stretch", "--remote"])
            self.assertTrue(all(call.kwargs["timeout"] == completion.COMPLETION_TIMEOUT
                                for call in request.call_args_list))
        with patch.object(network, "remote", return_value=status) as request:
            self.assertEqual(self.candidates("hint", "--remote", "birch", "--stop", "HINT-"), ["HINT-001"])
            request.assert_called_once_with("birch", ["--status"], timeout=completion.COMPLETION_TIMEOUT)

    def test_status_still_completes_saved_names_when_service_is_stopped(self):
        with patch("hint_runtime.request", side_effect=hint_config.HintError("HINT service is not running. Run hint --ready.")):
            self.assertEqual(self.candidates("hint", "--status", "st"), ["stretch"])
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                completion.main(["2", "hint", "--stop", "st"])
            self.assertEqual(output.getvalue(), "")

    def test_optional_values_offer_switches_only_as_separate_arguments(self):
        self.assertEqual(self.candidates("hint", "--fire", ""),
                         ["bedtime", "breakfast", "stretch", "--action", "--popup"])
        self.assertNotIn("--fire=--action", self.candidates("hint", "--fire="))
        self.assertNotIn("--fire=--popup", self.candidates("hint", "--fire="))
        with patch("hint_runtime.request", return_value={"definitions": {"stretch": {}}, "runs": []}):
            self.assertEqual(self.candidates("hint", "--status", ""), ["stretch", "--remote"])
            self.assertEqual(self.candidates("hint", "--status="), ["--status=stretch"])
        with patch.object(network, "remote", return_value={"definitions": {"stretch": {}}, "runs": []}):
            self.assertEqual(self.candidates("hint", "--remote", "birch", "--status", ""), ["stretch"])

    def test_new_modifiers_nameless_fire_and_ready_context(self):
        self.assertIn("--action", self.candidates("hint", "--fire", "--a"))
        self.assertIn("--action", self.candidates("hint", "--fire", ""))
        self.assertNotIn("--action", self.candidates("hint", "--fire", "stretch", ""))
        self.assertNotIn("--time", self.candidates("hint", "--set", "stretch", "--no-time", ""))
        self.assertNotIn("--wake", self.candidates("hint", "--set", "stretch", "--no-wake", ""))
        self.assertNotIn("--no-wake", self.candidates("hint", "--set", "stretch", "--wake", ""))
        self.assertNotIn("--remote", self.candidates("hint", "--ready", ""))
        self.assertIn("--server", self.candidates("hint", "--ready", ""))
        self.assertNotIn("--ready", self.candidates("hint", "--remote", "birch", ""))

    def test_missing_or_broken_data_and_offline_errors_are_quiet(self):
        output, errors = io.StringIO(), io.StringIO()
        self.table.write_text("broken json")
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            self.assertEqual(completion.main(["2", "hint", "--get", ""]), 0)
            self.assertEqual(completion.main([]), 0)
            self.assertEqual(completion.main(["oops"]), 0)
            with patch.object(network, "discover", side_effect=RuntimeError("offline")):
                self.assertEqual(completion.main(["2", "hint", "--remote", ""]), 0)
        self.assertEqual(output.getvalue() + errors.getvalue(), "")
        self.table.unlink()
        self.assertEqual(self.candidates("hint", "--get", ""), [])

    def test_shell_calls_installed_command_through_symlink_with_spaces(self):
        project = self.root / "installed app"
        project.mkdir()
        command = project / "hint"
        command.write_text(
            f"#!{sys.executable}\n"
            "import sys\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "from pathlib import Path\n"
            "import hint_config\n"
            f"hint_config.TABLE = Path({str(self.table)!r})\n"
            "from hint_completion import main\n"
            "raise SystemExit(main(sys.argv[2:]))\n"
        )
        command.chmod(0o755)
        launcher = self.root / "hint"
        launcher.symlink_to(command)
        script = '''
source "$1"
complete -p hint >/dev/null || exit 1
COMP_WORDS=("$2" --get sh)
COMP_CWORD=2
_hint
printf '%s\\n' "${COMPREPLY[@]}"
'''
        self.table.write_text('{"shell_signal":{"action":"unused"}}')
        result = subprocess.run(["bash", "--noprofile", "--norc", "-c", script,
                                 "test", str(ROOT / "completions/hint.bash"), str(launcher)],
                                text=True, capture_output=True, check=True)
        self.assertEqual(result.stdout, "shell_signal\n")
        self.assertEqual(result.stderr, "")

    def test_shell_bounds_stalled_installed_command(self):
        command = self.root / "hint"
        command.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")
        command.chmod(0o755)
        script = '''
source "$1"
COMP_WORDS=("$2" --remote "")
COMP_CWORD=2
_hint
'''
        started = time.monotonic()
        result = subprocess.run(["bash", "--noprofile", "--norc", "-c", script,
                                 "test", str(ROOT / "completions/hint.bash"), str(command)],
                                text=True, capture_output=True, timeout=4, check=True)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(result.stdout + result.stderr, "")


if __name__ == "__main__":
    unittest.main()
