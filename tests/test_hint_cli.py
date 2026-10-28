"""Routing and JSON behavior, isolated from the user's files and actions."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import hint
from hint_config import HintError, sanitize_error, validate_config


class CliTests(unittest.TestCase):
    def invoke(self, argv):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = hint.main(argv)
        return status, json.loads(output.getvalue())

    def test_remote_options_have_one_route_and_preserve_action_quoting(self):
        for argv in (
            ['--remote', 'hint_server_dibbles', '--set', 'bell', '--action', '~/Music/a b.wav'],
            ['--set', 'bell', '--action', '~/Music/a b.wav', '--remote=hint_server_dibbles'],
        ):
            with self.subTest(argv=argv), patch('hint_network.remote', return_value={'bell': {'action': '~/Music/a b.wav'}}) as remote, patch('hint.set_signal') as local:
                status, result = self.invoke(argv)
                self.assertEqual(status, 0)
                remote.assert_called_once_with('hint_server_dibbles', ['--set', 'bell', '--action', '~/Music/a b.wav'])
                local.assert_not_called()
                self.assertIn('bell', result)

    def test_remote_roundtrip_preserves_leading_dash_names_and_action_values(self):
        import shlex

        def reply(recipient, argv):
            return json.loads(hint.command_text(shlex.join(['hint', *argv])))

        with patch('hint_network.remote', side_effect=reply), patch('hint.set_signal', return_value={'-bell': {'action': '--help'}}) as set_signal:
            status, result = self.invoke(['--remote=peer', '--set=-bell', '--action=--help'])
            self.assertEqual(status, 0)
            set_signal.assert_called_once_with('-bell', '--help', None, False)
        with patch('hint_network.remote', side_effect=reply), patch('hint.get_signal', return_value={'--help': None}) as get_signal:
            status, result = self.invoke(['--remote=peer', '--get=--help'])
            self.assertEqual(status, 0)
            get_signal.assert_called_once_with('--help')

    def test_invalid_remote_routing_never_fires_locally_or_remotely(self):
        for argv in (
            ['--remot=hint_server_dibbles', '--fire', 'bell'],
            ['--remote'], ['--remote='],
            ['--remote=a', '--remote=b', '--fire', 'bell'],
            ['--remote=a', '--discover'], ['--remote=a', '--serve'],
            ['--fire', 'bell', '--action', '/bin/true'],
        ):
            with self.subTest(argv=argv), patch('hint.fire') as fire, patch('hint_network.remote') as remote:
                status, result = self.invoke(argv)
                self.assertEqual(status, 2)
                self.assertIn('error', result)
                fire.assert_not_called()
                remote.assert_not_called()

    def test_remote_error_sets_failure_exit_status(self):
        with patch('hint_network.remote', return_value={'signal': 'missing', 'error': 'signal not found', 'fired': False}):
            status, result = self.invoke(['--remote=a', '--fire', 'missing'])
            self.assertEqual(status, 1)
            self.assertFalse(result['fired'])

    def test_only_action_option_is_accepted_without_mutating_signals(self):
        for argv in (
            ['--set', 'bell', '--handler', '/bin/true'],
            ['--remote=peer', '--set', 'bell', '--handler=/bin/true'],
            ['--set', 'bell', '--action', '/bin/true', '--handler', '/bin/false'],
        ):
            with self.subTest(argv=argv), patch('hint.set_signal') as set_signal, patch('hint_network.remote') as remote:
                status, result = self.invoke(argv)
                self.assertEqual(status, 2)
                self.assertIn('error', result)
                set_signal.assert_not_called()
                remote.assert_not_called()

    def test_signal_named_error_is_not_an_operation_error(self):
        with patch('hint.list_signals', return_value={'error': {'action': '/bin/true'}}):
            status, result = self.invoke(['--list'])
            self.assertEqual(status, 0)
            self.assertIn('error', result)

    def test_remote_callback_always_replies_and_does_not_forward_operations(self):
        for text in ('hint --help', 'hint --serve', 'hint --serve=true', 'hint --discover', 'hint --remote=a --list', 'hint --remot=a --fire bell', 'other --list'):
            with self.subTest(text=text), patch('hint.fire') as fire:
                self.assertIn('error', json.loads(hint.command_text(text)))
                fire.assert_not_called()
        with patch('hint.list_signals', side_effect=OSError('disk unavailable')):
            self.assertIn('disk unavailable', json.loads(hint.command_text('hint --list'))['error'])

    def test_installed_style_cli_uses_xdg_paths_not_the_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = {**os.environ, 'HOME': str(root), 'XDG_CONFIG_HOME': str(root/'config'), 'XDG_DATA_HOME': str(root/'data')}
            expected = {'bell': {'action': '/bin/true'}}
            for argv, reply in (
                (['--set', 'bell', '--action', '/bin/true'], expected),
                (['--get', 'bell'], expected),
                (['--list'], expected),
                (['--remove', 'bell'], {'signal': 'bell', 'removed': True}),
            ):
                result = subprocess.run([sys.executable, '-m', 'hint', *argv], env=env, text=True, capture_output=True, timeout=3)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(json.loads(result.stdout), reply)
                if argv[0] == '--set':
                    self.assertEqual(json.loads((root/'data/hint/signals.json').read_text()), expected)
            self.assertEqual(json.loads((root/'data/hint/signals.json').read_text()), {})
            self.assertEqual((root/'data/hint/signals.json').stat().st_mode & 0o777, 0o600)

    def test_help_explains_remote_and_discovery(self):
        result = subprocess.run([sys.executable, '-m', 'hint', '--help'], capture_output=True, text=True, check=True)
        self.assertIn('--remote SERVER', result.stdout)
        self.assertIn('--discover', result.stdout)
        self.assertIn('--action STRING', result.stdout)
        self.assertNotIn('--handler', result.stdout)


class ConfigurationTests(unittest.TestCase):
    def test_local_only_and_invalid_configuration(self):
        self.assertIsNone(validate_config({'nats_server': None})['nats_server'])
        for value in ({'nats_server': 'example.com'}, {'nats_server': 3}, {'nats_server':'nats://host:0'}, {'wake_lead_seconds': True}, {'request_timeout_seconds': 0}, {'discovery_timeout_seconds': float('nan')}, {'unknown': 1}):
            with self.subTest(value=value), self.assertRaises(HintError):
                validate_config(value)

    def test_credentials_are_removed_from_diagnostics(self):
        result = sanitize_error('cannot connect to nats://alice:private-password@localhost:4222: refused')
        self.assertNotIn('alice', result)
        self.assertNotIn('private-password', result)
        self.assertIn('localhost:4222', result)


if __name__ == '__main__':
    unittest.main()
