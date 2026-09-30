import json
import sys
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dev_harness import local_stt, local_parakeet, local_whisperkit


def test_whisperkit_report_is_offline_and_keeps_words(tmp_path, monkeypatch):
    audio = tmp_path / 'audio.wav'
    audio.write_bytes(b'synthetic')
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-secret')
    monkeypatch.setattr(local_whisperkit.shutil, 'which', lambda _: '/usr/bin/sandbox-exec')
    def process(command, **kwargs):
        assert command[:3] == ['/usr/bin/sandbox-exec', '-p', '(version 1)(allow default)(deny network*)']
        assert '--model-path' in command and '--download-tokenizer-path' in command
        assert command[command.index('--concurrent-worker-count') + 1] == '1'
        assert kwargs['capture_output'] and 'OPENAI_API_KEY' not in kwargs['env']
        (tmp_path / 'audio.json').write_text(json.dumps({'language': 'ru', 'segments': [{
            'text': ' Проверка.', 'start': 0.1, 'end': 0.8,
            'words': [{'word': ' Проверка.', 'start': 0.1, 'end': 0.8, 'probability': 0.9}]}]}))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(local_whisperkit.subprocess, 'run', process)
    raw = local_whisperkit.transcribe(tmp_path, audio, tmp_path, 'ru', 1)
    segment = raw['segments'][0]
    assert segment['text'] == 'Проверка.' and segment['speaker'] == 'SPEAKER_00'
    assert segment['words'][0]['score'] == 0.9 and segment['words'][0]['end'] == 0.8


def test_whisperkit_failure_has_no_transcript_and_does_not_fall_back(tmp_path, monkeypatch):
    monkeypatch.setattr(local_whisperkit.shutil, 'which', lambda _: '/usr/bin/sandbox-exec')
    monkeypatch.setattr(local_whisperkit.subprocess, 'run', lambda *a, **kw:
                        SimpleNamespace(returncode=1, stdout=b'private synthetic speech'))
    with pytest.raises(local_whisperkit.WhisperKitError, match='original WAV retained') as error:
        local_whisperkit.transcribe(tmp_path, tmp_path / 'audio.wav', tmp_path, 'ru', 1)
    assert 'private' not in str(error.value)
    monkeypatch.setattr(local_whisperkit.shutil, 'which', lambda _: None)
    with pytest.raises(local_whisperkit.WhisperKitError, match='sandbox-exec'):
        local_whisperkit.transcribe(tmp_path, tmp_path / 'audio.wav', tmp_path, 'ru', 1)


@pytest.mark.parametrize('end', [float('nan'), 3, -1, 0.05, True])
def test_whisperkit_rejects_invalid_timestamps(end):
    with pytest.raises(local_whisperkit.WhisperKitError):
        local_whisperkit.normalize({'language': 'ru', 'segments': [
            {'text': 'Synthetic', 'start': 0.1, 'end': end}]}, 1)


def test_whisperkit_keeps_valid_speech_when_padding_produces_an_extra_segment():
    raw = {'language': 'ru', 'segments': [
        {'text': ' Проверка.', 'start': 0.02, 'end': 11.84,
         'words': [{'word': ' Проверка.', 'start': 0.02, 'end': 11.84, 'probability': 0.9}]},
        {'text': 'Synthetic padding', 'start': 40.94, 'end': 40.96,
         'words': [{'word': 'Synthetic padding', 'start': 40.94, 'end': 40.96, 'probability': 0.1}]},
    ]}
    result = local_whisperkit.normalize(raw, 16.4)
    assert result['segments'] == [{
        'text': 'Проверка.', 'start': 0.02, 'end': 11.84, 'speaker': 'SPEAKER_00',
        'words': [{'word': ' Проверка.', 'start': 0.02, 'end': 11.84,
                   'score': 0.9, 'speaker': 'SPEAKER_00'}],
    }]


@pytest.mark.parametrize('segments', [[], [
    {'text': 'Synthetic padding', 'start': 29.1, 'end': 29.12},
]])
def test_whisperkit_empty_or_padding_only_report_is_no_speech(segments):
    # A real short CV1 WAV returned a successful WhisperKit report with zero segments.
    result = local_whisperkit.normalize({'language': 'en', 'segments': segments}, 2.9)
    assert result == {'language': 'en', 'segments': [], 'outcome': 'no_speech'}


@pytest.mark.parametrize('segments', [None, {}, '', [None]])
def test_whisperkit_malformed_segments_are_not_no_speech(segments):
    with pytest.raises(local_whisperkit.WhisperKitError, match='invalid report'):
        local_whisperkit.normalize({'language': 'en', 'segments': segments}, 2.9)


def test_whisperkit_ignores_padding_before_checking_order_of_real_segments():
    result = local_whisperkit.normalize({'language': 'ru', 'segments': [
        {'text': 'Synthetic padding', 'start': 29.1, 'end': 29.12},
        {'text': 'Synthetic speech', 'start': 0, 'end': 1},
    ]}, 2)
    assert [segment['text'] for segment in result['segments']] == ['Synthetic speech']


def test_whisperkit_drops_internal_chunk_padding_and_preserves_real_speech_timing():
    # The VAD split is 26 seconds. A first-chunk hypothesis at 28.72 seconds
    # is padding, even though its timestamps are inside the complete WAV.
    raw = {'language': 'ru', 'segments': [
        {'text': 'First', 'seek': 0, 'start': 22.34, 'end': 23.76,
         'words': [{'word': 'First', 'start': 22.34, 'end': 23.76, 'probability': 0.8}]},
        {'text': 'Synthetic padding', 'seek': 0, 'start': 28.72, 'end': 29.92,
         'words': [{'word': 'Synthetic padding', 'start': 28.72, 'end': 29.92, 'probability': 0.9}]},
        {'text': 'Second', 'seek': 416000, 'start': 26.78, 'end': 28.56,
         'words': [{'word': 'Second', 'start': 26.78, 'end': 28.56, 'probability': 0.8}]},
        {'text': 'Third', 'seek': 416000, 'start': 29.52, 'end': 31.62,
         'words': [{'word': 'Third', 'start': 29.52, 'end': 31.62, 'probability': 0.7}]},
    ]}
    result = local_whisperkit.normalize(raw, 43.49)
    assert [segment['text'] for segment in result['segments']] == ['First', 'Second', 'Third']
    originals = {segment['text']: segment for segment in raw['segments']}
    for segment in result['segments']:
        original = originals[segment['text']]
        assert (segment['start'], segment['end']) == (original['start'], original['end'])
        assert segment['words'] == [{
            'word': word['word'], 'start': word['start'], 'end': word['end'],
            'score': word['probability'], 'speaker': 'SPEAKER_00',
        } for word in original['words']]


@pytest.mark.parametrize('internal_boundary', [False, True])
def test_whisperkit_keeps_speech_at_stop_and_excludes_padding_words(internal_boundary):
    raw = {'language': 'en', 'segments': [
        {'text': ' Earlier.', 'seek': 0, 'start': 1, 'end': 2,
         'words': [{'word': ' Earlier.', 'start': 1, 'end': 2, 'probability': 0.9}]},
        {'text': ' Final word padding.', 'seek': 0, 'start': 37.23, 'end': 39.66,
         'words': [
             {'word': ' Final', 'start': 37.23, 'end': 38.2, 'probability': 0.9},
             {'word': ' word', 'start': 38.68, 'end': 39.1, 'probability': 0.98},
             {'word': ' padding.', 'start': 39.1, 'end': 39.66, 'probability': 0.2},
         ]},
    ]}
    if internal_boundary:
        raw['segments'].append({'text': ' Next.', 'seek': 623840, 'start': 40, 'end': 41,
                                'words': [{'word': ' Next.', 'start': 40, 'end': 41, 'probability': 0.9}]})
    result = local_whisperkit.normalize(raw, 45 if internal_boundary else 38.99)
    final = result['segments'][1]
    assert final['text'] == 'Final word'
    assert (final['start'], final['end']) == (37.23, 38.99)
    assert len(final['words']) == 2
    assert final['words'][-1]['end'] == 38.99
    assert result['segments'][0]['text'] == 'Earlier.'
    assert result['segments'][0]['end'] == 2
    if internal_boundary:
        assert result['segments'][2]['text'] == 'Next.'


def test_pinned_queue_profile_does_not_inherit_another_engine_runtime(tmp_path):
    settings = cfg(tmp_path)
    settings.repo_root = tmp_path
    path = settings.layout.state_root / 'stt-engine.json'
    path.write_text(json.dumps({'engine': 'whisperkit', 'language': 'ru'}))
    current = local_stt.EngineConfig.load(settings)
    assert current.assets_path == str(tmp_path / '.local/whisperkit')
    assert current.profile()['engine'] == 'whisperkit'
    assert 'compute_type' not in current.profile()
    legacy = local_stt.EngineConfig(engine='whisperx', diarization_model='none')
    old_job = local_stt.EngineConfig.load(settings, profile=legacy.profile())
    assert old_job.engine == 'whisperx' and not old_job.assets_path
    path.write_text(json.dumps({'engine': 'whisperx', 'python': '/old/python', 'diarization_model': 'none'}))
    retried = local_stt.EngineConfig.load(settings, profile=current.profile())
    assert retried.engine == 'whisperkit' and not retried.python
    assert retried.assets_path == current.assets_path


def audio_file(tmp_path):
    path = tmp_path / 'test.wav'
    with wave.open(str(path), 'wb') as wav:
        wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        wav.writeframes(b'\0\0' * 16000)
    return path


def cfg(tmp_path):
    state = tmp_path / 'state'
    state.mkdir(exist_ok=True)
    return SimpleNamespace(provider_mode='offline', local_transport='ngrok',
                           layout=SimpleNamespace(state_root=state, services_dir=state / 'services'))


@pytest.mark.parametrize("transport", ["ngrok", "tailscale"])
def test_import_retry_reuses_finished_engine_output_and_model_change_is_separate(tmp_path, monkeypatch, transport):
    settings = cfg(tmp_path)
    settings.local_transport = transport
    audio = audio_file(tmp_path)
    python = tmp_path / 'python'
    python.write_text('fixture')
    python.chmod(0o700)
    profile_path = settings.layout.state_root / 'stt-engine.json'
    profile_path.write_text(json.dumps({'python': str(python)}))
    monkeypatch.setattr(local_stt.shutil, 'which', lambda tool: '/test/ffmpeg' if tool == 'ffmpeg' else None)
    calls = []
    def process(command, **kwargs):
        calls.append(command)
        if '-m' in command:
            output = Path(command[command.index('--output_dir') + 1])
            (output / 'audio.json').write_text(json.dumps({'segments': [{'text': 'Synthetic fixture'}]}))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(local_stt.subprocess, 'run', process)
    def unavailable(cfg, result_dir=None):
        if result_dir:
            raise local_stt.TranscriptionError('database temporarily unavailable')
        return {'preflight': 'passed'}
    monkeypatch.setattr(local_stt, 'backend_step', unavailable)
    with pytest.raises(local_stt.TranscriptionError):
        local_stt.transcribe(settings, str(audio))
    assert len(calls) == 2
    monkeypatch.setattr(local_stt, 'backend_step', lambda *_: {'import': 'passed'})
    assert local_stt.transcribe(settings, str(audio)) == 0
    assert len(calls) == 2
    profile_path.write_text(json.dumps({'python': str(python), 'model': 'small'}))
    assert local_stt.transcribe(settings, str(audio)) == 0
    assert len(calls) == 4
    assert '--model' in calls[-1] and 'small' in calls[-1]
    assert len(list(settings.layout.services_dir.glob('local-transcripts/*/audio.json'))) == 2


def test_failed_engine_keeps_audio_and_does_not_import(tmp_path, monkeypatch):
    settings = cfg(tmp_path)
    audio = audio_file(tmp_path)
    original = audio.read_bytes()
    monkeypatch.setattr(local_stt, 'backend_step', lambda *args: {'preflight': 'passed'})
    monkeypatch.setattr(local_stt.Path, 'home', lambda: tmp_path)
    with pytest.raises(local_stt.TranscriptionError):
        local_stt.transcribe(settings, str(audio))
    assert audio.read_bytes() == original
    assert not list(settings.layout.services_dir.glob('local-transcripts/*/audio.json'))


def test_model_process_does_not_receive_backend_or_provider_credentials(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-secret')
    monkeypatch.setenv('HF_TOKEN', 'synthetic-secret')
    monkeypatch.setenv('OMI_LOCAL_PAIRING_FILE', '/private/example')
    monkeypatch.setenv('HF_HOME', '/tmp/models')
    monkeypatch.setenv('HF_HUB_CACHE', '/tmp/hub')
    monkeypatch.setenv('TORCH_HOME', '/tmp/torch')
    monkeypatch.setenv('NLTK_DATA', '/tmp/nltk-data')
    monkeypatch.setenv('HF_HUB_OFFLINE', '0')
    monkeypatch.setattr(local_stt.shutil, 'which', lambda _: None)
    env = local_stt.model_environment(local_stt.EngineConfig())
    assert not {'OPENAI_API_KEY', 'HF_TOKEN', 'OMI_LOCAL_PAIRING_FILE'} & env.keys()
    assert env['HF_HUB_OFFLINE'] == '1'
    assert env['HF_HOME'] == '/tmp/models'
    assert env['HF_HUB_CACHE'] == '/tmp/hub'
    assert env['TORCH_HOME'] == '/tmp/torch'
    assert env['NLTK_DATA'] == '/tmp/nltk-data'


def test_completed_capture_with_stale_metadata_is_accepted_but_active_pcm_is_not(tmp_path):
    audio = audio_file(tmp_path)
    metadata = {'status': 'completed', 'decode_errors': 0, 'duration_seconds': 1,
                'started_at': '2026-01-01T00:00:00Z', 'source': 'phone'}
    audio.with_name('metadata.json').write_text(json.dumps(metadata))
    audio.with_name('metadata.json.part').write_text('stale progress')
    assert local_stt.inspect_audio(audio)['source'] == 'phone'
    audio.with_name('audio.pcm.part').write_bytes(b'\0\0')
    with pytest.raises(local_stt.TranscriptionError):
        local_stt.inspect_audio(audio)


def test_parakeet_selection_dispatch_and_existing_whisper_profile_stays_compatible(tmp_path, monkeypatch):
    settings = cfg(tmp_path)
    assert local_stt.EngineConfig().profile() == {
        'engine': 'whisperx', 'model': 'large-v3-turbo', 'language': 'ru', 'compute_type': 'float32', 'batch_size': 1}
    path = settings.layout.state_root / 'stt-engine.json'
    path.write_text(json.dumps({'engine': 'parakeet-mlx'}))
    engine = local_stt.EngineConfig.load(settings)
    assert engine.device == 'gpu' and engine.compute_type == 'float32' and engine.language == 'auto'
    calls = []
    monkeypatch.setattr(local_stt, 'run_parakeet', lambda e, *args: calls.append(e.profile()) or {
        'segments': [{'text': 'Fixture', 'start': 0, 'end': 1}]})
    monkeypatch.setattr(local_stt, 'backend_step', lambda *_: {'import': 'passed'})
    local_stt.transcribe(settings, str(audio_file(tmp_path)))
    assert calls == [engine.profile()]
    path.write_text(json.dumps({'engine': 'parakeet-mlx', 'compute_type': 'int8'}))
    with pytest.raises(local_stt.TranscriptionError):
        local_stt.EngineConfig.load(settings)
    path.write_text(json.dumps({'engine': 'parakeet-mlx', 'diarization_model': 'none'}))
    assert local_stt.EngineConfig.load(settings).profile()['diarization_model'] == 'none'


def test_parakeet_failed_process_never_imports_or_writes_raw_result(tmp_path, monkeypatch):
    settings = cfg(tmp_path)
    (settings.layout.state_root / 'stt-engine.json').write_text(json.dumps({'engine': 'parakeet-mlx'}))
    monkeypatch.setattr(local_stt, 'check_model', lambda _: Path('/fixture/python'))
    monkeypatch.setattr(local_stt.shutil, 'which', lambda _: None)
    monkeypatch.setattr(local_stt.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1))
    imports = []
    monkeypatch.setattr(local_stt, 'backend_step', lambda _cfg, folder=None: imports.append(folder) or {})
    audio = audio_file(tmp_path)
    with pytest.raises(local_stt.TranscriptionError):
        local_stt.transcribe(settings, str(audio))
    assert imports == [None] and audio.exists()
    assert not list(settings.layout.services_dir.glob('local-transcripts/*/audio.json'))


@pytest.mark.parametrize('diarization', ['none', 'pyannote/speaker-diarization-community-1'])
def test_whisperx_diarization_mode_controls_cli_speakers_and_cache_identity(tmp_path, monkeypatch, diarization):
    settings = cfg(tmp_path)
    (settings.layout.state_root / 'stt-engine.json').write_text(json.dumps({'diarization_model': diarization}))
    engine = local_stt.EngineConfig.load(settings)
    monkeypatch.setattr(local_stt, 'check_model', lambda _: Path('/fixture/python'))
    monkeypatch.setattr(local_stt.shutil, 'which', lambda _: None)
    commands = []
    def process(command, **kwargs):
        commands.append(command)
        output = Path(command[command.index('--output_dir') + 1])
        (output / 'audio.json').write_text(json.dumps({
            'segments': [{'text': 'Fixture', 'start': 0, 'end': 1,
                          'words': [{'word': 'Fixture', 'start': 0, 'end': 1}]}]}))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(local_stt.subprocess, 'run', process)
    audio = audio_file(tmp_path)
    raw = local_stt.run_whisperx(engine, audio, tmp_path, local_stt.inspect_audio(audio))
    assert ('--diarize' in commands[0]) == (diarization != 'none')
    if diarization == 'none':
        assert raw['segments'][0]['speaker'] == 'SPEAKER_00'
        assert raw['segments'][0]['words'][0]['speaker'] == 'SPEAKER_00'
        assert engine.profile()['diarization_model'] == 'none'
        assert engine.profile() != local_stt.EngineConfig().profile()
    else:
        assert engine.profile() == local_stt.EngineConfig().profile()


def test_parakeet_subwords_preserve_text_and_actual_speaker_boundaries():
    tokens = [SimpleNamespace(text=' Про', start=0.0, end=0.2),
              SimpleNamespace(text='верка.', start=0.2, end=0.6),
              SimpleNamespace(text=' Да.', start=1.0, end=1.4)]
    words = local_parakeet.timed_words(tokens, 2)
    assert [w['word'] for w in words] == ['Проверка.', 'Да.']
    segments = [{'words': words}]
    local_parakeet.assign_speakers(segments, [(0, 0.8, 'SPEAKER_00'), (0.9, 1.6, 'SPEAKER_01')])
    assert [w['speaker'] for w in words] == ['SPEAKER_00', 'SPEAKER_01']
    assert segments[0]['speaker'] is None
    local_parakeet.assign_speakers(segments, [])
    assert all(w['speaker'] is None for w in words)
    local_parakeet.single_speaker(segments)
    assert segments[0]['speaker'] == 'SPEAKER_00'
    assert all(w['speaker'] == 'SPEAKER_00' for w in words)
    with pytest.raises(ValueError):
        local_parakeet.timed_words([SimpleNamespace(text='bad', start=0, end=float('nan'))], 2)


@pytest.mark.parametrize('explicit', [False, True])
def test_model_finds_ffmpeg_in_another_prefix_or_respects_override(tmp_path, monkeypatch, explicit):
    prefix = tmp_path / 'Other Brew' / 'ffmpeg@7'
    libraries = prefix / 'lib'
    libraries.mkdir(parents=True)
    calls = []
    monkeypatch.setattr(local_stt.shutil, 'which', lambda name: '/test/brew' if name == 'brew' else None)
    def find(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=str(prefix) + '\n')
    monkeypatch.setattr(local_stt.subprocess, 'run', find)
    engine = local_stt.EngineConfig(library_path=str(libraries) if explicit else '')
    assert local_stt.model_environment(engine)['DYLD_LIBRARY_PATH'] == str(libraries)
    assert calls == ([] if explicit else [['/test/brew', '--prefix', 'ffmpeg@7']])


def test_model_uses_libraries_next_to_ffmpeg_when_no_brew(tmp_path, monkeypatch):
    binary = tmp_path / 'ffmpeg/bin/ffmpeg'
    binary.parent.mkdir(parents=True)
    binary.write_text('fixture')
    (binary.parent.parent / 'lib').mkdir()
    monkeypatch.setattr(local_stt.shutil, 'which', lambda name: str(binary) if name == 'ffmpeg' else None)
    assert local_stt.ffmpeg_library_path(local_stt.EngineConfig()) == str(binary.parent.parent / 'lib')


def test_custom_provider_request_and_profile_are_timed_and_local(tmp_path, monkeypatch):
    import httpx
    from dev_harness import local_openai_stt
    cfg = SimpleNamespace(repo_root=tmp_path, layout=SimpleNamespace(state_root=tmp_path))
    (tmp_path / 'stt-engine.json').write_text(json.dumps({
        'engine': 'openai-compatible', 'model': 'turbo', 'provider_url': 'http://127.0.0.1:10301/v1'}))
    engine = local_stt.EngineConfig.load(cfg)
    assert engine.language == 'auto' and engine.diarization_model == 'none'
    assert engine.profile()['provider_url'] == 'http://127.0.0.1:10301/v1'
    assert 'provider_url' not in local_stt.EngineConfig().profile()
    requests = []
    def respond(request):
        requests.append(request)
        if request.url.path.endswith('/models'):
            return httpx.Response(200, json={'data': [{'id': 'turbo'}]})
        body = request.read()
        assert b'name="language"' not in body
        assert b'filename="audio.wav"' in body
        assert b'verbose_json' in body
        return httpx.Response(200, json={'language': 'ru', 'text': 'Проверка.', 'segments': [
            {'text': ' Проверка.', 'start': 0, 'end': 1}],
            'words': [{'word': ' Проверка.', 'start': 0, 'end': 1}]})
    factory = httpx.Client
    def client(**kwargs):
        assert kwargs['trust_env'] is False and kwargs['follow_redirects'] is False
        return factory(transport=httpx.MockTransport(respond), **kwargs)
    monkeypatch.setattr(local_openai_stt.httpx, 'Client', client)
    audio = tmp_path / 'source.wav'
    audio.write_bytes(b'synthetic wav')
    import hashlib
    report = local_stt.run_openai(engine, audio, tmp_path,
        {'audio_sha256': hashlib.sha256(audio.read_bytes()).hexdigest(), 'duration_seconds': 1})
    assert report['segments'][0]['words'][0]['word'] == ' Проверка.'
    assert len(requests) == 2


@pytest.mark.parametrize('url', ['https://example.com/v1', 'http://127.0.0.1:9/v1?secret=x',
                                 'http://u:secret@127.0.0.1:9/v1', 'http://localhost:9/v1'])
def test_custom_provider_never_routes_off_host(url):
    from dev_harness.local_openai_stt import ProviderError, validate_url
    with pytest.raises(ProviderError):
        validate_url(url)


def test_custom_provider_failure_is_sanitized_and_original_is_retained(tmp_path, monkeypatch):
    import httpx
    from dev_harness import local_openai_stt
    audio = tmp_path / 'audio.wav'
    audio.write_bytes(b'synthetic')
    factory = httpx.Client
    monkeypatch.setattr(local_openai_stt.httpx, 'Client', lambda **kwargs:
        factory(transport=httpx.MockTransport(lambda request: httpx.Response(503, text='private speech')), **kwargs))
    with pytest.raises(local_openai_stt.ProviderError) as error:
        local_openai_stt.transcribe('http://127.0.0.1:9/v1', 'turbo', 'ru', audio, 1)
    assert 'private' not in str(error.value) and audio.read_bytes() == b'synthetic'
    with pytest.raises(local_openai_stt.ProviderError):
        local_openai_stt.normalize({'text': 'un-timed', 'segments': []}, 1)
    assert local_openai_stt.normalize({'text': '', 'segments': []}, 1)['outcome'] == 'no_speech'


def test_live_provider_is_persisted_independently_and_legacy_is_off(tmp_path):
    from dev_harness import local_stt_services
    cfg = SimpleNamespace(layout=SimpleNamespace(state_root=tmp_path))
    assert local_stt_services.live_url(cfg) == ''
    path = tmp_path / 'live-stt.json'
    path.write_text(json.dumps({'enabled': True, 'provider': 'external', 'url': 'ws://127.0.0.1:18090/asr'}))
    assert local_stt_services.live_url(cfg) == 'ws://127.0.0.1:18090/asr'
    path.write_text(json.dumps({'enabled': True, 'provider': 'external', 'url': 'wss://example.com/asr'}))
    with pytest.raises(ValueError):
        local_stt_services.live_url(cfg)


def test_apply_live_rechecks_capture_after_model_startup(tmp_path, monkeypatch):
    import httpx
    from dev_harness import cli, local_env, local_stt_services as services
    cfg = SimpleNamespace(repo_root=tmp_path, backend_url='http://127.0.0.1:20000')
    monkeypatch.setattr(local_env, 'read_env', lambda _: {'OMI_LOCAL_APP_KEY': 'synthetic-key'})
    states = iter(['idle', 'received'])
    factory = httpx.Client
    monkeypatch.setattr(services.httpx, 'Client', lambda **kwargs:
        factory(transport=httpx.MockTransport(lambda request: httpx.Response(
            200, json={'capture': {'state': next(states)}})), **kwargs))
    started = []
    monkeypatch.setattr(services, 'start_configured', lambda cfg: started.append(True))
    monkeypatch.setattr(cli, '_stop_single_service', lambda *a: pytest.fail('must preserve active recording'))
    with pytest.raises(ValueError, match='Stop the current recording'):
        services.apply(cfg)
    assert started == [True]


@pytest.mark.parametrize("transport", ["ngrok", "tailscale"])
def test_managed_live_preserves_virtualenv_interpreter_path(tmp_path, monkeypatch, transport):
    from dev_harness import cli, config, local_stt_services as services
    runtime = tmp_path / 'runtime'
    runtime.write_text('synthetic')
    venv = tmp_path / 'venv/bin'
    venv.mkdir(parents=True)
    python = venv / 'python'
    python.symlink_to(runtime)
    model = tmp_path / 'model'
    model.mkdir()
    cfg = SimpleNamespace(repo_root=tmp_path, provider_mode='offline', local_transport=transport,
                          layout=SimpleNamespace(state_root=tmp_path, services_dir=tmp_path / 'services'))
    (tmp_path / 'live-stt.json').write_text(json.dumps({
        'enabled': True, 'provider': 'whisperlivekit', 'url': 'ws://127.0.0.1:18090/asr',
        'python': str(python), 'model_dir': str(model)}))
    starts = []
    monkeypatch.setattr(config, 'child_env_for', lambda cfg: {})
    monkeypatch.setattr(cli, '_start_process', lambda cfg, service, command, **kw: starts.append(command))
    monkeypatch.setattr(cli, '_service_record', lambda *a: None)
    monkeypatch.setattr(services, 'health', lambda *a: (True, 'ready'))
    services.start_configured(cfg)
    assert starts[0][0] == str(python)  # Resolving this symlink loses the venv packages.

    # A healthy process must still reload when the requested language changes.
    recorded = {'command': starts[0]}
    monkeypatch.setattr(cli, '_service_record', lambda *a: recorded)
    stops = []
    monkeypatch.setattr(cli, '_stop_single_service', lambda *a: stops.append(True))
    services.start_configured(cfg)
    assert stops == []
    path = tmp_path / 'live-stt.json'
    changed = json.loads(path.read_text())
    changed['language'] = 'auto'
    path.write_text(json.dumps(changed))
    import fcntl
    with (cfg.layout.services_dir / 'local-transcripts/.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(services.ServiceError, match='Stop recording'):
            services.start_configured(cfg)
        assert stops == []
    services.start_configured(cfg)
    assert stops == [True] and 'auto' in starts[-1]


def test_managed_stt_detects_failed_start_without_waiting_for_model_timeout(tmp_path, monkeypatch):
    from dev_harness import cli, config, local_stt_services as services
    cfg = SimpleNamespace(repo_root=tmp_path, provider_mode='offline', local_transport='ngrok',
                          layout=SimpleNamespace(state_root=tmp_path, services_dir=tmp_path / 'services'))
    binary = tmp_path / 'argmax'
    binary.write_text('synthetic executable')
    binary.chmod(0o700)
    (tmp_path / 'argmax-stt.json').write_text(json.dumps({
        'enabled': True, 'port': 10301, 'model': 'turbo', 'binary': str(binary),
        'model_dir': str(tmp_path), 'tokenizer_dir': str(tmp_path)}))
    monkeypatch.setattr(config, 'child_env_for', lambda cfg: {})
    monkeypatch.setattr(cli, '_start_process', lambda *a, **kw: None)
    records = iter([None, {'pid': 123}])
    monkeypatch.setattr(cli, '_service_record', lambda *a: next(records))
    monkeypatch.setattr(services, 'health', lambda *a: (False, 'unavailable'))
    monkeypatch.setattr(services.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0, stdout='Z'))
    monkeypatch.setattr(services.time, 'sleep', lambda *a: pytest.fail('must fail promptly'))
    with pytest.raises(ValueError, match='process exited'):
        services.start_configured(cfg)


def test_independent_speakers_split_words_without_losing_timing():
    from dev_harness.local_diarization import reconcile
    words = [{'word': 'Hello.', 'start': 0, 'end': 1}, {'word': 'Reply.', 'start': 1.2, 'end': 2}]
    result = reconcile([{'text': 'Hello. Reply.', 'start': 0, 'end': 2, 'words': words}],
                       [(0, 1.1, 'SPEAKER_00'), (1.1, 2.2, 'SPEAKER_01')])
    assert [(s['text'], s['speaker'], s['start'], s['end']) for s in result] == [
        ('Hello.', 'SPEAKER_00', 0, 1), ('Reply.', 'SPEAKER_01', 1.2, 2)]
    assert 'speaker' not in words[0]
    assert reconcile([{'text': 'Unknown', 'start': 3, 'end': 4}], [])[0]['speaker'] is None


@pytest.mark.parametrize('name', ['openai-compatible', 'whisperkit', 'parakeet-mlx', 'whisperx'])
def test_independent_diarization_keeps_all_stt_profiles_separate(tmp_path, monkeypatch, name):
    settings = SimpleNamespace(repo_root=tmp_path, layout=SimpleNamespace(state_root=tmp_path))
    monkeypatch.setattr(local_whisperkit, 'runtime_revision', lambda _: 'fixture')
    data = {'engine': name, 'diarization_model': 'none',
            'speaker_model': 'pyannote/speaker-diarization-community-1'}
    if name == 'openai-compatible':
        data['provider_url'] = 'http://127.0.0.1:10301/v1'
    (tmp_path / 'stt-engine.json').write_text(json.dumps(data))
    engine = local_stt.EngineConfig.load(settings)
    assert engine.profile()['speaker_model'] == data['speaker_model']
    assert engine.speaker_python.endswith('diarization/venv/bin/python')
    assert 'speaker_python' not in engine.profile()


def test_independent_diarization_failure_preserves_asr(tmp_path, monkeypatch):
    from dataclasses import replace
    settings = SimpleNamespace(provider_mode='offline', local_transport='ngrok',
                               layout=SimpleNamespace(services_dir=tmp_path))
    audio = tmp_path / 'fixture.wav'
    with wave.open(str(audio), 'wb') as wav:
        wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        wav.writeframes(b'\0\0' * 16000)
    calls = []
    monkeypatch.setattr(local_stt, 'backend_step', lambda *a: {})
    def asr(*args):
        calls.append(True)
        return {'segments': [{'text': 'Hello', 'start': 0, 'end': 1}]}
    monkeypatch.setattr(local_stt, 'run_openai', asr)
    engine = local_stt.EngineConfig(engine='openai-compatible', diarization_model='none',
                                    speaker_model='pyannote/speaker-diarization-community-1',
                                    speaker_python=str(tmp_path / 'missing'))
    for _ in range(2):
        with pytest.raises(local_stt.TranscriptionError, match='Python is unavailable'):
            local_stt.transcribe(settings, str(audio), engine=engine)
    assert len(calls) == 1
    assert len(list(tmp_path.glob('local-transcripts/*/asr.json'))) == 1
    assert not list(tmp_path.glob('local-transcripts/*/audio.json'))


def test_old_queued_profile_does_not_inherit_new_diarization(tmp_path):
    settings = SimpleNamespace(repo_root=tmp_path, layout=SimpleNamespace(state_root=tmp_path))
    data = {'engine': 'openai-compatible', 'provider_url': 'http://127.0.0.1:10301/v1',
            'model': 'turbo', 'diarization_model': 'none'}
    (tmp_path / 'stt-engine.json').write_text(json.dumps({**data,
        'speaker_model': 'pyannote/speaker-diarization-3.1', 'speaker_device': 'mps'}))
    old = local_stt.EngineConfig.load(settings, profile=data)
    assert old.speaker_model == ''
    assert 'speaker_model' not in old.profile()


def selected_pipeline_cfg(tmp_path):
    settings = cfg(tmp_path)
    settings.repo_root = tmp_path
    pipeline = {
        'stt': {'id': 'speech', 'stage': 'stt', 'kind': 'openai-compatible', 'name': 'Speech server',
                'credential_ref': 'speech-key', 'settings': {'provider_url': 'https://speech.example/v1',
                                                          'model': 'turbo', 'language': 'auto'}},
        'diarization': {'id': 'speakers', 'stage': 'diarization', 'kind': 'mycelia', 'name': 'Speaker server',
                        'credential_ref': 'speaker-key', 'settings': {'base_url': 'https://speakers.example',
                                                                    'min_speakers': 2, 'max_speakers': 2}},
        'summary': {'id': 'summary', 'stage': 'summary', 'kind': 'openai-compatible', 'name': 'Summary server',
                    'credential_ref': 'summary-key', 'settings': {'base_url': 'https://summary.example/v1',
                                                                'model': 'local-model:latest'}},
    }
    path = settings.layout.state_root / 'providers.json'
    path.write_text(json.dumps({'version': 1, 'revision': 1, 'profiles': list(pipeline.values()),
                                'effective': pipeline}))
    (settings.layout.state_root / 'provider-secrets.json').write_text(json.dumps({
        'speech-key': 'synthetic-speech-private', 'speaker-key': 'synthetic-speaker-private',
        'summary-key': 'synthetic-summary-private'}))
    return settings


def test_selected_pipeline_retries_only_failed_stage_and_pins_routes(tmp_path, monkeypatch):
    import httpx
    from collections import Counter
    from dev_harness import local_provider_http, local_pipeline
    settings = selected_pipeline_cfg(tmp_path)
    engine = local_stt.EngineConfig.load(settings)
    admitted = engine.profile()
    audio = audio_file(tmp_path)
    imported = []
    monkeypatch.setattr(local_stt, 'backend_step', lambda cfg, folder=None:
                        (imported.append(json.loads((folder / 'audio.json').read_text())) or {}) if folder else {})
    attempts = Counter()
    fail_summary = True

    def respond(request):
        nonlocal fail_summary
        attempts[request.url.path] += 1
        keys = {'speech.example': 'speech', 'speakers.example': 'speaker', 'summary.example': 'summary'}
        assert request.headers['authorization'] == 'Bearer synthetic-' + keys[request.url.host] + '-private'
        if request.url.path == '/v1/models':
            return httpx.Response(200, json={'data': [{'id': 'turbo'}]})
        if request.url.path == '/v1/audio/transcriptions':
            assert b'filename="audio.wav"' in request.read()
            return httpx.Response(200, json={'segments': [{'text': 'Hello. Reply.', 'start': 0, 'end': 1}],
                'words': [{'word': 'Hello.', 'start': 0, 'end': .4}, {'word': 'Reply.', 'start': .6, 'end': 1}]})
        if request.url.path == '/diarize':
            assert request.url.params['min_speakers'] == request.url.params['max_speakers'] == '2'
            return httpx.Response(200, json={'segments': [
                {'start': 0, 'end': .5, 'speaker': 'external-person-a', 'embedding': [1, 2]},
                {'start': .5, 'end': 1, 'speaker': 'external-person-b', 'embedding': [3, 4]}]})
        assert request.url.path == '/v1/chat/completions'
        if fail_summary:
            return httpx.Response(503, text='private server response')
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {
            'content': json.dumps({'title': 'Synthetic discussion', 'overview': 'Two short replies.'})}}]})

    factory = httpx.Client
    monkeypatch.setattr(local_provider_http.httpx, 'Client', lambda **kwargs:
                        factory(transport=httpx.MockTransport(respond), **kwargs))
    with pytest.raises(local_pipeline.PipelineError, match='Summary request failed'):
        local_stt.transcribe(settings, str(audio), engine=engine)
    root = settings.layout.services_dir / 'local-transcripts'
    folder = next(path for path in root.iterdir() if path.is_dir())
    assert json.loads((folder / 'processing.json').read_text())['summary']['status'] == 'failed'
    assert (folder / 'asr.json').exists() and (folder / 'diarized.json').exists()
    assert not (folder / 'audio.json').exists() and not imported
    state_path = settings.layout.state_root / 'providers.json'
    state = json.loads(state_path.read_text())
    state['effective']['stt']['settings']['provider_url'] = 'https://changed.example/v1'
    state['effective']['summary']['settings']['model'] = 'new-model'
    state_path.write_text(json.dumps(state))
    retried = local_stt.EngineConfig.load(settings, profile=admitted)
    assert retried.provider_url == 'https://speech.example/v1' and retried.profile() == admitted
    fail_summary = False
    assert local_stt.transcribe(settings, str(audio), engine=retried) == 0
    assert attempts == {'/v1/models': 1, '/v1/audio/transcriptions': 1, '/diarize': 1, '/v1/chat/completions': 2}
    assert imported[0]['structured']['title'] == 'Synthetic discussion'
    assert [s['speaker'] for s in imported[0]['segments']] == ['SPEAKER_00', 'SPEAKER_01']
    assert all(item['status'] == 'ready' for item in imported[0]['processing'].values())
    checkpoints = ''.join(path.read_text() for path in folder.rglob('*.json'))
    assert not any(secret in checkpoints for secret in ('synthetic-speech-private', 'synthetic-speaker-private',
                                                        'synthetic-summary-private', 'external-person-a', 'embedding'))


def test_chunked_summary_covers_all_text_and_reuses_completed_parts(tmp_path, monkeypatch):
    import httpx
    from dev_harness import local_pipeline, local_provider_http
    text = ('One synthetic sentence. ' * 1500) + 'UNIQUE_END'
    assert ''.join(local_pipeline.text_chunks(text)) == text
    assert all(len(part) <= local_pipeline.SUMMARY_CHARS for part in local_pipeline.text_chunks(text))
    snapshot = {'settings': {'base_url': 'https://summary.example/v1', 'model': 'synthetic'}}
    raw = {'segments': [{'start': 0, 'end': 1, 'text': text}]}
    seen, failed_once = [], False

    def respond(request):
        nonlocal failed_once
        content = json.loads(request.read())['messages'][1]['content']
        seen.append(content)
        if len(seen) == 2 and not failed_once:
            failed_once = True
            return httpx.Response(503)
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps({
            'title': 'Synthetic', 'overview': 'Synthetic facts and decisions.'})}}]})

    factory = httpx.Client
    monkeypatch.setattr(local_provider_http.httpx, 'Client', lambda **kwargs:
                        factory(transport=httpx.MockTransport(respond), **kwargs))
    with pytest.raises(local_pipeline.PipelineError):
        local_pipeline.summarize(snapshot, '', raw, tmp_path, local_stt.atomic_json)
    result = local_pipeline.summarize(snapshot, '', raw, tmp_path, local_stt.atomic_json)
    assert result['title'] == 'Synthetic'
    assert seen.count(seen[0]) == 1
    assert any('UNIQUE_END' in part for part in seen)
    before = len(seen)
    assert local_pipeline.summarize(snapshot, '', raw, tmp_path, local_stt.atomic_json) == result
    assert len(seen) == before


@pytest.mark.parametrize('response', [
    {'segments': [{'start': -1, 'end': .5, 'speaker': 'one'}]},
    {'segments': [{'start': 0, 'end': 3, 'speaker': 'one'}]},
    {'segments': [{'start': False, 'end': .5, 'speaker': 'one'}]},
    {'segments': 'invalid'},
])
def test_remote_diarization_rejects_invalid_turns_without_replacing_asr(tmp_path, monkeypatch, response):
    import httpx
    from dev_harness import local_pipeline, local_provider_http
    audio = audio_file(tmp_path)
    raw = {'segments': [{'text': 'Synthetic', 'start': 0, 'end': 1}]}
    factory = httpx.Client
    monkeypatch.setattr(local_provider_http.httpx, 'Client', lambda **kwargs:
                        factory(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response)), **kwargs))
    with pytest.raises(local_pipeline.PipelineError, match='transcription retained'):
        local_pipeline.remote_diarization({'settings': {'base_url': 'https://speakers.example'}}, '',
            audio, tmp_path, local_stt.inspect_audio(audio), raw)
    assert raw == {'segments': [{'text': 'Synthetic', 'start': 0, 'end': 1}]}


def test_selected_legacy_embedded_diarization_runs_once(tmp_path, monkeypatch):
    settings = selected_pipeline_cfg(tmp_path)
    path = settings.layout.state_root / 'providers.json'
    state = json.loads(path.read_text())
    state['effective']['stt'] = {'id': 'legacy', 'name': 'WhisperX', 'stage': 'stt', 'kind': 'whisperx',
        'settings': {'model': 'large-v3-turbo', 'language': 'ru',
                     'diarization_model': 'pyannote/speaker-diarization-community-1'}}
    state['effective']['diarization'] = {'id': 'legacy-speakers', 'name': 'Embedded speakers',
        'stage': 'diarization', 'kind': 'pyannote', 'embedded': True,
        'settings': {'speaker_model': 'pyannote/speaker-diarization-community-1'}}
    state['effective']['summary'] = None
    path.write_text(json.dumps(state))
    calls = []
    def infer(engine, *args):
        assert engine.diarization_model == 'pyannote/speaker-diarization-community-1'
        calls.append(True)
        return {'segments': [{'text': 'Synthetic', 'start': 0, 'end': 1, 'speaker': 'SPEAKER_01'}]}
    monkeypatch.setattr(local_stt, 'run_whisperx', infer)
    monkeypatch.setattr(local_stt, 'apply_diarization', lambda *args: pytest.fail('embedded stage ran twice'))
    monkeypatch.setattr(local_stt, 'backend_step', lambda *args: {})
    assert local_stt.transcribe(settings, str(audio_file(tmp_path))) == 0
    output = next(settings.layout.services_dir.glob('local-transcripts/*/audio.json'))
    raw = json.loads(output.read_text())
    assert calls == [True] and raw['segments'][0]['speaker'] == 'SPEAKER_01'
    assert raw['processing']['diarization']['status'] == 'ready'
    assert raw['processing']['summary']['status'] == 'disabled'


def test_incompatible_embedded_snapshot_cannot_report_diarization_ready(tmp_path, monkeypatch):
    from dev_harness.local_pipeline import PipelineError
    settings = selected_pipeline_cfg(tmp_path)
    path = settings.layout.state_root / 'providers.json'
    state = json.loads(path.read_text())
    state['effective']['diarization'] = {'id': 'legacy-speakers', 'name': 'Embedded speakers',
        'stage': 'diarization', 'kind': 'pyannote', 'embedded': True,
        'settings': {'speaker_model': 'pyannote/speaker-diarization-community-1'}}
    state['effective']['summary'] = None
    path.write_text(json.dumps(state))
    monkeypatch.setattr(local_stt, 'run_openai', lambda *args: {
        'segments': [{'text': 'Synthetic', 'start': 0, 'end': 1, 'speaker': 'SPEAKER_00'}]})
    monkeypatch.setattr(local_stt, 'backend_step', lambda *args: {})
    with pytest.raises(local_stt.TranscriptionError, match='standalone diarization'):
        local_stt.EngineConfig.load(settings)
    engine = local_stt.EngineConfig(engine='openai-compatible', diarization_model='none', pipeline=state['effective'])
    with pytest.raises(PipelineError, match='does not supply'):
        local_stt.transcribe(settings, str(audio_file(tmp_path)), engine=engine)
    folder = next(settings.layout.services_dir.glob('local-transcripts/*/asr.json')).parent
    assert json.loads((folder / 'processing.json').read_text())['diarization']['status'] == 'failed'
    assert not (folder / 'audio.json').exists()


def test_selected_diarization_stage_controls_old_stt_embedded_setting(tmp_path):
    settings = selected_pipeline_cfg(tmp_path)
    path = settings.layout.state_root / 'providers.json'
    state = json.loads(path.read_text())
    state['effective']['stt'] = {'id': 'legacy', 'name': 'WhisperX', 'stage': 'stt', 'kind': 'whisperx',
        'settings': {'model': 'small', 'language': 'ru',
                     'diarization_model': 'pyannote/speaker-diarization-community-1'}}
    state['effective']['diarization'] = None
    path.write_text(json.dumps(state))
    assert local_stt.EngineConfig.load(settings).diarization_model == 'none'
    state['effective']['stt']['settings']['diarization_model'] = 'none'
    state['effective']['diarization'] = {'id': 'legacy-speakers', 'name': 'Embedded speakers',
        'stage': 'diarization', 'kind': 'pyannote', 'embedded': True,
        'settings': {'speaker_model': 'pyannote/speaker-diarization-community-1'}}
    path.write_text(json.dumps(state))
    assert local_stt.EngineConfig.load(settings).diarization_model == 'pyannote/speaker-diarization-community-1'


def test_independent_speakers_preserve_untimed_word_and_complete_sentence():
    from dev_harness.local_diarization import reconcile
    from dev_harness.local_pipeline import validate_transcript
    words = [{'word': 'Hello', 'start': 0, 'end': .4, 'speaker': 'SPEAKER_00'},
             {'word': 'API', 'speaker': 'SPEAKER_01'},
             {'word': 'works.', 'start': .6, 'end': 1, 'speaker': 'SPEAKER_02'}]
    original = {'segments': [{'text': 'Hello API works.', 'start': 0, 'end': 1, 'words': words}]}
    validate_transcript(original, 1)
    result = reconcile(original['segments'], [(0, .1, 'SPEAKER_00'), (.1, 1, 'SPEAKER_01')])
    assert len(result) == 1 and result[0]['text'] == 'Hello API works.'
    assert (result[0]['start'], result[0]['end'], result[0]['speaker']) == (0, 1, 'SPEAKER_01')
    assert {word['speaker'] for word in result[0]['words']} == {'SPEAKER_01'}
    assert 'start' not in result[0]['words'][1] and words[0]['speaker'] == 'SPEAKER_00'


def test_selected_manual_stt_model_can_transcribe_when_catalogue_is_unsupported(tmp_path, monkeypatch):
    import httpx
    from dev_harness import local_provider_http, local_openai_stt
    paths = []
    def respond(request):
        paths.append(request.url.path)
        if request.url.path == '/v1/models':
            return httpx.Response(404)
        assert b'namespace:model' in request.read()
        return httpx.Response(200, json={'segments': [{'text': 'Synthetic', 'start': 0, 'end': 1}]})
    factory = httpx.Client
    monkeypatch.setattr(local_provider_http.httpx, 'Client', lambda **kwargs:
                        factory(transport=httpx.MockTransport(respond), **kwargs))
    settings = selected_pipeline_cfg(tmp_path)
    path = settings.layout.state_root / 'providers.json'
    state = json.loads(path.read_text())
    state['effective']['stt']['settings']['model'] = 'namespace:model'
    path.write_text(json.dumps(state))
    engine = local_stt.EngineConfig.load(settings)
    audio = audio_file(tmp_path)
    raw = local_stt.run_openai(engine, audio, tmp_path, local_stt.inspect_audio(audio))
    assert raw['segments'][0]['text'] == 'Synthetic'
    assert paths == ['/v1/models', '/v1/audio/transcriptions']
    with pytest.raises(local_openai_stt.ProviderError):
        local_openai_stt.check('http://127.0.0.1:9/v1', 'manual-model')
    for invalid in ('', 'a' * 257, 'model\ncontrol', 'model\x7fcontrol'):
        state['effective']['stt']['settings']['model'] = invalid
        path.write_text(json.dumps(state))
        with pytest.raises(local_stt.TranscriptionError):
            local_stt.EngineConfig.load(settings)
    with pytest.raises(local_stt.TranscriptionError):
        local_stt.EngineConfig.load(settings, profile={'engine': 'openai-compatible',
            'provider_url': 'http://127.0.0.1:9/v1', 'model': 'namespace:model', 'diarization_model': 'none'})


@pytest.mark.parametrize('model', ['pyannote/speaker-diarization-3.1', 'pyannote/speaker-diarization-community-1'])
def test_both_pyannote_models_use_exclusive_turns_for_text(tmp_path, monkeypatch, model):
    from dev_harness import local_diarization
    audio = tmp_path / 'audio.wav'
    with wave.open(str(audio), 'wb') as wav:
        wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        wav.writeframes(b'\0\0' * 16000)
    class Tensor:
        def unsqueeze(self, _): return self
    monkeypatch.setitem(sys.modules, 'torch', SimpleNamespace(set_num_threads=lambda _: None,
                       device=lambda x: x, from_numpy=lambda _: Tensor()))
    class Pipeline:
        @staticmethod
        def from_pretrained(_): return Pipeline()
        def to(self, _): pass
        def __call__(self, audio, **kwargs):
            return SimpleNamespace(
                speaker_diarization=SimpleNamespace(itertracks=lambda **_: iter([
                    (SimpleNamespace(start=0, end=1), None, 'SPEAKER_00')])),
                exclusive_speaker_diarization=SimpleNamespace(itertracks=lambda **_: iter([
                    (SimpleNamespace(start=0, end=.5), None, 'SPEAKER_00'),
                    (SimpleNamespace(start=.5, end=1), None, 'SPEAKER_01')])))
    monkeypatch.setitem(sys.modules, 'pyannote', SimpleNamespace())
    monkeypatch.setitem(sys.modules, 'pyannote.audio', SimpleNamespace(Pipeline=Pipeline))
    result = local_diarization.infer(audio, model)
    assert result['turns'] == [(0., .5, 'SPEAKER_00'), (.5, 1., 'SPEAKER_01')]


def test_live_diarization_wraps_local_or_external_asr_without_changing_upstream(tmp_path, monkeypatch):
    from dev_harness import cli, config, local_stt_services as services
    python = tmp_path / 'python'
    python.write_text('fixture')
    cfg = SimpleNamespace(repo_root=tmp_path, provider_mode='offline', local_transport='ngrok',
                          layout=SimpleNamespace(state_root=tmp_path, services_dir=tmp_path / 'services'))
    path = tmp_path / 'live-stt.json'
    original = {'enabled': True, 'provider': 'external', 'url': 'ws://127.0.0.1:18090/asr'}
    path.write_text(json.dumps(original))
    assert services.live_url(cfg) == original['url']
    path.write_text(json.dumps({**original, 'diarization': {'enabled': True, 'port': 18091, 'python': str(python)}}))
    assert services.live_url(cfg) == 'ws://127.0.0.1:18091/asr'
    starts = []
    monkeypatch.setattr(config, 'child_env_for', lambda cfg: {})
    monkeypatch.setattr(cli, '_start_process', lambda cfg, name, command, **kw: starts.append((name, command)))
    monkeypatch.setattr(cli, '_service_record', lambda *a: None)
    monkeypatch.setattr(services, 'health', lambda *a: (True, 'ready'))
    services.start_configured(cfg)
    assert len(starts) == 1 and starts[0][0] == 'live-diarization'
    assert starts[0][1][starts[0][1].index('--upstream') + 1] == original['url']
    invalid = {**original, 'diarization': {'enabled': True, 'port': 18090, 'python': str(python)}}
    path.write_text(json.dumps(invalid))
    with pytest.raises(services.ServiceError, match='diarization settings'):
        services.live_url(cfg)
    path.write_text(json.dumps({**original, 'diarization': 'invalid'}))
    with pytest.raises(services.ServiceError, match='diarization settings'):
        services.live_url(cfg)
