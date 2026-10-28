"""Full CLI/service integration with isolated data and an optional local broker.

No installed service, hardware alarm, media player, or user signal is touched.
Set NATS_SERVER_BIN when nats-server is not on PATH. CI can require it with
REQUIRE_NATS_SERVER=1; the local-only service check needs no broker.
"""

from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from hint_network import server_name


PROJECT = Path(__file__).resolve().parents[1]


def can_inspect_processes():
    # Some test sandboxes expose host /proc inside a different PID namespace.
    # In that case numeric subprocess PIDs must never be looked up or signaled
    # through those host entries.
    try:
        return int(os.readlink("/proc/self")) == os.getpid()
    except (OSError, ValueError):
        return False


def child_pids(pid):
    """Include children spawned by any of the service's threads on Linux."""
    if not can_inspect_processes():
        return set()
    children = set()
    for path in Path(f"/proc/{pid}/task").glob("*/children"):
        try:
            children.update(int(value) for value in path.read_text().split())
        except FileNotFoundError:
            pass
    return children


class ServiceFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="hint-service-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.service = None
        self.workers = set()
        self.server_env = self.environment("server")
        self.client_env = self.environment("client")
        self.server_table = Path(self.server_env["XDG_DATA_HOME"]) / "hint/signals.json"
        self.client_table = Path(self.client_env["XDG_DATA_HOME"]) / "hint/signals.json"
        self.stdout_path = self.root / "service.stdout"
        self.stderr_path = self.root / "service.stderr"
        self.addCleanup(self.stop_service)

    def environment(self, name):
        environment = os.environ.copy()
        environment.update({
            "XDG_CONFIG_HOME": str(self.root / name / "config"),
            "XDG_DATA_HOME": str(self.root / name / "data"),
            "XDG_CACHE_HOME": str(self.root / name / "cache"),
            "PYTHONPATH": str(PROJECT),
            "PYTHONUNBUFFERED": "1",
        })
        return environment

    def configure(self, environment, broker):
        path = Path(environment["XDG_CONFIG_HOME"]) / "hint/config.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"nats_server": broker, "request_timeout_seconds": 1}), encoding="utf-8")

    def action(self, name):
        script = self.root / "write_marker.py"
        script.write_text("import pathlib, sys\npathlib.Path(sys.argv[1]).write_text('fired')\n", encoding="utf-8")
        marker = self.root / name
        return shlex.join([sys.executable, str(script), str(marker)]), marker

    def start_service(self, wrapper=None):
        command = ([sys.executable, "-u", "-m", "hint", "--serve"] if wrapper is None
                   else [sys.executable, "-u", "-c", wrapper])
        with self.stdout_path.open("w") as output, self.stderr_path.open("w") as errors:
            self.service = subprocess.Popen(command, env=self.server_env, cwd=self.root,
                                            stdin=subprocess.DEVNULL, stdout=output, stderr=errors)

    def stop_service(self):
        if self.service is None:
            return
        self.workers.update(child_pids(self.service.pid))
        if self.service.poll() is None:
            self.service.terminate()
            try:
                self.service.wait(timeout=4)
            except subprocess.TimeoutExpired:
                self.service.kill()
                self.service.wait(timeout=1)
        # Also clean up owned worker processes if a regression prevents normal
        # supervision from collecting them. Never target unrelated processes.
        for pid in self.workers:
            if Path(f"/proc/{pid}").exists():
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def assert_graceful_shutdown(self):
        self.workers.update(child_pids(self.service.pid))
        self.service.terminate()
        self.assertEqual(self.service.wait(timeout=4), 0, self.stderr_path.read_text())
        remaining = [pid for pid in self.workers if Path(f"/proc/{pid}").exists()]
        self.assertEqual(remaining, [], "service left its network worker running")

    def cli(self, *arguments, remote=False, check=True):
        command = [sys.executable, "-m", "hint"]
        if remote:
            command += ["--remote", server_name()]
        result = subprocess.run(command + list(arguments), env=self.client_env, cwd=self.root,
                                capture_output=True, text=True, timeout=2)
        self.assertEqual(result.stderr, "", result.stderr)
        try:
            value = json.loads(result.stdout)
        except ValueError:
            self.fail(f"CLI did not return JSON: {result.stdout!r}; stderr: {result.stderr!r}")
        if check:
            self.assertEqual(result.returncode, 0, value)
        return result.returncode, value

    def wait_for(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            if self.service is not None and self.service.poll() is not None:
                self.fail(f"service exited early: {self.stderr_path.read_text()}")
            time.sleep(0.02)
        self.fail(f"service did not complete operation: {self.stderr_path.read_text()}")


class LiveServiceTests(ServiceFixture):
    @classmethod
    def setUpClass(cls):
        binary = os.environ.get("NATS_SERVER_BIN") or shutil.which("nats-server")
        if not binary:
            if os.environ.get("REQUIRE_NATS_SERVER"):
                raise RuntimeError("nats-server is required for live service tests")
            raise unittest.SkipTest("install nats-server or set NATS_SERVER_BIN")
        with socket.socket() as candidate:
            candidate.bind(("127.0.0.1", 0))
            port = candidate.getsockname()[1]
        cls.broker_url = f"nats://127.0.0.1:{port}"
        cls.broker = subprocess.Popen([binary, "-a", "127.0.0.1", "-p", str(port)],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.addClassCleanup(cls.stop_broker)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if cls.broker.poll() is not None:
                raise RuntimeError("test NATS broker exited")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    return
            except OSError:
                time.sleep(0.01)
        raise RuntimeError("test NATS broker did not become ready")

    @classmethod
    def stop_broker(cls):
        if cls.broker.poll() is None:
            cls.broker.terminate()
            try:
                cls.broker.wait(timeout=1)
            except subprocess.TimeoutExpired:
                cls.broker.kill()
                cls.broker.wait(timeout=1)

    def test_real_service_remote_operations_clock_and_shutdown(self):
        self.configure(self.server_env, self.broker_url)
        self.configure(self.client_env, self.broker_url)
        self.client_table.parent.mkdir(parents=True)
        local_table = {"client_only": {"action": "unused"}}
        original_client_table = json.dumps(local_table).encode()
        self.client_table.write_bytes(original_client_table)
        self.start_service()

        # A successful real remote request establishes readiness all the way
        # through broker, transport, Messenger and HINT command processing.
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status, response = self.cli("--list", remote=True, check=False)
            if status == 0:
                self.assertEqual(response, {})
                break
            self.assertIsNone(self.service.poll(), self.stderr_path.read_text())
        else:
            self.fail(f"service did not accept remote requests: {self.stderr_path.read_text()}")
        self.workers.update(child_pids(self.service.pid))
        if can_inspect_processes():
            self.assertTrue(self.workers, "full service never created its network worker")

        timed_action, timed_marker = self.action("clock.marker")
        when = (datetime.now() + timedelta(seconds=2)).strftime("%Y-%m-%d %H:%M:%S")
        self.cli("--set", "clock", "--time", when, "--action", timed_action, remote=True)

        action, marker = self.action("remote.marker")
        expected = {"bell": {"action": action}}
        self.assertEqual(self.cli("--set", "bell", "--action", action, remote=True)[1], expected)
        self.assertEqual(self.cli("--get", "bell", remote=True)[1], expected)
        self.assertEqual(self.cli("--list", remote=True)[1]["bell"], expected["bell"])
        self.assertTrue(self.cli("--fire", "bell", remote=True)[1]["fired"])
        self.wait_for(marker.exists)
        self.assertTrue(self.cli("--remove", "bell", remote=True)[1]["removed"])
        status, error = self.cli("--fire", "missing", remote=True, check=False)
        self.assertNotEqual(status, 0)
        self.assertFalse(error["fired"])
        self.assertIn("error", error)
        self.assertEqual(self.client_table.read_bytes(), original_client_table)
        self.assertEqual(self.cli("--list")[1], local_table)

        self.wait_for(timed_marker.exists)
        self.wait_for(lambda: json.loads(self.server_table.read_text()) == {})
        self.assertEqual(self.cli("--list", remote=True)[1], {})
        self.assert_graceful_shutdown()


class LocalOnlyServiceTests(ServiceFixture):
    def test_startup_wake_failure_does_not_stop_the_local_clock(self):
        self.configure(self.server_env, None)
        action, marker = self.action("local-clock.marker")
        when = (datetime.now() + timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
        self.server_table.parent.mkdir(parents=True)
        self.server_table.write_text(json.dumps({"clock": {"action": action, "time": when}}))
        wrapper = """
import hint
from hint_config import HintError
def unavailable_wake():
    raise HintError('simulated unavailable RTC')
hint.arm_wake = unavailable_wake
raise SystemExit(hint.serve())
"""
        self.start_service(wrapper)
        self.wait_for(marker.exists)
        self.wait_for(lambda: json.loads(self.server_table.read_text()) == {})
        self.assertIn("simulated unavailable RTC", self.stderr_path.read_text())
        self.assertEqual(child_pids(self.service.pid), set())
        self.assert_graceful_shutdown()


if __name__ == "__main__":
    unittest.main()
