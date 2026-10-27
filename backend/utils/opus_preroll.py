"""Bounded Opus seek context, decoded and discarded per RFC 7845 section 4.6."""

from __future__ import annotations

from collections.abc import Callable

MAX_PREROLL_FRAMES = 32


def packet_microseconds(packet: bytes) -> int:
    """Read the RFC 6716 TOC without importing codec or ML dependencies."""
    config = packet[0] >> 3
    duration = (
        (10000, 20000, 40000, 60000)[config % 4]
        if config < 12
        else (10000, 20000)[config % 2] if config < 16 else (2500, 5000, 10000, 20000)[config % 4]
    )
    code = packet[0] & 3
    if code == 3 and len(packet) < 2:
        raise ValueError('Invalid Opus frame count')
    count = 1 if code == 0 else packet[1] & 63 if code == 3 else 2
    if not count or count * duration > 120000:
        raise ValueError('Invalid Opus packet duration')
    return duration * count


def validate_preroll(header: dict, frames: list[bytes]) -> int:
    version = header['version']
    if version not in {1, 2} or (version == 2 and 'preroll_frames' not in header):
        raise ValueError('Unsupported audio pre-roll protocol version')
    count = header.get('preroll_frames', 0)
    if type(count) is not int or not 0 <= count <= MAX_PREROLL_FRAMES or len(frames) <= count:
        raise ValueError('Invalid audio pre-roll count or empty fresh audio')
    if count:
        if version != 2:
            raise ValueError('Audio pre-roll requires delivery protocol version 2')
        if header['codec'] == 'pcm16' or header['index'] == 0:
            raise ValueError('Audio pre-roll requires a later Opus part')
        durations = [packet_microseconds(frame) for frame in frames[:count]]
        total = sum(durations)
        if not 80000 <= total < 200000 or total - durations[0] >= 80000:
            raise ValueError('Audio pre-roll must be the bounded 80 ms preceding suffix')
    return count


def decode_fresh_audio(frames: list[bytes], preroll: int, decode: Callable[[bytes], bytes], *, limit: int) -> bytes:
    pcm = bytearray()
    for index, frame in enumerate(frames):
        decoded = decode(frame)
        if not decoded or len(decoded) % 2:
            raise ValueError('Invalid decoded PCM16 audio')
        if index < preroll:
            continue
        pcm.extend(decoded)
        if len(pcm) > limit:
            raise ValueError('Audio part exceeds its duration limit')
    return bytes(pcm)
