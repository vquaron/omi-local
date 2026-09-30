import pytest

from utils.local_transcript import build_conversation


def manifest():
    return {'audio_sha256': 'a' * 64, 'result_key': 'b' * 64, 'duration_seconds': 4,
            'started_at': '2026-01-01T00:00:00Z', 'source': 'omi',
            'profile': {'engine': 'whisperx', 'model': 'large-v3-turbo'}}


def raw():
    return {'language': 'ru', 'segments': [
        {'text': 'Проверка API.', 'start': 0.111, 'end': 1.456, 'speaker': 'SPEAKER_01'},
        {'text': 'Да.', 'start': 2.123, 'end': 3.456, 'speaker': 'SPEAKER_00'},
    ]}


def test_existing_wire_format_case_times_speakers_and_stable_identity():
    before = raw()
    conversation = build_conversation(before, manifest())
    assert before == raw()
    assert conversation.model_dump(mode='json') == build_conversation(raw(), manifest()).model_dump(mode='json')
    assert [s.text for s in conversation.transcript_segments] == ['Проверка API.', 'Да.']
    assert [s.speaker_id for s in conversation.transcript_segments] == [1, 0]
    assert conversation.transcript_segments[0].start == 0.111
    assert all(not s.is_user and s.speaker_identity_status == 'unknown' for s in conversation.transcript_segments)
    assert conversation.status == 'completed' and not conversation.deferred
    assert not conversation.structured.action_items
    alternative = manifest()
    alternative['result_key'] = 'c' * 64
    assert build_conversation(raw(), alternative).id != conversation.id


@pytest.mark.parametrize('engine,provider', [('parakeet-mlx', 'parakeet-mlx-local'),
                                           ('whisperkit', 'whisperkit-local'),
                                           ('openai-compatible', 'openai-compatible-local')])
def test_alternative_engine_provenance_uses_same_mobile_contract(engine, provider):
    data = manifest()
    data['profile'] = {'engine': engine, 'model': 'synthetic-model'}
    conversation = build_conversation(raw(), data)
    assert all(s.stt_provider == provider for s in conversation.transcript_segments)
    assert conversation.external_data['local_transcript']['profile'] == data['profile']
    assert conversation.status == 'completed' and conversation.uses_custom_stt


def test_word_speaker_change_splits_sentence_without_losing_text():
    data = {'language': 'ru', 'segments': [{'text': 'Да. API работает.', 'start': 0, 'end': 3,
        'speaker': 'SPEAKER_00', 'words': [
            {'word': 'Да.', 'start': 0, 'end': 0.8, 'speaker': 'SPEAKER_00'},
            {'word': 'API', 'start': 1, 'end': 1.8, 'speaker': 'SPEAKER_01'},
            {'word': 'работает.', 'start': 2, 'end': 3, 'speaker': 'SPEAKER_01'},
        ]}]}
    conversation = build_conversation(data, manifest())
    assert [s.text for s in conversation.transcript_segments] == ['Да.', 'API работает.']
    assert [s.speaker_id for s in conversation.transcript_segments] == [0, 1]
    del data['segments'][0]['words'][1]['start']
    with pytest.raises(ValueError):
        build_conversation(data, manifest())


@pytest.mark.parametrize('start', [None, True, float('nan'), -0.1, 5])
def test_invalid_timestamp_never_creates_completed_conversation(start):
    data = raw()
    data['segments'][0]['start'] = start
    with pytest.raises(ValueError):
        build_conversation(data, manifest())


def test_empty_transcript_is_not_success_and_unknown_speaker_is_not_user():
    with pytest.raises(ValueError):
        build_conversation({'segments': []}, manifest())
    data = raw()
    del data['segments'][0]['speaker']
    first = build_conversation(data, manifest()).transcript_segments[0]
    assert first.speaker is None and first.speaker_id == -1 and not first.is_user


def test_selected_pipeline_summary_projects_into_existing_conversation_contract():
    data, result = manifest(), raw()
    data['profile']['pipeline'] = {'summary': {'kind': 'openai-compatible'}}
    result['structured'] = {'title': 'Обсуждение API', 'overview': 'Проверили работу API.'}
    conversation = build_conversation(result, data)
    assert conversation.structured.title == 'Обсуждение API'
    assert conversation.structured.overview == 'Проверили работу API.'
    assert conversation.external_data['local_transcript']['profile'] == data['profile']
    assert conversation.structured.action_items == []
    result['structured']['overview'] = {'invalid': True}
    with pytest.raises(ValueError, match='summary'):
        build_conversation(result, data)


@pytest.mark.parametrize('state', ['pending', 'processing', 'failed', 'no_speech'])
def test_saved_recording_stays_visible_until_import_succeeds(tmp_path, monkeypatch, state):
    import hashlib
    import json
    import uuid
    from utils import local_recording_rows as rows
    from utils.offline_audio_capture import OfflineAudioCapture

    storage = tmp_path / 'services/storage'
    results = tmp_path / 'services/local-transcripts'
    results.mkdir(parents=True)
    monkeypatch.setattr(rows, 'is_offline_runtime', lambda: True)
    monkeypatch.setattr(rows, 'local_tunnel_enabled', lambda: True)
    monkeypatch.setattr(rows, 'load_pairing', lambda: {'owner_uid': 'synthetic-owner'})
    monkeypatch.setattr(rows, 'local_storage_root_from_env', lambda: storage)
    monkeypatch.setenv('OMI_HARNESS_STATE_ROOT', str(tmp_path))
    capture = OfflineAudioCapture(session_id=str(uuid.uuid4()), input_codec='pcm16', source='phone', root=storage)
    capture.record_decoded_frame(encoded_bytes=32000, pcm=b'\x00\x00' * 16000)
    assert rows.local_recording_rows('synthetic-owner') == []  # unfinished input lives in the capture card
    capture.finalize()
    key = hashlib.sha256(capture.session_id.encode()).hexdigest()
    queue = results / 'watch-queue.json'
    queue.write_text(json.dumps({key: {'state': state, 'profile': {'api_key': 'never-return'}}}))
    pending = rows.local_recording_rows('synthetic-owner')
    assert len(pending) == 1
    assert pending[0]['status'] == 'processing'
    assert pending[0]['external_data'] == {'local_recording': {'status': state, 'duration_seconds': 1.0}}
    assert rows.local_recording_rows('another-owner') == []
    assert 'never-return' not in str(pending) and str(storage) not in str(pending)
    queue.write_text(json.dumps({key: {'state': 'completed'}}))
    assert rows.local_recording_rows('synthetic-owner') == []
    # Manual import also retires the projection even if no queue job exists.
    queue.write_text('{}')
    assert len(rows.local_recording_rows('synthetic-owner')) == 1
    (results / ('b' * 64)).mkdir()
    (results / ('b' * 64) / 'import.json').write_text(json.dumps({'import': 'passed', 'capture_key': key}))
    assert rows.local_recording_rows('synthetic-owner') == []


def test_pending_projection_obeys_filters_and_merged_page_offsets():
    from datetime import datetime, timezone
    from utils.local_recording_rows import filter_local_recording_rows, merge_local_recording_page

    now = datetime(2026, 9, 17, tzinfo=timezone.utc)
    pending = [{'id': 'pending', 'created_at': now, 'source': 'omi'}]
    filters = dict(statuses=['processing', 'completed'], sources=[], start_date=None, end_date=None,
                   folder_id=None, starred=None)
    assert filter_local_recording_rows(pending, **filters) == pending
    for key, value in [('statuses', ['completed']), ('sources', ['phone']), ('folder_id', 'folder'), ('starred', True)]:
        assert filter_local_recording_rows(pending, **{**filters, key: value}) == []
    completed = [{'id': 'completed', 'created_at': now.replace(day=16)}]
    assert merge_local_recording_page(completed, pending, offset=0, limit=1) == pending
    assert merge_local_recording_page(completed, pending, offset=1, limit=1) == completed
