"""Read-only Conversation projections for saved local audio awaiting a transcript."""

from __future__ import annotations

import hashlib
import json
import stat
import math
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from models.conversation import Conversation
from models.structured import Structured
from utils.env_loader import is_offline_runtime
from utils.local_transport_auth import load_pairing, local_tunnel_enabled
from utils.other.local_storage import local_storage_root_from_env


def _optional_json(path: Path) -> dict:
    try:
        limit = 4 * 1024 * 1024
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError('Invalid local recording state')
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ValueError('Invalid local recording state')
        value = json.loads(raw)
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise ValueError('Invalid local recording state')
    return value


def local_recording_rows(uid: str) -> list[dict]:
    if not is_offline_runtime() or not local_tunnel_enabled():
        return []
    if uid != load_pairing()['owner_uid']:
        return []
    storage = local_storage_root_from_env()
    state = os.environ.get('OMI_HARNESS_STATE_ROOT')
    if storage is None or not state:
        return []
    results = Path(state) / 'services' / 'local-transcripts'
    queue = _optional_json(results / 'watch-queue.json')
    imported = set()
    for receipt in results.glob('*/import.json'):
        if receipt.parent.is_symlink():
            continue
        data = _optional_json(receipt)
        if data.get('import') == 'passed':
            imported.add(data.get('capture_key'))
    rows = []
    for folder in (storage / 'listen-captures').glob('*'):
        if folder.is_symlink() or not folder.is_dir():
            continue
        try:
            capture_id = str(uuid.UUID(folder.name))
            key = hashlib.sha256(capture_id.encode()).hexdigest()
            job = queue.get(key, {})
            if key in imported or job.get('state') == 'completed':
                continue
            metadata = _optional_json(folder / 'metadata.json')
            if metadata.get('status') not in {'completed', 'recovered'} or not (folder / 'audio.wav').is_file():
                continue
            duration = metadata.get('duration_seconds')
            if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
                continue
            if metadata.get('session_id') != capture_id or metadata.get('source') not in {'omi', 'phone'}:
                continue
            started = datetime.fromisoformat(metadata['started_at'].replace('Z', '+00:00'))
            if started.tzinfo is None:
                continue
            status = job.get('state', 'saved')
            if status not in {'pending', 'processing', 'failed', 'no_speech'}:
                status = 'saved'
            rows.append(Conversation(
                id=str(uuid.uuid5(uuid.NAMESPACE_URL, 'omi-local-capture:' + capture_id)),
                created_at=started, started_at=started, finished_at=started + timedelta(seconds=duration),
                source=metadata['source'], status='processing', uses_custom_stt=True,
                structured=Structured(title='Local recording', overview='', emoji='🎙️'),
                transcript_segments=[], discarded=False, deferred=False,
                external_data={'local_recording': {'status': status, 'duration_seconds': duration}},
            ).model_dump())
        except (ValueError, KeyError, TypeError, OSError):
            # An incomplete/corrupt capture cannot hide other saved recordings.
            continue
    return rows


def filter_local_recording_rows(rows, *, statuses, sources, start_date, end_date, folder_id, starred):
    if 'processing' not in statuses or folder_id or starred:
        return []
    if start_date is not None and start_date.tzinfo is None:
        start_date = start_date.replace(tzinfo=timezone.utc)
    if end_date is not None and end_date.tzinfo is None:
        end_date = end_date.replace(tzinfo=timezone.utc)
    return [row for row in rows if (not sources or row['source'] in sources)
            and (start_date is None or row['created_at'] >= start_date)
            and (end_date is None or row['created_at'] <= end_date)]


def merge_local_recording_page(conversations, recordings, *, offset, limit):
    rows = sorted([*conversations, *recordings], key=lambda row: row['created_at'], reverse=True)
    return rows[offset:offset + limit]
