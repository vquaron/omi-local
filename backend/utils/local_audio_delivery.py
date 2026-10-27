"""Immutable phone outbox parts: validate, decode, publish, then acknowledge."""

from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable

from utils.offline_audio_capture import (
    CAPTURE_DIRECTORY,
    METADATA_NAME,
    OfflineAudioCapture,
    create_offline_audio_capture,
    _atomic_json,
)
from utils.opus_preroll import decode_fresh_audio, validate_preroll

MAGIC = b'OMILOC1\n'
MAX_BODY_BYTES = 16 * 1024 * 1024
MAX_HEADER_BYTES = 4096
MAX_FRAME_BYTES = 4096
MAX_PCM_BYTES = 301 * 16000 * 2


def initialize_local_capture(
    *, offline: bool, headers: object, session_id: str, source: str | None, codec: str, sample_rate: int, channels: int
) -> tuple[bool, OfflineAudioCapture | None]:
    """One production capture-owner decision shared by listen and its tests."""
    preview_only = (
        offline
        and headers.get('x-omi-local-audio-outbox') == '1'
        and (source, codec) in {('omi', 'opus'), ('omi', 'opus_fs320'), ('phone', 'pcm16')}
        and sample_rate == 16000
        and channels == 1
    )
    sink = (
        None
        if preview_only
        else create_offline_audio_capture(
            session_id=session_id,
            source=source,
            input_codec=codec,
            sample_rate=sample_rate,
            channels=channels,
        )
    )
    return preview_only, sink


class DeliveryConflict(ValueError):
    """A stable part identity already belongs to different bytes."""


def _uuid(value: object) -> str:
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError('Invalid audio part identity')
    return value


def parse_part(body: bytes) -> tuple[dict, list[bytes]]:
    if len(body) > MAX_BODY_BYTES or len(body) < 12 or body[:8] != MAGIC:
        raise ValueError('Invalid audio part envelope')
    header_size = struct.unpack('>I', body[8:12])[0]
    if not 0 < header_size <= MAX_HEADER_BYTES or 12 + header_size > len(body):
        raise ValueError('Invalid audio part header')
    header = json.loads(body[12 : 12 + header_size])
    if not isinstance(header, dict) or type(header.get('version')) is not int or header['version'] not in {1, 2}:
        raise ValueError('Invalid audio part version')
    _uuid(header.get('id'))
    _uuid(header.get('recording_id'))
    if type(header.get('index')) is not int or not 0 <= header['index'] <= 1_000_000_000:
        raise ValueError('Invalid audio part order')
    if not isinstance(header.get('server_id'), str) or not re.fullmatch('[0-9a-f]{64}', header['server_id']):
        raise ValueError('Invalid audio part server binding')
    if (header.get('source'), header.get('codec')) not in {('omi', 'opus'), ('omi', 'opus_fs320'), ('phone', 'pcm16')}:
        raise ValueError('Unsupported audio part format')
    if not isinstance(header.get('started_at'), str) or len(header['started_at']) > 40:
        raise ValueError('Invalid audio part time')
    started = datetime.fromisoformat(header['started_at'].replace('Z', '+00:00'))
    if started.tzinfo is None:
        raise ValueError('Audio part time requires a timezone')
    position = 12 + header_size
    frames = []
    while position < len(body):
        if position + 4 > len(body):
            raise ValueError('Incomplete audio packet length')
        size = struct.unpack('>I', body[position : position + 4])[0]
        position += 4
        if not 0 < size <= MAX_FRAME_BYTES or position + size > len(body):
            raise ValueError('Incomplete or oversized audio packet')
        frames.append(body[position : position + size])
        position += size
        if len(frames) > 60_000:
            raise ValueError('Too many audio packets')
    if not frames:
        raise ValueError('Audio part is empty')
    validate_preroll(header, frames)
    return header, frames


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _receipt(target: Path, part_id: str, digest: str) -> dict | None:
    if not target.exists():
        return None
    try:
        metadata = json.loads((target / METADATA_NAME).read_text())
        delivery = metadata['delivery']
        if delivery['sha256'] == digest and delivery['id'] == part_id and (target / 'audio.wav').is_file():
            return {'id': part_id, 'sha256': digest, 'stored': True}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    raise DeliveryConflict('Audio part identity already exists')


def accept_part(
    body: bytes,
    root: Path,
    *,
    decoder_factory: Callable[[], object] | None = None,
    check_capacity: Callable[[Path], None] | None = None,
) -> dict:
    """Return a receipt only after a whole immutable part is durably published.

    Staging is outside the transcription watcher's capture tree. Concurrent or
    repeated uploads converge on the exact body checksum, including lost ACKs.
    """
    header, frames = parse_part(body)
    digest = hashlib.sha256(body).hexdigest()
    capture_root = root / CAPTURE_DIRECTORY
    capture_root.mkdir(parents=True, exist_ok=True)
    _sync_directory(root)
    target = capture_root / header['id']
    existing = _receipt(target, header['id'], digest)
    if existing is not None:
        _sync_directory(target)
        _sync_directory(capture_root)
        return existing
    if check_capacity is not None:
        check_capacity(root)
    if header['codec'] == 'pcm16':
        decode = lambda frame: frame
    else:
        if decoder_factory is None:
            import opuslib

            decoder_factory = lambda: opuslib.Decoder(16000, 1)
        decoder = decoder_factory()
        decode = lambda frame: decoder.decode(frame, frame_size=1920)
    preroll = validate_preroll(header, frames)
    pcm = decode_fresh_audio(frames, preroll, decode, limit=MAX_PCM_BYTES)
    staging_root = root / 'local-audio-staging'
    staging_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='part-', dir=staging_root) as temporary:
        capture = OfflineAudioCapture(
            session_id=header['id'], source=header['source'], input_codec=header['codec'], root=Path(temporary)
        )
        capture.session_dir.chmod(0o700)
        capture.started_at = header['started_at']
        capture.record_decoded_frame(encoded_bytes=sum(map(len, frames[preroll:])), pcm=pcm)
        capture.frames_received = len(frames) - preroll
        metadata = capture.finalize()
        metadata['delivery'] = {key: header[key] for key in ('id', 'recording_id', 'index')}
        metadata['delivery']['sha256'] = digest
        metadata['delivery']['preroll_frames'] = preroll
        _atomic_json(capture.metadata_path, metadata)
        _sync_directory(capture.session_dir)
        try:
            os.rename(capture.session_dir, target)
        except OSError:
            existing = _receipt(target, header['id'], digest)
            if existing is None:
                raise
        _sync_directory(capture_root)
    return {'id': header['id'], 'sha256': digest, 'stored': True}
