import asyncio
import json
from types import SimpleNamespace

import pytest

from routers.listen import local_status
from utils import local_transcription_status


def write(tmp_path, payload):
    (tmp_path / 'provider-health.json').write_text(json.dumps(payload))


def test_provider_stages_are_absent_without_the_harness_report(monkeypatch, tmp_path):
    monkeypatch.delenv('OMI_HARNESS_STATE_ROOT', raising=False)
    assert local_transcription_status.provider_stages() is None
    monkeypatch.setenv('OMI_HARNESS_STATE_ROOT', str(tmp_path))
    assert local_transcription_status.provider_stages() is None


def test_provider_stages_keep_only_fixed_fields_and_flag_stale_reports(monkeypatch, tmp_path):
    monkeypatch.setenv('OMI_HARNESS_STATE_ROOT', str(tmp_path))
    write(tmp_path, {'version': 1, 'checked_at': 1_000, 'stages': {
        'stt': {'status': 'ready', 'kind': 'openai-compatible', 'name': 'GPU whisper', 'latency_ms': 12, 'message': 'private'},
        'diarization': {'status': 'unavailable', 'kind': 'mycelia', 'name': 'x' * 300},
        'live': {'status': 'disabled'},
        'summary': {'status': 'weird'},
        'bogus': {'status': 'ready'},
    }})
    stages = local_transcription_status.provider_stages(now=1_100)
    assert stages == {
        'checked_at': 1_000, 'stale': False,
        'stt': {'status': 'ready', 'kind': 'openai-compatible', 'name': 'GPU whisper'},
        'diarization': {'status': 'unavailable', 'kind': 'mycelia', 'name': 'x' * 100},
        'live': {'status': 'disabled'},
        'summary': {'status': 'unknown'},
    }
    assert local_transcription_status.provider_stages(now=1_000 + 400)['stale'] is True


@pytest.mark.parametrize('raw', ['not json', '[]', json.dumps({'version': 2, 'stages': {}}), json.dumps({'version': 1, 'stages': []})])
def test_invalid_reports_are_ignored(monkeypatch, tmp_path, raw):
    monkeypatch.setenv('OMI_HARNESS_STATE_ROOT', str(tmp_path))
    (tmp_path / 'provider-health.json').write_text(raw)
    assert local_transcription_status.provider_stages() is None


def test_status_snapshot_includes_provider_stages(monkeypatch, tmp_path):
    monkeypatch.setenv('OMI_HARNESS_STATE_ROOT', str(tmp_path))
    monkeypatch.delenv('OMI_LOCAL_LIVE_PREVIEW_URL', raising=False)
    write(tmp_path, {'version': 1, 'checked_at': 5, 'stages': {'diarization': {'status': 'ready', 'kind': 'mycelia', 'name': 'GPU pyannote'}}})
    monkeypatch.setattr(local_status.registry, '_sessions_for', lambda uid: [])
    monkeypatch.setattr(local_status, 'preview_url', lambda: 'ws://127.0.0.1:8177/asr')

    async def busy_worker(_url):
        return {'state': 'busy', 'diarization': {'state': 'busy', 'labeled_segments': 0}}

    monkeypatch.setattr(local_status, '_worker_status', busy_worker)
    status = asyncio.run(local_status.snapshot('synthetic-user'))
    assert status['stages']['diarization'] == {'status': 'ready', 'kind': 'mycelia', 'name': 'GPU pyannote'}
    assert status['live_transcript']['state'] == 'busy'
    assert status['diarization'] == {'state': 'busy', 'labeled_segments': 0}
    (tmp_path / 'provider-health.json').unlink()
    status = asyncio.run(local_status.snapshot('synthetic-user'))
    assert 'stages' not in status
