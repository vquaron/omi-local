import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dev_harness import local_provider_health as health
from dev_harness.local_providers import Registry


@pytest.fixture
def cfg(tmp_path):
    state = tmp_path / 'state'
    state.mkdir()
    (state / 'stt-engine.json').write_text(json.dumps({'engine': 'openai-compatible',
        'provider_url': 'http://127.0.0.1:10301/v1', 'model': 'synthetic', 'language': 'auto', 'diarization_model': 'none'}))
    return SimpleNamespace(repo_root=tmp_path, backend_port=20000,
                           layout=SimpleNamespace(state_root=state, services_dir=tmp_path / 'services'))


def activate(registry, profile, key=None):
    state = registry.read()
    saved = registry.save({'revision': state['revision'], 'profile': profile, **({'api_key': key} if key else {})})
    saved_profile = saved['profiles'][-1]
    registry.activate({'revision': saved['revision'], 'stage': profile['stage'], 'id': saved_profile['id']},
                      prepare=lambda _snapshot, publish: publish())
    return saved_profile


def test_no_registry_reports_only_the_legacy_engine(cfg):
    report = health.collect(cfg, probe=lambda *_a, **_k: pytest.fail('legacy engines are not probed'))
    assert report['version'] == 1 and report['stages'] == {}


def test_report_probes_remote_stages_and_never_leaks_keys(cfg, tmp_path):
    registry = Registry(cfg)
    activate(registry, {'name': 'GPU whisper', 'stage': 'stt', 'kind': 'openai-compatible',
                        'settings': {'provider_url': 'http://100.100.12.34:8001/v1', 'model': 'large-v3-turbo', 'language': 'auto'}},
             key='private-test-key')
    activate(registry, {'name': 'GPU pyannote', 'stage': 'diarization', 'kind': 'mycelia',
                        'settings': {'base_url': 'http://100.100.12.34:8085'}})
    probed = []

    def probe(_cfg, profile, *, key='', models=False):
        probed.append((profile['stage'], key))
        return {'state': 'ready' if profile['stage'] == 'stt' else 'unavailable', 'message': 'private text', 'latency_ms': 7}

    report = health.collect(cfg, probe=probe, now=1_700_000_000)
    assert sorted(probed) == [('diarization', ''), ('stt', 'private-test-key')]
    assert report['stages']['stt'] == {'status': 'ready', 'kind': 'openai-compatible', 'name': 'GPU whisper', 'latency_ms': 7}
    assert report['stages']['diarization']['status'] == 'unavailable'
    assert report['stages']['live'] == {'status': 'disabled'} and report['stages']['summary'] == {'status': 'disabled'}
    assert report['checked_at'] == 1_700_000_000
    serialized = json.dumps(report)
    assert 'private' not in serialized and 'message' not in serialized

    health.write(cfg, report)
    path = health.path(cfg)
    assert json.loads(path.read_text()) == report
    assert (os.stat(path).st_mode & 0o777) == 0o600


def test_local_engines_count_as_ready_without_a_probe(cfg):
    registry = Registry(cfg)
    activate(registry, {'name': 'This Mac', 'stage': 'stt', 'kind': 'whisperkit',
                        'settings': {'model': 'openai_whisper-large-v3-v20240930_626MB', 'language': 'auto'}})
    report = health.collect(cfg, probe=lambda *_a, **_k: pytest.fail('local engines are validated by the worker'))
    assert report['stages']['stt'] == {'status': 'ready', 'kind': 'whisperkit', 'name': 'This Mac'}


def test_monitor_writes_on_first_tick_then_on_interval(cfg):
    calls = []
    monitor = health.Monitor(cfg, interval=60, collect=lambda _cfg, now: calls.append(now) or {'version': 1, 'checked_at': now, 'stages': {}})
    monitor.tick(now=1000)
    monitor.tick(now=1030)
    monitor.tick(now=1061)
    assert calls == [1000, 1061]
    assert json.loads(health.path(cfg).read_text())['checked_at'] == 1061


def test_probe_errors_do_not_stop_the_worker(cfg):
    registry = Registry(cfg)
    activate(registry, {'name': 'LLM', 'stage': 'summary', 'kind': 'openai-compatible',
                        'settings': {'base_url': 'http://100.100.12.34:11434/v1', 'model': 'm'}})

    def probe(*_a, **_k):
        raise RuntimeError('boom')

    report = health.collect(cfg, probe=probe)
    assert report['stages']['summary']['status'] == 'unavailable'
