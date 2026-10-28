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
        self.assertEqual(self.candidates("hint", "--r"), ["--remove", "--remote"])
        self.assertEqual(self.candidates("hint", "--set", "new_name", "--a"), ["--action"])
        self.assertEqual(self.candidates("hint", "--set", "new_name", "--h"), ["--help"])
        self.assertNotIn("--handler", self.candidates("hint", "--set", "new_name", ""))
        for operation in ("--get", "--fire", "--remove"):
            candidates = self.candidates("hint", operation, "name", "")
            self.assertFalse(set(candidates) & {"--action", "--time", "--wake"})
        self.assertEqual(self.candidates("hint", "--set", "new_name", "--action", "--f"), [])
        self.assertEqual(self.candidates("hint", "--set", "new_name", "--time", "2026-"), [])
        self.assertNotIn("--get", self.candidates("hint", "--set", "new_name", ""))

    def test_remote_discovery_and_remote_names(self):
        before = self.table.read_bytes()
        with patch.object(network, "discover", return_value=["hint_server_alder", "hint_server_birch"]):
            self.assertEqual(self.candidates("hint", "--remote", "hint_server_b"), ["hint_server_birch"])
        with patch.object(network, "remote", return_value={"remote_alarm": {"action": "unused"}}) as request:
            self.assertEqual(self.candidates("hint", "--remote", "hint_server_birch", "--get", "r"), ["remote_alarm"])
            request.assert_called_once_with("hint_server_birch", ["--list"])
        self.assertEqual(self.table.read_bytes(), before)

    def test_remote_after_cursor_still_selects_remote_names(self):
        with patch.object(network, "remote", return_value={"remote_alarm": {"action": "unused"}}) as request:
            words = ["hint", "--get", "r", "--remote=hint_server_birch"]
            self.assertEqual(completion.complete(words, 2), ["remote_alarm"])
            request.assert_called_once_with("hint_server_birch", ["--list"])

    def test_action_values_are_not_mistaken_for_remote_options(self):
        with patch.object(network, "remote", side_effect=AssertionError("network used")):
            words = ["hint", "--set", "br", "--action", "--remote=hint_server_other"]
            self.assertEqual(completion.complete(words, 2), ["breakfast"])

    def test_discovery_and_remote_are_not_combined(self):
        self.assertNotIn("--remote", self.candidates("hint", "--discover", ""))
        self.assertNotIn("--discover", self.candidates("hint", "--remote", "hint_server_birch", ""))

    def test_attached_values_and_bash_equals_wordbreaks(self):
        self.assertEqual(self.candidates("hint", "--get=st"), ["--get=stretch"])
        self.assertEqual(self.candidates("hint", "--get", "=", "st"), ["stretch"])
        self.assertEqual(self.candidates("hint", "--get", "="), ["bedtime", "breakfast", "stretch"])
        with patch.object(network, "discover", return_value=["hint_server_birch"]):
            self.assertEqual(self.candidates("hint", "--remote", "=", "hint_server_b"), ["hint_server_birch"])
        with patch.object(network, "remote", return_value={"remote_alarm": {"action": "unused"}}):
            words = ["hint", "--get", "=", "r", "--remote", "=", "hint_server_birch"]
            self.assertEqual(completion.complete(words, 3), ["remote_alarm"])

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
