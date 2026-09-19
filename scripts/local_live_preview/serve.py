"""Loopback WLK /asr with one resident model and isolated trial sessions.

Run with the prepared WLK virtualenv, not the Omi backend environment.
No transcript/audio logging, diagnostic replay, or model downloads.
"""

import argparse
import asyncio
from contextlib import suppress
import fcntl
import hashlib
import importlib
from importlib.metadata import version
import json
import logging
import math
import os
from pathlib import Path
import sys
import time


def deny_network(event, args):
    if event == 'socket.getaddrinfo':
        raise PermissionError('Live preview does not resolve network hosts')
    if event == 'socket.connect' and not isinstance(args[1], str):
        raise PermissionError('Live preview has no outbound network access')


def inference_busy(lock_path):
    """Observe admission using a separate descriptor; never unlock a live session."""
    with lock_path.open('r+') as observer:
        try:
            fcntl.flock(observer, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False  # Closing this descriptor releases only this probe's lock.


def snapshot_with_words(response):
    """Expose WLK's existing absolute word times for independent diarization.

    HypothesisBuffer already applies the audio-window offset before commit.
    PreviewResetRetention forwards those same tokens, so no second offset is
    applied here. Whitespace stays on each token exactly as emitted by ASR.
    """
    message = response.to_dict()
    visible = [line for line in response.lines if line.text or line.speaker == -2]
    if len(visible) != len(message['lines']):
        raise ValueError('Inconsistent live line serialization')
    for line, output in zip(visible, message['lines']):
        tokens = getattr(line, 'tokens', None)
        if not tokens:
            continue
        words = []
        for token in tokens:
            if (not isinstance(token.text, str)
                    or any(isinstance(value, bool) or not isinstance(value, (int, float))
                           or not math.isfinite(value) or value < 0 for value in (token.start, token.end))
                    or token.end < token.start):
                raise ValueError('Invalid live word timing')
            words.append({'word': token.text, 'start': float(token.start), 'end': float(token.end)})
        if ''.join(word['word'] for word in words).strip() != (line.text or '').strip():
            raise ValueError('Inconsistent live word text')
        output['words'] = words
    return message


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-dir', type=Path, required=True)
    parser.add_argument('--inference-lock', type=Path, required=True)
    parser.add_argument('--port', type=int, default=18090)
    parser.add_argument('--language', choices=['ru', 'en', 'auto'], default='ru')
    parser.add_argument('--chunk-seconds', type=float, default=4)
    parser.add_argument('--runtime-revision', default='')
    args = parser.parse_args()
    if not 1 <= args.chunk_seconds <= 10:
        parser.error('chunk-seconds must be between 1 and 10')
    # Third-party libraries print decoded text even at reduced log levels.
    # Keep only our explicit, content-free lifecycle records.
    output = os.fdopen(os.dup(1), 'w', buffering=1)
    null = os.open(os.devnull, os.O_WRONLY)
    os.dup2(null, 1)
    os.dup2(null, 2)
    os.close(null)
    logging.disable(logging.CRITICAL)

    def emit(event, **fields):
        output.write(json.dumps({'event': event, **fields}) + '\n')

    try:
        sys.addaudithook(deny_network)
        os.environ.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1')
        for package, expected in [('whisperlivekit', '0.2.26'), ('mlx-whisper', '0.4.3'), ('mlx', '0.32.2')]:
            if version(package) != expected:
                raise ValueError('unverified_dependency_version')
        model_dir = args.model_dir.resolve(strict=True)
        with (model_dir / 'weights.npz').open('rb') as weights:
            digest = hashlib.file_digest(weights, 'sha256').hexdigest()
        if digest != '862bbc832b05f3f4ec19dd632b701d61a6d3f5c7906360a10d72a79870642a80':
            raise ValueError('unverified_weights')
        lock = args.inference_lock.open('r+')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        emit('preflight_passed')

        import numpy as np
        import mlx.core as mx
        import torch
        import uvicorn
        from fastapi import FastAPI, WebSocket
        from starlette.websockets import WebSocketDisconnect
        from whisperlivekit import AudioProcessor, TranscriptionEngine, WhisperLiveKitConfig
        import whisperlivekit.audio_processor as audio_module
        from .alignment_reuse import AlignmentReuse
        from .preview_reuse import LastAudioResult
        from .repetition_guard import RepetitionGuard
        from .reset_retention import PreviewResetRetention

        torch.set_num_threads(2)
        mx.set_cache_limit(256 * 2**20)
        if not mx.metal.is_available() or float(mx.sum(mx.arange(8)).item()) != 28:
            raise RuntimeError('metal_unavailable')
        config = WhisperLiveKitConfig(
            backend='mlx-whisper', backend_policy='localagreement',
            model_size='large-v3-turbo', model_dir=str(model_dir), lan=args.language,
            pcm_input=True, min_chunk_size=1, asr_coalesce_min_s=args.chunk_seconds,
            vac=True, warmup_file='', diarization=False,
        )
        engine = TranscriptionEngine(config=config)
        native_transcribe = engine.asr.model

        def deterministic(*a, **kw):
            return native_transcribe(*a, temperature=0.0, **kw)

        engine.asr.model = deterministic
        # Load weights once before advertising readiness. No private warmup.
        engine.asr.transcribe(np.zeros(16000, dtype=np.float32))
        native = importlib.import_module('mlx_whisper.transcribe')
        alignment = AlignmentReuse(native, native.ModelHolder.model)
        alignment.__enter__()
        guard = RepetitionGuard(engine.asr.transcribe)
        reuse = LastAudioResult(guard)
        engine.asr.transcribe = reuse

        original_vad = audio_module.FixedVADIterator

        class PauseVAD(original_vad):
            def __init__(self, *a, **kw):
                kw['min_silence_duration_ms'] = 800
                super().__init__(*a, **kw)

        audio_module.FixedVADIterator = PauseVAD

        class SessionProcessor(AudioProcessor):
            async def _run_counted_transcription_call(self, method, *a):
                # Cancelling to_thread does not stop Metal inference. Keep its
                # owner alive until completion before clearing reuse/unlocking.
                call = asyncio.create_task(
                    super()._run_counted_transcription_call(method, *a), name='preview-inference')
                try:
                    return await asyncio.shield(call)
                except asyncio.CancelledError:
                    with suppress(Exception):
                        await call
                    raise

        fcntl.flock(lock, fcntl.LOCK_UN)
        active = False
        last = None
        completed = 0
        app = FastAPI()

        @app.get('/health')
        async def health():
            return {'ready': True, 'diarization': False, 'active': active,
                    'busy': active or inference_busy(args.inference_lock), 'completed': completed, 'last': last,
                    'profile': f'turbo-q4-{args.language}-localagreement', 'chunk_s': args.chunk_seconds, 'mlx_cache_limit_bytes': 256 * 2**20}

        @app.websocket('/asr')
        async def asr(socket: WebSocket):
            nonlocal active, last, completed
            await socket.accept()
            if active:
                await socket.close(code=1013, reason='live_preview_busy')
                return
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                await socket.close(code=1013, reason='final_transcription_busy')
                return
            active = True
            processor = None
            retention = None
            results_task = None
            started = time.monotonic()
            stopped = None
            received = 0
            snapshots = 0
            first_text = None
            disconnected = False
            outcome = 'passed'
            before = (alignment.hits, alignment.fallbacks, reuse.hits, guard.rejected)
            try:
                reuse.clear()
                processor = SessionProcessor(transcription_engine=engine, mode='full')
                retention = PreviewResetRetention(processor.transcription)
                if processor.vac.min_silence_samples != 12800:
                    raise RuntimeError('unexpected_vac_profile')
                generator = await processor.create_tasks()
                await socket.send_json({'type': 'config', 'useAudioWorklet': True, 'mode': 'full',
                                        'diarization': False, 'stt_provider': 'whisperlivekit-local'})

                async def results():
                    nonlocal snapshots, first_text, disconnected
                    async for response in generator:
                        message = snapshot_with_words(response)
                        snapshots += 1
                        if first_text is None and (message.get('buffer_transcription', '').strip()
                                or any(line.get('text', '').strip() for line in message.get('lines', []))):
                            first_text = round(time.monotonic() - started, 3)
                            emit('first_text', seconds=first_text)
                        if not disconnected:
                            try:
                                await socket.send_json(message)
                            except (WebSocketDisconnect, RuntimeError, OSError):
                                disconnected = True

                results_task = asyncio.create_task(results(), name='preview-results')
                emit('session_started')
                try:
                    while True:
                        data = await socket.receive_bytes()
                        if len(data) % 2:
                            raise ValueError('unaligned_pcm16')
                        received += len(data)
                        if not data:
                            break
                        await processor.process_audio(data)
                except WebSocketDisconnect:
                    disconnected = True
                stopped = time.monotonic()
                await processor.process_audio(b'')
                await asyncio.wait_for(asyncio.shield(results_task), 60)
            except asyncio.TimeoutError:
                outcome = 'indeterminate'
            except Exception:
                outcome = 'failed'
            finally:
                if results_task is not None:
                    results_task.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await results_task
                try:
                    if processor is not None:
                        await processor.cleanup()
                finally:
                    reuse.clear()
                    # No inference survives cleanup; discard any unused mel.
                    import threading
                    alignment.local = threading.local()
                    fcntl.flock(lock, fcntl.LOCK_UN)
                    active = False
                    completed += 1
                    last = {'outcome': outcome, 'audio_s': round(received / 32000, 3),
                            'first_text_s': first_text, 'snapshots': snapshots,
                            'stop_drain_s': round(time.monotonic() - stopped, 3) if stopped else None,
                            'alignment_hits': alignment.hits - before[0],
                            'alignment_fallbacks': alignment.fallbacks - before[1],
                            'exact_reuse_hits': reuse.hits - before[2],
                            'repetitions_rejected': guard.rejected - before[3],
                            'preview_reset_flushes': retention.reset_flushes if retention else 0,
                            'preview_retained_tokens': retention.retained_tokens if retention else 0}
                    emit('session_finished', **last)
                if not disconnected:
                    with suppress(WebSocketDisconnect, RuntimeError, OSError):
                        await socket.send_json({'type': 'ready_to_stop' if outcome == 'passed' else 'error'})
                        await socket.close()

        emit('model_ready')
        try:
            uvicorn.run(app, host='127.0.0.1', port=args.port, access_log=False, log_config=None)
        finally:
            alignment.__exit__()
            audio_module.FixedVADIterator = original_vad
            lock.close()
    except Exception as error:
        # Exception values/tracebacks may contain private library buffers.
        emit('startup_failed', error_type=type(error).__name__)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
