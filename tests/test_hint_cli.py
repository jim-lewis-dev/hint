"""CLI presentation, literal remote routing, and validation without side effects."""
import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import hint
from hint_config import HintError, sanitize_error, validate_config


class CliTests(unittest.TestCase):
    def invoke(self, argv):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            status = hint.main(argv)
        return status, output.getvalue(), errors.getvalue()

    def test_partial_remote_updates_preserve_literal_arguments(self):
        for argv in (
            ['--remote', 'dibbles', '--set', 'bell', '--action', '~/Music/a b.wav'],
            ['--set', 'bell', '--action', '~/Music/a b.wav', '--remote=dibbles']
        ):
            with self.subTest(argv=argv), patch('hint_network.remote', return_value={}) as remote, patch('hint.set_signal') as local:
                hint.run(argv)
                remote.assert_called_once_with('dibbles', ['--set', 'bell', '--action', '~/Music/a b.wav'])
                local.assert_not_called()
        with patch('hint_network.remote', return_value={}) as remote:
            hint.run(['--set', 'bell', '--no-time', '--no-wake', '--remote', 'dibbles'])
            remote.assert_called_once_with('dibbles', ['--set', 'bell', '--no-time', '--no-wake'])

    def test_remote_roundtrip_preserves_leading_dash_values_and_popup(self):
        def reply(recipient, argv):
            return json.loads(hint.command_text(shlex.join(['hint', *argv])))
        value = {'op':'set', 'name':'-bell', 'created':True, 'changed':['action'], 'definition':{'action':'--help'}}
        with patch('hint_network.remote', side_effect=reply), patch('hint.set_signal', return_value=value) as saved:
            self.assertEqual(hint.run(['--remote=peer', '--set=-bell', '--action=--help']), value)
            saved.assert_called_once_with('-bell', action='--help', popup=None, when=None, wake=None, no_time=False)
        with patch('hint_network.remote', side_effect=reply), patch('hint_runtime.request', return_value={'fired':True,'signal':'HINT-001'}) as runtime:
            message = 'Dinner! "Now"; $HOME <script>\n$(touch unexpected)'
            hint.run(['--remote=peer', '--fire', '--popup', message])
            runtime.assert_called_once_with({'op':'fire','name':None,'popup':message})

    def test_remote_saved_popup_preserves_literal_text_and_schedule(self):
        message = '--help! "Now"; $HOME\n$(touch unexpected)'
        with patch('hint_network.remote', return_value={}) as remote:
            hint.run(['--set', 'dinner', '--popup', message, '--time', '2031', '--remote', 'dibbles'])
            remote.assert_called_once_with('dibbles', ['--set', 'dinner', '--popup=' + message, '--time', '2031'])

    def test_popup_and_action_use_distinct_runtime_requests(self):
        with patch('hint_runtime.request', return_value={'fired': True}) as runtime:
            hint.run(['--fire', '--popup=--help'])
            runtime.assert_called_once_with({'op': 'fire', 'name': None, 'popup': '--help'})
        with patch('hint_runtime.request', return_value={'fired': True}) as runtime:
            hint.run(['--fire', '--action', 'POPUP:hello'])
            runtime.assert_called_once_with({'op': 'fire', 'name': None, 'action': 'POPUP:hello'})

    def test_invalid_commands_never_execute(self):
        for argv in (
            ['--remot=peer','--fire','bell'], ['--remote'], ['--remote='],
            ['--remote=a','--remote=b','--fire','bell'], ['--remote=a','--discover'],
            ['--remote=a','--serve'], ['--remote=a','--ready'],
            ['--fire','bell','--action','true'], ['--fire'], ['--fire','bell','--no-time'],
            ['--set','bell'], ['--set','bell','--time','2027','--no-time'],
            ['--set','bell','--wake','--no-wake'], ['--set','bell','--action',''],
            ['--set','bell','--popup','  '], ['--list','--server','nats://host:4222'],
            ['--list','--handler','true'], ['--fire','bell','--fire','other'],
            ['--set','bell','--action','a','--action','b'],
            ['--fire','--action','true','--popup','hello'],
            ['--set','bell','--popup','hello','--action','true'],
            ['--fire','bell','--popup','hello'], ['--fire','--popup',''],
            ['--list','--popup','hello'], ['--fire','--popup','a','--popup','b']
        ):
            with self.subTest(argv=argv), patch('hint.execute') as execute:
                status, output, errors = self.invoke(argv)
                self.assertEqual(status, 2, (output,errors))
                self.assertTrue(errors)
                self.assertEqual(output, '')
                execute.assert_not_called()

    def test_saved_and_temporary_fire_have_only_their_public_names(self):
        with patch('hint_runtime.request', return_value={'signal':'stretch','run':'stretch','fired':True}):
            status, output, errors = self.invoke(['--fire','stretch'])
            self.assertEqual(status,0)
            self.assertIn("Fired 'stretch'.",output)
            self.assertNotIn('(',output)
            self.assertEqual(errors,'')
        with patch('hint_runtime.request', return_value={'signal':'HINT-001','fired':True}):
            status, output, errors = self.invoke(['--fire','--popup','Dinner is ready!'])
            self.assertEqual(status,0)
            self.assertIn('Fired HINT-001.',output)
            self.assertEqual(errors,'')

    def test_set_changes_only_supplied_attributes(self):
        with patch('hint.set_signal', return_value={'definition':{'action':'true'},'created':False,'changed':['time']}) as saved:
            status, output, errors = self.invoke(['--set','bell','--no-time'])
            saved.assert_called_once_with('bell',action=None,popup=None,when=None,wake=None,no_time=True)
            self.assertEqual(status,0)
            self.assertIn('schedule cleared',output)

    def test_remote_errors_use_stderr_and_nonzero_exit(self):
        with patch('hint_network.remote', return_value={'error':'No reply; request may have run.'}):
            status, output, errors = self.invoke(['--fire','bell','--remote','dirtsy'])
            self.assertEqual(status,1)
            self.assertEqual(output,'')
            self.assertTrue(errors.startswith('dirtsy:'))

    def test_definition_named_error_is_valid_data(self):
        with patch('hint.list_signals', return_value={'error':{'action':'true'}}):
            status, output, errors = self.invoke(['--list'])
            self.assertEqual(status,0)
            self.assertIn('error',output)
            self.assertEqual(errors,'')

    def test_wire_always_json_and_rejects_lifecycle_or_forwarding(self):
        for text in ('hint --help','hint --serve','hint --ready','hint --discover','hint --remote=a --list','other --list'):
            with self.subTest(text=text), patch('hint.execute') as execute:
                self.assertIn('error',json.loads(hint.command_text(text)))
                execute.assert_not_called()
        with patch('hint.list_signals', return_value={'bell':{'action':'true'}}):
            self.assertEqual(json.loads(hint.command_text('hint --list')),{'bell':{'action':'true'}})

    def test_status_displays_multiple_same_named_runs_and_temporary_names(self):
        value = {'service':'running','host':'dirtsy','network':'connected','definitions':{},'runs':[
            {'name':'music','state':'paused','detail':'song.mp3'},
            {'name':'music','state':'running','detail':'song.mp3'},
            {'name':'HINT-001','state':'finished','exit_code':0}
        ]}
        with patch('hint_runtime.request', return_value=value):
            status, output, errors = self.invoke(['--status'])
            self.assertEqual(status,0)
            self.assertEqual(output.count('music'),2)
            self.assertIn('HINT-001',output)
            self.assertIn('exit code 0',output)

    def test_partial_control_failure_is_not_success(self):
        with patch('hint_runtime.request', return_value={'count':1,'failed':[{'name':'music','reason':'process refused'}]}):
            status, output, errors = self.invoke(['--stop','music'])
            self.assertEqual(status,1)
            self.assertIn('1 failed',errors)
            self.assertIn('process refused',errors)

    def test_repeated_stop_reports_pending_cleanup(self):
        with patch('hint_runtime.request', return_value={'count':0,'already':1,'pending':1}):
            status, output, errors = self.invoke(['--stop','music'])
            self.assertEqual(status,0)
            self.assertIn('1 already stopping',output)
            self.assertEqual(errors,'')

    def test_terminal_control_characters_are_not_rendered(self):
        with patch('hint.list_signals', return_value={'bell':{'action':'hello\x1b[2J\nworld'}}):
            status, output, errors = self.invoke(['--list'])
            self.assertEqual(status,0)
            self.assertNotIn('\x1b',output)

    def test_remote_timezone_is_rendered_from_receiving_computer(self):
        value = {'definition':{'action':'true','time':'2027-02-10 09:00:00'},
                 'created':False,'changed':['time'],'time_display':'2027-02-10 09:00:00 JST'}
        with patch('hint_network.remote', return_value=value):
            status, output, errors = self.invoke(['--set','bell','--time','2027-02-10 09','--remote','tokyo'])
            self.assertEqual(status,0)
            self.assertIn('09:00:00 JST',output)
        with patch('hint_network.remote', return_value={'bell':value['definition']}):
            status, output, errors = self.invoke(['--list','--remote','tokyo'])
            self.assertIn('(target local)',output)

    def test_unsupported_control_is_not_a_success(self):
        with patch('hint_runtime.request', return_value={'count':0,'unsupported':1,'failed':[]}):
            status, output, errors = self.invoke(['--pause','popup'])
            self.assertEqual(status,1)
            self.assertIn('unsupported',errors)

    def test_graphical_session_refresh_returns_success_not_a_status_object(self):
        with patch('hint_ready.session', return_value={'service':'running'}) as session:
            status, output, errors = self.invoke(['--session'])
            self.assertEqual((status,output,errors),(0,'',''))
            session.assert_called_once_with()
        self.assertIn('error', json.loads(hint.command_text('hint --session')))

    def test_ready_delegates_setup_flags(self):
        with patch('hint_ready.main', return_value=0) as ready:
            status, _, _ = self.invoke(['--ready','--server','nats://host:4222','--enable-wake'])
            self.assertEqual(status,0)
            ready.assert_called_once_with(['--server','nats://host:4222','--enable-wake'])

    def test_help_and_empty_command_show_current_interface(self):
        result = subprocess.run([sys.executable,'-m','hint','--help'],capture_output=True,text=True,check=True)
        for option in ('--remote [HOST]','--ready','--pause','--no-time','--no-wake','--popup TEXT'):
            self.assertIn(option,result.stdout)
        self.assertNotIn('popup:TEXT', result.stdout)
        self.assertNotIn('--handler',result.stdout)
        status, output, errors = self.invoke([])
        self.assertEqual(status,0)
        self.assertIn('usage:',output)


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
