"""Bounded live NATS clients and supervised worker lifecycle checks."""

import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import hint
import hint_network as network


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class NetworkTests(unittest.TestCase):
    def test_local_only_rejects_network_operations_without_starting_worker(self):
        with patch.object(hint, "config", return_value={"nats_server": None}), \
                patch.object(subprocess, "run", side_effect=AssertionError("worker started")):
            for operation in (network.discover, lambda: network.remote("test", ["--list"])):
                with self.assertRaisesRegex(hint.HintError, "disabled"):
                    operation()

    def test_unresponsive_tcp_server_cannot_hang_client_connection(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen()
        server.settimeout(0.1)
        stop = threading.Event()
        accepted = []

        def accept():
            while not stop.is_set():
                try:
                    connection, _ = server.accept()
                    accepted.append(connection)
                except socket.timeout:
                    pass
                except OSError:
                    return

        thread = threading.Thread(target=accept, daemon=True)
        thread.start()
        config = {"nats_server": f"nats://127.0.0.1:{server.getsockname()[1]}",
                  "request_timeout_seconds": 1.0}
        try:
            started = time.monotonic()
            with patch.object(hint, "config", return_value=config):
                with self.assertRaisesRegex(hint.HintError, "No reply|timed out"):
                    network.remote("test", ["--list"])
            self.assertLess(time.monotonic() - started, 2)
            self.assertTrue(accepted, "client never reached the unresponsive test server")
        finally:
            stop.set()
            server.close()
            thread.join(1)
            for connection in accepted:
                connection.close()

    def test_connection_error_redacts_broker_credentials(self):
        config = {"nats_server": "nats://alice:private-password@127.0.0.1:4222",
                  "request_timeout_seconds": 1}
        # Exercise an actual error envelope, independent of whether a live
        # connection error or the process deadline wins on a busy computer.
        result = subprocess.CompletedProcess(
            [], 1, json.dumps({"ok": False, "error": "cannot connect to " + config["nats_server"]}), "",
        )
        with patch.object(hint, "config", return_value=config), \
                patch.object(network.subprocess, "run", return_value=result):
            with self.assertRaises(hint.HintError) as raised:
                network.remote("test", ["--list"])
        self.assertNotIn("private-password", str(raised.exception))
        self.assertNotIn("alice", str(raised.exception))
        self.assertIn("[redacted]", str(raised.exception))
        self.assertIn("connect", str(raised.exception))

    def test_hostnames_route_directly_without_discovery(self):
        envelope = subprocess.CompletedProcess([], 0, json.dumps({"ok": True, "value": {}}), "")
        with patch.object(hint, "config", return_value={"nats_server": "nats://localhost:4222"}), \
                patch.object(network.subprocess, "run", return_value=envelope) as run, \
                patch.object(network, "discover", side_effect=AssertionError("discovery required")):
            self.assertEqual(network.remote("dirtsy", ["--list"]), {})
            body = json.loads(run.call_args.kwargs["input"])
            self.assertEqual(body["recipient"], "hint_server_dirtsy")
            self.assertEqual(body["argv"], ["--list"])

    def test_hostname_prefix_is_not_mistaken_for_an_internal_identity(self):
        self.assertEqual(network._recipient("hint_server_lab"), "hint_server_hint_server_lab")
        with patch.object(network, "_run_client", return_value={"error": "unrecognized arguments: --pause stretch"}) as run:
            reply = network.remote("hint_server_lab", ["--pause", "stretch"])
            self.assertEqual(run.call_args.kwargs["recipient"], "hint_server_hint_server_lab")
            self.assertEqual(reply["error"], "'hint_server_lab' does not support pause. Run hint --ready on that computer.")
        self.assertEqual(network._timeout_message({"recipient": "hint_server_hint_server_lab", "argv": ["--status"]}),
                         "No reply from 'hint_server_lab'. Check HINT on that computer.")

    def test_literal_action_options_do_not_change_timeout_or_preflight(self):
        for action in ("--list", "--get", "--status", "--set"):
            with self.subTest(action=action):
                argv = ["--fire", "--action", action]
                message = network._timeout_message({"recipient": "hint_server_dirtsy", "argv": argv})
                self.assertIn("may have taken effect", message)
                with patch.object(network, "_run_client", return_value={"fired": True}) as run:
                    self.assertEqual(network.remote("dirtsy", argv), {"fired": True})
                    run.assert_called_once_with("remote", recipient="hint_server_dirtsy", argv=argv, timeout=None)
        for argv in (["--get", "--fire"], ["--get=--fire"], ["--status=--set"]):
            self.assertEqual(network._timeout_message({"recipient": "hint_server_dirtsy", "argv": argv}),
                             "No reply from 'dirtsy'. Check HINT on that computer.")

    def test_partial_set_preflight_never_mutates_an_older_peer(self):
        with patch.object(hint, "config", return_value={"nats_server": "nats://localhost:4222"}), \
                patch.object(network, "_run_client", return_value={"error": "unrecognized arguments: --status"}) as run:
            result = network.remote("dirtsy", ["--set", "stretch", "--popup", "new"])
            self.assertIn("does not support partial updates", result["error"])
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.kwargs["argv"], ["--status"])

    def test_partial_set_preflight_and_mutation_share_one_deadline(self):
        replies = [{"op": "status", "capabilities": ["partial-set"]}, {"op": "set"}]
        with patch.object(hint, "config", return_value={"nats_server": "nats://localhost:4222", "request_timeout_seconds": 3}), \
                patch.object(network, "_run_client", side_effect=replies) as run, \
                patch.object(network.time, "monotonic", side_effect=[10.0, 11.5]):
            result = network.remote("dirtsy", ["--set", "stretch", "--popup", "new"])
            self.assertEqual(result, {"op": "set"})
            self.assertEqual(run.call_args_list[0].kwargs["timeout"], 3)
            self.assertEqual(run.call_args_list[1].kwargs["timeout"], 1.5)
            self.assertEqual(run.call_args_list[1].kwargs["argv"], ["--set", "stretch", "--popup", "new"])

    def test_completion_budget_overrides_long_configured_discovery(self):
        envelope = subprocess.CompletedProcess([], 0, json.dumps({"ok": True, "value": ["hint_server_dirtsy"]}), "")
        config = {"nats_server": "nats://localhost:4222", "discovery_timeout_seconds": 300}
        with patch.object(hint, "config", return_value=config), \
                patch.object(network.subprocess, "run", return_value=envelope) as run:
            self.assertEqual(network.discover(timeout=1.6), ["dirtsy"])
            self.assertLessEqual(run.call_args.kwargs["timeout"], 1.6)
            self.assertLessEqual(json.loads(run.call_args.kwargs["input"])["config"]["discovery_timeout_seconds"], 1.1)
        self.assertEqual(config["discovery_timeout_seconds"], 300)

    def test_supervisor_reaps_unresponsive_worker_and_stops_promptly(self):
        process = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"],
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            started = time.monotonic()
            with patch.object(network, "SERVER_START_TIMEOUT", 0.15):
                reason = network._supervise(process, threading.Event())
            self.assertIn("timed out", reason)
            network._stop_process(process)
            self.assertLess(time.monotonic() - started, 1)
            self.assertIsNotNone(process.poll())
        finally:
            network._stop_process(process)

    def test_local_only_server_does_not_spawn_child(self):
        with patch.object(hint, "config", return_value={"nats_server": None}), \
                patch.object(subprocess, "Popen", side_effect=AssertionError("worker started")):
            network.serve(threading.Event())


class LiveNetworkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        binary = os.environ.get("NATS_SERVER_BIN") or shutil.which("nats-server")
        if not binary:
            if os.environ.get("REQUIRE_NATS_SERVER"):
                raise RuntimeError("nats-server is required for live network tests")
            raise unittest.SkipTest("install nats-server or set NATS_SERVER_BIN")
        port = free_port()
        cls.server_url = f"nats://127.0.0.1:{port}"
        cls.process = subprocess.Popen([binary, "-a", "127.0.0.1", "-p", str(port)],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.addClassCleanup(cls.stop_server)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if cls.process.poll() is not None:
                raise RuntimeError("test NATS server exited")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.01)
        else:
            raise RuntimeError("test NATS server did not become ready")

    @classmethod
    def stop_server(cls):
        cls.process.terminate()
        try:
            cls.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            cls.process.kill()
            cls.process.wait(timeout=2)

    def setUp(self):
        from agent_messenger import AgentMessenger
        from nats_transport import NatsTransport
        self.responder = AgentMessenger("hint_server_test", NatsTransport(self.server_url))
        self.addCleanup(self.responder.close)
        self.received = []
        self.reply = {"remote_alarm": {"action": "unused"}}

        def answer(event):
            if event["type"] == "in_request":
                self.received.append(event["payload"])
                self.responder.respond(event["request_id"], json.dumps(self.reply))

        self.responder.listen(answer)
        self.config = {"nats_server": self.server_url, "request_timeout_seconds": 1,
                       "discovery_timeout_seconds": 0.1}
        patcher = patch.object(hint, "config", return_value=self.config)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_live_remote_preserves_existing_command_string_and_json_response(self):
        self.assertEqual(network.remote("test", ["--list"]), self.reply)
        self.assertEqual(self.received, ["hint --list"])
        self.reply = {"error": "unknown signal: missing"}
        self.assertEqual(network.remote("test", ["--get", "missing"]), self.reply)
        self.assertEqual(self.received[-1], "hint --get missing")

    def test_live_remote_popup_text_survives_transport_without_interpretation(self):
        message = 'Dinner! "Now"\n$HOME; $(touch unexpected); --set'
        self.reply = {"fired": True, "signal": "HINT-001"}
        self.assertEqual(network.remote("test", ["--fire", "--popup", message]), self.reply)
        self.assertEqual(shlex.split(self.received[-1]), ["hint", "--fire", "--popup", message])

    def test_live_hostname_with_internal_prefix_is_unambiguous(self):
        from agent_messenger import AgentMessenger
        from nats_transport import NatsTransport
        peer = AgentMessenger("hint_server_hint_server_lab", NatsTransport(self.server_url))
        self.addCleanup(peer.close)
        received = []

        def reply(event):
            if event["type"] == "in_request":
                received.append(event["payload"])
                peer.respond(event["request_id"], json.dumps({"on_lab": {"action": "unused"}}))

        peer.listen(reply)
        self.assertEqual(network.remote("hint_server_lab", ["--list"]), {"on_lab": {"action": "unused"}})
        self.assertEqual(received, ["hint --list"])
        self.assertIn("hint_server_lab", network.discover())

    def test_live_remote_shell_action_runs_only_on_receiving_computer(self):
        try:
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        except PermissionError:
            if os.environ.get("REQUIRE_LOCAL_IPC") == "1":
                self.fail("Unix sockets are required for the full service integration test")
            self.skipTest("environment prohibits Unix sockets; full service integration runs on Ubuntu CI")
        else:
            probe.close()
        # Use real CLI and server worker processes with separate homes. A
        # serialization-only test cannot catch local expansion or a wrong cwd.
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            sender = temporary / "sender home"
            receiver = temporary / "receiver home"
            launch_directory = temporary / "worker launch directory"
            launch_directory.mkdir()
            configuration = {"nats_server": self.server_url,
                             "request_timeout_seconds": 3}

            def environment(home):
                home.mkdir()
                settings = home / "config/hint/config.json"
                settings.parent.mkdir(parents=True)
                settings.write_text(json.dumps(configuration))
                return {**os.environ, "HOME": str(home),
                        "XDG_CONFIG_HOME": str(home / "config"),
                        "XDG_DATA_HOME": str(home / "data"),
                        "XDG_RUNTIME_DIR": str(home / "runtime"),
                        "PYTHONPATH": os.pathsep.join(filter(None, (
                            str(root), os.environ.get("PYTHONPATH"))))}

            sender_environment = environment(sender)
            receiver_environment = environment(receiver)
            output = temporary / "worker-output"
            errors = temporary / "worker-errors"
            with output.open("w") as stdout, errors.open("w") as stderr:
                worker = subprocess.Popen(
                    [sys.executable, "-u", "-m", "hint", "--serve"],
                    stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                    cwd=launch_directory, env=receiver_environment, text=True,
                )
                try:
                    deadline = time.monotonic() + 5
                    while "HINT listening on" not in output.read_text():
                        if worker.poll() is not None or time.monotonic() >= deadline:
                            self.fail("remote worker did not start: " + errors.read_text())
                        time.sleep(0.01)

                    def cli(*arguments):
                        result = subprocess.run(
                            [sys.executable, "-c", "import json,sys,hint; print(json.dumps(hint.run(sys.argv[1:])))", *arguments,
                             "--remote", network.hostname()],
                            cwd=sender, env=sender_environment, text=True,
                            capture_output=True, timeout=5,
                        )
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        return json.loads(result.stdout)

                    text = 'booyah "quoted" and apostrophe\'s'
                    action = (
                        "printf '%s\\n' " + shlex.quote(text)
                        + " | tr '[:lower:]' '[:upper:]' > \"$HOME/result with spaces\""
                        + " && printf '%s\\n' \"$PWD\" > relative-result"
                        + " && printf '%s\\n' '$HOME > | && literal'"
                        + " >> \"$HOME/result with spaces\""
                    )
                    expected = {"shell_test": {"action": action}}
                    self.assertEqual(cli("--set", "shell_test", "--action", action)["definition"], expected["shell_test"])
                    self.assertEqual(json.loads(
                        (receiver / "data/hint/signals.json").read_text()), expected)
                    self.assertFalse((receiver / "result with spaces").exists(),
                                     "setting a signal must not execute its command")
                    self.assertFalse((receiver / "relative-result").exists())
                    fired = cli("--fire", "shell_test")
                    self.assertNotIn("error", fired, fired)
                    self.assertTrue(fired["fired"])

                    expected_output = text.upper() + "\n$HOME > | && literal\n"
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline:
                        result_path = receiver / "result with spaces"
                        relative_path = receiver / "relative-result"
                        if (result_path.exists() and relative_path.exists()
                                and result_path.read_text() == expected_output):
                            break
                        time.sleep(0.01)
                    else:
                        self.fail("remote shell action did not finish: " + errors.read_text())
                    self.assertEqual(relative_path.read_text(), str(receiver) + "\n")
                    for other in (sender, launch_directory):
                        self.assertFalse((other / "result with spaces").exists())
                        self.assertFalse((other / "relative-result").exists())
                    self.assertFalse((sender / "data/hint/signals.json").exists())
                    unnamed = cli("--fire", "--action", "sleep 30")
                    self.assertTrue(unnamed.get("fired"), unnamed)
                    self.assertEqual(unnamed["signal"], "HINT-001")
                    cli("--pause", "HINT-001")
                    status = cli("--status", "HINT-001")
                    self.assertEqual(status["runs"][0]["state"], "paused")
                    cli("--resume", "HINT-001")
                    cli("--stop", "HINT-001")
                    status = cli("--status", "HINT-001")
                    self.assertFalse(any(run["active"] for run in status["runs"]))
                    cli("--set", "repeat", "--action", "sleep 30")
                    self.assertTrue(cli("--fire", "repeat").get("fired"))
                    self.assertTrue(cli("--fire", "repeat").get("fired"))
                    status = cli("--status", "repeat")
                    self.assertEqual(sum(run["active"] for run in status["runs"]), 2)
                    cli("--stop", "repeat")
                    self.assertFalse(any(run["active"] for run in cli("--status", "repeat")["runs"]))

                finally:
                    network._stop_process(worker)

    def test_live_discovery_filters_unrelated_agents(self):
        from agent_messenger import AgentMessenger
        from nats_transport import NatsTransport
        unrelated = AgentMessenger("other_agent", NatsTransport(self.server_url))
        self.addCleanup(unrelated.close)
        self.assertEqual(network.discover(), ["test"])

    def test_new_operation_on_old_peer_has_actionable_update_message(self):
        self.reply = {"error": "unrecognized arguments: --pause stretch"}
        reply = network.remote("test", ["--pause", "stretch"])
        self.assertEqual(reply["error"], "'test' does not support pause. Run hint --ready on that computer.")
        self.assertEqual(self.received, ["hint --pause stretch"])

    def test_remote_completion_reflects_additions_and_removals_immediately(self):
        import hint_completion
        words = ["hint", "--remote", "test", "--fire", "remote_"]
        self.assertEqual(hint_completion.complete(words, 4), ["remote_alarm"])
        self.reply = {"remote_new": {"action": "unused"}}
        self.assertEqual(hint_completion.complete(words, 4), ["remote_new"])
        self.reply = {}
        self.assertEqual(hint_completion.complete(words, 4), [])
        self.assertEqual(self.received, ["hint --list", "hint --list", "hint --list"])

    def test_malformed_remote_json_shape_is_an_error(self):
        self.reply = ["unexpected"]
        with self.assertRaisesRegex(hint.HintError, "invalid response"):
            network.remote("test", ["--list"])

    def test_completed_reply_survives_stalled_cleanup_without_repeating_command(self):
        worker = '''
import json,sys,time
import nats_transport
import hint_network
nats_transport.NatsTransport.close=lambda self: time.sleep(30)
raise SystemExit(hint_network._client_worker(json.load(sys.stdin)))
'''
        original_popen = subprocess.Popen
        children = []

        def start_process(command, *args, **kwargs):
            if "--client-worker" in command:
                command = [sys.executable, "-c", worker]
            process = original_popen(command, *args, **kwargs)
            children.append(process)
            return process

        self.config["request_timeout_seconds"] = 1.0
        started = time.monotonic()
        with patch.object(subprocess, "Popen", side_effect=start_process):
            self.assertEqual(network.remote("test", ["--fire", "alarm"]), self.reply)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(self.received, ["hint --fire alarm"])
        self.assertTrue(children and all(child.poll() is not None for child in children))

    def test_completion_uses_live_remote_discovery_and_names(self):
        import hint_completion
        self.assertEqual(hint_completion.complete(["hint", "--remote", "t"], 2),
                         ["test"])
        words = ["hint", "--get", "remote_", "--remote", "test"]
        self.assertEqual(hint_completion.complete(words, 2), ["remote_alarm"])
        self.assertEqual(self.received, ["hint --list"])

    def test_default_discovery_and_bash_completion_include_delayed_first_reply(self):
        # A slow first reply used to miss the 0.3s poll. Exercise the actual CLI
        # and Bash wrapper so a shorter shell watchdog cannot hide the fix.
        root = Path(__file__).resolve().parents[1]
        original_receive = self.responder._receive
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            config = temporary / "config/hint/config.json"
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({"nats_server": self.server_url}))
            environment = {**os.environ, "XDG_CONFIG_HOME": str(temporary / "config"),
                           "XDG_DATA_HOME": str(temporary / "data")}
            command = temporary / "hint"
            command.write_text(
                f"#!{sys.executable}\n"
                "import sys\n"
                f"sys.path.insert(0, {str(root)!r})\n"
                "from hint import main\n"
                "raise SystemExit(main())\n"
            )
            command.chmod(0o755)
            shell = '''
source "$1"
COMP_WORDS=("$2" --remote t)
COMP_CWORD=2
_hint
printf '%s\\n' "${COMPREPLY[@]}"
'''
            cases = (
                ("CLI", [sys.executable, "-c", "import json,hint_network; print(json.dumps(hint_network.discover()))"]),
                ("Bash completion", ["bash", "--noprofile", "--norc", "-c", shell,
                                     "test", str(root / "completions/hint.bash"), str(command)]),
            )
            for label, arguments in cases:
                with self.subTest(path=label):
                    queries = []

                    def delayed_receive(envelope, received_at):
                        if isinstance(envelope, dict) and envelope.get("type") == "discover":
                            queries.append(envelope["request_id"])
                            if len(queries) == 1:
                                time.sleep(0.45)
                        return original_receive(envelope, received_at)

                    with patch.object(self.responder, "_receive", side_effect=delayed_receive):
                        result = subprocess.run(arguments, cwd=root, env=environment,
                                                text=True, capture_output=True, timeout=5, check=True)
                    self.assertEqual(len(queries), 1, "first poll should find the delayed peer")
                    found = json.loads(result.stdout) if label == "CLI" else result.stdout.splitlines()
                    self.assertEqual(found, ["test"])
                    self.assertEqual(result.stderr, "")

    def test_server_recovers_after_its_native_connection_closes(self):
        # Close the first child's real native connection after startup. This
        # reaches the same terminal state as exhausted native reconnects,
        # without holding the test suite through the client's long retry window.
        first_worker = '''
import asyncio,json,sys,time
import hint_network as network
original=network._native_connection_closed
started=time.monotonic()
def close_connection(transport):
    if time.monotonic()-started > 0.2:
        asyncio.run_coroutine_threadsafe(transport._nc.close(), transport._loop).result(timeout=1)
    return original(transport)
network._native_connection_closed=close_connection
raise SystemExit(network._server_worker(json.load(sys.stdin)))
'''
        original_popen = subprocess.Popen
        workers = []

        def start_process(command, *args, **kwargs):
            is_server = "--server-worker" in command
            if is_server and not workers:
                command = [sys.executable, "-u", "-c", first_worker]
            process = original_popen(command, *args, **kwargs)
            if is_server:
                workers.append(process)
            return process

        stop = threading.Event()
        states = []
        thread = threading.Thread(target=network.serve, args=(stop,),
                                  kwargs={"state_callback": states.append}, daemon=True)
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {"XDG_CONFIG_HOME": directory + "/config",
                                        "XDG_DATA_HOME": directory + "/data"}), \
                patch.object(subprocess, "Popen", side_effect=start_process), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            thread.start()
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if len(workers) >= 2:
                        try:
                            reply = network.remote(network.hostname(), ["--list"])
                        except hint.HintError:
                            pass
                        else:
                            self.assertEqual(reply, {})
                            break
                    time.sleep(0.05)
                else:
                    self.fail("supervised server did not recover its remote list operation")
                self.assertIsNotNone(workers[0].poll())
            finally:
                stop.set()
                thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertIn("connected", states)
        self.assertIn("unavailable", states)
        self.assertTrue(all(worker.poll() is not None for worker in workers))


if __name__ == "__main__":
    unittest.main()
