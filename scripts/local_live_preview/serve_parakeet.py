"""Trial Parakeet /asr: existing PCM16 and full-snapshot wire contract."""

import argparse
import asyncio
from contextlib import asynccontextmanager, suppress
import fcntl
import json
import logging
import os
from pathlib import Path
import struct
import sys
import time

from .serve import deny_network, inference_busy

RECOVERY_RETRY_SECONDS = 5


def startup_diagnostic(error):
    """Only fixed codes: model/tool exception messages may contain private data."""
    if isinstance(error, asyncio.TimeoutError):
        return {'reason': 'model_start_timeout', 'outcome': 'indeterminate',
                'action': 'check_model_initialization_then_retry'}
    if isinstance(error, asyncio.CancelledError):
        return {'reason': 'model_start_cancelled', 'outcome': 'indeterminate',
                'action': 'retry_live_start'}
    if isinstance(error, BlockingIOError):
        return {'reason': 'inference_busy', 'outcome': 'failed',
                'action': 'wait_for_transcription_then_retry'}
    if isinstance(error, FileNotFoundError):
        return {'reason': 'local_inputs_missing', 'outcome': 'failed',
                'action': 'prepare_live_model_and_worker'}
    if isinstance(error, PermissionError):
        return {'reason': 'runtime_permission_denied', 'outcome': 'indeterminate',
                'action': 'check_host_runtime_permissions'}
    return {'reason': 'model_start_failed', 'outcome': 'failed',
            'action': 'verify_live_model_and_worker'}


class Worker:
    def __init__(self, binary, model_dir):
        self.binary = binary
        self.model_dir = model_dir
        self.process = None

    async def start(self):
        self.process = await asyncio.create_subprocess_exec(
            '/usr/bin/sandbox-exec', '-p', '(version 1)(allow default)(deny network*)',
            str(self.binary), str(self.model_dir), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            env={**os.environ, 'OS_ACTIVITY_MODE': 'disable'},
        )
        if (await asyncio.wait_for(self.read(), 60)).get('type') != 'model_ready':
            raise RuntimeError('parakeet_start_failed')

    async def read(self):
        # A full transcript grows beyond StreamReader's per-read buffer limit.
        # Consume complete chunks without losing the JSONL boundary or imposing
        # that buffer limit on transcript length. Decode UTF-8 only after the
        # whole message arrives; the next protocol message stays in the pipe.
        reader = self.process.stdout
        raw = bytearray()
        while True:
            try:
                raw.extend(await reader.readuntil(b'\n'))
                break
            except asyncio.LimitOverrunError as error:
                raw.extend(await reader.readexactly(error.consumed))
            except asyncio.IncompleteReadError:
                raise RuntimeError('parakeet_worker_closed') from None
        message = json.loads(raw)
        if message.get('type') == 'error':
            raise RuntimeError('parakeet_worker_failed')
        return message

    async def send(self, kind, pcm=b''):
        self.process.stdin.write(struct.pack('!I', len(pcm) + 1) + bytes([kind]) + pcm)
        await self.process.stdin.drain()

    async def close(self):
        if self.process is not None:
            if self.process.returncode is None:
                with suppress(ProcessLookupError):
                    self.process.kill()
            await self.process.wait()  # No inference survives lock release.


def create_app(worker, lock_path, emit):
    from fastapi import FastAPI, WebSocket
    from starlette.websockets import WebSocketDisconnect

    active = False
    ready = False
    completed = 0
    last = None
    recovering = False
    recovery_task = None
    retry_at = 0
    last_error = None
    stopping = False
    lock = lock_path.open('r+')

    def worker_ready():
        return ready and worker.process is not None and worker.process.returncode is None

    async def recover():
        nonlocal ready, recovering, retry_at, last_error
        acquired = False
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
            # Reap an idle worker that died between sessions before replacing it.
            await worker.close()
            await worker.start()
            ready = True
            retry_at = 0
            last_error = None
            emit('model_ready', recovered=True)
        except (Exception, asyncio.CancelledError) as error:
            ready = False
            diagnostic = startup_diagnostic(error)
            last_error = diagnostic['reason']
            retry_at = time.monotonic() + RECOVERY_RETRY_SECONDS
            emit('recovery_failed', **diagnostic)
            if isinstance(error, asyncio.CancelledError):
                raise
        finally:
            if acquired:
                # Never let final inference enter while a failed load survives.
                if not ready:
                    await worker.close()
                fcntl.flock(lock, fcntl.LOCK_UN)
            recovering = False

    def request_recovery():
        nonlocal ready, recovering, recovery_task
        if stopping or active or recovering or worker_ready() or time.monotonic() < retry_at:
            return
        ready = False
        # Set before scheduling: flock on our own fd would not exclude a second
        # coroutine. Exactly one task owns model reload and its inference lock.
        recovering = True
        recovery_task = asyncio.create_task(recover(), name='parakeet-recovery')

    @asynccontextmanager
    async def lifespan(app):
        nonlocal ready, stopping
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                await worker.start()
                ready = True
                fcntl.flock(lock, fcntl.LOCK_UN)
            except (Exception, asyncio.CancelledError) as error:
                emit('startup_failed', **startup_diagnostic(error))
                raise
            emit('model_ready')
            yield
        finally:
            stopping = True
            if recovery_task is not None:
                recovery_task.cancel()
                with suppress(Exception, asyncio.CancelledError):
                    await recovery_task
            ready = False
            # Startup failures/cancellation must reap the child while the
            # inference lock is still held, just like failed ASR sessions.
            await worker.close()
            lock.close()

    app = FastAPI(lifespan=lifespan)

    @app.get('/health')
    async def health():
        return {'ready': worker_ready(), 'diarization': False, 'active': active, 'recovering': recovering,
                'busy': active or recovering or inference_busy(lock_path),
                'last_error': last_error,
                'completed': completed, 'last': last, 'profile': 'parakeet-v3-int8-ane-auto',
                'chunk_s': 11, 'left_context_s': 2, 'right_context_s': 2}

    @app.websocket('/asr')
    async def asr(socket: WebSocket):
        nonlocal active, ready, completed, last
        await socket.accept()
        if active:
            await socket.close(code=1013, reason='live_preview_busy')
            return
        if not worker_ready():
            request_recovery()
            await socket.close(code=1013, reason='live_preview_recovering' if recovering else 'live_preview_unavailable')
            return
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            await socket.close(code=1013, reason='final_transcription_busy')
            return
        active = True
        started = time.monotonic()
        stopped = None
        received = 0
        snapshots = 0
        first_text = None
        disconnected = False
        outcome = 'passed'
        reader = None
        feeder = None
        try:
            await worker.send(1)
            if (await asyncio.wait_for(worker.read(), 3)).get('type') != 'started':
                raise RuntimeError('parakeet_session_start_failed')
            await socket.send_json({'type': 'config', 'useAudioWorklet': True, 'mode': 'full',
                                    'diarization': False, 'stt_provider': 'parakeet-local'})

            async def results():
                nonlocal snapshots, first_text, disconnected
                previous = ''
                while True:
                    message = await worker.read()
                    if message.get('type') == 'finished':
                        return
                    if message.get('type') != 'snapshot':
                        raise RuntimeError('parakeet_protocol_error')
                    text = message['text'].strip()
                    if not text or text == previous:
                        continue
                    previous = text
                    snapshots += 1
                    if first_text is None:
                        first_text = round(time.monotonic() - started, 3)
                        emit('first_text', seconds=first_text)
                    if not disconnected:
                        try:
                            await socket.send_json({'lines': [{'text': text, 'speaker': 0}],
                                                    'buffer_transcription': ''})
                        except (WebSocketDisconnect, RuntimeError, OSError):
                            disconnected = True

            async def feed():
                nonlocal received, stopped, disconnected
                try:
                    while True:
                        pcm = await socket.receive_bytes()
                        if len(pcm) % 2:
                            raise ValueError('unaligned_pcm16')
                        if not pcm:
                            break
                        received += len(pcm)
                        await worker.send(2, pcm)
                except WebSocketDisconnect:
                    disconnected = True
                stopped = time.monotonic()
                await worker.send(3)

            reader = asyncio.create_task(results(), name='parakeet-results')
            feeder = asyncio.create_task(feed(), name='parakeet-pcm')
            emit('session_started')
            done, _ = await asyncio.wait([reader, feeder], return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            if reader in done and not feeder.done():
                raise RuntimeError('parakeet_early_eof')
            await asyncio.wait_for(asyncio.shield(reader), 60)
        except asyncio.TimeoutError:
            outcome = 'indeterminate'
        except (Exception, asyncio.CancelledError):
            outcome = 'failed'
        finally:
            for task in (feeder, reader):
                if task is not None:
                    task.cancel()
                    with suppress(Exception, asyncio.CancelledError):
                        await task
            if outcome != 'passed':
                ready = False
                await worker.close()
            fcntl.flock(lock, fcntl.LOCK_UN)
            active = False
            completed += 1
            last = {'outcome': outcome, 'audio_s': round(received / 32000, 3),
                    'snapshots': snapshots, 'first_text_s': first_text,
                    'stop_drain_s': round(time.monotonic() - stopped, 3) if stopped else None}
            emit('session_finished', **last)
            if outcome != 'passed':
                # The failed request is never replayed. Prepare the next session
                # outside its 3-second ASR handshake; no automatic retry loop.
                request_recovery()
            if not disconnected:
                with suppress(WebSocketDisconnect, RuntimeError, OSError):
                    await socket.send_json({'type': 'ready_to_stop' if outcome == 'passed' else 'error'})
                    await socket.close()

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker', type=Path, required=True)
    parser.add_argument('--model-dir', type=Path, required=True)
    parser.add_argument('--inference-lock', type=Path, required=True)
    parser.add_argument('--port', type=int, default=18090)
    args = parser.parse_args()
    output = os.fdopen(os.dup(1), 'w', buffering=1)
    null = os.open(os.devnull, os.O_WRONLY)
    os.dup2(null, 1); os.dup2(null, 2); os.close(null)
    logging.disable(logging.CRITICAL)

    startup_reported = False

    def emit(event, **fields):
        nonlocal startup_reported
        if event == 'startup_failed':
            startup_reported = True
        output.write(json.dumps({'event': event, **fields}) + '\n')

    try:
        sys.addaudithook(deny_network)
        import uvicorn
        app = create_app(Worker(args.worker.resolve(strict=True), args.model_dir.resolve(strict=True)),
                         args.inference_lock, emit)
        uvicorn.run(app, host='127.0.0.1', port=args.port, access_log=False, log_config=None)
    except SystemExit as error:
        # Uvicorn converts lifespan and bind failures to SystemExit, bypassing
        # Exception. Keep a nonzero exit visible even with private stderr muted.
        code = error.code if type(error.code) is int else 1
        if code and not startup_reported:
            emit('startup_failed', reason='server_start_failed', outcome='failed',
                 action='check_live_port_and_runtime')
        return code
    except Exception as error:
        if not startup_reported:
            emit('startup_failed', **startup_diagnostic(error))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
