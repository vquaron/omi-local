"""Authenticated, bounded immutable audio delivery for the paired local owner."""

from __future__ import annotations

import asyncio
import errno
from pathlib import Path
import shutil
import threading
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Request

from routers.listen.local_status import require_local_status
from utils.executors import run_blocking, storage_executor
from utils.local_audio_delivery import DeliveryConflict, MAX_BODY_BYTES, MAX_PCM_BYTES, accept_part
from utils.other.local_storage import local_storage_root_from_env

DISK_RESERVE_BYTES = 128 * 1024 * 1024
STAGING_ALLOWANCE_BYTES = 2 * MAX_PCM_BYTES


def require_new_part_capacity(root: Path) -> None:
    """Keep a reserve after the temporary PCM and final WAV coexist."""
    existing = root
    while not existing.exists():
        existing = existing.parent
    if shutil.disk_usage(existing).free < DISK_RESERVE_BYTES + STAGING_ALLOWANCE_BYTES:
        raise OSError(errno.ENOSPC, 'Insufficient local audio storage')


def build_local_audio_router(authenticate: Callable) -> APIRouter:
    router = APIRouter()
    # Bound in-memory request bodies and decoding across all paired connections.
    admission = asyncio.Semaphore(2)
    # Serialize this endpoint's capacity check and writes: two admitted bodies
    # must not spend the same staging allowance. Keep the lock until disk work
    # actually ends, including when the HTTP caller times out.
    publication = threading.Lock()
    pending_publications: set[asyncio.Task] = set()

    def store_part(body: bytes, root: Path) -> dict:
        with publication:
            return accept_part(body, root, check_capacity=require_new_part_capacity)

    async def publish_part(body: bytes, root: Path) -> dict:
        try:
            return await run_blocking(storage_executor, store_part, body, root)
        finally:
            admission.release()

    def publication_finished(task: asyncio.Task) -> None:
        pending_publications.discard(task)
        if not task.cancelled():
            task.exception()  # Consume failures after the HTTP caller has left, without logging private data.

    @router.post('/v1/local/audio/parts', dependencies=[Depends(require_local_status)])
    async def receive_part(request: Request, uid: str = Depends(authenticate)) -> dict:
        if uid != 'alice':
            raise HTTPException(status_code=403, detail='Local owner required')
        try:
            async with asyncio.timeout(35):
                await admission.acquire()
                owns_admission = True
                try:
                    content_length = request.headers.get('content-length')
                    if content_length is not None and (not content_length.isdigit() or int(content_length) > MAX_BODY_BYTES):
                        raise HTTPException(status_code=413, detail='Audio part is too large')
                    body = bytearray()
                    async for block in request.stream():
                        if len(body) + len(block) > MAX_BODY_BYTES:
                            raise HTTPException(status_code=413, detail='Audio part is too large')
                        body.extend(block)
                    root = local_storage_root_from_env()
                    if root is None:
                        raise HTTPException(status_code=503, detail='Local audio storage unavailable')
                    # The permit follows the actual disk work. HTTP timeout or
                    # cancellation must not admit more bodies while its worker
                    # still owns memory or waits for the publication lock.
                    task = asyncio.create_task(publish_part(bytes(body), root))
                    pending_publications.add(task)
                    task.add_done_callback(publication_finished)
                    owns_admission = False
                    return await asyncio.shield(task)
                finally:
                    if owns_admission:
                        admission.release()
        except TimeoutError:
            raise HTTPException(status_code=408, detail='Audio part delivery timed out') from None
        except DeliveryConflict:
            raise HTTPException(status_code=409, detail='Audio part identity conflict') from None
        except OSError as error:
            if error.errno in {errno.ENOSPC, errno.EDQUOT}:
                raise HTTPException(status_code=507, detail='Insufficient local audio storage') from None
            raise HTTPException(status_code=503, detail='Local audio storage unavailable') from None
        except (ValueError, KeyError, TypeError, UnicodeError):
            raise HTTPException(status_code=422, detail='Invalid audio part') from None

    return router
