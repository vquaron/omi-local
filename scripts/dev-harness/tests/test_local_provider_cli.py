import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dev_harness import local_provider_probe, local_provider_cli
from dev_harness.local_provider_api import Settings
from dev_harness.local_provider_cli import execute, parser
from dev_harness.local_providers import SettingsError
from dev_harness.local_stt_services import ActivationIndeterminate, ServiceError


@pytest.fixture
def settings(tmp_path):
    state = tmp_path / 'state'
    state.mkdir()
    (state / 'stt-engine.json').write_text(json.dumps({'engine': 'openai-compatible',
        'provider_url': 'http://127.0.0.1:10301/v1', 'model': 'synthetic',
        'language': 'auto', 'diarization_model': 'none'}))
    return Settings(SimpleNamespace(repo_root=tmp_path, backend_port=20000,
                    layout=SimpleNamespace(state_root=state, services_dir=tmp_path / 'services')))


def remote():
    return {'id': 'remote-stt', 'name': 'Remote transcription', 'stage': 'stt', 'kind': 'openai-compatible',
            'settings': {'provider_url': 'https://stt.example/v1', 'model': 'synthetic', 'diarization_model': 'none'}}


def run(settings, *args, body='', env=None):
    stdout = io.StringIO()
    code = execute(parser().parse_args(args), settings, stdin=io.StringIO(body), stdout=stdout, environ=env or {})
    return code, stdout.getvalue()


def test_save_export_apply_roundtrip_keeps_credentials_and_selection_separate(settings, tmp_path, monkeypatch):
    run(settings, 'save', '--file', '-', '--api-key-env', 'TEST_PROVIDER_KEY',
        body=json.dumps(remote()), env={'TEST_PROVIDER_KEY': 'synthetic-secret'})
    assert settings.read()['active']['stt'] == 'current-stt'
    exported = tmp_path / 'profiles.json'
    run(settings, 'export', '--file', str(exported))
    document = json.loads(exported.read_text())
    assert exported.stat().st_mode & 0o777 == 0o600
    assert 'synthetic-secret' not in exported.read_text()
    assert 'credential_ref' not in exported.read_text()
    with pytest.raises(SettingsError, match='new writable path'):
        run(settings, 'export', '--file', str(exported))
    document['profiles'][-1]['name'] = 'Remote renamed'
    run(settings, 'save', '--file', '-', body=json.dumps(document))
    profile = settings.read()['profiles'][-1]
    assert profile['has_key']
    assert settings.read()['active']['stt'] == 'current-stt'
    seen = []
    def available(_cfg, selected, *, key):
        seen.append((selected['id'], key))
        return {'state': 'available', 'message': 'Synthetic metadata ready'}
    monkeypatch.setattr(local_provider_probe, 'probe', available)
    run(settings, 'use', 'stt', 'Remote renamed')
    assert seen == [('remote-stt', 'synthetic-secret')]
    assert settings.read()['active']['stt'] == 'remote-stt'
    assert 'Remote renamed' in run(settings, 'list')[1]


def test_invalid_bundle_validates_before_writes_and_activation_failure_keeps_selection(settings, monkeypatch):
    invalid = {**remote(), 'id': 'invalid', 'settings': {'provider_url': 'https://stt.example/wrong'}}
    with pytest.raises(SettingsError):
        run(settings, 'save', '--file', '-', body=json.dumps({'version': 1, 'profiles': [remote(), invalid]}))
    assert not settings.registry.exists
    run(settings, 'save', '--file', '-', body=json.dumps(remote()))
    before = settings.read()
    monkeypatch.setattr(local_provider_probe, 'probe', lambda *_args, **_kwargs: {
        'state': 'unavailable', 'message': 'Synthetic service unavailable'})
    with pytest.raises(SettingsError, match='unavailable'):
        run(settings, 'use', 'stt', 'remote-stt')
    assert settings.read() == before
    with pytest.raises(SettingsError, match='not found'):
        run(settings, 'use', 'live', 'remote-stt')


def test_keys_cannot_be_imported_inline_and_empty_env_does_not_mutate(settings):
    with pytest.raises(SettingsError, match='--api-key-env'):
        run(settings, 'save', '--file', '-', body=json.dumps({**remote(), 'api_key': 'synthetic-secret'}))
    with pytest.raises(SettingsError, match='empty or unset'):
        run(settings, 'save', '--file', '-', '--api-key-env', 'TEST_PROVIDER_KEY', body=json.dumps(remote()))
    assert not settings.registry.exists


def test_legacy_embedded_diarization_keeps_its_execution_mode_after_json_roundtrip(settings):
    source = settings.registry.root / 'stt-engine.json'
    source.write_text(json.dumps({'engine': 'whisperx', 'model': 'small', 'language': 'ru',
                                 'diarization_model': 'pyannote/speaker-diarization-community-1'}))
    before = settings.registry.snapshot('diarization')
    assert before['embedded']
    _, exported = run(settings, 'export')
    document = json.loads(exported)
    document['profiles'][-1]['name'] = 'Renamed embedded speakers'
    run(settings, 'save', '--file', '-', body=json.dumps(document))
    saved = settings.read()['profiles'][-1]
    assert saved['embedded'] is True
    assert settings.registry.snapshot('diarization') == before


def test_bundle_ids_and_key_options_are_unambiguous(settings):
    for profiles in ([remote(), remote()], [remote(), {**remote(), 'id': ['invalid']}]):
        with pytest.raises(SettingsError, match='unique strings'):
            run(settings, 'save', '--file', '-', body=json.dumps({'version': 1, 'profiles': profiles}))
    with pytest.raises(SettingsError, match='exactly one'):
        run(settings, 'save', '--file', '-', '--clear-key',
            body=json.dumps({'version': 1, 'profiles': [remote(), {**remote(), 'id': 'other'}]}))
    assert not settings.registry.exists


@pytest.mark.parametrize('transport', ['ngrok', 'tailscale'])
def test_main_uses_saved_native_transport_without_requiring_live_vpn(settings, tmp_path, monkeypatch, transport):
    monkeypatch.chdir(tmp_path)
    for name in ('OMI_TAILSCALE_IP', 'OMI_CONTAINER_RUNTIME', 'OMI_LOCAL_STATE_ROOT'):
        monkeypatch.delenv(name, raising=False)
    # prepare_environment writes this key directly; track it even when the
    # caller had no value so the next suite does not inherit this transport.
    monkeypatch.setenv('OMI_LOCAL_TRANSPORT', '')
    monkeypatch.setenv('PROVIDER_MODE', 'offline')
    monkeypatch.setenv('OMI_DEV_BIND_HOST', '127.0.0.1')
    private = tmp_path / '.env'
    private.write_text(f'OMI_LOCAL_TRANSPORT={transport}\n')
    private.chmod(0o600)
    def factory(cfg):
        assert cfg.local_transport == transport
        return settings
    monkeypatch.setattr(local_provider_cli, 'Settings', factory)
    assert local_provider_cli.main(['disable', 'diarization']) == 0
    assert settings.read()['active']['diarization'] is None


@pytest.mark.parametrize('template', ['processing.local.json', 'processing.remote.json'])
def test_documented_templates_save_as_drafts(settings, template):
    example = Path(__file__).resolve().parents[3] / 'docs/examples' / template
    run(settings, 'save', '--file', str(example))
    assert settings.read()['active']['stt'] == 'current-stt'
    assert settings.read()['active']['live'] is None


@pytest.mark.parametrize('failure,expected', [
    (ServiceError('Stop the current recording before applying live settings'), 'Stop the current recording'),
    (ActivationIndeterminate('Backend readiness is indeterminate'), 'Inspect providers list'),
])
def test_main_preserves_safe_activation_diagnostics(settings, monkeypatch, capsys, failure, expected):
    monkeypatch.setenv('OMI_CONTAINER_RUNTIME', '1')
    monkeypatch.setattr(local_provider_cli.config, 'load_config', lambda *_args, **_kwargs: settings.cfg)
    monkeypatch.setattr(local_provider_cli, 'Settings', lambda _cfg: settings)
    def fail(*_args):
        raise failure
    monkeypatch.setattr(settings, 'dispatch', fail)
    assert local_provider_cli.main(['disable', 'live']) == 1
    assert expected in capsys.readouterr().err
