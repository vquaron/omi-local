from __future__ import annotations

import uuid
import wave

import pytest

from utils.local_audio_delivery import initialize_local_capture


@pytest.mark.parametrize('outbox', [False, True])
def test_legacy_client_keeps_server_wav_and_updated_client_previews_without_duplicate(tmp_path, monkeypatch, outbox):
    root = tmp_path / 'state/services/storage'
    monkeypatch.setenv('OMI_ENV_STAGE', 'offline')
    monkeypatch.setenv('PROVIDER_MODE', 'offline')
    monkeypatch.setenv('OMI_HARNESS_STATE_ROOT', str(tmp_path / 'state'))
    monkeypatch.setenv('OMI_LOCAL_STORAGE_ROOT', str(root))
    monkeypatch.setenv('FIREBASE_PROJECT_ID', 'demo-omi-local')
    monkeypatch.setenv('FIRESTORE_EMULATOR_HOST', '127.0.0.1:8085')
    preview_only, sink = initialize_local_capture(
        offline=True, session_id=str(uuid.uuid4()), headers={'x-omi-local-audio-outbox': '1'} if outbox else {},
        codec='pcm16', source='phone', sample_rate=16000, channels=1)
    pcm = b'\x01\x00' * 160
    if outbox:
        assert preview_only and sink is None
        assert not (root / 'listen-captures').exists()
    else:
        assert not preview_only
        sink.record_decoded_frame(encoded_bytes=len(pcm), pcm=pcm)
        sink.finalize()
        with wave.open(str(sink.wav_path)) as audio:
            assert audio.readframes(160) == pcm
