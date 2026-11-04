"""Read-only all-computer listing, partial failures and optional host syntax."""
import contextlib
import io
import json
import threading
import unittest
from unittest.mock import patch

import hint
import hint_completion as completion
import hint_network as network


class ListAllTests(unittest.TestCase):
    def invoke(self, argv):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            status = hint.main(argv)
        return status, output.getvalue(), errors.getvalue()

    def test_both_option_orders_list_all_and_plain_list_stays_local(self):
        expected = {"local_host": "alder", "hosts": {"alder": {"definitions": {}}}}
        for argv in (["--list", "--remote"], ["--remote", "--list"]):
            with self.subTest(argv=argv), patch.object(network, "list_all", return_value=expected) as all_hosts:
                self.assertEqual(hint.run(argv), expected)
                all_hosts.assert_called_once_with()
        with patch.object(network, "list_all") as all_hosts, patch.object(hint, "list_signals", return_value={}):
            self.assertEqual(hint.run(["--list"]), {})
            all_hosts.assert_not_called()

    def test_explicit_host_is_still_one_direct_request(self):
        for argv in (["--list", "--remote", "beacon"], ["--remote=beacon", "--list"]):
            with self.subTest(argv=argv), patch.object(network, "remote", return_value={}) as remote, \
                    patch.object(network, "list_all") as all_hosts:
                self.assertEqual(hint.run(argv), {})
                remote.assert_called_once_with("beacon", ["--list"])
                all_hosts.assert_not_called()

    def test_bare_remote_cannot_broadcast_other_operations(self):
        for operation in (["--fire", "bell"], ["--fire", "--popup", "hello"],
                          ["--set", "bell", "--action", "true"], ["--stop", "bell"],
                          ["--pause", "bell"], ["--resume", "bell"], ["--remove", "bell"],
                          ["--get", "bell"], ["--status"], ["--discover"], ["--ready"]):
            for argv in (["--remote", *operation], [*operation, "--remote"]):
                with self.subTest(argv=argv), patch.object(hint, "execute") as execute:
                    status, output, errors = self.invoke(argv)
                    self.assertEqual(status, 2)
                    self.assertEqual(output, "")
                    self.assertIn("Supply a hostname after --remote", errors)
                    self.assertNotIn("True:", errors)
                    execute.assert_not_called()

    def test_empty_explicit_hostname_and_repeated_remote_remain_invalid(self):
        for argv in (["--list", "--remote="], ["--list", "--remote", ""],
                     ["--list", "--remote", "--remote"]):
            with self.subTest(argv=argv), patch.object(hint, "execute") as execute:
                self.assertEqual(self.invoke(argv)[0], 2)
                execute.assert_not_called()

    def test_network_commands_cannot_forward_an_all_host_list(self):
        for text in ("hint --list --remote", "hint --remote --list"):
            with self.subTest(text=text), patch.object(hint, "execute") as execute:
                self.assertIn("error", json.loads(hint.command_text(text)))
                execute.assert_not_called()

    def test_grouping_keeps_empty_hosts_and_remote_schedule_timezone(self):
        result = {"local_host": "alder", "hosts": {
            "beacon": {"definitions": {"bell": {"popup": "Hi", "time": "2027-02-10 09:00:00"}}},
            "alder": {"definitions": {}},
        }}
        with patch.object(network, "list_all", return_value=result):
            status, output, errors = self.invoke(["--remote", "--list"])
        self.assertEqual((status, errors), (0, ""))
        self.assertIn("alder\nNo saved actions.\n\nbeacon\n", output)
        self.assertIn("Popup: Hi", output)
        self.assertIn("2027-02-10 09:00:00 (target local)", output)

    def test_peer_failure_keeps_other_lists_and_has_nonzero_exit(self):
        result = {"local_host": "alder", "hosts": {
            "alder": {"definitions": {"error": {"action": "true"}}},
            "beacon": {"error": "No reply.\x1b[2J"},
        }}
        with patch.object(network, "list_all", return_value=result):
            status, output, errors = self.invoke(["--list", "--remote"])
        self.assertEqual(status, 1)
        self.assertIn("alder", output)
        self.assertIn("error", output)
        self.assertIn("beacon: No reply.", errors)
        self.assertNotIn("\x1b", output + errors)

    def test_discovery_failure_still_lists_local_actions(self):
        with patch.object(network, "hostname", return_value="alder"), \
                patch.object(hint, "list_signals", return_value={"bell": {"popup": "hello"}}), \
                patch.object(network, "_configured", return_value={}), \
                patch.object(network, "discover", side_effect=hint.HintError("NATS unavailable")), \
                patch.object(network, "remote") as remote:
            status, output, errors = self.invoke(["--list", "--remote"])
        self.assertEqual(status, 1)
        self.assertIn("bell", output)
        self.assertIn("Discovery: NATS unavailable", errors)
        remote.assert_not_called()

    def test_queries_each_discovered_peer_once_and_keeps_failures(self):
        def response(host, argv, timeout):
            self.assertEqual(argv, ["--list"])
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, 2)
            if host == "gone":
                raise hint.HintError("No reply from 'gone'.")
            if host == "broken":
                return {"bell": {"unexpected": 1}}
            return {"error": {"action": "true"}}

        with patch.object(network, "hostname", return_value="alder"), \
                patch.object(hint, "list_signals", return_value={}), \
                patch.object(network, "_configured", return_value={"request_timeout_seconds": 2}), \
                patch.object(network, "discover", return_value=["alder", "beacon", "beacon", "gone", "broken"]), \
                patch.object(network, "remote", side_effect=response) as remote:
            result = network.list_all()
        self.assertEqual(remote.call_count, 3)
        self.assertEqual(set(result["hosts"]), {"alder", "beacon", "gone", "broken"})
        self.assertEqual(result["hosts"]["beacon"], {"definitions": {"error": {"action": "true"}}})
        self.assertIn("No reply", result["hosts"]["gone"]["error"])
        self.assertIn("Invalid saved-action list", result["hosts"]["broken"]["error"])

    def test_local_read_failure_does_not_hide_peers(self):
        with patch.object(network, "hostname", return_value="alder"), \
                patch.object(hint, "list_signals", side_effect=hint.HintError("signals.json unreadable")), \
                patch.object(network, "_configured", return_value={}), \
                patch.object(network, "discover", return_value=["beacon"]), \
                patch.object(network, "remote", return_value={}):
            result = network.list_all()
        self.assertIn("unreadable", result["hosts"]["alder"]["error"])
        self.assertEqual(result["hosts"]["beacon"], {"definitions": {}})

    def test_peer_queries_are_parallel_bounded_and_share_one_deadline(self):
        barrier = threading.Barrier(8, timeout=2)
        clock = [0.0]

        def response(host, argv, timeout):
            self.assertEqual(timeout, 2)
            barrier.wait()
            clock[0] = 3.0
            return {}

        with patch.object(network, "hostname", return_value="alder"), \
                patch.object(hint, "list_signals", return_value={}), \
                patch.object(network, "_configured", return_value={"request_timeout_seconds": 2}), \
                patch.object(network, "discover", return_value=[f"peer{i}" for i in range(9)]), \
                patch.object(network.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(network, "remote", side_effect=response) as remote:
            result = network.list_all()
        self.assertEqual(remote.call_count, 8)
        self.assertEqual(sum("definitions" in value for value in result["hosts"].values()), 9)
        self.assertIn("deadline expired", result["hosts"]["peer8"]["error"])

    def test_completion_offers_list_after_bare_remote_without_network(self):
        with patch.object(network, "discover", side_effect=AssertionError("unnecessary network request")):
            self.assertEqual(completion.complete(["hint", "--remote", "--l"], 2), ["--list"])
            self.assertEqual(completion.complete(["hint", "--remote", "--f"], 2), [])
            self.assertEqual(completion.complete(["hint", "--list", "--r"], 2), ["--remote"])
        with patch.object(network, "discover", return_value=["alder", "beacon"]):
            self.assertEqual(completion.complete(["hint", "--list", "--remote", "b"], 3), ["beacon"])


if __name__ == "__main__":
    unittest.main()
