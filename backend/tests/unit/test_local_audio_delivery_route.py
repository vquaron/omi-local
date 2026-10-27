from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import errno
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from routers.local_audio import build_local_audio_router
from utils.local_audio_delivery import MAX_BODY_BYTES
from utils.local_transport_auth import LocalTransportAuthMiddleware
from tests.unit.test_local_audio_delivery import part


def client(tmp_path: Path, monkeypatch) -> tuple[TestClient, str]:
    key = 's' * 43
    pairing = tmp_path / 'pairing.json'
    pairing.write_text(json.dumps({'version': 1, 'owner_uid': 'alice', 'key_sha256': hashlib.sha256(key.encode()).hexdigest()}))
    pairing.chmod(0o600)
    monkeypatch.setenv('OMI_ENV_STAGE', 'offline')
    monkeypatch.setenv('PROVIDER_MODE', 'offline')
    monkeypatch.setenv('OMI_LOCAL_TRANSPORT', 'tailscale')
    monkeypatch.setenv('OMI_LOCAL_PAIRING_FILE', str(pairing))
    monkeypatch.setenv('OMI_HARNESS_STATE_ROOT', str(tmp_path / 'state'))
    monkeypatch.setenv('OMI_LOCAL_STORAGE_ROOT', str(tmp_path / 'state' / 'services' / 'storage'))
    monkeypatch.setenv('FIREBASE_PROJECT_ID', 'demo-omi-local')
    monkeypatch.setenv('FIRESTORE_EMULATOR_HOST', '127.0.0.1:8085')
    app = FastAPI()
    app.include_router(build_local_audio_router(lambda: 'alice'))
    app.add_middleware(LocalTransportAuthMiddleware)
    return TestClient(app), key


def test_route_authenticates_before_consuming_upload(tmp_path: Path, monkeypatch):
    api, _ = client(tmp_path, monkeypatch)
    consumed = []

    def body():
        consumed.append(True)
        yield b'private audio must not be read'

    response = api.post('/v1/local/audio/parts', content=body())
    assert response.status_code == 401
    assert consumed == []


def test_route_acknowledges_exact_saved_audio_and_lost_ack_retry(tmp_path: Path, monkeypatch):
    api, key = client(tmp_path, monkeypatch)
    body = part(b'\x00\x00' * 160)
    headers = {'Authorization': f'Bearer {key}'}
    response = api.post('/v1/local/audio/parts', content=body, headers=headers)
    assert response.status_code == 200
    assert response.json()['sha256'] == hashlib.sha256(body).hexdigest()
    assert api.post('/v1/local/audio/parts', content=body, headers=headers).json() == response.json()
    conflict = api.post('/v1/local/audio/parts', content=body[:-1] + b'\x01', headers=headers)
    assert conflict.status_code == 409
    assert len(list((tmp_path / 'state/services/storage/listen-captures').iterdir())) == 1


def test_route_rejects_declared_and_chunked_body_limits(tmp_path: Path, monkeypatch):
    api, key = client(tmp_path, monkeypatch)
    headers = {'Authorization': f'Bearer {key}', 'Content-Length': str(MAX_BODY_BYTES + 1)}
    assert api.post('/v1/local/audio/parts', content=b'x', headers=headers).status_code == 413
    headers.pop('Content-Length')
    assert api.post('/v1/local/audio/parts', content=iter([b'x' * MAX_BODY_BYTES, b'x']), headers=headers).status_code == 413
    assert not (tmp_path / 'state/services/storage/listen-captures').exists()


def test_low_capacity_retains_new_part_for_retry_but_acknowledges_existing_part(tmp_path: Path, monkeypatch):
    from routers import local_audio
    from utils import local_audio_delivery

    api, key = client(tmp_path, monkeypatch)
    body = part(b'\x01\x00' * 160)
    headers = {'Authorization': f'Bearer {key}'}
    required = local_audio.DISK_RESERVE_BYTES + local_audio.STAGING_ALLOWANCE_BYTES
    free = [required - 1]
    capacity_checks = []
    created_wavs = []
    original_capture = local_audio_delivery.OfflineAudioCapture

    def usage(path):
        assert path.is_dir()
        capacity_checks.append(path)
        return SimpleNamespace(free=free[0])

    def capture(**kwargs):
        created_wavs.append(True)
        return original_capture(**kwargs)

    monkeypatch.setattr(local_audio.shutil, 'disk_usage', usage)
    monkeypatch.setattr(local_audio_delivery, 'OfflineAudioCapture', capture)
    low = api.post('/v1/local/audio/parts', content=body, headers=headers)
    assert low.status_code == 507
    assert low.json() == {'detail': 'Insufficient local audio storage'}
    assert not created_wavs
    root = tmp_path / 'state/services/storage'
    assert not list(root.rglob('audio.wav'))

    free[0] = required
    saved = api.post('/v1/local/audio/parts', content=body, headers=headers)
    assert saved.status_code == 200
    assert saved.json()['sha256'] == hashlib.sha256(body).hexdigest()
    assert len(created_wavs) == 1
    checks_before_retry = len(capacity_checks)
    free[0] = 0
    retried = api.post('/v1/local/audio/parts', content=body, headers=headers)
    assert retried.status_code == 200 and retried.json() == saved.json()
    assert len(capacity_checks) == checks_before_retry
    assert len(created_wavs) == 1 and len(list(root.rglob('audio.wav'))) == 1


@pytest.mark.parametrize('fault,status', [(errno.ENOSPC, 507), (errno.EDQUOT, 507), (errno.EIO, 503)])
def test_filesystem_failure_returns_fixed_error_without_ack_or_private_path(tmp_path: Path, monkeypatch, fault, status):
    from routers import local_audio

    api, key = client(tmp_path, monkeypatch)
    def fail(*_args, **_kwargs):
        raise OSError(fault, 'synthetic private filesystem path and recording details')

    monkeypatch.setattr(local_audio, 'accept_part', fail)
    result = api.post('/v1/local/audio/parts', content=part(b'\x00\x00' * 160),
                      headers={'Authorization': f'Bearer {key}'})
    assert result.status_code == status
    assert result.json() == {'detail': 'Insufficient local audio storage' if status == 507 else 'Local audio storage unavailable'}
    assert 'private' not in result.text and 'sha256' not in result.json()
    assert not (tmp_path / 'state/services/storage/listen-captures').exists()


@pytest.mark.parametrize('leave', ['cancel', 'timeout'])
def test_departed_http_callers_hold_body_admission_until_disk_workers_finish(tmp_path: Path, monkeypatch, leave):
    from routers import local_audio

    async def exercise():
        loop = asyncio.get_running_loop()
        unblock_disk = threading.Event()
        submitted = asyncio.Event()
        first_worker_started = asyncio.Event()
        third_body_read = asyncio.Event()
        worker_submissions = []
        body_reads = []
        deadlines = []
        original_timeout = asyncio.timeout
        original_blocking = local_audio.run_blocking

        def controllable_timeout(_seconds):
            context = original_timeout(None)
            deadlines.append(context)
            return context

        def store(body, _root, **_kwargs):
            if body == b'1':
                loop.call_soon_threadsafe(first_worker_started.set)
                if not unblock_disk.wait(3):
                    raise RuntimeError('Test failed to release its synthetic disk worker')
            return {'stored': True, 'id': body.decode()}

        async def observed_blocking(executor, function, *args):
            worker_submissions.append(args[0])
            if len(worker_submissions) == 2:
                submitted.set()
            return await original_blocking(executor, function, *args)

        def request(number):
            async def receive():
                body_reads.append(number)
                if number == 3:
                    third_body_read.set()
                return {'type': 'http.request', 'body': str(number).encode(), 'more_body': False}

            return Request({'type': 'http', 'method': 'POST', 'path': '/v1/local/audio/parts', 'headers': []}, receive)

        monkeypatch.setattr(local_audio.asyncio, 'timeout', controllable_timeout)
        monkeypatch.setattr(local_audio, 'accept_part', store)
        monkeypatch.setattr(local_audio, 'run_blocking', observed_blocking)
        monkeypatch.setattr(local_audio, 'local_storage_root_from_env', lambda: tmp_path)
        with ThreadPoolExecutor(max_workers=2) as workers:
            monkeypatch.setattr(local_audio, 'storage_executor', workers)
            handler = build_local_audio_router(lambda: 'alice').routes[0].endpoint
            first = asyncio.create_task(handler(request(1), uid='alice'))
            second = asyncio.create_task(handler(request(2), uid='alice'))
            third = None
            try:
                await asyncio.wait_for(submitted.wait(), 2)
                await asyncio.wait_for(first_worker_started.wait(), 2)
                if leave == 'cancel':
                    first.cancel()
                    second.cancel()
                else:
                    for context in deadlines:
                        context.reschedule(loop.time())
                results = await asyncio.gather(first, second, return_exceptions=True)
                if leave == 'cancel':
                    assert all(isinstance(result, asyncio.CancelledError) for result in results)
                else:
                    assert all(isinstance(result, HTTPException) and result.status_code == 408 for result in results)
                third = asyncio.create_task(handler(request(3), uid='alice'))
                checkpoint = loop.create_future()
                loop.call_soon(checkpoint.set_result, None)
                await checkpoint
                assert body_reads == [1, 2]
                assert worker_submissions == [b'1', b'2']
                assert not third.done()
                unblock_disk.set()
                await asyncio.wait_for(third_body_read.wait(), 2)
                assert await asyncio.wait_for(third, 2) == {'stored': True, 'id': '3'}
            finally:
                unblock_disk.set()
                for task in (first, second, third):
                    if task is not None and not task.done():
                        task.cancel()
                await asyncio.gather(*[task for task in (first, second, third) if task is not None], return_exceptions=True)

    asyncio.run(exercise())
