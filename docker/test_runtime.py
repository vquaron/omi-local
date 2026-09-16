"""Portable runtime contracts; no Docker daemon, models or network required."""
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
import wave

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts/dev-harness'), str(ROOT / 'docker')]
import runtime
import stt
from dev_harness import config, local_env


def wav_bytes():
    out = io.BytesIO()
    with wave.open(out, 'wb') as wav:
        wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        wav.writeframes(b'\0\0' * 16000)
    return out.getvalue()


def test_timed_stt_core_and_bad_input():
    calls = []

    class Model:
        def transcribe(self, audio, **kwargs):
            calls.append(kwargs)
            segment = SimpleNamespace(start=0., end=1., text=' test',
                                      words=[SimpleNamespace(start=0., end=1., word=' test')])
            return iter([segment]), SimpleNamespace(language='en', duration=1.)

    with TestClient(stt.create_app(Model)) as client:
        assert client.get('/v1/models').json()['data'][0]['id'] == stt.MODEL
        payload = {'model': stt.MODEL, 'response_format': 'verbose_json'}
        response = client.post('/v1/audio/transcriptions', data=payload, files={'file': ('audio.wav', wav_bytes())})
        assert response.status_code == 200
        assert response.json()['segments'][0]['words'][0]['end'] == 1.
        assert calls == [{'language': None, 'word_timestamps': True, 'vad_filter': True, 'beam_size': 5}]
        assert client.post('/v1/audio/transcriptions', data=payload, files={'file': ('bad.wav', b'bad')}).status_code == 400
        payload['model'] = 'unknown'
        assert client.post('/v1/audio/transcriptions', data=payload, files={'file': ('audio.wav', wav_bytes())}).status_code == 400
        assert len(calls) == 1


def test_inference_failure_does_not_leak_content_or_keep_lock():
    class Model:
        def transcribe(self, *args, **kwargs):
            raise RuntimeError('private fixture text')
    with TestClient(stt.create_app(Model)) as client:
        for _ in range(2):
            response = client.post('/v1/audio/transcriptions', data={'model': stt.MODEL},
                                   files={'file': ('audio.wav', wav_bytes())})
            assert response.status_code == 503
            assert 'private fixture' not in response.text


def test_missing_model_or_gpu_fails_startup():
    def unavailable():
        raise RuntimeError('No CUDA or cache')
    with pytest.raises(RuntimeError):
        with TestClient(stt.create_app(unavailable)):
            pass


def test_initialization_preserves_identity_settings_and_existing_archive(tmp_path):
    root, data = tmp_path / 'repo', tmp_path / 'data'
    root.mkdir()
    data.mkdir()
    env = {'PROVIDER_MODE': 'offline', 'OMI_LOCAL_TRANSPORT': 'ngrok', 'OMI_DEV_BIND_HOST': '127.0.0.1',
           'OMI_LOCAL_INSTANCE': 'docker', 'OMI_LOCAL_STATE_ROOT': str(data / 'harness'),
           'OMI_HARNESS_PORT_OFFSET': '13000'}
    cfg = config.load_config(root, env, create_layout=True)
    capture = cfg.layout.services_dir / 'storage/listen-captures/existing'
    capture.mkdir(parents=True)
    first = runtime.prepare(cfg, root, data)
    assert config.child_env_for(cfg)['OMI_LOCAL_LIVE_PREVIEW_URL'] == ''
    live = cfg.layout.state_root / 'live-preview.json'
    live.write_text('{"enabled": true, "url": "ws://127.0.0.1:19090/asr"}')
    settings = cfg.layout.state_root / 'stt-engine.json'
    settings.write_text('{"custom": true}')
    second = runtime.prepare(cfg, root, data)
    assert first == second == local_env.read_env(root / '.env')
    assert config.child_env_for(cfg)['OMI_LOCAL_LIVE_PREVIEW_URL'] == 'ws://127.0.0.1:19090/asr'
    assert json.loads(settings.read_text()) == {'custom': True}
    assert len(json.loads((cfg.layout.state_root / 'stt-watch.json').read_text())['excluded']) == 1
    assert (root / '.env').stat().st_mode & 0o077 == 0
    assert (data / 'connection.env').stat().st_mode & 0o077 == 0


def test_backend_reload_is_explicit_and_loopback_only():
    cfg = SimpleNamespace(repo_root=ROOT, backend_port=21000)
    assert '--reload' not in runtime.backend_command(cfg)
    command = runtime.backend_command(cfg, reload=True)
    assert command[command.index('--host') + 1] == '127.0.0.1'
    assert '--no-proxy-headers' in command and '--reload' in command


def test_tailscale_volume_needs_no_ngrok_and_keeps_identity_when_switching(tmp_path):
    root, data = tmp_path / 'repo', tmp_path / 'data'
    root.mkdir()
    data.mkdir()
    env = {'PROVIDER_MODE': 'offline', 'OMI_LOCAL_TRANSPORT': 'tailscale', 'OMI_DEV_BIND_HOST': '127.0.0.1',
           'OMI_LOCAL_INSTANCE': 'docker', 'OMI_LOCAL_STATE_ROOT': str(data / 'harness'),
           'OMI_HARNESS_PORT_OFFSET': '13000'}
    cfg = config.load_config(root, env, create_layout=True)
    values = runtime.prepare(cfg, root, data)
    assert values['OMI_LOCAL_TRANSPORT'] == 'tailscale'
    assert not {'NGROK_AUTHTOKEN', 'OMI_NGROK_URL'} & values.keys()
    assert not (data / 'tunnel-url').exists()
    assert not (cfg.layout.state_root / 'ngrok-agent.yml').exists()
    identity = (cfg.layout.state_root / 'pairing.json').read_bytes()
    providers = cfg.layout.state_root / 'stt-engine.json'
    providers.write_text('{"custom": true}')
    volume_settings = (data / 'connection.env').read_bytes()
    env['OMI_LOCAL_TRANSPORT'] = 'ngrok'
    cfg = config.load_config(root, env, create_layout=True)
    ngrok = runtime.prepare(cfg, root, data)
    assert ngrok['OMI_LOCAL_APP_KEY'] == values['OMI_LOCAL_APP_KEY']
    assert (cfg.layout.state_root / 'pairing.json').read_bytes() == identity
    assert (data / 'connection.env').read_bytes() == volume_settings
    assert json.loads(providers.read_text()) == {'custom': True}
    env['OMI_LOCAL_TRANSPORT'] = 'tailscale'
    cfg = config.load_config(root, env, create_layout=True)
    assert runtime.prepare(cfg, root, data)['OMI_LOCAL_APP_KEY'] == values['OMI_LOCAL_APP_KEY']
    assert (cfg.layout.state_root / 'pairing.json').read_bytes() == identity


def test_legacy_ngrok_volume_can_select_tailscale_without_changing_saved_values(tmp_path):
    root, data = tmp_path / 'repo', tmp_path / 'data'
    root.mkdir()
    data.mkdir()
    env = {'PROVIDER_MODE': 'offline', 'OMI_LOCAL_TRANSPORT': 'ngrok', 'OMI_DEV_BIND_HOST': '127.0.0.1',
           'OMI_LOCAL_INSTANCE': 'docker', 'OMI_LOCAL_STATE_ROOT': str(data / 'harness')}
    cfg = config.load_config(root, env, create_layout=True)
    original = runtime.prepare(cfg, root, data)
    private = data / 'connection.env'
    private.write_text(private.read_text().replace('OMI_LOCAL_TRANSPORT=ngrok\n', ''))
    before = {path: path.read_bytes() for path in
              (private, cfg.layout.state_root / 'pairing.json', cfg.layout.state_root / 'ngrok-agent.yml')}
    env['OMI_LOCAL_TRANSPORT'] = 'tailscale'
    cfg = config.load_config(root, env, create_layout=True)
    active = runtime.prepare(cfg, root, data)
    assert active['OMI_LOCAL_TRANSPORT'] == 'tailscale'
    assert active['OMI_LOCAL_APP_KEY'] == original['OMI_LOCAL_APP_KEY']
    assert all(path.read_bytes() == content for path, content in before.items())


def docker_wrapper(tmp_path, args, *, transport='local', saved=True, **overrides):
    repo = tmp_path / 'project with spaces'
    repo.mkdir(exist_ok=True)
    shutil.copy2(ROOT / 'docker.sh', repo / 'docker.sh')
    shutil.copy2(ROOT / 'omiloc', repo / 'omiloc')
    settings = repo / '.env.docker'
    if saved and not settings.exists():
        settings.write_text(f'OMI_DOCKER_DEVICE=cpu\nOMI_DOCKER_TRANSPORT={transport}\nSTT_REVISION=' + 'a' * 40 + '\n')
        settings.chmod(0o600)
    # Native credentials must not be read by Docker, even in the same checkout.
    (repo / '.env').write_text('native-private-fixture-do-not-read')
    fake = tmp_path / 'bin'
    fake.mkdir(exist_ok=True)
    calls = tmp_path / 'calls.jsonl'
    calls.write_text('')
    docker = fake / 'docker'
    docker.write_text(f'#!{sys.executable}\n' + '''import json, os, sys
args = sys.argv[1:]
if args[:2] == ["context", "inspect"]:
    print(os.environ.get("DOCKER_TEST_ENDPOINT", "unix:///fixture/docker.sock"))
    sys.exit(0)
if args[:2] == ["compose", "version"]:
    print("2.40.3")
    sys.exit(0)
with open(os.environ["DOCKER_CALLS"], "a") as f:
    f.write(json.dumps({"args": args, "ip": os.environ.get("OMI_TAILSCALE_IP"),
                       "device": os.environ.get("OMI_DOCKER_DEVICE"),
                       "gpu": os.environ.get("OMI_DOCKER_GPU_ID"),
                       "revision": os.environ.get("STT_REVISION")}) + "\\n")
if "ps" in args and "-q" in args and os.environ.get("DOCKER_TEST_RUNNING"):
    print("fixture-running-container")
if "run" in args and "download" in args:
    if os.environ.get("DOCKER_FAIL") == "download": sys.exit(7)
    print("Model revision: " + "b" * 40)
if "run" in args and "stt" in args and os.environ.get("DOCKER_FAIL") == "cuda":
    print("CUDA unavailable", file=sys.stderr)
    sys.exit(7)
if "up" in args and os.environ.get("DOCKER_FAIL") == "up": sys.exit(7)
''')
    docker.chmod(0o755)
    tailscale = fake / 'tailscale'
    tailscale.write_text(f'#!{sys.executable}\nimport json, os, sys\n'
                         'if sys.argv[1] == "status":\n'
                         '    print(json.dumps({"BackendState": os.environ.get("TS_STATE", "Running"), '
                         '"Self": {"Online": os.environ.get("TS_ONLINE", "true") == "true"}}, indent=2))\n'
                         'else:\n'
                         '    print(os.environ.get("TS_IP", "100.100.12.34"))\n')
    tailscale.chmod(0o755)
    env = dict(os.environ, PATH=str(fake) + os.pathsep + os.environ['PATH'],
               DOCKER_CALLS=str(calls), OMI_TAILSCALE_CLI=str(tailscale))
    for name in ('OMI_LOCAL_TRANSPORT', 'OMI_DOCKER_TRANSPORT', 'OMI_DOCKER_DEVICE', 'OMI_DOCKER_GPU_ID',
                 'OMI_TAILSCALE_IP', 'DOCKER_HOST', 'DOCKER_CONTEXT', 'STT_MODEL', 'STT_REVISION', 'STT_THREADS'):
        env.pop(name, None)
    env.update(overrides)
    result = subprocess.run(['bash', str(repo / 'omiloc'), '--runtime', 'docker', *args],
                            cwd=tmp_path, capture_output=True, text=True, env=env)
    recorded = [json.loads(line) for line in calls.read_text().splitlines()]
    return result, recorded


@pytest.mark.parametrize('command', ['up', 'dev'])
def test_wrapper_start_reuses_saved_selection_without_build_or_download(tmp_path, command):
    result, calls = docker_wrapper(tmp_path, [command], transport='tailscale')
    assert result.returncode == 0, result.stderr
    assert all('compose.tailscale.yaml' in c['args'] for c in calls)
    assert all(c['ip'] == '100.100.12.34' for c in calls)
    assert not any('build' in c['args'] or 'run' in c['args'] for c in calls)
    startup = next(c['args'] for c in calls if 'up' in c['args'])
    assert '--no-build' in startup and '--pull' in startup and 'never' in startup
    assert ('--watch' in startup) == (command == 'dev')
    assert ('--wait' in startup) == (command == 'up')


@pytest.mark.parametrize('env', [
    {'TS_STATE': 'Stopped'}, {'TS_STATE': 'NeedsLogin'}, {'TS_STATE': 'Starting'}, {'TS_ONLINE': 'false'},
    {'OMI_TAILSCALE_IP': '0.0.0.0'}, {'OMI_TAILSCALE_IP': '100.100.12.35'},
    {'TS_IP': '100.63.12.34'}, {'TS_IP': '100.128.12.34'}, {'TS_IP': '100.100.256.34'},
    {'DOCKER_TEST_ENDPOINT': 'ssh://remote.example'}, {'DOCKER_HOST': 'tcp://remote.example:2376'},
])
def test_wrapper_tailscale_rejects_unavailable_or_foreign_ip_before_start(tmp_path, env):
    result, calls = docker_wrapper(tmp_path, ['up'], transport='tailscale', **env)
    assert result.returncode != 0
    assert calls == []
    assert '100.100.12.35' not in result.stdout + result.stderr


@pytest.mark.parametrize('args', [['down'], ['status'], ['logs'], ['logs', '-f']])
def test_wrapper_recovery_needs_neither_tailscale_nor_gpu(tmp_path, args):
    result, calls = docker_wrapper(tmp_path, args, transport='tailscale', TS_STATE='Stopped', OMI_DOCKER_DEVICE='cuda')
    assert result.returncode == 0, result.stderr
    assert all('compose.tailscale.yaml' not in c['args'] and 'compose.gpu.yaml' not in c['args'] for c in calls)
    assert not any('info' in c['args'] or 'run' in c['args'] for c in calls)


def test_bootstrap_saves_gpu_and_immutable_model_without_starting_stack(tmp_path):
    args = ['bootstrap', '--device', 'cuda', '--gpu', 'GPU-abcd-1234', '--transport', 'tailscale']
    result, calls = docker_wrapper(tmp_path, args, saved=False)
    assert result.returncode == 0, result.stderr
    assert not any('up' in c['args'] for c in calls)
    assert all(c['gpu'] == 'GPU-abcd-1234' and c['device'] == 'cuda' for c in calls)
    settings = tmp_path / 'project with spaces/.env.docker'
    first = settings.read_bytes()
    assert b'OMI_DOCKER_DEVICE=cuda' in first and b'STT_REVISION=' + b'b' * 40 in first
    assert settings.stat().st_mode & 0o077 == 0
    result, _ = docker_wrapper(tmp_path, ['bootstrap'])
    assert result.returncode == 0 and settings.read_bytes() == first
    result, calls = docker_wrapper(tmp_path, ['up'], OMI_DOCKER_DEVICE='cpu')
    assert result.returncode == 0, result.stderr
    assert all(c['device'] == 'cuda' for c in calls)  # persisted choice wins over ambient env
    assert 'native-private-fixture' not in result.stdout + result.stderr


@pytest.mark.parametrize('failure', ['download', 'cuda'])
def test_bootstrap_failure_preserves_config_and_can_resume(tmp_path, failure):
    result, _ = docker_wrapper(tmp_path, ['bootstrap', '--device', 'cuda'], DOCKER_FAIL=failure)
    assert result.returncode != 0
    settings = tmp_path / 'project with spaces/.env.docker'
    assert 'OMI_DOCKER_DEVICE=cpu' in settings.read_text()
    result, calls = docker_wrapper(tmp_path, ['up'])
    assert result.returncode != 0 and not any('up' in c['args'] for c in calls)
    result, _ = docker_wrapper(tmp_path, ['bootstrap', '--device', 'cuda'])
    assert result.returncode == 0, result.stderr
    assert 'OMI_DOCKER_DEVICE=cuda' in settings.read_text()


def test_bootstrap_refuses_to_rebuild_running_stack(tmp_path):
    result, calls = docker_wrapper(tmp_path, ['bootstrap'], DOCKER_TEST_RUNNING='1')
    assert result.returncode != 0
    assert not any('build' in c['args'] or 'run' in c['args'] for c in calls)


def test_missing_bootstrap_and_noninteractive_pair_fail_without_secret_output(tmp_path):
    result, calls = docker_wrapper(tmp_path, ['up'], saved=False)
    assert result.returncode != 0 and 'bootstrap' in result.stderr and not calls
    result, calls = docker_wrapper(tmp_path, ['pair'])
    assert result.returncode != 0 and not calls


def test_docker_config_is_never_executed(tmp_path):
    docker_wrapper(tmp_path, ['status'])
    repo = tmp_path / 'project with spaces'
    (repo / '.env.docker').write_text('STT_MODEL=$(touch injected)\n')
    result, calls = docker_wrapper(tmp_path, ['up'])
    assert result.returncode != 0 and not calls
    assert not (repo / 'injected').exists()
    assert 'touch injected' not in result.stderr


def test_tailscale_compose_render_publishes_only_paired_api_on_host_vpn():
    docker = shutil.which('docker')
    if not docker:
        pytest.skip('Docker Compose CLI unavailable; no daemon is needed for this check')
    plugin = subprocess.run([docker, 'compose', 'version'], capture_output=True)
    if plugin.returncode:
        pytest.skip('Docker Compose plugin unavailable')
    result = subprocess.run([docker, 'compose', '--env-file', '/dev/null', '-f', 'compose.yaml',
                             '-f', 'compose.tailscale.yaml', '-f', 'compose.gpu.yaml',
                             '-f', 'compose.dev.yaml', 'config', '--format', 'json'],
                            cwd=ROOT, env=dict(os.environ, OMI_TAILSCALE_IP='100.100.12.34'),
                            capture_output=True, text=True, check=True)
    services = json.loads(result.stdout)['services']
    ports = {(p['host_ip'], str(p['published']), p['target']) for p in services['ingress']['ports']}
    assert ports == {('127.0.0.1', '21000', 22000), ('127.0.0.1', '21001', 22001),
                     ('100.100.12.34', '21000', 22000)}
    assert services['app']['environment']['OMI_LOCAL_TRANSPORT'] == 'tailscale'
    assert services['app']['environment']['OMI_CONTAINER_RUNTIME'] == '1'
    assert services['app']['environment']['OMI_DEV_BIND_HOST'] == '127.0.0.1'
    for name in ('app', 'stt'):
        assert services[name]['network_mode'] == 'service:ingress'
        assert 'ports' not in services[name]
    assert services['app']['volumes'][0]['type'] == 'volume'
    gpu = services['stt']['deploy']['resources']['reservations']['devices'][0]
    assert gpu['device_ids'] == ['0'] and 'count' not in gpu


def test_runtime_pins_keep_vcs_commit_and_platform_markers():
    result = subprocess.run([sys.executable, str(ROOT / 'docker/export_pins.py'),
                             str(ROOT / 'backend/pylock.runtime.toml')], capture_output=True, text=True, check=True)
    assert 'git+https://github.com/TeamPyOgg/PyOgg@6871a4f234e8a3a346c4874a12509bfa02c4c63a' in result.stdout
    assert "lc3py==1.1.3; platform_machine == 'x86_64'" in result.stdout


def test_supervisor_exit_is_detected_even_before_parent_reaps(tmp_path):
    process = tmp_path / '42'
    process.mkdir()
    (process / 'stat').write_text('42 (python worker) S 1 42 42')
    assert runtime.supervisor_running(42, tmp_path)
    (process / 'stat').write_text('42 (python worker) Z 1 42 42')
    assert not runtime.supervisor_running(42, tmp_path)
    assert not runtime.supervisor_running(43, tmp_path)


def test_shutdown_reaps_owned_children(monkeypatch):
    import os
    import time
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
    monkeypatch.setattr(runtime.cli, '_process_records', lambda cfg: [{'pid': child.pid}])

    def stop(cfg):
        child.terminate()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                os.kill(child.pid, 0)
            except ProcessLookupError:
                return
            time.sleep(.05)
        pytest.fail('Exited supervisor was not reaped')

    monkeypatch.setattr(runtime.cli, '_stop_owned', stop)
    try:
        runtime.stop_owned(None)
    finally:
        child.wait(timeout=3)
