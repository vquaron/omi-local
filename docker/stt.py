"""Single-model, serialized OpenAI timed-WAV API; no request-content logging."""
import io
import os
from pathlib import Path
import sys
import threading
import wave
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, HTTPException, UploadFile

MODEL = os.environ.get('STT_MODEL', 'Systran/faster-whisper-small')
REVISION = os.environ.get('STT_REVISION', 'main')
CACHE = os.environ.get('STT_CACHE', '/models')
DEVICE = os.environ.get('STT_DEVICE', 'cpu')
COMPUTE = os.environ.get('STT_COMPUTE_TYPE', 'int8' if DEVICE == 'cpu' else 'float16')
MAX_BYTES = 256 * 1024 * 1024


def model_path(*, download=False):
    from huggingface_hub import snapshot_download
    return snapshot_download(MODEL, revision=REVISION, cache_dir=CACHE,
                             local_files_only=not download,
                             allow_patterns=['*.bin', '*.json', '*.txt'])


def load_model():
    from faster_whisper import WhisperModel
    return WhisperModel(model_path(), device=DEVICE, compute_type=COMPUTE,
                        cpu_threads=int(os.environ.get('STT_THREADS', '4')), num_workers=1,
                        local_files_only=True)


def timed_result(segments, info):
    items = []
    for segment in segments:
        words = [{'start': w.start, 'end': w.end, 'word': w.word} for w in (segment.words or [])]
        items.append({'start': segment.start, 'end': segment.end, 'text': segment.text, 'words': words})
    return {'text': ''.join(s['text'] for s in items).strip(), 'language': info.language,
            'duration': info.duration, 'segments': items}


def create_app(loader=load_model):
    lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(app):
        app.state.model = loader()  # Missing cache/CUDA must fail startup, never silently fall back.
        yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get('/health')
    def health():
        return {'status': 'ready', 'device': DEVICE, 'compute_type': COMPUTE}

    @app.get('/v1/models')
    def models():
        return {'object': 'list', 'data': [{'id': MODEL, 'object': 'model'}]}

    @app.post('/v1/audio/transcriptions')
    def transcribe(file: UploadFile = File(...), model: str = Form(...),
                   response_format: str = Form('verbose_json'), language: str = Form('auto')):
        if model != MODEL or response_format != 'verbose_json':
            raise HTTPException(400, 'Use the served model and verbose_json')
        if not lock.acquire(blocking=False):
            raise HTTPException(429, 'Transcription already in progress')
        try:
            audio = file.file.read(MAX_BYTES + 1)
            if len(audio) > MAX_BYTES:
                raise HTTPException(413, 'WAV exceeds 256 MiB')
            try:
                with wave.open(io.BytesIO(audio)) as wav:
                    if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getcomptype()) != (1, 2, 16000, 'NONE'):
                        raise ValueError()
                    if wav.getnframes() == 0:
                        raise ValueError()
            except (wave.Error, EOFError, ValueError):
                raise HTTPException(400, 'Expected nonempty PCM16 mono 16 kHz WAV') from None
            try:
                segments, info = app.state.model.transcribe(
                    io.BytesIO(audio), language=None if language == 'auto' else language,
                    word_timestamps=True, vad_filter=True, beam_size=5)
                return timed_result(segments, info)
            except Exception:
                raise HTTPException(503, 'Transcription failed; retry after checking the runtime') from None
        finally:
            file.file.close()
            lock.release()

    return app


if __name__ == '__main__':
    if sys.argv[1:] == ['download']:
        resolved = model_path(download=True)
        print('Model revision: ' + Path(resolved).name)
    else:
        import uvicorn
        uvicorn.run(create_app(), host='127.0.0.1', port=10301, access_log=False, log_level='warning')
