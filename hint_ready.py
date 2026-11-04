"""Prepare, install and update HINT without replacing a working environment in place."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import ipaddress
import pwd
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time

from hint_config import HintError


class ReadyError(Exception):
    pass


DESKTOP_VARIABLES = ('DISPLAY', 'WAYLAND_DISPLAY', 'XAUTHORITY',
                     'DBUS_SESSION_BUS_ADDRESS', 'XDG_SESSION_TYPE')


def _run(argv, *, capture=False, check=True, cwd=None, timeout=60, env=None, input=None):
    result = subprocess.run([str(arg) for arg in argv], cwd=cwd, env=env, text=True,
                            stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.PIPE if capture else None, timeout=timeout, input=input)
    if check and result.returncode:
        reason = ((result.stderr or result.stdout or '').strip() if capture else '')
        raise ReadyError(reason or f'{Path(str(argv[0])).name} exited with code {result.returncode}')
    return result


def _xdg(variable, fallback):
    value = os.environ.get(variable, '')
    return Path(value) if value.startswith('/') else fallback


@dataclass
class Paths:
    home: Path
    config_home: Path
    data_home: Path

    @classmethod
    def current(cls):
        home = Path.home()
        return cls(home, _xdg('XDG_CONFIG_HOME', home / '.config'),
                   _xdg('XDG_DATA_HOME', home / '.local/share'))

    @property
    def app(self):
        return self.data_home / 'hint'

    @property
    def environment(self):
        return self.app / 'venv'

    @property
    def config(self):
        return self.config_home / 'hint/config.json'

    @property
    def signals(self):
        return self.app / 'signals.json'

    @property
    def command(self):
        return self.home / '.local/bin/hint'

    @property
    def service(self):
        return self.config_home / 'systemd/user/hint.service'

    @property
    def completion(self):
        return self.data_home / 'bash-completion/completions/hint'


    @property
    def autostart(self):
        return self.config_home / 'autostart/hint.desktop'


@contextmanager
def install_lock(paths):
    paths.app.mkdir(parents=True, exist_ok=True, mode=0o700)
    if paths.app.is_symlink() or paths.app.stat().st_uid != os.getuid():
        raise ReadyError(f'Cannot replace {paths.app}: it is not owned by this HINT installation.')
    fd = os.open(paths.app, os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ReadyError('Another HINT installation or update is already running.') from None
        yield
    finally:
        os.close(fd)


def _owned_environment(path, paths):
    if not path.exists() and not path.is_symlink():
        return None
    actual = path.resolve()
    if path.is_symlink() and (actual.parent != paths.app.resolve() or not actual.name.startswith('env-')):
        raise ReadyError(f'Cannot replace {path}: it is not owned by this HINT installation.')
    marker = actual / '.hint-owned'
    if not actual.is_dir() or not marker.is_file() or marker.read_text().strip() != 'HINT managed environment':
        raise ReadyError(f'Cannot replace {path}: it is not owned by this HINT installation.')
    return actual


def _check_ownership(paths):
    _owned_environment(paths.environment, paths)
    if paths.command.exists() or paths.command.is_symlink():
        if not paths.command.is_symlink() or os.readlink(paths.command) != str(paths.environment / 'bin/hint'):
            raise ReadyError(f'Cannot replace {paths.command}: it is not owned by this HINT installation.')
    for path, marker in ((paths.service, 'HINT managed user service'),
                         (paths.completion, 'Bash completion for the installed HINT command'),
                         (paths.autostart, 'HINT managed desktop session')):
        if (path.exists() or path.is_symlink()) and (path.is_symlink() or marker not in path.read_text()):
            raise ReadyError(f'Cannot replace {path}: it is not owned by this HINT installation.')
    source = paths.app / 'source'
    if source.exists() and not source.is_symlink():
        raise ReadyError(f'Cannot replace {source}: it is not owned by this HINT installation.')


def _server_url(value):
    """Accept simple server addresses at setup without rewriting saved URLs."""
    value = value.strip()
    if '://' in value:
        return value
    host, separator, port = value.partition(':')
    hostname = host[:-1] if host.endswith('.') else host
    labels = hostname.split('.')
    valid = (len(hostname) <= 253 and all(
        re.fullmatch(r'[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?', label)
        for label in labels
    ))
    if separator:
        valid = valid and bool(re.fullmatch(r'[0-9]{1,5}', port))
        if valid:
            valid = 1 <= int(port) <= 65535
    if valid and re.fullmatch(r'[0-9.]+', host):
        try:
            ipaddress.IPv4Address(host)
        except ValueError:
            valid = False
    if not valid:
        raise HintError('NATS server must be a hostname or IPv4 address with optional :port, or a full nats:// or tls:// URL.')
    return f'nats://{host}:{int(port) if separator else 4222}'


def _configuration(paths, args, prepared=None):
    from hint_config import DEFAULTS, validate_config
    selected_server = _server_url(args.server) if args.server is not None else None
    if paths.config.exists():
        configuration = json.loads(paths.config.read_text())
        current_server = validate_config(configuration)['nats_server']
        if (args.server is not None or args.local) and (None if args.local else selected_server) != current_server:
            raise ReadyError(f'Existing configuration was preserved. Edit {paths.config} to change its server.')
    elif prepared is not None:
        configuration = prepared
    else:
        server = selected_server
        if args.local:
            server = None
        elif server is None:
            if not sys.stdin.isatty():
                raise ReadyError('Choose a NATS server with --server nats://HOST:4222, or use --local for this computer only.')
            while True:
                try:
                    answer = input("NATS server (hostname, IP or URL), or 'local' for this computer only: ").strip()
                except EOFError:
                    raise ReadyError('No server selected. Run again with --server nats://HOST:4222 or --local.') from None
                if not answer:
                    print("Enter a NATS server hostname, IP or URL, or 'local'.", file=sys.stderr)
                    continue
                try:
                    server = None if answer.lower() == 'local' else _server_url(answer)
                    validate_config({**DEFAULTS, 'nats_server': server})
                except HintError as error:
                    print(str(error), file=sys.stderr)
                    continue
                break
        configuration = {**DEFAULTS, 'nats_server': server}
    configuration = validate_config(configuration)
    if prepared is not None and configuration != prepared:
        raise ReadyError('HINT settings changed during installation. Run hint --ready again.')
    table = json.loads(paths.signals.read_text()) if paths.signals.exists() else {}
    if not isinstance(table, dict):
        raise ReadyError('signals.json must contain a JSON object')
    return configuration, table


def _validate_signals(python, table):
    # The system interpreter may lack the timezone backport on first install.
    # Validate with the prepared application environment before activation.
    script = (
        'import json,sys\n'
        'from hint_signals import validate\n'
        'from hint_config import HintError\n'
        'try:\n'
        '    for name, entry in json.load(sys.stdin).items():\n'
        '        validate(name, entry)\n'
        'except (HintError, ValueError, TypeError) as error:\n'
        '    print(str(error), file=sys.stderr)\n'
        '    raise SystemExit(1)\n'
    )
    _run([python, '-I', '-c', script], capture=True, input=json.dumps(table), timeout=10)


def _create_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())


def _replace_link(path, destination):
    # The temporary name is never an application-visible path.
    temporary = path.with_name(path.name + '.new-' + str(os.getpid()))
    if temporary.exists() or temporary.is_symlink():
        raise ReadyError(f'Cannot replace temporary installation path: {temporary}')
    try:
        temporary.symlink_to(destination)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _unit(source, paths):
    def quote(value):
        if '\n' in value or '\r' in value:
            raise ReadyError('Installation paths cannot contain line breaks.')
        return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'
    lines = []
    for line in (source / 'hint.service').read_text().splitlines():
        if line.startswith('ExecStart='):
            lines.extend(('ExecStart=' + quote(str(paths.environment / 'bin/hint')) + ' --serve',
                          'Environment=' + quote('XDG_CONFIG_HOME=' + str(paths.config_home)),
                          'Environment=' + quote('XDG_DATA_HOME=' + str(paths.data_home))))
        else:
            lines.append(line)
    return '\n'.join(lines) + '\n'


def _desktop_entry(paths):
    command = str(paths.environment / 'bin/hint')
    if any(character in command for character in '\n\r\0='):
        raise ReadyError('Installation paths cannot contain line breaks, NUL or equals signs.')
    # Desktop Entry string unescaping precedes Exec argument unescaping.
    command = command.replace('\\', '\\\\\\\\')
    for character in ('"', '`', '$'):
        command = command.replace(character, '\\\\' + character)
    command = command.replace('%', '%%')
    return ('[Desktop Entry]\n'
            '# HINT managed desktop session\n'
            'Type=Application\n'
            'Name=HINT\n'
            'Comment=Connect HINT to this desktop session\n'
            'Exec=/usr/bin/env "' + command + '" --session\n'
            'Terminal=false\n'
            'NoDisplay=true\n'
            'X-GNOME-Autostart-enabled=true\n')


def _service(*args, check=True, capture=False):
    return _run(['systemctl', '--user', *args], check=check, capture=capture, timeout=10)


def _runtime(python, body, timeout=5, process_timeout=None):
    script = ('import json; from hint_runtime import request; '
              'print(json.dumps(request(json.loads(' + repr(json.dumps(body)) + '), timeout=' + str(timeout) + ')))')
    result = _run([python, '-I', '-c', script], capture=True,
                  timeout=timeout + 2 if process_timeout is None else process_timeout)
    return json.loads(result.stdout)


def _wait_running(python):
    deadline = time.monotonic() + 8
    last = 'service did not become ready'
    while time.monotonic() < deadline:
        try:
            status = _runtime(python, {'op': 'status'}, timeout=1)
            if status.get('service') == 'running':
                return status
        except (ReadyError, ValueError, subprocess.TimeoutExpired) as error:
            last = str(error)
        time.sleep(0.15)
    raise ReadyError(last)


def _import_desktop_environment():
    environment = {key: os.environ[key] for key in DESKTOP_VARIABLES if os.environ.get(key)}
    if environment:
        _service('import-environment', *environment)
    return environment


def session(paths=None):
    """Refresh a logged-in desktop without restarting or interrupting runs."""
    paths = paths or Paths.current()
    _check_ownership(paths)
    environment = _import_desktop_environment()
    _service('start', 'hint.service')
    python = paths.environment / 'bin/python'
    result = _wait_running(python)
    if environment.get('DISPLAY') or environment.get('WAYLAND_DISPLAY'):
        response = _runtime(python, {'op': 'session', 'environment': environment})
        if response.get('error'):
            raise ReadyError(response['error'])
        result = _runtime(python, {'op': 'status'})
    return result


def _snapshot(path):
    if path.is_symlink():
        return ('link', os.readlink(path))
    if path.exists():
        return ('file', path.read_bytes(), path.stat().st_mode & 0o777)
    return None


def _restore(path, value):
    if path.exists() or path.is_symlink():
        path.unlink()
    if value is None:
        return
    if value[0] == 'link':
        path.symlink_to(value[1])
    else:
        path.write_bytes(value[1])
        path.chmod(value[2])


def _wake_support(source, paths):
    user = pwd.getpwuid(os.getuid()).pw_name
    _run(['sudo', 'apt-get', 'install', '-y', 'util-linux'], timeout=600)
    _run(['sudo', 'install', '-o', 'root', '-g', 'root', '-m', '0755', source / 'rtc_helper.py', '/usr/local/sbin/hint-rtc'])
    with tempfile.TemporaryDirectory(prefix='hint-rtc-') as directory:
        rule = Path(directory) / 'rule'
        rule.write_text('# HINT managed RTC access\n' + user + ' ALL=(root) NOPASSWD: /usr/local/sbin/hint-rtc *\n')
        rule.chmod(0o600)
        _run(['sudo', 'visudo', '-cf', rule])
        _run(['sudo', 'install', '-o', 'root', '-g', 'root', '-m', '0440', rule, '/etc/sudoers.d/hint-rtc-' + user])
    marker = paths.app / 'wake-access'
    marker.write_text('HINT managed RTC access for ' + user + '\n')
    marker.chmod(0o600)


def _ready_message(paths, status):
    host = status.get('host') or socket.gethostname()
    configuration = json.loads(paths.config.read_text())
    if configuration.get('nats_server') is not None and status.get('network') == 'connecting':
        # A freshly started worker can connect just after the local service.
        # This allowance includes child startup and never waits for the Internet.
        deadline = time.monotonic() + 1
        while status.get('network') == 'connecting':
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.05, remaining))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                response = _runtime(paths.environment / 'bin/python', {'op': 'status'},
                                    timeout=min(0.25, remaining), process_timeout=remaining)
            except (ReadyError, ValueError, OSError, subprocess.TimeoutExpired):
                break
            if response.get('error'):
                break
            status.update(response)
    if configuration.get('nats_server') is None:
        print(f'Ready on {host} (local-only).')
    elif status.get('network') == 'connected':
        print(f'Ready on {host}.')
    else:
        from hint_config import sanitize_error

        reason = status.get('network') or 'not connected'
        server = sanitize_error(configuration['nats_server'])
        print(f'Ready locally on {host}; NATS connection to {server}: {reason}.')
        print(f'Settings: {paths.config}')
        print('Check the server address, firewall, and VPN LAN access.')
        print('Details: journalctl --user -u hint.service -b -n 20 --no-pager')


def install(source, args, paths=None):
    paths = paths or Paths.current()
    if os.geteuid() == 0:
        raise ReadyError('Run this as your normal desktop user, without sudo.')
    source = Path(source).resolve()
    for item in ('pyproject.toml', 'hint.service', 'rtc_helper.py', 'completions/hint.bash'):
        if not (source / item).is_file():
            raise ReadyError(f'Missing installation file: {item}')
    _check_ownership(paths)
    _desktop_entry(paths)
    configuration, table = _configuration(paths, args)
    with install_lock(paths):
        _check_ownership(paths)
        print('Installing HINT...', flush=True)
        _run(['sudo', 'apt-get', 'update'], timeout=600)
        _run(['sudo', 'apt-get', 'install', '-y', 'python3-venv', 'python3-gi',
              'gir1.2-gtk-3.0', 'mpv', 'xdg-utils', 'bash-completion', 'git'], timeout=600)
        stage = Path(tempfile.mkdtemp(prefix='env-', dir=paths.app))
        (stage / '.hint-owned').write_text('HINT managed environment\n')
        python = stage / 'bin/python'
        old = _owned_environment(paths.environment, paths)
        old_was_link = paths.environment.is_symlink()
        old_link = os.readlink(paths.environment) if old_was_link else None
        old_moved = None
        active = False
        enabled = False
        gate = None
        switched = False
        activation_started = False
        committed = False
        saved = {path: _snapshot(path) for path in (paths.service, paths.completion, paths.command,
                                                   paths.autostart, paths.app / 'source')}
        try:
            # venv paths remain permanent: moving them breaks script shebangs.
            _run(['python3', '-m', 'venv', stage], timeout=120)
            _run([python, '-m', 'pip', 'install', source], timeout=600)
            _run([python, '-m', 'pip', 'check'], timeout=30)
            _validate_signals(python, table)
            _run([stage / 'bin/hint', '--help'], capture=True, timeout=10)
            _run([python, '-I', '-c', 'import hint, hint_runtime, hint_playback, hint_network, hint_completion'], timeout=10)
            _run(['/usr/bin/python3', '-c', "import gi; gi.require_version('Gtk', '3.0'); from gi.repository import Gtk"], timeout=10)
            revision = _source_revision(source)
            if revision:
                (stage / '.hint-revision').write_text(revision + '\n')
            wake_marker = paths.app / 'wake-access'
            if args.enable_wake or any(entry.get('wake') for entry in table.values()) or wake_marker.exists():
                _wake_support(source, paths)
            active = _service('is-active', '--quiet', 'hint.service', check=False).returncode == 0
            enabled = _service('is-enabled', '--quiet', 'hint.service', check=False).returncode == 0
            if active:
                # An unresponsive service is not permission to interrupt its work.
                gate = _runtime(python, {'op': 'prepare_update', 'lease_seconds': 300})
                if not gate.get('ready'):
                    count = gate.get('active', 0)
                    raise ReadyError(f'Update available. Finish or stop {count} active runs, then run hint --ready again.'
                                     if count else 'Another HINT update is already in progress.')
            configuration, table = _configuration(paths, args, prepared=configuration)
            _validate_signals(python, table)
            for parent in (paths.command.parent, paths.service.parent, paths.completion.parent, paths.autostart.parent):
                parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            _create_json(paths.config, configuration)
            _create_json(paths.signals, table)
            activation_started = True
            if active:
                _service('stop', 'hint.service')
            if old is not None and not old_was_link:
                old_moved = paths.app / ('env-previous-' + stage.name[4:])
                os.rename(paths.environment, old_moved)
            _replace_link(paths.environment, stage)
            switched = True
            _replace_link(paths.command, paths.environment / 'bin/hint')
            _replace_link(paths.app / 'source', source)
            paths.completion.write_bytes((source / 'completions/hint.bash').read_bytes())
            paths.completion.chmod(0o644)
            paths.service.write_text(_unit(source, paths))
            paths.service.chmod(0o644)
            paths.autostart.write_text(_desktop_entry(paths))
            paths.autostart.chmod(0o644)
            _service('daemon-reload')
            _import_desktop_environment()
            _service('enable', 'hint.service')
            _service('restart', 'hint.service')
            status = _wait_running(python)
            committed = True
            _ready_message(paths, status)
            old_delete = old_moved or old
            if old_delete is not None and old_delete != stage:
                try:
                    shutil.rmtree(old_delete)
                except OSError as error:
                    print(f'HINT installed; could not remove the previous environment: {error}.', file=sys.stderr)
            print('Completion in this shell: source ' + str(paths.completion))
            return status
        except BaseException:
            if committed:
                raise
            if activation_started:
                _service('stop', 'hint.service', check=False)
                if not enabled:
                    _service('disable', 'hint.service', check=False)
                if switched:
                    paths.environment.unlink(missing_ok=True)
                if old_moved is not None:
                    os.rename(old_moved, paths.environment)
                elif old_was_link:
                    _replace_link(paths.environment, old_link)
                for path, snapshot in saved.items():
                    _restore(path, snapshot)
                _service('daemon-reload', check=False)
                restored = not active
                if active:
                    restored = _service('start', 'hint.service', check=False).returncode == 0
                    if restored:
                        try:
                            _wait_running(paths.environment / 'bin/python')
                        except (ReadyError, ValueError, subprocess.TimeoutExpired):
                            restored = False
                print('Update failed; the previous installation was restored.' if old is not None and restored
                      else 'Installation failed; inspect the HINT service before retrying.', file=sys.stderr)
            elif gate and gate.get('token'):
                try:
                    _runtime(python, {'op': 'cancel_update', 'token': gate['token']})
                except Exception:
                    pass
            if stage.exists() and (not paths.environment.is_symlink() or paths.environment.resolve() != stage):
                shutil.rmtree(stage)
            raise


def _source_revision(source):
    result = _run(['git', '-C', source, 'rev-parse', 'HEAD'], check=False, capture=True)
    return result.stdout.strip() if result.returncode == 0 else None


def find_source(paths=None):
    paths = paths or Paths.current()
    candidates = []
    if os.environ.get('HINT_SOURCE_DIR'):
        candidates.append(Path(os.environ['HINT_SOURCE_DIR']))
    candidates.extend((Path(__file__).resolve().parent, paths.app / 'source'))
    for candidate in candidates:
        if (candidate / 'hint_ready.py').is_file() and (candidate / 'pyproject.toml').is_file():
            return candidate.resolve()
    raise ReadyError('Cannot locate the HINT checkout. Run ./hint --ready from the project directory.')


def ready(source, args, paths=None):
    paths = paths or Paths.current()
    source = Path(source).resolve()
    _check_ownership(paths)
    if not (source / '.git').exists():
        # A downloaded source archive can bootstrap too; it cannot fetch updates.
        return install(source, args, paths)
    print('Checking for updates...', flush=True)
    status = _run(['git', '-C', source, 'status', '--porcelain'], capture=True)
    if status.stdout.strip():
        raise ReadyError('Local source changes prevent updating. Your changes were left untouched.')
    branch = _run(['git', '-C', source, 'symbolic-ref', '--quiet', '--short', 'HEAD'], capture=True, check=False)
    if branch.returncode:
        raise ReadyError('The HINT checkout is not on a branch. Select its update branch before running hint --ready.')
    upstream = _run(['git', '-C', source, 'rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{u}'], capture=True, check=False)
    remote, separator, remote_branch = upstream.stdout.strip().partition('/')
    if upstream.returncode or not separator:
        raise ReadyError('The HINT branch has no upstream repository. Configure its Git upstream before updating.')
    try:
        _run(['git', '-C', source, 'fetch', '--', remote, remote_branch], capture=True, timeout=45)
    except (ReadyError, subprocess.TimeoutExpired) as error:
        if paths.environment.exists():
            print(f'Could not check for updates: {error}.', file=sys.stderr)
            _service('enable', 'hint.service')
            result = session(paths)
            _ready_message(paths, result)
            return result
        raise
    before = _source_revision(source)
    merged = _run(['git', '-C', source, 'merge', '--ff-only', '--', 'FETCH_HEAD'], capture=True, check=False)
    if merged.returncode:
        raise ReadyError('Local and remote history have diverged. Resolve the Git history before updating.')
    after = _source_revision(source)
    marker = paths.environment / '.hint-revision'
    installed_revision = marker.read_text().strip() if marker.is_file() else None
    if before == after and installed_revision == after and not args.enable_wake:
        if args.server is not None or args.local:
            configuration, table = _configuration(paths, args)
            _validate_signals(paths.environment / 'bin/python', table)
        print('HINT is already up to date.')
        _replace_link(paths.app / 'source', source)
        _service('enable', 'hint.service')
        result = session(paths)
        _ready_message(paths, result)
        return result
    if before != after:
        print(f'Updating HINT from {before[:7]} to {after[:7]}...', flush=True)
        # Continue with the freshly fetched installer, not this older module.
        options = ['--enable-wake'] if args.enable_wake else []
        if args.server is not None:
            options.extend(('--server', args.server))
        if args.local:
            options.append('--local')
        result = _run(['bash', source / 'install.sh', *options], check=False, timeout=1200)
        if result.returncode:
            raise ReadyError('The updated installer did not complete successfully.')
        return None
    return install(source, args, paths)


def uninstall(paths=None):
    paths = paths or Paths.current()
    if os.geteuid() == 0:
        raise ReadyError('Run this as your normal desktop user, without sudo.')
    _check_ownership(paths)
    environment = _owned_environment(paths.environment, paths)
    present = environment is not None or paths.service.exists() or paths.command.is_symlink()
    if not present:
        print('HINT is not installed.')
        return
    with install_lock(paths):
        _check_ownership(paths)
        environment = _owned_environment(paths.environment, paths)
        if paths.service.exists():
            _service('disable', '--now', 'hint.service')
        marker = paths.app / 'wake-access'
        user = pwd.getpwuid(os.getuid()).pw_name
        if marker.is_file() and marker.read_text().strip() == 'HINT managed RTC access for ' + user:
            rule = Path('/etc/sudoers.d') / ('hint-rtc-' + user)
            exists = _run(['sudo', 'test', '-f', rule], check=False).returncode == 0
            if exists:
                recognized = _run(['sudo', 'grep', '-Fq', '# HINT managed RTC access', rule], check=False).returncode == 0
                if not recognized:
                    raise ReadyError(f'Cannot remove {rule}: it is not owned by this HINT installation.')
                _run(['sudo', '-n', '/usr/local/sbin/hint-rtc', 'disable'], check=False)
                _run(['sudo', 'rm', '-f', rule])
            others = _run(['sudo', 'find', '/etc/sudoers.d', '-maxdepth', '1', '-name', 'hint-rtc-*', '-print', '-quit'], capture=True)
            if not others.stdout.strip():
                recognized = _run(['sudo', 'grep', '-Fq', '# HINT managed RTC helper', '/usr/local/sbin/hint-rtc'], check=False).returncode == 0
                if recognized:
                    _run(['sudo', 'rm', '-f', '/usr/local/sbin/hint-rtc'])
            marker.unlink()
        for path in (paths.command, paths.service, paths.completion, paths.autostart, paths.app / 'source'):
            path.unlink(missing_ok=True)
        if paths.environment.is_symlink():
            paths.environment.unlink()
        if environment is not None:
            shutil.rmtree(environment)
        _service('daemon-reload')
        print('Uninstalled HINT. Settings and saved actions were kept.')


def main(argv=None):
    parser = argparse.ArgumentParser(prog='hint --ready', description='Install or update HINT and prepare its user service.')
    parser.add_argument('--ready', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--install', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--uninstall', action='store_true', help=argparse.SUPPRESS)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--server', metavar='URL')
    mode.add_argument('--local', action='store_true')
    parser.add_argument('--enable-wake', action='store_true')
    args = parser.parse_args(argv)
    try:
        if sys.version_info < (3, 8):
            raise ReadyError('HINT requires Python 3.8 or newer.')
        if os.geteuid() == 0:
            raise ReadyError('Run this as your normal desktop user, without sudo.')
        paths = Paths.current()
        if args.uninstall:
            if args.ready or args.install or args.server is not None or args.local or args.enable_wake:
                raise ReadyError('Uninstall does not accept installation options.')
            uninstall(paths)
            return 0
        source = find_source(paths)
        if args.install:
            install(source, args, paths)
        else:
            ready(source, args, paths)
        return 0
    except (ReadyError, HintError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        try:
            from hint_config import sanitize_error
            reason = sanitize_error(error)
        except ImportError:
            reason = str(error)
        print(reason, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('HINT setup interrupted.', file=sys.stderr)
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
