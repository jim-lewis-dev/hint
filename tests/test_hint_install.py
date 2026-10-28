"""Installation lifecycle checks with isolated files and fake system services.

No APT, RTC, sudoers or real user-service changes are made. Root test runners
use nobody when permitted, or simulate a nonroot identity in fixture copies
when the runtime's user namespace maps only UID zero.
"""
from __future__ import annotations

import json
import errno
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]
DRIVER = r"""import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

mode, arguments = sys.argv[1], sys.argv[2:]
root = Path(os.environ["HINT_TEST_ROOT"])
with (root / "calls.jsonl").open("a") as output:
    output.write(json.dumps([mode, arguments]) + "\n")
state = root / "service-active"
def wrapper(path, tool):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/bin/sh\nexec "$HINT_TEST_REAL_PYTHON" "$HINT_TEST_DRIVER" ' + tool + ' "$@"\n')
    path.chmod(0o755)
if mode == "python3" and arguments[:2] == ["-m", "venv"]:
    environment = Path(arguments[2])
    (environment / "pyvenv.cfg").write_text("home = /usr/bin\n")
    wrapper(environment / "bin/python", "environment-python")
elif mode == "python3":
    os.execv(os.environ["HINT_TEST_REAL_PYTHON"], [os.environ["HINT_TEST_REAL_PYTHON"], *arguments])
elif mode == "environment-python":
    if arguments[:3] != ["-m", "pip", "install"]:
        raise SystemExit("unexpected environment command")
    if os.environ.get("HINT_TEST_FAIL_PIP"):
        raise SystemExit(7)
    environment = Path(os.environ["XDG_DATA_HOME"]) / "hint/venv"
    wrapper(environment / "bin/hint", "hint")
elif mode == "hint":
    if arguments == ["--list"]:
        print((Path(os.environ["XDG_DATA_HOME"]) / "hint/signals.json").read_text())
    else:
        print("usage: hint")
elif mode == "systemctl":
    if "is-active" in arguments:
        raise SystemExit(0 if state.exists() else 3)
    if "stop" in arguments or "disable" in arguments:
        state.unlink(missing_ok=True)
    if "start" in arguments or "restart" in arguments:
        if "restart" in arguments and os.environ.get("HINT_TEST_FAIL_RESTART"):
            raise SystemExit(8)
        state.write_text("active")
elif mode == "sudo":
    if arguments[:1] == ["-n"]:
        arguments = arguments[1:]
    command, arguments = arguments[0], arguments[1:]
    if command == "apt-get":
        pass
    elif command == "visudo":
        assert "# HINT managed RTC access" in Path(arguments[-1]).read_text()
    elif command == "install":
        cleaned = []
        index = 0
        while index < len(arguments):
            if arguments[index] in ("-o", "-g"):
                index += 2
            else:
                cleaned.append(arguments[index])
                index += 1
        assert all(str(root) in item for item in cleaned[-2:])
        raise SystemExit(subprocess.call(["/usr/bin/install", *cleaned]))
    elif command.endswith("/hint-rtc"):
        assert str(root) in command
    elif command in ("test", "grep", "rm", "find"):
        assert any(str(root) in item for item in arguments)
        raise SystemExit(subprocess.call([shutil.which(command), *arguments]))
    else:
        raise SystemExit("unexpected sudo command: " + command)
else:
    raise SystemExit("unexpected tool: " + mode)
"""


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="hint-install-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "checkout with spaces"
        self.source.mkdir()
        self.profile = self.root / "user files"
        self.profile.mkdir()
        self.config_home = self.profile / "config"
        self.data_home = self.profile / "data"
        self.system = self.root / "system"
        for path in ("etc/sudoers.d", "usr/local/sbin"):
            (self.system / path).mkdir(parents=True)
        self.config = self.config_home / "hint/config.json"
        self.table = self.data_home / "hint/signals.json"
        self.environment = self.data_home / "hint/venv"
        self.command = self.profile / ".local/bin/hint"
        self.service = self.config_home / "systemd/user/hint.service"
        self.completion = self.data_home / "bash-completion/completions/hint"
        for path in PROJECT.glob("*.py"):
            shutil.copyfile(path, self.source / path.name)
        for name in ("pyproject.toml", "LICENSE", "hint.service", "hint", "config.example.json"):
            shutil.copyfile(PROJECT / name, self.source / name)
        shutil.copytree(PROJECT / "completions", self.source / "completions")
        for name in ("install.sh", "uninstall.sh"):
            text = (PROJECT / name).read_text()
            # Redirect all user/system destinations in the test copy only.
            text = text.replace("$HOME", "$HINT_TEST_HOME")
            text = text.replace("/etc/sudoers.d", str(self.system / "etc/sudoers.d"))
            text = text.replace("/usr/local/sbin/hint-rtc", str(self.system / "usr/local/sbin/hint-rtc"))
            (self.source / name).write_text(text)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.driver = self.root / "driver.py"
        self.driver.write_text(DRIVER)
        (self.root / "temporary").mkdir()
        for tool in ("sudo", "systemctl", "python3"):
            path = self.bin / tool
            path.write_text('#!/bin/sh\nexec "$HINT_TEST_REAL_PYTHON" "$HINT_TEST_DRIVER" ' + tool + ' "$@"\n')
            path.chmod(0o755)
        self.uid = os.geteuid()
        self.gid = os.getegid()
        self.identity = {}
        self.simulate_identity = False
        if self.uid == 0:
            account = pwd.getpwnam("nobody")
            try:
                os.chown(self.root, account.pw_uid, account.pw_gid)
            except OSError as error:
                if error.errno not in (errno.EINVAL, errno.EPERM):
                    raise
                self.simulate_identity = True
            else:
                self.uid, self.gid = account.pw_uid, account.pw_gid
                self.identity = {"user": self.uid, "group": self.gid, "extra_groups": []}
        self.user_name = pwd.getpwuid(self.uid).pw_name

    def run_script(self, name="install.sh", arguments=(), extra=None, root_user=False):
        script = self.source / name
        if self.simulate_identity and not root_user:
            text = script.read_text().replace('"$EUID"', '"$HINT_TEST_EUID"')
            script = self.source / (name + ".test-copy")
            script.write_text(text)
        if os.geteuid() == 0:
            for base, directories, files in os.walk(self.root):
                os.chown(base, self.uid, self.gid)
                for item in directories + files:
                    os.chown(Path(base) / item, self.uid, self.gid, follow_symlinks=False)
        environment = dict(os.environ)
        environment.update({
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "XDG_CONFIG_HOME": str(self.config_home),
            "XDG_DATA_HOME": str(self.data_home),
            "HINT_TEST_HOME": str(self.profile),
            "HINT_TEST_ROOT": str(self.root),
            "HINT_TEST_DRIVER": str(self.driver),
            "HINT_TEST_REAL_PYTHON": "/usr/bin/python3",
            "HINT_TEST_EUID": "1000",
            "PYTHONDONTWRITEBYTECODE": "1",
            "TMPDIR": str(self.root / "temporary"),
        })
        if extra:
            environment.update(extra)
        return subprocess.run(
            ["bash", str(script), *arguments],
            cwd=self.profile, env=environment, input="", text=True,
            capture_output=True, timeout=25,
            **({} if root_user else self.identity),
        )

    def calls(self):
        path = self.root / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def install(self, arguments=("--local",), **kwargs):
        result = self.run_script(arguments=arguments, **kwargs)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result


    def test_fresh_local_install_uses_user_paths_and_no_rtc_access(self):
        self.install()
        self.assertIsNone(json.loads(self.config.read_text())["nats_server"])
        self.assertEqual(json.loads(self.table.read_text()), {})
        self.assertEqual(self.command.resolve(), self.environment / "bin/hint")
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.table.stat().st_mode & 0o777, 0o600)
        unit = self.service.read_text()
        self.assertIn(str(self.environment / "bin/hint"), unit)
        self.assertNotIn(str(self.source), unit)
        self.assertIn("WorkingDirectory=%h", unit)
        self.assertIn("XDG_CONFIG_HOME=", unit)
        self.assertTrue(self.completion.is_file())
        self.assertFalse((self.data_home / "hint/wake-access").exists())
        self.assertTrue(any("git" in args for tool, args in self.calls() if tool == "sudo"))
        self.assertFalse(any("visudo" in args for tool, args in self.calls() if tool == "sudo"))

    def test_server_url_and_noninteractive_default(self):
        self.install(("--server", "nats://broker.example:4333"))
        self.assertEqual(json.loads(self.config.read_text())["nats_server"], "nats://broker.example:4333")

    def test_noninteractive_install_defaults_to_localhost(self):
        self.install(())
        self.assertEqual(json.loads(self.config.read_text())["nats_server"], "nats://127.0.0.1:4222")


    def test_reinstall_preserves_personal_data_and_reuses_environment(self):
        self.install()
        configuration = b'{"nats_server":"nats://own.example:4222","wake_lead_seconds":17}\n'
        table = b'{"personal":{"action":"/bin/true"}}\n'
        self.config.write_bytes(configuration)
        self.table.write_bytes(table)
        self.install(())
        self.assertEqual(self.config.read_bytes(), configuration)
        self.assertEqual(self.table.read_bytes(), table)
        venv_calls = [args for tool, args in self.calls() if tool == "python3" and args[:2] == ["-m", "venv"]]
        self.assertEqual(len(venv_calls), 1)

    def test_invalid_url_is_rejected_before_package_or_service_changes(self):
        result = self.run_script(arguments=("--server", "https://wrong.example"))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.config.exists())
        self.assertFalse(any(tool in ("sudo", "systemctl") for tool, args in self.calls()))

    def test_foreign_command_and_environment_are_preserved(self):
        self.command.parent.mkdir(parents=True)
        self.command.write_text("unrelated command")
        result = self.run_script(arguments=("--local",))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.command.read_text(), "unrelated command")
        self.assertFalse(self.calls())

    def test_foreign_environment_is_not_modified(self):
        self.environment.mkdir(parents=True)
        marker = self.environment / "foreign"
        marker.write_text("keep")
        result = self.run_script(arguments=("--local",))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(marker.read_text(), "keep")
        self.assertFalse(self.calls())

    def test_failed_update_restarts_service_and_preserves_data(self):
        self.install()
        configuration = self.config.read_bytes()
        table = b'{"personal":{"action":"/bin/true"}}\n'
        self.table.write_bytes(table)
        unit = self.service.read_bytes()
        before = len(self.calls())
        result = self.run_script(extra={"HINT_TEST_FAIL_PIP": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.root / "service-active").exists())
        self.assertEqual(self.service.read_bytes(), unit)
        self.assertEqual(self.config.read_bytes(), configuration)
        self.assertEqual(self.table.read_bytes(), table)
        calls = self.calls()[before:]
        stop = next(i for i, (tool, args) in enumerate(calls) if tool == "systemctl" and "stop" in args)
        pip = next(i for i, (tool, args) in enumerate(calls) if tool == "environment-python")
        self.assertLess(stop, pip)
        self.install(())

    def test_wake_access_is_installed_and_refreshed_on_update(self):
        self.install(("--local", "--enable-wake"))
        self.assertTrue((self.data_home / "hint/wake-access").exists())
        self.assertTrue((self.system / f"etc/sudoers.d/hint-rtc-{self.user_name}").is_file())
        before = len(self.calls())
        self.install(())
        self.assertTrue(any("visudo" in args for tool, args in self.calls()[before:] if tool == "sudo"))

    def test_existing_wake_signal_installs_required_wake_support(self):
        self.install()
        table = b'{"alarm":{"action":"/bin/true","time":"2030-01-01 07:00:00","wake":true}}\n'
        self.table.write_bytes(table)
        self.install(())
        self.assertEqual(self.table.read_bytes(), table)
        self.assertTrue((self.data_home / "hint/wake-access").exists())

    def test_foreign_service_is_not_replaced(self):
        self.service.parent.mkdir(parents=True)
        self.service.write_text("[Service]\nExecStart=/bin/true\n")
        result = self.run_script(arguments=("--local",))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.service.read_text(), "[Service]\nExecStart=/bin/true\n")
        self.assertFalse(self.calls())

    def test_uninstall_preserves_settings_and_signals(self):
        self.install()
        configuration, table = self.config.read_bytes(), self.table.read_bytes()
        before = len(self.calls())
        result = self.run_script("uninstall.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.config.read_bytes(), configuration)
        self.assertEqual(self.table.read_bytes(), table)
        for path in (self.command, self.service, self.completion, self.environment):
            self.assertFalse(path.exists(), str(path))
        self.assertFalse(any(tool == "sudo" for tool, args in self.calls()[before:]))

    def test_uninstall_keeps_shared_helper_for_other_users(self):
        self.install(("--local", "--enable-wake"))
        other_rule = self.system / "etc/sudoers.d/hint-rtc-someone_else"
        other_rule.write_text("# HINT managed RTC access\n")
        result = self.run_script("uninstall.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(other_rule.is_file())
        self.assertTrue((self.system / "usr/local/sbin/hint-rtc").is_file())
        self.assertFalse((self.system / f"etc/sudoers.d/hint-rtc-{self.user_name}").exists())
        self.assertFalse((self.data_home / "hint/wake-access").exists())
        self.assertTrue(self.table.is_file())

    @unittest.skipUnless(os.geteuid() == 0, "requires a root runner to verify root refusal")
    def test_install_and_uninstall_refuse_root_without_changes(self):
        for name in ("install.sh", "uninstall.sh"):
            arguments = ("--local",) if name == "install.sh" else ()
            result = self.run_script(name, arguments=arguments, root_user=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("without sudo", result.stderr)
        self.assertFalse(self.calls())


if __name__ == "__main__":
    unittest.main()
