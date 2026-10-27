"""Live-translation control frames reach the session host without touching audio or transcripts."""

import builtins
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from utils import local_live_preview, offline_audio_capture
from utils.stt import speaker_identity


@pytest.fixture
def anyio_backend():
    return 'asyncio'


class _UnusedDependency:
    def __init__(self, *args, **kwargs):
        pytest.fail('Translation control reached an unrelated cloud/audio dependency')


@pytest.fixture
def listen_modules():
    """Load unchanged production classes without the unrelated cloud pipelines.

    The import override belongs only to these two module namespaces, never to
    builtins or sys.modules. Other tests keep their real modules and bindings.
    The exercised normalization and speaker epoch helpers remain real too.
    """
    real_helpers = {
        'utils.local_live_preview': local_live_preview,
        'utils.offline_audio_capture': offline_audio_capture,
        'utils.stt.speaker_identity': speaker_identity,
    }

    def isolated_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name in real_helpers:
            return real_helpers[name]
        if name in {'lc3', 'opuslib'}:
            raise ModuleNotFoundError(name)
        if level or name.startswith(('database.', 'models.', 'utils.', 'config.')):
            if fromlist:
                return SimpleNamespace(**{symbol: _UnusedDependency for symbol in fromlist})
            # `import database.users as users_db` returns the top-level package.
            result = _UnusedDependency
            for part in reversed(name.split('.')[1:]):
                result = SimpleNamespace(**{part: result})
            return result
        return builtins.__import__(name, globals, locals, fromlist, level)

    def load(name):
        path = Path(__file__).resolve().parents[2] / 'routers' / 'listen' / f'{name}.py'
        spec = importlib.util.spec_from_file_location(f'routers.listen._translation_test_{name}', path)
        module = importlib.util.module_from_spec(spec)
        module.__dict__['__builtins__'] = {**vars(builtins), '__import__': isolated_import}
        spec.loader.exec_module(module)
        return module

    return load('receiver'), load('runtime')


class _FramesWebSocket:
    def __init__(self, frames):
        self.frames = iter(frames)

    async def receive(self):
        return next(self.frames)


@pytest.mark.anyio
async def test_translation_frame_is_dispatched_to_host_and_never_enqueued(listen_modules):
    receiver, _ = listen_modules
    received_segments = []
    languages = []
    websocket = _FramesWebSocket([
        {'text': json.dumps({'type': 'translation', 'language': 'en'})},
        {'text': json.dumps({'type': 'translation', 'language': 'es', 'other_language': 'ru'})},
        {'text': json.dumps({'type': 'translation'})},
        {'type': 'websocket.disconnect', 'code': 1000},
    ])
    host = SimpleNamespace(
        request=SimpleNamespace(websocket=websocket, codec='pcm16'),
        state=SimpleNamespace(active=True, close_code=1001, last_audio_received_time=None, last_activity_time=None,
                              first_audio_byte_timestamp=None, last_usage_record_timestamp=None,
                              audio_ring_buffer=None),
        limits=SimpleNamespace(ws_receive_timeout=1.0),
        is_multi_channel=False,
        use_custom_stt=True,
        audio_bytes_send=None,
        transcripts=SimpleNamespace(enqueue=received_segments.extend),
        set_live_translation=lambda language, other: languages.append((language, other)),
    )
    await receiver.ListenReceiver(host, [], {}).receive_data()
    assert languages == [('en', None), ('es', 'ru'), (None, None)]
    assert received_segments == []
    assert host.state.close_code == 1000
    assert host.state.first_audio_byte_timestamp is None


def test_runtime_normalizes_language_and_updates_an_existing_preview(listen_modules):
    _, module = listen_modules
    calls = []
    runtime = module.ListenSessionRuntime.__new__(module.ListenSessionRuntime)
    runtime.live_translation_language = None
    runtime.live_translation_other_language = None
    runtime.local_preview = None
    runtime.set_live_translation(' DE ')
    assert runtime.live_translation_language == 'de'
    runtime.set_live_translation(' ES ', 'RU')
    assert (runtime.live_translation_language, runtime.live_translation_other_language) == ('es', 'ru')
    runtime.local_preview = SimpleNamespace(set_translation=lambda language, other: calls.append((language, other)))
    runtime.set_live_translation('en-US')
    runtime.set_live_translation('fr')
    runtime.set_live_translation('es', 'ru')
    runtime.set_live_translation('es', 'es')
    assert runtime.live_translation_language is None
    assert runtime.live_translation_other_language is None
    assert calls == [(None, None), ('fr', None), ('es', 'ru'), (None, None)]


def test_live_preview_runtime_preserves_original_pcm_capture(tmp_path, listen_modules):
    import wave

    _, module = listen_modules
    from utils.offline_audio_capture import OfflineAudioCapture

    capture = OfflineAudioCapture(session_id='00000000-0000-0000-0000-000000000001',
                                  input_codec='pcm16', source='phone', root=tmp_path)
    received = []
    runtime = module.ListenSessionRuntime.__new__(module.ListenSessionRuntime)
    runtime.capture_sink = capture
    runtime.local_preview = SimpleNamespace(feed=received.append)
    frames = [b'\x01\x00' * 320, b'\x02\x00' * 320]
    for pcm in frames:
        runtime.capture_decoded_audio(encoded_bytes=len(pcm), pcm=pcm)
    assert runtime.capture_sink is capture
    assert received == frames
    metadata = capture.finalize()
    with wave.open(str(capture.wav_path), 'rb') as wav:
        assert wav.readframes(wav.getnframes()) == b''.join(frames)
    assert metadata['frames_received'] == 2
