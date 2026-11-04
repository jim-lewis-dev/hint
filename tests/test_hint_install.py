"""Staged installation, rollback and source-update tests with no system changes."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import hint_ready
from hint_ready import Paths, ReadyError

PROJECT = Path(__file__).resolve().parents[1]


def options(**values):
    return argparse.Namespace(server=None, local=False, enable_wake=False, **values)


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='hint-install-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'source with spaces'
        self.source.mkdir()
        for name in ('pyproject.toml', 'hint.service', 'rtc_helper.py', 'hint_ready.py', 'install.sh', 'hint'):
            shutil.copyfile(PROJECT / name, self.source / name)
        shutil.copytree(PROJECT / 'completions', self.source / 'completions')
        self.paths = Paths(self.root / 'home', self.root / 'config', self.root / 'data')
        self.paths.home.mkdir()
        self.calls = []
        self.active = False
        self.enabled = False
        self.busy = 0
        self.fail_pip = False
        self.fail_restart = False
        self.fail_probe = False
        self.previous_environment = None
        self.session_environments = []
        self.output = io.StringIO()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('hint_ready.os.geteuid', return_value=1000))
        self.stack.enter_context(patch('hint_ready._run', side_effect=self.run_tool))
        self.stack.enter_context(patch('hint_ready._runtime', side_effect=self.runtime))
        self.stack.enter_context(contextlib.redirect_stdout(self.output))
        self.stack.enter_context(contextlib.redirect_stderr(self.output))

    def run_tool(self, argv, **kwargs):
        args = [str(value) for value in argv]
        self.calls.append(args)
        code, stdout, stderr = 0, '', ''
        if args[:3] == ['python3', '-m', 'venv']:
            environment = Path(args[3])
            (environment / 'bin').mkdir()
            (environment / 'bin/python').write_text('python\n')
        elif len(args) > 3 and args[1:4] == ['-m', 'pip', 'install']:
            if self.fail_pip:
                code, stderr = 7, 'package preparation failed'
            else:
                executable = Path(args[0]).parent / 'hint'
                executable.write_text('#!' + args[0] + '\n')
                executable.chmod(0o755)
        elif args[:2] == ['systemctl', '--user']:
            if 'is-active' in args:
                code = 0 if self.active else 3
            elif 'is-enabled' in args:
                code = 0 if self.enabled else 1
            elif 'enable' in args:
                self.enabled = True
            elif 'stop' in args or 'disable' in args:
                self.active = False
                if 'disable' in args:
                    self.enabled = False
            elif 'restart' in args and self.fail_restart:
                self.fail_restart = False
                code, stderr = 8, 'new service start failed'
            elif 'start' in args or 'restart' in args:
                self.active = True
        elif args[:1] == ['git']:
            code, stdout = 128, ''
        elif args[:1] == ['sudo']:
            if args[1:3] == ['find', '/etc/sudoers.d']:
                stdout = ''
        result = subprocess.CompletedProcess(args, code, stdout, stderr)
        if code and kwargs.get('check', True):
            raise ReadyError(stderr or 'test tool failed')
        return result

    def runtime(self, python, body, timeout=5, process_timeout=None):
        self.calls.append(['runtime', str(python), body['op']])
        if body['op'] == 'prepare_update':
            return {'ready': not self.busy, 'active': self.busy, 'token': 'lease'}
        if body['op'] == 'cancel_update':
            return {'cancelled': True}
        if body['op'] == 'session':
            self.session_environments.append(body['environment'])
            return {'updated': True}
        if self.fail_probe and Path(python).parent.parent != self.previous_environment:
            raise ReadyError('new service unhealthy')
        if not self.active:
            raise ReadyError('service unavailable')
        return {'service': 'running', 'host': 'testhost', 'network': 'connected'}

    def install(self, *, local=None, **values):
        args = options()
        args.local = not self.paths.config.exists() if local is None else local
        for key, value in values.items():
            setattr(args, key, value)
        return hint_ready.install(self.source, args, self.paths)

    def test_fresh_install_uses_permanent_env_and_stable_links(self):
        self.install()
        target = self.paths.environment.resolve()
        self.assertTrue(self.paths.environment.is_symlink())
        self.assertEqual(target.parent, self.paths.app)
        self.assertTrue(target.name.startswith('env-'))
        self.assertEqual(self.paths.command.resolve(), target / 'bin/hint')
        self.assertEqual((target / 'bin/hint').read_text(), '#!' + str(target / 'bin/python') + '\n')
        self.assertEqual((self.paths.app / 'source').resolve(), self.source)
        self.assertIsNone(json.loads(self.paths.config.read_text())['nats_server'])
        self.assertEqual(json.loads(self.paths.signals.read_text()), {})
        self.assertEqual(self.paths.config.stat().st_mode & 0o777, 0o600)
        self.assertIn(str(self.paths.environment / 'bin/hint'), self.paths.service.read_text())
        self.assertIn('HINT managed desktop session', self.paths.autostart.read_text())
        self.assertIn(' --session\n', self.paths.autostart.read_text())
        self.assertEqual(self.paths.autostart.stat().st_mode & 0o777, 0o644)
        self.assertIn('python3-gi', next(call for call in self.calls if call[:4] == ['sudo', 'apt-get', 'install', '-y']))
        self.assertFalse(any('python3-tk' in call for call in self.calls))

    def test_update_prepares_before_stop_preserves_data_and_removes_old_environment(self):
        self.install()
        old = self.paths.environment.resolve()
        config = b'{"nats_server":null,"wake_lead_seconds":17}\n'
        signals = b'{"personal":{"action":"/bin/true"}}\n'
        self.paths.config.write_bytes(config)
        self.paths.signals.write_bytes(signals)
        self.calls.clear()
        self.install()
        self.assertEqual(self.paths.config.read_bytes(), config)
        self.assertEqual(self.paths.signals.read_bytes(), signals)
        self.assertNotEqual(self.paths.environment.resolve(), old)
        self.assertFalse(old.exists())
        self.assertEqual(len(list(self.paths.app.glob('env-*'))), 1)
        pip_index = next(i for i, call in enumerate(self.calls) if call[1:4] == ['-m', 'pip', 'install'])
        gate_index = next(i for i, call in enumerate(self.calls) if call[-1] == 'prepare_update')
        stop_index = next(i for i, call in enumerate(self.calls) if call == ['systemctl', '--user', 'stop', 'hint.service'])
        self.assertLess(pip_index, gate_index)
        self.assertLess(gate_index, stop_index)

    def test_fresh_install_prompts_once_and_keeps_selected_server(self):
        server = 'nats://broker.example:4333'
        with patch('hint_ready.sys.stdin.isatty', return_value=True), \
                patch('builtins.input', return_value=server) as prompt:
            self.install(local=False)
        prompt.assert_called_once()
        self.assertEqual(json.loads(self.paths.config.read_text())['nats_server'], server)

    def test_fresh_install_prompts_once_for_local_mode(self):
        with patch('hint_ready.sys.stdin.isatty', return_value=True), \
                patch('builtins.input', return_value='local') as prompt:
            self.install(local=False)
        prompt.assert_called_once()
        self.assertIsNone(json.loads(self.paths.config.read_text())['nats_server'])

    def test_explicit_server_does_not_prompt(self):
        server = 'nats://broker.example:4333'
        with patch('builtins.input', side_effect=AssertionError('unexpected prompt')):
            self.install(local=False, server=server)
        self.assertEqual(json.loads(self.paths.config.read_text())['nats_server'], server)

    def test_explicit_local_does_not_prompt(self):
        with patch('builtins.input', side_effect=AssertionError('unexpected prompt')):
            self.install(local=True)
        self.assertIsNone(json.loads(self.paths.config.read_text())['nats_server'])

    def test_configuration_created_during_install_is_not_silently_substituted(self):
        changed = b'{"nats_server":"nats://other.example:4222"}\n'

        def run_tool(argv, **kwargs):
            result = self.run_tool(argv, **kwargs)
            if [str(value) for value in argv[1:4]] == ['-m', 'pip', 'install']:
                self.paths.config.parent.mkdir(parents=True)
                self.paths.config.write_bytes(changed)
            return result

        with patch('hint_ready.sys.stdin.isatty', return_value=True), \
                patch('builtins.input', return_value='nats://chosen.example:4222') as prompt, \
                patch('hint_ready._run', side_effect=run_tool), \
                self.assertRaisesRegex(ReadyError, 'settings changed during installation'):
            self.install(local=False)
        prompt.assert_called_once()
        self.assertEqual(self.paths.config.read_bytes(), changed)
        self.assertFalse(self.paths.environment.exists())
        self.assertNotIn(['systemctl', '--user', 'restart', 'hint.service'], self.calls)

    def test_revalidation_keeps_signal_changes_during_preparation(self):
        self.install()
        changed = b'{"later":{"action":"/bin/true"}}\n'

        def run_tool(argv, **kwargs):
            result = self.run_tool(argv, **kwargs)
            if [str(value) for value in argv[1:4]] == ['-m', 'pip', 'install']:
                self.paths.signals.write_bytes(changed)
            return result

        with patch('builtins.input', side_effect=AssertionError('unexpected prompt')), \
                patch('hint_ready._run', side_effect=run_tool):
            self.install()
        self.assertEqual(self.paths.signals.read_bytes(), changed)

    def test_invalid_signal_change_during_preparation_aborts_before_service_stop(self):
        self.install()
        old = self.paths.environment.resolve()
        self.calls.clear()

        def run_tool(argv, **kwargs):
            result = self.run_tool(argv, **kwargs)
            if [str(value) for value in argv[1:4]] == ['-m', 'pip', 'install']:
                self.paths.signals.write_text('[]\n')
            return result

        with patch('hint_ready._run', side_effect=run_tool), \
                self.assertRaisesRegex(ReadyError, 'signals.json must contain a JSON object'):
            self.install()
        self.assertEqual(self.paths.environment.resolve(), old)
        self.assertNotIn(['systemctl', '--user', 'stop', 'hint.service'], self.calls)

    def test_failed_preparation_never_stops_or_changes_working_installation(self):
        self.install()
        old = self.paths.environment.resolve()
        unit = self.paths.service.read_bytes()
        self.calls.clear()
        self.fail_pip = True
        with self.assertRaisesRegex(ReadyError, 'preparation failed'):
            self.install()
        self.assertTrue(self.active)
        self.assertEqual(self.paths.environment.resolve(), old)
        self.assertEqual(self.paths.service.read_bytes(), unit)
        self.assertNotIn(['systemctl', '--user', 'stop', 'hint.service'], self.calls)
        self.assertEqual(len(list(self.paths.app.glob('env-*'))), 1)

    def test_failed_activation_restores_environment_links_and_service(self):
        self.install()
        old = self.paths.environment.resolve()
        old_content = (old / 'bin/hint').read_bytes()
        unit = self.paths.service.read_bytes()
        desktop = self.paths.autostart.read_bytes()
        self.fail_restart = True
        with self.assertRaisesRegex(ReadyError, 'new service start failed'):
            self.install()
        self.assertTrue(self.active)
        self.assertEqual(self.paths.environment.resolve(), old)
        self.assertEqual((self.paths.environment / 'bin/hint').read_bytes(), old_content)
        self.assertEqual(self.paths.service.read_bytes(), unit)
        self.assertEqual(self.paths.autostart.read_bytes(), desktop)
        self.assertEqual(len(list(self.paths.app.glob('env-*'))), 1)
        self.assertIn('previous installation was restored', self.output.getvalue())

    def test_failed_first_activation_removes_new_enablement_and_installation_files(self):
        self.fail_restart = True
        with self.assertRaisesRegex(ReadyError, 'new service start failed'):
            self.install()
        self.assertFalse(self.enabled)
        self.assertFalse(self.active)
        self.assertIn(['systemctl', '--user', 'disable', 'hint.service'], self.calls)
        for path in (self.paths.environment, self.paths.service, self.paths.autostart, self.paths.command):
            self.assertFalse(path.exists())
            self.assertFalse(path.is_symlink())
        self.assertEqual(list(self.paths.app.glob('env-*')), [])

    def test_failed_activation_restores_previously_disabled_service(self):
        self.install()
        self.enabled = False
        old = self.paths.environment.resolve()
        old_unit = self.paths.service.read_bytes()
        self.calls.clear()
        self.fail_restart = True
        with self.assertRaisesRegex(ReadyError, 'new service start failed'):
            self.install()
        self.assertFalse(self.enabled)
        self.assertTrue(self.active)
        self.assertEqual(self.paths.environment.resolve(), old)
        self.assertEqual(self.paths.service.read_bytes(), old_unit)
        self.assertIn(['systemctl', '--user', 'disable', 'hint.service'], self.calls)

    def test_active_runs_refuse_update_without_stopping(self):
        self.install()
        old = self.paths.environment.resolve()
        self.calls.clear()
        self.busy = 2
        with self.assertRaisesRegex(ReadyError, 'Finish or stop 2 active runs'):
            self.install()
        self.assertTrue(self.active)
        self.assertEqual(self.paths.environment.resolve(), old)
        self.assertNotIn(['systemctl', '--user', 'stop', 'hint.service'], self.calls)

    def test_flat_managed_environment_restores_exact_path_after_failed_activation(self):
        self.paths.environment.mkdir(parents=True)
        (self.paths.environment / '.hint-owned').write_text('HINT managed environment\n')
        (self.paths.environment / 'bin').mkdir()
        (self.paths.environment / 'bin/python').write_text('old python')
        (self.paths.environment / 'bin/hint').write_text('old command')
        self.paths.command.parent.mkdir(parents=True)
        self.paths.command.symlink_to(self.paths.environment / 'bin/hint')
        self.fail_restart = True
        with self.assertRaises(ReadyError):
            self.install()
        self.assertFalse(self.paths.environment.is_symlink())
        self.assertEqual((self.paths.environment / 'bin/hint').read_text(), 'old command')
        self.assertEqual(list(self.paths.app.glob('env-*')), [])

    def test_foreign_command_and_environment_are_untouched(self):
        self.paths.command.parent.mkdir(parents=True)
        self.paths.command.write_text('foreign')
        with self.assertRaisesRegex(ReadyError, 'not owned'):
            self.install()
        self.assertEqual(self.paths.command.read_text(), 'foreign')
        self.assertEqual(self.calls, [])
        self.paths.command.unlink()
        self.paths.environment.mkdir(parents=True)
        (self.paths.environment / 'foreign').write_text('keep')
        with self.assertRaisesRegex(ReadyError, 'not owned'):
            self.install()
        self.assertEqual((self.paths.environment / 'foreign').read_text(), 'keep')
        self.assertEqual(self.calls, [])

    def test_foreign_symlink_environment_is_not_followed(self):
        foreign = self.root / 'foreign-env'
        foreign.mkdir()
        (foreign / '.hint-owned').write_text('HINT managed environment\n')
        self.paths.app.mkdir(parents=True)
        self.paths.environment.symlink_to(foreign)
        with self.assertRaisesRegex(ReadyError, 'not owned'):
            self.install()
        self.assertTrue(foreign.exists())
        self.assertEqual(self.calls, [])

    def test_invalid_server_rejected_before_packages(self):
        with self.assertRaises(Exception):
            self.install(local=False, server='https://wrong.example')
        self.assertEqual(self.calls, [])

    def test_reconfiguration_never_overwrites_existing_settings(self):
        self.install()
        configuration = self.paths.config.read_bytes()
        self.calls.clear()
        with self.assertRaisesRegex(ReadyError, 'Existing configuration was preserved'):
            self.install(local=False, server='nats://different.example:4222')
        self.assertEqual(self.paths.config.read_bytes(), configuration)
        self.assertEqual(self.calls, [])

    def test_repeated_matching_initial_flags_preserve_custom_settings(self):
        self.install()
        local_config = b'{"nats_server":null,"request_timeout_seconds":27}\n'
        self.paths.config.write_bytes(local_config)
        self.install(local=True)
        self.assertEqual(self.paths.config.read_bytes(), local_config)
        network_config = b'{"nats_server":"nats://broker.example:4333","discovery_timeout_seconds":0.8}\n'
        self.paths.config.write_bytes(network_config)
        self.install(local=False, server='nats://broker.example:4333')
        self.assertEqual(self.paths.config.read_bytes(), network_config)

    def test_ready_allows_connecting_worker_to_finish_without_waiting_when_connected(self):
        self.install(local=False, server='nats://broker.example:4333')
        self.output.truncate(0)
        self.output.seek(0)
        status = {'host': 'testhost', 'network': 'connecting'}
        with patch('hint_ready._runtime', return_value={'network': 'connected'}) as runtime:
            hint_ready._ready_message(self.paths, status)
        self.assertEqual(runtime.call_count, 1)
        self.assertLessEqual(runtime.call_args.kwargs['process_timeout'], 1)
        self.assertEqual(status['network'], 'connected')
        self.assertEqual(self.output.getvalue(), 'Ready on testhost.\n')
        with patch('hint_ready._runtime') as runtime:
            hint_ready._ready_message(self.paths, status)
        runtime.assert_not_called()

    def test_wake_install_is_explicit_and_preserved(self):
        self.install(enable_wake=True)
        marker = self.paths.app / 'wake-access'
        self.assertTrue(marker.is_file())
        self.assertTrue(any(call[:3] == ['sudo', 'visudo', '-cf'] for call in self.calls))
        self.calls.clear()
        self.install()
        self.assertTrue(any(call[:3] == ['sudo', 'visudo', '-cf'] for call in self.calls))

    def test_uninstall_removes_only_installation_and_keeps_data(self):
        self.install()
        config, signals = self.paths.config.read_bytes(), self.paths.signals.read_bytes()
        hint_ready.uninstall(self.paths)
        self.assertFalse(self.active)
        for item in (self.paths.command, self.paths.environment, self.paths.service, self.paths.completion,
                     self.paths.autostart, self.paths.app / 'source'):
            self.assertFalse(item.exists())
            self.assertFalse(item.is_symlink())
        self.assertEqual(self.paths.config.read_bytes(), config)
        self.assertEqual(self.paths.signals.read_bytes(), signals)
        self.assertEqual(list(self.paths.app.glob('env-*')), [])
        hint_ready.uninstall(self.paths)
        self.assertIn('HINT is not installed.', self.output.getvalue())

    def test_checkout_ready_bootstraps_before_environment_exists(self):
        tools = self.root / 'tools'
        tools.mkdir()
        python = tools / 'python3'
        python.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n")
        python.chmod(0o755)
        env = {**os.environ, 'PATH': str(tools) + os.pathsep + os.environ['PATH'],
               'XDG_DATA_HOME': str(self.paths.data_home)}
        result = subprocess.run(['bash', str(self.source / 'hint'), '--ready', '--local'],
                                env=env, capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [str(self.source / 'hint_ready.py'), '--ready', '--local'])
        result = subprocess.run(['bash', str(self.source / 'hint'), '--fire', '--action', 'popup: --ready text'],
                                env=env, capture_output=True, text=True, timeout=3)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('not installed', result.stderr)
        self.assertEqual(result.stdout, '')

    def test_root_refused_before_any_changes(self):
        with patch('hint_ready.os.geteuid', return_value=0):
            with self.assertRaisesRegex(ReadyError, 'without sudo'):
                self.install()
            with self.assertRaisesRegex(ReadyError, 'without sudo'):
                hint_ready.uninstall(self.paths)
        self.assertEqual(self.calls, [])

    def test_session_refresh_does_not_restart_service_or_interrupt_active_runs(self):
        self.install()
        self.busy = 3
        self.calls.clear()
        environment = {'WAYLAND_DISPLAY': 'wayland-1', 'DISPLAY': ':1',
                       'XDG_SESSION_TYPE': 'wayland', 'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/run/user/1000/bus',
                       'UNRELATED_SECRET': 'do not copy'}
        with patch.dict(os.environ, environment, clear=True):
            hint_ready.session(self.paths)
        expected = {key: value for key, value in environment.items() if key != 'UNRELATED_SECRET'}
        self.assertEqual(self.session_environments, [expected])
        self.assertEqual(self.busy, 3)
        self.assertTrue(self.active)
        self.assertNotIn(['systemctl', '--user', 'restart', 'hint.service'], self.calls)
        self.assertNotIn(['systemctl', '--user', 'stop', 'hint.service'], self.calls)
        self.assertFalse(any(call[-1] == 'prepare_update' for call in self.calls))
        imported = next(call for call in self.calls if call[:3] == ['systemctl', '--user', 'import-environment'])
        self.assertEqual(set(imported[3:]), set(expected))

    def test_headless_ready_session_does_not_clear_existing_desktop(self):
        self.install()
        with patch.dict(os.environ, {}, clear=True):
            hint_ready.session(self.paths)
        self.assertEqual(self.session_environments, [])

    def test_foreign_autostart_file_is_preserved(self):
        self.paths.autostart.parent.mkdir(parents=True)
        self.paths.autostart.write_text('[Desktop Entry]\nName=Unrelated\n')
        with self.assertRaisesRegex(ReadyError, 'not owned'):
            self.install()
        self.assertEqual(self.paths.autostart.read_text(), '[Desktop Entry]\nName=Unrelated\n')
        self.assertEqual(self.calls, [])

    def test_source_symlink_is_used_by_installed_ready(self):
        self.install()
        with patch('hint_ready.__file__', '/unrelated/site-packages/hint_ready.py'), patch.dict(os.environ, {}, clear=True):
            self.assertEqual(hint_ready.find_source(self.paths), self.source)


class ReadyGitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='hint-ready-git-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.origin = self.root / 'origin'
        self.source = self.root / 'checkout'
        self.git('init', '--initial-branch=main', self.origin)
        self.git('-C', self.origin, 'config', 'user.name', 'HINT test')
        self.git('-C', self.origin, 'config', 'user.email', 'hint-test@example.invalid')
        (self.origin / 'hint_ready.py').write_text('# fixture\n')
        (self.origin / 'install.sh').write_text('# fixture\n')
        shutil.copyfile(PROJECT / 'pyproject.toml', self.origin / 'pyproject.toml')
        self.git('-C', self.origin, 'add', '.')
        self.git('-C', self.origin, 'commit', '-m', 'fixture')
        self.git('clone', self.origin, self.source)
        self.paths = Paths(self.root / 'home', self.root / 'config', self.root / 'data')
        self.output = io.StringIO()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(self.output))

    def git(self, *args):
        return subprocess.run(['git', *map(str, args)], text=True, capture_output=True, check=True)

    def test_clean_checkout_initial_ready_installs_without_global_command(self):
        with patch('hint_ready.install', return_value={'service': 'running'}) as install:
            hint_ready.ready(self.source, options(), self.paths)
        install.assert_called_once_with(self.source, options(), self.paths)

    def test_dirty_checkout_is_not_modified(self):
        (self.source / 'hint_ready.py').write_text('# local edit\n')
        with patch('hint_ready.install') as install, self.assertRaisesRegex(ReadyError, 'Local source changes'):
            hint_ready.ready(self.source, options(), self.paths)
        install.assert_not_called()
        self.assertEqual((self.source / 'hint_ready.py').read_text(), '# local edit\n')

    def test_fastforward_runs_new_installer_and_forwards_initial_setup_options(self):
        (self.origin / 'hint_ready.py').write_text('# updated\n')
        self.git('-C', self.origin, 'commit', '-am', 'update source')
        original_run = hint_ready._run
        installers = []
        def run(argv, **kwargs):
            if str(argv[0]) == 'bash':
                installers.append(list(map(str, argv)))
                return subprocess.CompletedProcess(argv, 0, '', '')
            return original_run(argv, **kwargs)
        args = options()
        args.server = 'nats://dibbles:4222'
        args.enable_wake = True
        with patch('hint_ready._run', side_effect=run):
            hint_ready.ready(self.source, args, self.paths)
        self.assertEqual((self.source / 'hint_ready.py').read_text(), '# updated\n')
        self.assertEqual(installers, [['bash', str(self.source / 'install.sh'), '--enable-wake', '--server', 'nats://dibbles:4222']])

    def test_diverged_checkout_is_not_merged(self):
        self.git('-C', self.source, 'config', 'user.name', 'HINT test')
        self.git('-C', self.source, 'config', 'user.email', 'hint-test@example.invalid')
        (self.source / 'local').write_text('local\n')
        self.git('-C', self.source, 'add', 'local')
        self.git('-C', self.source, 'commit', '-m', 'local commit')
        original_head = self.git('-C', self.source, 'rev-parse', 'HEAD').stdout
        (self.origin / 'remote').write_text('remote\n')
        self.git('-C', self.origin, 'add', 'remote')
        self.git('-C', self.origin, 'commit', '-m', 'remote commit')
        with patch('hint_ready.install') as install, self.assertRaisesRegex(ReadyError, 'history have diverged'):
            hint_ready.ready(self.source, options(), self.paths)
        install.assert_not_called()
        self.assertEqual(self.git('-C', self.source, 'rev-parse', 'HEAD').stdout, original_head)

    def test_same_revision_ensures_service_without_reinstalling_or_restarting(self):
        self.paths.environment.mkdir(parents=True)
        revision = self.git('-C', self.source, 'rev-parse', 'HEAD').stdout
        (self.paths.environment / '.hint-revision').write_text(revision)
        (self.paths.environment / '.hint-owned').write_text('HINT managed environment\n')
        self.paths.config.parent.mkdir(parents=True)
        self.paths.config.write_text('{"nats_server": null}')
        with patch('hint_ready.install') as install, patch('hint_ready._service') as service, patch('hint_ready._import_desktop_environment', return_value={}), patch('hint_ready._wait_running', return_value={'host': 'test', 'service': 'running'}):
            hint_ready.ready(self.source, options(), self.paths)
        install.assert_not_called()
        self.assertEqual([call.args for call in service.call_args_list], [('enable', 'hint.service'), ('start', 'hint.service')])


if __name__ == '__main__':
    unittest.main()
