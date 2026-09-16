import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dev_harness import config, safety
from dev_harness.local_mac import endpoint, pairing_data, private_json, prepare_emulator, require_auth_boundary


def env_fixture(tmp_path):
    path = tmp_path / '.env'
    path.write_text('OMI_NGROK_URL=https://synthetic.ngrok.app\n'
                    'NGROK_AUTHTOKEN=synthetic-agent-token-1234\n'
                    'OMI_LOCAL_APP_KEY=' + 'a' * 43 + '\n')
    path.chmod(0o600)
    return path


def test_private_env_validation_and_no_shell_expansion(tmp_path):
    from dev_harness.local_env import read_env, LocalEnvError
    path = env_fixture(tmp_path)
    assert read_env(path)['OMI_LOCAL_APP_KEY'] == 'a' * 43
    path.chmod(0o644)
    with pytest.raises(LocalEnvError, match='chmod 600'):
        read_env(path)
    path.chmod(0o600)
    original = path.read_text()
    path.write_text(original + 'OMI_LOCAL_APP_KEY=duplicate\n')
    with pytest.raises(LocalEnvError, match='exactly once'):
        read_env(path)
    path.write_text(original.replace('synthetic-agent-token-1234', '$(touch injected)'))
    with pytest.raises(LocalEnvError, match='NGROK_AUTHTOKEN'):
        read_env(path)
    assert not (tmp_path / 'injected').exists()
    link = tmp_path / 'linked.env'
    link.symlink_to(path)
    with pytest.raises(LocalEnvError, match='symlinks'):
        read_env(link)


def test_noninteractive_env_configuration_preserves_live_settings_and_never_prints_secrets(
    monkeypatch, tmp_path, capsys,
):
    from types import SimpleNamespace
    from dev_harness import local_mac, local_env
    env_path = env_fixture(tmp_path)
    cfg = SimpleNamespace(repo_root=tmp_path, layout=SimpleNamespace(state_root=tmp_path / 'state'), backend_port=20000)
    monkeypatch.setattr(local_mac.sys.stdin, 'isatty', lambda: False)
    monkeypatch.setattr(local_mac.cli, '_service_record', lambda *a: None)
    local_mac.configure(cfg)
    pairing = cfg.layout.state_root / 'pairing.json'
    assert 'a' * 43 not in pairing.read_text()
    before = pairing.read_bytes()
    monkeypatch.setattr(local_mac.cli, '_service_record', lambda *a: {'pid': 123})
    local_mac.configure(cfg)
    assert pairing.read_bytes() == before
    env_path.write_text(env_path.read_text().replace('a' * 43, 'b' * 43))
    with pytest.raises(local_env.LocalEnvError, match='Stop the local stack'):
        local_mac.configure(cfg)
    assert pairing.read_bytes() == before
    output = capsys.readouterr().out
    assert 'a' * 43 not in output and 'b' * 43 not in output and 'synthetic-agent-token' not in output


def test_env_initialization_never_overwrites_existing_settings(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    from dev_harness import local_env
    cfg = SimpleNamespace(repo_root=tmp_path, layout=SimpleNamespace(state_root=tmp_path / 'state'))
    monkeypatch.setattr(local_env.Path, 'home', lambda: tmp_path / 'home')
    local_env.initialize(cfg)
    path = tmp_path / '.env'
    assert path.stat().st_mode & 0o777 == 0o600
    original = path.read_bytes()
    with pytest.raises(local_env.LocalEnvError, match='already exists'):
        local_env.initialize(cfg)
    assert path.read_bytes() == original
    key = next(line.split('=', 1)[1] for line in path.read_text().splitlines() if line.startswith('OMI_LOCAL_APP_KEY='))
    assert len(key) == 43 and key not in capsys.readouterr().out


def iphone_fixture(*, tunnel='connected'):
    return {
        'hardwareProperties': {'deviceType': 'iPhone', 'udid': 'synthetic-phone'},
        'connectionProperties': {
            'pairingState': 'paired', 'transportType': 'localNetwork', 'tunnelState': tunnel,
        },
    }


def iphone_app_fixture(tmp_path):
    import plistlib
    app = tmp_path / 'Runner.app'
    app.mkdir()
    (app / 'Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier': 'com.omi.local.synthetic'}))
    return app


def test_iphone_launch_probes_stale_wifi_status_and_forwards_only_app_pairing(tmp_path, monkeypatch, capsys):
    from dev_harness import launch_iphone
    env_path = env_fixture(tmp_path)
    app = iphone_app_fixture(tmp_path)
    calls = []
    def capture(args, *, env=None):
        calls.append((args, env))
        if 'devices' in args:
            return json.dumps({'result': {'devices': [iphone_fixture(tunnel='disconnected')]}}).encode()
        if 'details' in args:
            return json.dumps({'result': iphone_fixture()}).encode()
        if 'lockState' in args:
            return json.dumps({'result': {'unlockedSinceBoot': True, 'passcodeRequired': False}}).encode()
        return json.dumps({'info': {'outcome': 'success'}}).encode()
    monkeypatch.setattr(launch_iphone, 'capture', capture)
    monkeypatch.setenv('NGROK_AUTHTOKEN', 'synthetic-agent-token-1234')
    monkeypatch.setenv('DEVICECTL_CHILD_NGROK_AUTHTOKEN', 'synthetic-agent-token-1234')
    launch_iphone.launch(env_path, app)
    assert [args[4] for args, _ in calls if 'info' in args] == ['details', 'lockState']
    args, env = calls[-1]
    assert 'launch' in args
    assert env['DEVICECTL_CHILD_OMI_LOCAL_MAC_KEY'] == 'a' * 43
    assert env['DEVICECTL_CHILD_OMI_LOCAL_MAC_URL'] == 'https://synthetic.ngrok.app'
    assert 'NGROK_AUTHTOKEN' not in env and 'DEVICECTL_CHILD_NGROK_AUTHTOKEN' not in env
    assert all('a' * 43 not in str(args) and 'synthetic-agent-token' not in str(args) for args, _ in calls)
    assert all(env is None for _, env in calls[:-1])
    output = capsys.readouterr().out
    assert 'a' * 43 not in output and 'synthetic-agent-token' not in output and 'synthetic-phone' not in output


@pytest.mark.parametrize('failure', ['locked', 'not-unlocked-since-boot', 'offline', 'probe-failed', 'ambiguous'])
def test_iphone_launch_rejects_unready_device_without_launch_or_secrets(tmp_path, monkeypatch, capsys, failure):
    from dev_harness import launch_iphone
    env_path = env_fixture(tmp_path)
    app = iphone_app_fixture(tmp_path)
    calls = []
    def capture(args, *, env=None):
        calls.append((args, env))
        if 'devices' in args:
            phones = [iphone_fixture()] * (2 if failure == 'ambiguous' else 1)
            return json.dumps({'result': {'devices': phones}}).encode()
        if 'details' in args:
            if failure == 'probe-failed':
                raise launch_iphone.LocalEnvError('iPhone operation failed')
            return json.dumps({'result': iphone_fixture(
                tunnel='disconnected' if failure == 'offline' else 'connected',
            )}).encode()
        if 'lockState' in args:
            return json.dumps({'result': {
                'unlockedSinceBoot': failure != 'not-unlocked-since-boot',
                'passcodeRequired': failure == 'locked',
            }}).encode()
        return b''
    monkeypatch.setattr(launch_iphone, 'capture', capture)
    with pytest.raises(launch_iphone.LocalEnvError) as error:
        launch_iphone.launch(env_path, app)
    assert not any('launch' in args for args, _ in calls)
    assert all(env is None for _, env in calls)
    visible = capsys.readouterr().out + str(error.value)
    assert 'a' * 43 not in visible and 'synthetic-agent-token' not in visible and 'synthetic-phone' not in visible


def test_iphone_build_selection_can_skip_unlock_but_still_probes_connection(monkeypatch):
    from dev_harness import launch_iphone
    calls = []
    def capture(args, *, env=None):
        calls.append(args)
        if 'devices' in args:
            return json.dumps({'result': {'devices': [iphone_fixture(tunnel='disconnected')]}}).encode()
        assert 'details' in args
        return json.dumps({'result': iphone_fixture()}).encode()
    monkeypatch.setattr(launch_iphone, 'capture', capture)
    assert launch_iphone.select_iphone(require_unlocked=False) == iphone_fixture()
    assert len(calls) == 2


@pytest.mark.parametrize(
    'url',
    [
        'http://demo.ngrok.app',
        'https://key@demo.ngrok.app',
        'https://demo.ngrok.app/path',
        'https://demo.ngrok.app?key=x',
        'https://demo.ngrok.app:8000',
        'https://127.0.0.1',
    ],
)
def test_bad_endpoint_rejected(url):
    with pytest.raises(ValueError):
        endpoint(url)


def test_endpoint_and_private_pairing(tmp_path):
    assert endpoint(' https://demo.ngrok.app/ ') == 'https://demo.ngrok.app'
    path = tmp_path / 'pairing.json'
    key = 'a' * 43
    private_json(path, pairing_data(key))
    assert path.stat().st_mode & 0o777 == 0o600
    assert key not in path.read_text()
    assert json.loads(path.read_text())['owner_uid'] == 'alice'
    private_json(path, pairing_data('b' * 43))
    assert json.loads(path.read_text()) == pairing_data('b' * 43)


def test_tunnel_requires_loopback_offline_and_disables_admin(monkeypatch, tmp_path):
    repo = Path(__file__).resolve().parents[3]
    source = {'PROVIDER_MODE': 'offline', 'OMI_LOCAL_TRANSPORT': 'ngrok', 'OMI_DEV_BIND_HOST': '127.0.0.1'}
    cfg = config.load_config(repo, source)
    child = config.child_env_for(cfg)
    assert child['OMI_LOCAL_TRANSPORT'] == 'ngrok'
    assert child['ADMIN_KEY_AUTH_ENABLED'] == 'false'
    assert child['OMI_LOCAL_PAIRING_FILE'].endswith('/pairing.json')
    with pytest.raises(safety.SafetyError):
        config.load_config(repo, {**source, 'OMI_DEV_BIND_HOST': '192.168.50.2'})


def test_public_startup_requires_observed_key_rejection(monkeypatch):
    import io
    import urllib.error
    import urllib.request

    monkeypatch.setattr(urllib.request, 'urlopen', lambda *a, **k: io.BytesIO(b'healthy legacy server'))
    with pytest.raises(ValueError, match='key boundary'):
        require_auth_boundary('http://127.0.0.1:20000')
    checked = []

    def denied(request, **kwargs):
        checked.append(bool(request.get_header('Authorization')))
        raise urllib.error.HTTPError(
            request.full_url, 401, 'Unauthorized', {}, io.BytesIO(b'{"detail":"local_auth_required"}')
        )

    monkeypatch.setattr(urllib.request, 'urlopen', denied)
    require_auth_boundary('http://127.0.0.1:20000')
    assert checked == [False, True]


def test_owner_profile_is_created_only_when_live_document_is_missing(monkeypatch):
    import io
    import urllib.error
    from types import SimpleNamespace
    from dev_harness import local_mac

    cfg = SimpleNamespace(local_transport='ngrok', provider_mode='offline', dev_bind_host='127.0.0.1',
                          firestore_host='127.0.0.1:20085', project_id='demo-omi-local', database_id='(default)')
    saved = None
    writes = []

    class Response(io.BytesIO):
        status = 200

    def request(req, **kwargs):
        nonlocal saved
        assert req.get_header('Authorization') == 'Bearer owner'
        if req.get_method() == 'POST':
            assert saved is None
            saved = json.loads(req.data)
            writes.append(saved)
        elif saved is None:
            raise urllib.error.HTTPError(req.full_url, 404, 'Not found', {}, io.BytesIO())
        return Response(json.dumps(saved).encode())

    monkeypatch.setattr(local_mac.urllib.request, 'urlopen', request)
    local_mac.ensure_owner_profile(cfg, 'alice')
    assert saved['fields']['uid']['stringValue'] == 'alice'
    saved['fields']['user_setting'] = {'stringValue': 'preserve-me'}
    local_mac.ensure_owner_profile(cfg, 'alice')
    assert len(writes) == 1
    assert saved['fields']['user_setting']['stringValue'] == 'preserve-me'


def test_owner_profile_check_fails_closed_on_emulator_failure(monkeypatch):
    import io
    import urllib.error
    from types import SimpleNamespace
    from dev_harness import local_mac

    cfg = SimpleNamespace(local_transport='ngrok', provider_mode='offline', dev_bind_host='127.0.0.1',
                          firestore_host='127.0.0.1:20085', project_id='demo-omi-local', database_id='(default)')
    def unavailable(req, **kwargs):
        assert req.get_method() == 'GET'
        raise urllib.error.HTTPError(req.full_url, 503, 'Unavailable', {}, io.BytesIO())

    monkeypatch.setattr(local_mac.urllib.request, 'urlopen', unavailable)
    with pytest.raises(local_mac.LocalMacError, match='check the local owner'):
        local_mac.ensure_owner_profile(cfg, 'alice')


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX process signal contract')
def test_supervisor_delivers_one_graceful_signal_to_service(tmp_path):
    import os
    import signal
    import subprocess
    import time
    from dev_harness import cli

    ready, result = tmp_path / 'ready', tmp_path / 'result'
    child = '''import json, pathlib, signal, sys, time
count = 0
def stop(signum, frame):
    global count
    count += 1
signal.signal(signal.SIGINT, stop)
pathlib.Path(sys.argv[1]).touch()
while not count: time.sleep(0.01)
time.sleep(0.3)
pathlib.Path(sys.argv[2]).write_text(json.dumps(count))
'''
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1])}
    process = subprocess.Popen(
        [sys.executable, '-m', 'dev_harness.supervise', '--marker', 'synthetic-signal-test', '--service', 'fixture',
         '--', sys.executable, '-c', child, str(ready), str(result)],
        env=env, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists()
        cli._signal_owned_supervisor(process.pid, 'fixture')
        assert process.wait(timeout=5) == 0
        assert json.loads(result.read_text()) == 1
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


@pytest.mark.parametrize("custom_cache", [False, True])
def test_emulator_reuse_checks_actual_archive(monkeypatch, tmp_path, custom_cache):
    import hashlib

    repo = tmp_path / 'repo'
    meta = repo / 'node_modules/firebase-tools/lib/emulator/downloadableEmulatorInfo.json'
    meta.parent.mkdir(parents=True)
    content = b'synthetic emulator archive'
    meta.write_text(
        json.dumps(
            {
                'firestore': {
                    'downloadPathRelativeToCacheDir': 'test.jar',
                    'expectedSize': len(content),
                    'expectedChecksum': hashlib.md5(content).hexdigest(),
                }
            }
        )
    )
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.delenv('FIREBASE_EMULATORS_PATH', raising=False)
    cache = tmp_path / '.cache/firebase/emulators'
    if custom_cache:
        cache = tmp_path / 'separate cache'
        monkeypatch.setenv('FIREBASE_EMULATORS_PATH', str(cache))
    archive = cache / 'test.jar'
    archive.parent.mkdir(parents=True)
    archive.write_bytes(content)
    before = archive.stat().st_mtime_ns
    prepare_emulator(repo)
    assert archive.stat().st_mtime_ns == before


@pytest.mark.parametrize(
    'input_path', ['backend/.python-version', 'backend/pylock.macos.toml', 'package.json', 'package-lock.json']
)
@pytest.mark.parametrize('empty', [False, True])
def test_installer_rejects_missing_or_empty_inputs_before_system_changes(tmp_path, input_path, empty):
    import os
    import shutil
    import subprocess

    repo = tmp_path / 'repo'
    (repo / 'scripts').mkdir(parents=True)
    installer = Path(__file__).resolve().parents[3] / 'scripts/install-local-mac.sh'
    shutil.copy2(installer, repo / 'scripts/install-local-mac.sh')
    shutil.copy2(installer.with_name('macos-runtime.sh'), repo / 'scripts/macos-runtime.sh')
    for name in ['backend/.python-version', 'backend/pylock.macos.toml', 'package.json', 'package-lock.json']:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('synthetic installer input')
    missing = repo / input_path
    if empty:
        missing.write_text('')
    else:
        missing.unlink()
    marker = tmp_path / 'system-change-attempted'
    shell_env = tmp_path / 'shell-env'
    shell_env.write_text(
        '''uname() { case "$1" in -s) echo Darwin;; -m) echo arm64;; esac; }
brew() { touch "$OMI_TEST_SYSTEM_MARKER"; return 1; }
'''
    )
    result = subprocess.run(
        ['bash', 'scripts/install-local-mac.sh'],
        cwd=repo,
        capture_output=True,
        text=True,
        env={**os.environ, 'BASH_ENV': str(shell_env), 'OMI_TEST_SYSTEM_MARKER': str(marker)},
    )
    assert result.returncode != 0
    assert f'Missing installer input: {input_path}' in result.stderr
    assert not marker.exists()


def test_wizard_preserves_pairing_on_repeat_and_address_edit(monkeypatch, tmp_path, capsys):
    from types import SimpleNamespace
    from dev_harness import local_mac

    cfg = SimpleNamespace(repo_root=tmp_path, layout=SimpleNamespace(state_root=tmp_path), backend_port=20000)
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(sys.stdout, 'isatty', lambda: True)
    monkeypatch.setattr(local_mac.cli, '_service_record', lambda *a: None)
    monkeypatch.setattr('builtins.input', lambda *a: 'https://synthetic.ngrok.app')
    monkeypatch.setattr(local_mac.getpass, 'getpass', lambda *a: 'synthetic-token-for-unit-test')
    monkeypatch.setattr(local_mac.secrets, 'token_urlsafe', lambda n: 'a' * 43)
    local_mac.configure(cfg)
    initial = (tmp_path / 'pairing.json').read_bytes()
    assert b'a' * 43 not in initial
    local_mac.configure(cfg)
    monkeypatch.setattr('builtins.input', lambda *a: 'https://changed.ngrok.app')
    monkeypatch.setattr(local_mac.getpass, 'getpass', lambda *a: '')
    local_mac.configure(cfg, edit=True)
    assert (tmp_path / 'pairing.json').read_bytes() == initial
    assert local_mac.read_config(cfg)['url'] == 'https://changed.ngrok.app'
    monkeypatch.setattr(local_mac.secrets, 'token_urlsafe', lambda n: 'b' * 43)
    local_mac.configure(cfg, rotate=True)
    assert json.loads((tmp_path / 'pairing.json').read_text()) == pairing_data('b' * 43)
    assert 'synthetic-token-for-unit-test' not in capsys.readouterr().out


@pytest.fixture
def env_wizard(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from dev_harness import local_mac

    cfg = SimpleNamespace(repo_root=tmp_path, layout=SimpleNamespace(state_root=tmp_path / 'state'),
                          backend_port=20000)
    monkeypatch.setattr(local_mac, 'sys', SimpleNamespace(
        stdin=SimpleNamespace(isatty=lambda: True), stdout=SimpleNamespace(isatty=lambda: True),
    ))
    monkeypatch.setattr(local_mac.cli, '_service_record', lambda *a: None)
    monkeypatch.setattr(local_mac.secrets, 'token_urlsafe', lambda _: 'a' * 43)

    def unexpected_prompt(*args):
        pytest.fail('Values present in .env must not require interactive entry')

    monkeypatch.setattr('builtins.input', unexpected_prompt)
    monkeypatch.setattr(local_mac.getpass, 'getpass', unexpected_prompt)
    path = tmp_path / '.env'
    path.write_text('NGROK_URL=https://synthetic.ngrok.app\nNGROK_AUTHTOKEN=synthetic-agent-token-123456\n')
    path.chmod(0o600)
    return cfg, path


def test_env_wizard_reads_both_values_and_only_explicit_edit_reloads(env_wizard, monkeypatch, capsys):
    from dev_harness import local_mac

    cfg, path = env_wizard
    monkeypatch.delenv('NGROK_AUTHTOKEN', raising=False)
    local_mac.configure(cfg)
    state = cfg.layout.state_root
    assert local_mac.read_config(cfg)['url'] == 'https://synthetic.ngrok.app'
    saved = json.loads((state / 'ngrok-agent.yml').read_text())
    assert saved['agent']['authtoken'] == 'synthetic-agent-token-123456'
    assert (state / 'ngrok-agent.yml').stat().st_mode & 0o777 == 0o600
    assert 'NGROK_AUTHTOKEN' not in local_mac.os.environ
    pairing = (state / 'pairing.json').read_bytes()
    path.write_text('NGROK_URL=https://changed.ngrok.app\nNGROK_AUTHTOKEN=synthetic-replacement-123456\n')
    local_mac.configure(cfg)
    assert local_mac.read_config(cfg)['url'] == 'https://synthetic.ngrok.app'
    local_mac.configure(cfg, edit=True)
    assert local_mac.read_config(cfg)['url'] == 'https://changed.ngrok.app'
    assert json.loads((state / 'ngrok-agent.yml').read_text())['agent']['authtoken'] == 'synthetic-replacement-123456'
    assert (state / 'pairing.json').read_bytes() == pairing
    output = capsys.readouterr()
    assert 'synthetic-agent-token' not in output.out + output.err
    assert 'synthetic-replacement' not in output.out + output.err


@pytest.mark.parametrize('contents', [
    'NGROK_URL=http://synthetic.ngrok.app\nNGROK_AUTHTOKEN=synthetic-agent-token-123456\n',
    'NGROK_URL=https://synthetic.ngrok.app\nNGROK_AUTHTOKEN=bad-token\n',
    'NGROK_URL=https://synthetic.ngrok.app\nNGROK_AUTHTOKEN=${SYNTHETIC_TOKEN}\n',
])
def test_env_wizard_rejects_invalid_values_before_writing(env_wizard, monkeypatch, contents):
    from dev_harness import local_mac

    cfg, path = env_wizard
    path.write_text(contents)
    monkeypatch.setenv('SYNTHETIC_TOKEN', 'synthetic-agent-token-123456')
    with pytest.raises(local_mac.LocalMacError):
        local_mac.configure(cfg)
    assert not cfg.layout.state_root.exists()


@pytest.mark.parametrize('unsafe_path', ['readable', 'symlink'])
def test_env_wizard_requires_private_regular_file(env_wizard, unsafe_path):
    from dev_harness import local_mac

    cfg, path = env_wizard
    if unsafe_path == 'readable':
        path.chmod(0o644)
    else:
        target = path.with_name('secret-env')
        path.rename(target)
        path.symlink_to(target)
    with pytest.raises(local_mac.LocalMacError, match=r'\.env'):
        local_mac.configure(cfg)
    assert not cfg.layout.state_root.exists()


def test_env_wizard_prompts_only_for_missing_value(env_wizard, monkeypatch):
    from dev_harness import local_mac

    cfg, path = env_wizard
    path.write_text('NGROK_URL=https://synthetic.ngrok.app\n')
    prompts = []
    monkeypatch.setattr(local_mac.getpass, 'getpass', lambda prompt: prompts.append(prompt) or 'synthetic-token-123456')
    local_mac.configure(cfg)
    assert len(prompts) == 1
    assert local_mac.read_config(cfg)['url'] == 'https://synthetic.ngrok.app'


def test_env_wizard_still_requires_terminal_for_pairing_key(env_wizard, monkeypatch):
    from dev_harness import local_mac

    cfg, _ = env_wizard
    monkeypatch.setattr(local_mac.sys.stdout, 'isatty', lambda: False)
    with pytest.raises(local_mac.LocalMacError, match='local interactive terminal'):
        local_mac.configure(cfg)
    assert not cfg.layout.state_root.exists()


@pytest.mark.parametrize('native_cache', [False, True])
def test_ios_cache_move_cleans_only_existing_native_metadata(tmp_path, native_cache):
    import os
    import subprocess

    app = tmp_path / 'app'
    (app / 'build/test_cache').mkdir(parents=True)
    if native_cache:
        (app / 'build/ios').mkdir()
    (app / 'ios/Flutter/ephemeral/Packages/FlutterGeneratedPluginSwiftPackage').mkdir(parents=True)
    temporary = tmp_path / 'temp'
    temporary.mkdir()
    marker = tmp_path / 'native-clean-called'
    setup = Path(__file__).resolve().parents[3] / 'app/setup.sh'
    command = '''set -euo pipefail
source "$OMI_TEST_SETUP" >/dev/null
uname() { echo Darwin; }
xattr() { return 0; }
xcodebuild() { touch "$OMI_TEST_CALL_FILE"; }
prepare_personal_ios_build_dir
'''
    result = subprocess.run(
        ['bash', '-c', command],
        cwd=app,
        capture_output=True,
        env={**os.environ, 'TMPDIR': str(temporary), 'OMI_TEST_SETUP': str(setup), 'OMI_TEST_CALL_FILE': str(marker)},
    )
    assert result.returncode == 0
    assert (app / 'build').is_symlink()
    assert marker.exists() == native_cache


@pytest.mark.skipif(sys.platform != 'darwin', reason='Personal Team preparation uses macOS PlistBuddy')
def test_fresh_personal_setup_creates_required_custom_config(tmp_path):
    import os
    import plistlib
    import subprocess

    for folder in ['ios/Flutter', 'ios/Config/Dev', 'ios/Runner']:
        (tmp_path / folder).mkdir(parents=True)
    for folder in ['ios/Config/Dev', 'ios/Runner']:
        (tmp_path / folder / 'GoogleService-Info.plist').write_bytes(plistlib.dumps({'BUNDLE_ID': 'com.example.old'}))
    setup = Path(__file__).resolve().parents[3] / 'app/setup.sh'
    result = subprocess.run(
        ['bash', '-c', 'source "$OMI_TEST_SETUP" >/dev/null; generate_ios_custom_config Dev omi-dev true'],
        cwd=tmp_path,
        capture_output=True,
        env={**os.environ, 'OMI_TEST_SETUP': str(setup), 'OMI_PERSONAL_BUNDLE_ID': 'com.example.omi.local'},
    )
    assert result.returncode == 0
    assert (tmp_path / 'ios/Flutter/Custom.xcconfig').is_file()
    for folder in ['ios/Config/Dev', 'ios/Runner']:
        assert (
            plistlib.loads((tmp_path / folder / 'GoogleService-Info.plist').read_bytes())['BUNDLE_ID']
            == 'com.example.omi.local'
        )


def test_manual_no_speech_is_successful_and_content_free(monkeypatch, capsys):
    from dev_harness import local_mac, local_stt
    monkeypatch.setattr(sys, 'argv', ['local-mac', 'transcribe', 'synthetic.wav'])
    monkeypatch.setattr(config, 'load_config', lambda *a, **kw: object())
    def no_speech(*a, **kw):
        raise local_stt.NoSpeechDetected('No speech detected; original WAV retained')
    monkeypatch.setattr(local_stt, 'transcribe', no_speech)
    assert local_mac.main() == 0
    output = capsys.readouterr()
    assert output.out.strip() == 'No speech detected. Recording preserved.'
    assert not output.err
