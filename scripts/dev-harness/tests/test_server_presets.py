"""Host preset parsing and runtime-only provisioning; all credentials synthetic."""

import base64
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dev_harness import ios_debug, ios_launcher, launch_iphone, server_presets
from dev_harness.local_env import LocalEnvError


def write_presets(root, *, identity='home', name='Home Mac', url='100.64.0.1:21000', key=None):
    path = root / '.env.servers'
    key = key or 'preset-canary-'.ljust(43, 'x')
    path.write_text(f'OMI_SERVER_{identity}_NAME={name}\nOMI_SERVER_{identity}_URL={url}\nOMI_SERVER_{identity}_KEY={key}\n')
    path.chmod(0o600)
    return path, key


def decode(env):
    return json.loads(base64.b64decode(env[server_presets.CHILD_ENV]))


@pytest.mark.parametrize('source', ['catalog', 'single-env'])
def test_runtime_provisioning_requires_only_stdlib_without_site_packages(tmp_path, source):
    if source == 'catalog':
        path, key = write_presets(tmp_path)
        path.write_text(path.read_text() + 'OMI_SERVER_remote_NAME=Remote\n'
                        'OMI_SERVER_remote_URL=https://Synthetic.Example:443/\n'
                        f'OMI_SERVER_remote_KEY={key}\n')
    else:
        path = tmp_path / '.env'
        path.write_text('OMI_NGROK_URL=https://synthetic.example\n'
                        'NGROK_AUTHTOKEN=synthetic-agent-1234\n'
                        'OMI_LOCAL_APP_KEY=' + 's' * 43 + '\n')
        path.chmod(0o600)
    code = '''
import base64
import json
from pathlib import Path
import sys
from dev_harness import launch_iphone, server_presets

assert sys.flags.no_site == 1
assert server_presets.normalize_url('100.64.0.1') == 'http://100.64.0.1:20000/'
assert server_presets.normalize_url('http://100.64.0.1') == 'http://100.64.0.1:80/'
assert server_presets.normalize_url('https://Synthetic.Example:443/') == 'https://synthetic.example/'
env, hidden = server_presets.launch_environment(Path.cwd(), environ={}, required=True)
payload = json.loads(base64.b64decode(env[server_presets.CHILD_ENV]))
assert payload['version'] == 1
assert payload['servers'][-1]['url'] == 'https://synthetic.example/'
assert hidden and callable(launch_iphone.launch)
assert 'dotenv' not in sys.modules and 'dev_harness.local_mac' not in sys.modules
print('stdlib provisioning passed')
'''
    result = subprocess.run([sys.executable, '-S', '-c', code], cwd=tmp_path, timeout=5,
                            env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1])},
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'stdlib provisioning passed'


def test_private_literal_presets_normalize_and_do_not_execute_or_probe(tmp_path, monkeypatch):
    marker = tmp_path / 'must-not-exist'
    monkeypatch.chdir(tmp_path)
    path, key = write_presets(tmp_path, name="'$(touch must-not-exist)'")
    from dev_harness import local_transport
    monkeypatch.setattr(local_transport, 'tailscale_ip', lambda *_: pytest.fail('remote presets need no local VPN probe'))
    env, hidden = server_presets.launch_environment(tmp_path, environ={
        'NGROK_AUTHTOKEN': 'agent-canary', 'OMI_LOCAL_MAC_KEY': 'stale-canary',
        server_presets.CHILD_ENV: 'stale-payload',
    })
    payload = decode(env)
    assert payload['version'] == 1 and len(payload['servers']) == 1
    assert payload['servers'][0] == {'id': 'home', 'name': '$(touch must-not-exist)',
                                     'url': 'http://100.64.0.1:21000/', 'key': key}
    assert not marker.exists()
    assert key in hidden and env[server_presets.CHILD_ENV] in hidden
    assert 'NGROK_AUTHTOKEN' not in env and 'OMI_LOCAL_MAC_KEY' not in env
    assert path.read_text().find(key) >= 0


@pytest.mark.parametrize('defect', ['permissions', 'symlink', 'duplicate', 'missing', 'unknown', 'key', 'url', 'size', 'many'])
def test_invalid_presets_fail_closed_without_values_or_partial_payload(tmp_path, capsys, defect):
    path, key = write_presets(tmp_path)
    if defect == 'permissions':
        path.chmod(0o644)
    elif defect == 'symlink':
        target = tmp_path / 'private-source'
        path.rename(target)
        path.symlink_to(target)
    elif defect == 'duplicate':
        path.write_text(path.read_text() + 'OMI_SERVER_home_KEY=' + key + '\n')
    elif defect == 'missing':
        path.write_text('OMI_SERVER_home_NAME=Missing fields\n')
    elif defect == 'unknown':
        path.write_text(path.read_text() + 'PRIVATE_CANARY=' + key + '\n')
    elif defect == 'key':
        path.write_text(path.read_text().replace(key, 'private-invalid-key-canary'))
    elif defect == 'url':
        path.write_text(path.read_text().replace('100.64.0.1:21000', 'https://u:private-url-canary@example.com/'))
    elif defect == 'size':
        path.write_bytes(b'#' * (server_presets.MAX_BYTES + 1))
    elif defect == 'many':
        path.write_text(''.join(f'OMI_SERVER_s{i}_NAME=Server\nOMI_SERVER_s{i}_URL=100.64.0.1\n'
                                f'OMI_SERVER_s{i}_KEY={key}\n' for i in range(17)))
    with pytest.raises(LocalEnvError) as error:
        server_presets.launch_environment(tmp_path, environ={})
    captured = capsys.readouterr()
    visible = str(error.value) + captured.out + captured.err
    assert key not in visible and 'private-url-canary' not in visible and 'private-invalid-key-canary' not in visible


def test_explicit_file_wins_and_missing_explicit_file_never_uses_default(tmp_path):
    write_presets(tmp_path)
    selected = tmp_path / 'selected.env'
    selected.write_text('OMI_SERVER_gpu_NAME=GPU\nOMI_SERVER_gpu_URL=https://synthetic.example\n'
                        'OMI_SERVER_gpu_KEY=' + 'z' * 43 + '\n')
    selected.chmod(0o600)
    env, _ = server_presets.launch_environment(tmp_path, environ={server_presets.FILE_ENV: str(selected)})
    assert decode(env)['servers'][0]['id'] == 'gpu'
    assert decode(env)['servers'][0]['url'] == 'https://synthetic.example/'
    with pytest.raises(LocalEnvError):
        server_presets.launch_environment(tmp_path, environ={server_presets.FILE_ENV: 'not-present.env'})
    with pytest.raises(LocalEnvError):
        server_presets.launch_environment(tmp_path, environ={server_presets.FILE_ENV: ''})


def test_absent_presets_preserve_single_private_pairing_source_and_hash_only_setup(tmp_path):
    env, _ = server_presets.launch_environment(tmp_path, environ={})
    assert server_presets.CHILD_ENV not in env
    private = tmp_path / '.env'
    key = 's' * 43
    private.write_text('OMI_NGROK_URL=https://synthetic.example\nNGROK_AUTHTOKEN=synthetic-agent-1234\n'
                       'OMI_LOCAL_APP_KEY=' + key + '\n')
    private.chmod(0o600)
    env, _ = server_presets.launch_environment(tmp_path, environ={})
    assert decode(env)['servers'] == [{'id': 'local', 'name': 'Local Mac',
                                      'url': 'https://synthetic.example/', 'key': key}]
    assert all(name not in env for name in ('DEVICECTL_CHILD_OMI_LOCAL_MAC_KEY', 'DEVICECTL_CHILD_OMI_LOCAL_MAC_URL'))
    private.write_text('OMI_LOCAL_TRANSPORT=tailscale\nOMI_LOCAL_APP_KEY=\n')
    env, _ = server_presets.launch_environment(tmp_path, environ={})
    assert server_presets.CHILD_ENV not in env


def test_profile_launcher_validates_presets_without_sending_them_to_build_children(tmp_path, monkeypatch, capsys):
    _, key = write_presets(tmp_path)
    monkeypatch.setattr(ios_launcher, 'active_sessions', lambda: [])
    monkeypatch.setattr(ios_launcher.sys.stdin, 'isatty', lambda: True)
    observed = []
    def child(args, **kwargs):
        observed.append((args, kwargs['env']))
        assert key not in str(args)
        assert not any(name in kwargs['env'] for name in server_presets.HANDOFF_FIELDS)
        assert key not in str(kwargs['env'])
        return subprocess.CompletedProcess(args, 0)
    monkeypatch.setattr(ios_launcher.subprocess, 'run', child)
    assert ios_launcher.launch(tmp_path, 'profile', lock_path=tmp_path / 'lock') == 0
    assert len(observed) == 1 and key not in capsys.readouterr().out


def test_private_fifo_is_rejected_without_waiting_for_a_writer(tmp_path):
    fifo = tmp_path / '.env.servers'
    os.mkfifo(fifo, 0o600)
    code = ('from dev_harness.server_presets import load_presets; '
            'from pathlib import Path; load_presets(Path.cwd(), environ={})')
    result = subprocess.run([sys.executable, '-c', code], cwd=tmp_path, timeout=3,
                            env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1])},
                            capture_output=True, text=True)
    assert result.returncode != 0 and 'owner-only regular file' in result.stderr


@pytest.mark.parametrize('mode,check,build_only', [('profile', True, False), ('debug', False, False), ('debug', False, True)])
def test_non_profile_launch_paths_strip_ambient_handoff(tmp_path, monkeypatch, mode, check, build_only):
    monkeypatch.setattr(ios_launcher, 'active_sessions', lambda: [])
    monkeypatch.setattr(ios_launcher.sys.stdin, 'isatty', lambda: True)
    for name in server_presets.HANDOFF_FIELDS:
        monkeypatch.setenv(name, 'stale-secret-canary')
    def child(args, **kwargs):
        assert not any(name in kwargs['env'] for name in server_presets.HANDOFF_FIELDS)
        return subprocess.CompletedProcess(args, 0)
    monkeypatch.setattr(ios_launcher.subprocess, 'run', child)
    assert ios_launcher.launch(tmp_path, mode, check=check, build_only=build_only, lock_path=tmp_path / 'lock') == 0


def test_debug_preset_handoff_happens_only_for_final_attested_launch(tmp_path, monkeypatch, capsys):
    _, key = write_presets(tmp_path)
    (tmp_path / 'app/ios/Flutter').mkdir(parents=True)
    artifact = tmp_path / 'app/build/ios/Debug-dev-iphoneos/Runner.app'
    events = []
    monkeypatch.setattr(ios_debug, 'prepare', lambda _: ({}, 'TEAM', 'com.omi.local.test', 'PHONE', [], b'tools'))
    monkeypatch.setattr(ios_debug, 'configure_debug_identity', lambda *_: None)
    monkeypatch.setattr(ios_debug, 'fingerprint', lambda *_: 'inputs')
    monkeypatch.setattr(ios_debug, 'capture', lambda *_a, **_k: b'commit')
    monkeypatch.setattr(ios_debug, 'attest', lambda *_: ('digest', ['get-task-allow']))
    monkeypatch.setattr(ios_debug.sys.stdin, 'isatty', lambda: True)
    def child(args, root, env, hidden):
        events.append(args[1])
        assert key not in str(args)
        if args[1] == 'build':
            assert server_presets.CHILD_ENV not in env
            artifact.mkdir(parents=True)
        else:
            assert decode(env)['servers'][0]['key'] == key
            assert key in hidden
            visible = ios_debug.redact(key + ' ' + env[server_presets.CHILD_ENV], hidden)
            assert key not in visible and env[server_presets.CHILD_ENV] not in visible
    monkeypatch.setattr(ios_debug, 'run_flutter', child)
    ios_debug.session(tmp_path)
    assert events == ['build', 'run']
    record = (tmp_path / '.local/ios-debug.json').read_text()
    assert key not in record and key not in capsys.readouterr().out


def test_invalid_debug_presets_stop_before_identity_or_build(tmp_path, monkeypatch):
    path, _ = write_presets(tmp_path)
    path.chmod(0o644)
    monkeypatch.setattr(ios_debug, 'prepare', lambda _: ({}, 'TEAM', 'bundle', 'PHONE', [], b'tools'))
    monkeypatch.setattr(ios_debug.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(ios_debug, 'configure_debug_identity', lambda *_: pytest.fail('invalid presets must precede writes'))
    monkeypatch.setattr(ios_debug, 'run_flutter', lambda *_: pytest.fail('invalid presets must precede build'))
    with pytest.raises(LocalEnvError):
        ios_debug.session(tmp_path)


def test_profile_build_attest_install_then_runtime_handoff_and_reuse(tmp_path, monkeypatch, capsys):
    import plistlib
    from datetime import datetime, timedelta, timezone
    _, key = write_presets(tmp_path)
    signing = tmp_path / 'app/ios/Flutter/PersonalTeam.xcconfig'
    signing.parent.mkdir(parents=True)
    signing.write_text('OMI_PERSONAL_LOCAL=YES\nOMI_RUNTIME_MODE=offline\nOMI_APPLE_TEAM_ID=ABCDEFGHIJ\n'
                       'APP_BUNDLE_IDENTIFIER=com.omi.local.synthetic\n')
    signing.chmod(0o600)
    app = tmp_path / 'app/build/ios/Profile-dev-iphoneos/Runner.app'
    phone = {'identifier': 'SYNTHETIC-CORE', 'hardwareProperties': {'udid': 'SYNTHETIC-PHONE'}}
    monkeypatch.setattr(launch_iphone, 'select_iphone', lambda: phone)
    monkeypatch.setattr(launch_iphone, '_profile_inputs', lambda *_: 'synthetic-inputs')
    monkeypatch.delenv('OMI_IOS_DEVICE_ID', raising=False)
    events = []
    entitlements = {'application-identifier': 'ABCDEFGHIJ.com.omi.local.synthetic',
                    'com.apple.developer.team-identifier': 'ABCDEFGHIJ', 'get-task-allow': True}
    profile = {'ExpirationDate': (datetime.now(timezone.utc) + timedelta(days=2)).replace(tzinfo=None),
               'TeamIdentifier': ['ABCDEFGHIJ'], 'ProvisionedDevices': ['SYNTHETIC-PHONE']}
    def capture(args, *, env=None, cwd=None):
        assert key not in str(args)
        if 'process' not in args:
            assert env is None or not any(name in env for name in server_presets.HANDOFF_FIELDS)
        if args[:3] == ['git', 'rev-parse', 'HEAD']:
            return b'synthetic-commit'
        if args[:3] == ['flutter', 'build', 'ios']:
            assert '--profile' in args and '--no-pub' in args
            events.append('build')
            app.mkdir(parents=True)
            (app / 'Info.plist').write_bytes(plistlib.dumps({
                'CFBundleIdentifier': 'com.omi.local.synthetic', 'CFBundleExecutable': 'Runner'}))
            (app / 'Runner').write_bytes(b'synthetic profile executable')
            return b''
        if args[:2] == ['codesign', '--verify']:
            events.append('attest')
            return b''
        if args[0] == 'codesign':
            return plistlib.dumps(entitlements)
        if args[0] == 'security':
            return plistlib.dumps(profile)
        if 'install' in args:
            events.append('install')
            assert args[-1] == str(app)
            record = json.loads((tmp_path / '.local/ios-profile.json').read_text())
            assert record['executable_sha256'] and key not in json.dumps(record)
        elif 'process' in args:
            events.append('launch')
            assert decode(env)['servers'][0]['key'] == key
        else:
            pytest.fail('Unexpected native operation')
        return b'{"info":{"outcome":"success"}}'
    monkeypatch.setattr(launch_iphone, 'capture', capture)
    launch_iphone.build_profile(tmp_path / '.env', app, ['--dart-define=OMI_RUNTIME_MODE=offline'])
    assert events == ['build', 'attest', 'install', 'launch']
    assert key not in capsys.readouterr().out
    events.clear()
    launch_iphone.build_profile(tmp_path / '.env', app, ['--dart-define=OMI_RUNTIME_MODE=offline'])
    assert events == ['attest', 'attest', 'install', 'launch']
    assert 'Decision: reuse' in capsys.readouterr().out


def test_profile_rejects_failed_attestation_without_install_or_secret_handoff(tmp_path, monkeypatch):
    write_presets(tmp_path)
    app = tmp_path / 'app/build/ios/Profile-dev-iphoneos/Runner.app'
    phone = {'hardwareProperties': {'udid': 'SYNTHETIC-PHONE'}}
    monkeypatch.setattr(launch_iphone, 'select_iphone', lambda: phone)
    monkeypatch.setattr(launch_iphone, '_profile_inputs', lambda *_: 'inputs')
    monkeypatch.delenv('OMI_IOS_DEVICE_ID', raising=False)
    calls = []
    def capture(args, **kwargs):
        calls.append(args)
        assert not any(name in kwargs.get('env', {}) for name in server_presets.HANDOFF_FIELDS)
        if args[0] == 'git': return b'commit'
        assert args[:3] == ['flutter', 'build', 'ios']
        return b''
    def reject(*_):
        raise LocalEnvError('Synthetic signature verification failed')
    monkeypatch.setattr(launch_iphone, 'capture', capture)
    monkeypatch.setattr(launch_iphone, '_attest_profile', reject)
    with pytest.raises(LocalEnvError, match='signature'):
        launch_iphone.build_profile(tmp_path / '.env', app, [])
    assert not any('install' in args or 'process' in args for args in calls)


def test_profile_fingerprint_ignores_flutter_derived_rewrite_but_tracks_real_inputs(tmp_path, monkeypatch):
    signing = tmp_path / 'app/ios/Flutter/PersonalTeam.xcconfig'
    signing.parent.mkdir(parents=True)
    signing.write_text('private signing input')
    generated = signing.with_name('Generated.xcconfig')
    generated.write_text('FLUTTER_BUILD_MODE=debug\n')
    source = tmp_path / 'app/lib/real.dart'
    source.parent.mkdir(parents=True)
    source.write_text('original application source')
    def capture(args, **_kwargs):
        if args[:2] == ['git', 'ls-files']:
            return b'app/lib/real.dart\0app/ios/Flutter/Generated.xcconfig\0'
        return b'synthetic tool version'
    monkeypatch.setattr(launch_iphone, 'capture', capture)
    before = launch_iphone._profile_inputs(tmp_path, {}, ['--dart-define=OMI_RUNTIME_MODE=offline'])
    generated.write_text('FLUTTER_BUILD_MODE=profile\nDART_DEFINES=derived-profile-defines\n')
    assert launch_iphone._profile_inputs(tmp_path, {}, ['--dart-define=OMI_RUNTIME_MODE=offline']) == before
    source.write_text('changed application source')
    assert launch_iphone._profile_inputs(tmp_path, {}, ['--dart-define=OMI_RUNTIME_MODE=offline']) != before
    source.write_text('original application source')
    signing.write_text('changed private signing input')
    assert launch_iphone._profile_inputs(tmp_path, {}, ['--dart-define=OMI_RUNTIME_MODE=offline']) != before


def test_profile_genuine_build_input_change_blocks_install(tmp_path, monkeypatch):
    write_presets(tmp_path)
    app = tmp_path / 'app/build/ios/Profile-dev-iphoneos/Runner.app'
    monkeypatch.setattr(launch_iphone, 'select_iphone', lambda: {'hardwareProperties': {'udid': 'SYNTHETIC'}})
    inputs = iter(['before-build', 'changed-during-build'])
    monkeypatch.setattr(launch_iphone, '_profile_inputs', lambda *_: next(inputs))
    monkeypatch.delenv('OMI_IOS_DEVICE_ID', raising=False)
    def capture(args, **_kwargs):
        if args[0] == 'git':return b'commit'
        assert args[:3] == ['flutter', 'build', 'ios']
        return b''
    monkeypatch.setattr(launch_iphone, 'capture', capture)
    monkeypatch.setattr(launch_iphone, '_attest_profile', lambda *_: pytest.fail('changed source cannot be installed'))
    with pytest.raises(LocalEnvError, match='inputs changed'):
        launch_iphone.build_profile(tmp_path / '.env', app, [])
