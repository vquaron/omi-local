"""Fail-closed PCM/WAV capture for the owned offline listen path."""

from __future__ import annotations

import json
import os
import time
import uuid
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from utils.env_loader import is_offline_runtime
from utils.other.local_storage import local_storage_root_from_env

CAPTURE_DIRECTORY = 'listen-captures'
PCM_PART_NAME = 'audio.pcm.part'
METADATA_PART_NAME = 'metadata.json.part'
WAV_NAME = 'audio.wav'
METADATA_NAME = 'metadata.json'
SCHEMA_VERSION = 1
OUTPUT_SAMPLE_RATE = 16000
OUTPUT_CHANNELS = 1
OUTPUT_SAMPLE_WIDTH_BYTES = 2
INPUT_CODECS_BY_SOURCE = {'omi': frozenset({'opus', 'opus_fs320'}), 'phone': frozenset({'pcm16'})}


def _input_format_supported(source: str, input_codec: str) -> bool:
    return input_codec in INPUT_CODECS_BY_SOURCE.get(source, ())


def _utc_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace('+00:00', 'Z')


def _validated_session_id(value: str) -> str:
    parsed = uuid.UUID(value)
    if str(parsed) != value.lower():
        raise ValueError('offline capture session ID must be a canonical UUID')
    return str(parsed)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_wav_atomic(pcm_path: Path, wav_path: Path) -> None:
    temporary = wav_path.with_name(f'.{wav_path.name}.{uuid.uuid4().hex}.tmp')
    try:
        with wave.open(str(temporary), 'wb') as target:
            target.setnchannels(OUTPUT_CHANNELS)
            target.setsampwidth(OUTPUT_SAMPLE_WIDTH_BYTES)
            target.setframerate(OUTPUT_SAMPLE_RATE)
            with pcm_path.open('rb') as source:
                while chunk := source.read(1024 * 1024):
                    target.writeframesraw(chunk)
        with temporary.open('rb') as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, wav_path)
    finally:
        temporary.unlink(missing_ok=True)


def _capture_metadata(
    *,
    session_id: str,
    started_at: str,
    ended_at: str | None,
    input_codec: str,
    source: str,
    frames_received: int,
    encoded_bytes: int,
    decoded_pcm_bytes: int,
    decode_errors: int,
    status: str,
) -> dict[str, Any]:
    return {
        'schema_version': SCHEMA_VERSION,
        'session_id': session_id,
        'started_at': started_at,
        'ended_at': ended_at,
        'source': source,
        'input': {
            'codec': input_codec,
            'sample_rate': OUTPUT_SAMPLE_RATE,
            'channels': OUTPUT_CHANNELS,
        },
        'output': {
            'file': WAV_NAME,
            'codec': 'pcm_s16le',
            'sample_rate': OUTPUT_SAMPLE_RATE,
            'channels': OUTPUT_CHANNELS,
        },
        'frames_received': frames_received,
        'encoded_bytes': encoded_bytes,
        'decoded_pcm_bytes': decoded_pcm_bytes,
        'duration_seconds': round(
            decoded_pcm_bytes / (OUTPUT_SAMPLE_RATE * OUTPUT_CHANNELS * OUTPUT_SAMPLE_WIDTH_BYTES), 6
        ),
        'decode_errors': decode_errors,
        'status': status,
    }


class OfflineAudioCapture:
    """Write decoded mono PCM immediately and atomically finalize a WAV session."""

    def __init__(self, *, session_id: str, input_codec: str, source: str, root: Path):
        self.session_id = _validated_session_id(session_id)
        if not _input_format_supported(source, input_codec):
            raise ValueError('unsupported offline capture source/codec combination')
        self.input_codec = input_codec
        self.source = source
        self.started_at_timestamp = time.time()
        self.started_at = _utc_iso(self.started_at_timestamp)
        self.frames_received = 0
        self.encoded_bytes = 0
        self.decoded_pcm_bytes = 0
        self.decode_errors = 0
        self._closed = False
        self.session_dir = root / CAPTURE_DIRECTORY / self.session_id
        self.session_dir.mkdir(parents=True, exist_ok=False)
        self.pcm_part_path = self.session_dir / PCM_PART_NAME
        self.metadata_part_path = self.session_dir / METADATA_PART_NAME
        self.wav_path = self.session_dir / WAV_NAME
        self.metadata_path = self.session_dir / METADATA_NAME
        self._pcm = self.pcm_part_path.open('xb', buffering=0)
        self._write_progress_metadata()

    def _metadata(self, *, ended_at: str | None, status: str) -> dict[str, Any]:
        return _capture_metadata(
            session_id=self.session_id,
            started_at=self.started_at,
            ended_at=ended_at,
            input_codec=self.input_codec,
            source=self.source,
            frames_received=self.frames_received,
            encoded_bytes=self.encoded_bytes,
            decoded_pcm_bytes=self.decoded_pcm_bytes,
            decode_errors=self.decode_errors,
            status=status,
        )

    def _write_progress_metadata(self) -> None:
        _atomic_json(self.metadata_part_path, self._metadata(ended_at=None, status='recording'))

    def record_decoded_frame(self, *, encoded_bytes: int, pcm: bytes) -> None:
        if self._closed:
            raise RuntimeError('offline capture is already closed')
        if len(pcm) % OUTPUT_SAMPLE_WIDTH_BYTES:
            raise ValueError('decoded PCM byte count is not aligned to PCM16 samples')
        self._pcm.write(pcm)
        self.frames_received += 1
        self.encoded_bytes += encoded_bytes
        self.decoded_pcm_bytes += len(pcm)
        self._write_progress_metadata()

    def record_decode_error(self, *, encoded_bytes: int) -> None:
        if self._closed:
            raise RuntimeError('offline capture is already closed')
        self.frames_received += 1
        self.encoded_bytes += encoded_bytes
        self.decode_errors += 1
        self._write_progress_metadata()

    def finalize(self) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError('offline capture is already closed')
        self._closed = True
        self._pcm.flush()
        os.fsync(self._pcm.fileno())
        self._pcm.close()
        ended_at = _utc_iso(time.time())
        metadata = self._metadata(ended_at=ended_at, status='completed')
        _write_wav_atomic(self.pcm_part_path, self.wav_path)
        _atomic_json(self.metadata_path, metadata)
        self.pcm_part_path.unlink()
        self.metadata_part_path.unlink()
        return metadata



# Finish bounded files for the watcher while the listen socket and input stay live.
# A boundary falls between decoded packets, so no samples are discarded or repeated.
CAPTURE_CHUNK_SECONDS = 5 * 60


def rotate_offline_audio_capture(capture: OfflineAudioCapture) -> OfflineAudioCapture:
    if capture.decoded_pcm_bytes < CAPTURE_CHUNK_SECONDS * OUTPUT_SAMPLE_RATE * OUTPUT_SAMPLE_WIDTH_BYTES:
        return capture
    capture.finalize()
    return OfflineAudioCapture(
        session_id=str(uuid.uuid4()), input_codec=capture.input_codec,
        source=capture.source, root=capture.session_dir.parent.parent,
    )


def create_offline_audio_capture(
    *,
    session_id: str,
    source: str | None,
    input_codec: str,
    sample_rate: int,
    channels: int,
) -> OfflineAudioCapture | None:
    """Capture existing CV1 Opus and phone-mic PCM16 streams without STT."""

    if not is_offline_runtime():
        return None
    if source not in INPUT_CODECS_BY_SOURCE:
        return None
    if sample_rate != OUTPUT_SAMPLE_RATE or channels != OUTPUT_CHANNELS:
        raise RuntimeError('offline capture requires mono 16 kHz audio')
    if not _input_format_supported(source, input_codec):
        raise RuntimeError('unsupported local audio source/codec combination')
    root = local_storage_root_from_env()
    if root is None:
        raise RuntimeError('offline capture requires validated OMI_LOCAL_STORAGE_ROOT')
    return OfflineAudioCapture(session_id=session_id, input_codec=input_codec, source=source, root=root)


def _recover_one(pcm_part_path: Path) -> bool:
    session_dir = pcm_part_path.parent
    session_id = _validated_session_id(session_dir.name)
    metadata_part_path = session_dir / METADATA_PART_NAME
    loaded = json.loads(metadata_part_path.read_text(encoding='utf-8'))
    if not isinstance(loaded, dict) or loaded.get('schema_version') != SCHEMA_VERSION:
        raise ValueError('unsupported offline capture recovery metadata')
    if loaded.get('session_id') != session_id or loaded.get('source') not in INPUT_CODECS_BY_SOURCE:
        raise ValueError('offline capture recovery identity mismatch')
    input_format = loaded.get('input')
    if not isinstance(input_format, dict) or not _input_format_supported(loaded['source'], input_format.get('codec')):
        raise ValueError('offline capture recovery codec mismatch')
    if input_format.get('sample_rate') != OUTPUT_SAMPLE_RATE or input_format.get('channels') != OUTPUT_CHANNELS:
        raise ValueError('offline capture recovery input format mismatch')
    decoded_pcm_bytes = pcm_part_path.stat().st_size
    if decoded_pcm_bytes % OUTPUT_SAMPLE_WIDTH_BYTES:
        raise ValueError('offline capture PCM part is not sample-aligned')
    ended_at = _utc_iso(max(pcm_part_path.stat().st_mtime, time.time()))
    recovered = _capture_metadata(
        session_id=session_id,
        started_at=str(loaded.get('started_at') or ended_at),
        ended_at=ended_at,
        input_codec=str(input_format['codec']),
        source=loaded['source'],
        frames_received=int(loaded.get('frames_received') or 0),
        encoded_bytes=int(loaded.get('encoded_bytes') or 0),
        decoded_pcm_bytes=decoded_pcm_bytes,
        decode_errors=int(loaded.get('decode_errors') or 0),
        status='recovered',
    )
    _write_wav_atomic(pcm_part_path, session_dir / WAV_NAME)
    _atomic_json(session_dir / METADATA_NAME, recovered)
    pcm_part_path.unlink()
    metadata_part_path.unlink()
    return True


def recover_offline_audio_captures() -> tuple[int, int]:
    """Recover valid leftover PCM parts; failures leave source parts untouched."""

    if not is_offline_runtime():
        return 0, 0
    root = local_storage_root_from_env()
    if root is None:
        raise RuntimeError('offline capture recovery requires validated OMI_LOCAL_STORAGE_ROOT')
    capture_root = root / CAPTURE_DIRECTORY
    if not capture_root.is_dir():
        return 0, 0
    recovered = 0
    failed = 0
    for pcm_part_path in sorted(capture_root.glob(f'*/{PCM_PART_NAME}')):
        try:
            _recover_one(pcm_part_path)
        except Exception:
            failed += 1
        else:
            recovered += 1
    return recovered, failed
