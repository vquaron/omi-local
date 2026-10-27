from __future__ import annotations

import hashlib
import json
import struct
import uuid
import wave
from pathlib import Path

import pytest

from utils.local_audio_delivery import DeliveryConflict, MAGIC, accept_part, parse_part
from utils.opus_preroll import decode_fresh_audio, validate_preroll


def part(
    *frames: bytes, source: str = 'phone', codec: str = 'pcm16', preroll_frames=None, index: int = 0, version=None
) -> bytes:
    fields = {
        'version': version if version is not None else 2 if preroll_frames is not None else 1,
        'id': str(uuid.uuid4()),
        'recording_id': str(uuid.uuid4()),
        'index': index,
        'source': source,
        'codec': codec,
        'server_id': 'a' * 64,
        'started_at': '2026-10-27T08:00:00Z',
    }
    if preroll_frames is not None:
        fields['preroll_frames'] = preroll_frames
    header = json.dumps(fields).encode()
    return (
        MAGIC
        + struct.pack('>I', len(header))
        + header
        + b''.join(struct.pack('>I', len(frame)) + frame for frame in frames)
    )


def test_lost_ack_retry_after_restart_publishes_one_identical_wav(tmp_path: Path):
    body = part(b'\x01\x00' * 160, b'\xff\xff' * 160)
    first = accept_part(body, tmp_path)
    # A fresh invocation has no process-local deduplication state.
    second = accept_part(body, tmp_path)
    assert (
        first == second == {'id': parse_part(body)[0]['id'], 'sha256': hashlib.sha256(body).hexdigest(), 'stored': True}
    )
    captures = list((tmp_path / 'listen-captures').iterdir())
    assert len(captures) == 1
    with wave.open(str(captures[0] / 'audio.wav')) as audio:
        assert audio.readframes(320) == b'\x01\x00' * 160 + b'\xff\xff' * 160
    metadata = json.loads((captures[0] / 'metadata.json').read_text())
    assert metadata['frames_received'] == 2
    assert metadata['started_at'] == '2026-10-27T08:00:00Z'
    assert metadata['delivery']['recording_id'] == parse_part(body)[0]['recording_id']
    assert list((tmp_path / 'local-audio-staging').iterdir()) == []


def test_same_id_with_changed_bytes_is_rejected_without_overwriting_audio(tmp_path: Path):
    body = part(b'\x01\x00' * 160)
    accept_part(body, tmp_path)
    before = {path: path.read_bytes() for path in (tmp_path / 'listen-captures').glob('*/*')}
    with pytest.raises(DeliveryConflict):
        accept_part(body[:-1] + b'\x01', tmp_path)
    assert all(path.read_bytes() == content for path, content in before.items())


@pytest.mark.parametrize('mutate', [lambda b: b[:-1], lambda b: b[:15], lambda b: b'wrong' + b[5:]])
def test_incomplete_and_corrupt_parts_never_publish_or_ack(tmp_path: Path, mutate):
    with pytest.raises(ValueError):
        accept_part(mutate(part(b'\x00\x00' * 160)), tmp_path)
    assert not (tmp_path / 'listen-captures').exists()


def test_opus_packets_decode_in_original_order_with_one_decoder(tmp_path: Path):
    observed = []

    class Decoder:
        def decode(self, frame, *, frame_size):
            observed.append(frame)
            assert frame_size == 1920
            return frame * 2

    body = part(b'\x01', b'\x02', source='omi', codec='opus_fs320')
    receipt = accept_part(body, tmp_path, decoder_factory=Decoder)
    assert observed == [b'\x01', b'\x02']
    with wave.open(str(tmp_path / 'listen-captures' / receipt['id'] / 'audio.wav')) as audio:
        assert audio.readframes(2) == b'\x01\x01\x02\x02'


def test_disk_sync_failure_never_returns_a_receipt(tmp_path: Path, monkeypatch):
    import utils.local_audio_delivery as delivery

    monkeypatch.setattr(delivery.os, 'fsync', lambda _: (_ for _ in ()).throw(OSError('synthetic disk failure')))
    with pytest.raises(OSError):
        accept_part(part(b'\x00\x00' * 160), tmp_path)
    assert list((tmp_path / 'listen-captures').iterdir()) == []


def test_preroll_helper_decodes_context_but_returns_only_fresh_stateful_samples():
    from collections import deque

    history = deque(maxlen=4)

    def decode(frame):
        history.append(frame[1])
        return struct.pack('<h', sum(history)) * 320

    frames = [bytes([0x78, value]) for value in range(1, 7)]
    count = validate_preroll({'version': 2, 'codec': 'opus_fs320', 'index': 1, 'preroll_frames': 4}, frames)
    assert (
        decode_fresh_audio(frames, count, decode, limit=32000)
        == struct.pack('<h', 14) * 320 + struct.pack('<h', 18) * 320
    )


def test_preroll_helper_rejects_prefix_only_pcm_and_excess_context():
    for header, frames in [
        ({'version': 2, 'codec': 'pcm16', 'index': 1, 'preroll_frames': 4}, [b'\x78\x00'] * 5),
        ({'version': 2, 'codec': 'opus', 'index': 1, 'preroll_frames': 4}, [b'\x78\x00'] * 4),
        ({'version': 2, 'codec': 'opus', 'index': 1, 'preroll_frames': 5}, [b'\x78\x00'] * 6),
        ({'version': 1, 'codec': 'opus', 'index': 1, 'preroll_frames': 4}, [b'\x78\x00'] * 5),
        ({'version': 2, 'codec': 'opus', 'index': 1}, [b'\x78\x00'] * 5),
    ]:
        with pytest.raises(ValueError):
            validate_preroll(header, frames)


def test_opus_preroll_warms_one_decoder_but_does_not_duplicate_audio_duration_or_counters(tmp_path: Path):
    from collections import deque

    decoded_order = []

    class StatefulDecoder:
        def __init__(self):
            self.history = deque(maxlen=4)

        def decode(self, frame, *, frame_size):
            assert frame_size == 1920
            self.history.append(frame[1])
            decoded_order.append(frame[1])
            return struct.pack('<h', sum(self.history)) * 320  # One 20 ms packet.

    frames = [bytes([0x78, value]) for value in range(1, 7)]
    body = part(*frames, source='omi', codec='opus_fs320', preroll_frames=4, index=1)
    saved = accept_part(body, tmp_path, decoder_factory=StatefulDecoder)
    assert decoded_order == [1, 2, 3, 4, 5, 6]
    with wave.open(str(tmp_path / 'listen-captures' / saved['id'] / 'audio.wav')) as audio:
        assert audio.getnframes() == 640
        assert audio.readframes(640) == struct.pack('<h', 14) * 320 + struct.pack('<h', 18) * 320
    metadata = json.loads((tmp_path / 'listen-captures' / saved['id'] / 'metadata.json').read_text())
    assert metadata['duration_seconds'] == 0.04
    assert metadata['frames_received'] == 2
    assert metadata['encoded_bytes'] == 4
    assert metadata['delivery']['preroll_frames'] == 4
    assert accept_part(body, tmp_path, decoder_factory=lambda: pytest.fail('Retry must not decode again')) == saved


@pytest.mark.parametrize(
    'count,index,toc,packets,codec',
    [
        (-1, 1, 0x78, 6, 'opus'),
        (True, 1, 0x78, 6, 'opus'),
        ('4', 1, 0x78, 6, 'opus'),
        (33, 1, 0x80, 34, 'opus'),
        (4, 0, 0x78, 6, 'opus'),
        (4, 1, 0x78, 4, 'opus'),  # Prefix with no new audio.
        (4, 1, 0x80, 6, 'opus'),  # Only 10 ms: insufficient decoder warm-up.
        (2, 1, 0x1B, 3, 'opus'),  # Zero/invalid Opus packet frame count.
        (3, 1, 0x18, 4, 'opus'),  # 180 ms could use the last 120 ms; not a minimal suffix.
        (5, 1, 0x78, 6, 'opus'),  # 100 ms could use the last 80 ms.
        (4, 1, 0x78, 6, 'pcm16'),
    ],
)
def test_invalid_preroll_is_rejected_before_any_capture_is_created(tmp_path, count, index, toc, packets, codec):
    source = 'phone' if codec == 'pcm16' else 'omi'
    frames = [bytes([toc, 0]) for _ in range(packets)]
    with pytest.raises(ValueError):
        accept_part(part(*frames, source=source, codec=codec, preroll_frames=count, index=index), tmp_path)
    assert not (tmp_path / 'listen-captures').exists()


def test_legacy_part_without_preroll_field_keeps_all_audio(tmp_path):
    body = part(b'\x01\x00' * 320)
    assert 'preroll_frames' not in parse_part(body)[0]
    saved = accept_part(body, tmp_path)
    metadata = json.loads((tmp_path / 'listen-captures' / saved['id'] / 'metadata.json').read_text())
    assert metadata['duration_seconds'] == 0.02
    assert metadata['delivery']['preroll_frames'] == 0


def test_v1_cannot_silently_opt_into_v2_preroll(tmp_path):
    with pytest.raises(ValueError):
        accept_part(
            part(*([b'\x78\x01'] * 5), source='omi', codec='opus', preroll_frames=4, index=1, version=1), tmp_path
        )
    assert not (tmp_path / 'listen-captures').exists()
