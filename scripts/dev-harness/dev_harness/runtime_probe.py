"""Explicit synthetic STT probes; never import a fixture into the audio library."""

import fcntl
import json
import os
from pathlib import Path
import tempfile
import urllib.request

from . import local_stt, local_openai_stt


def require_segments(raw, duration):
    result = local_openai_stt.normalize(raw, duration)
    if not result['segments']:
        raise ValueError('Synthetic inference returned no timed speech segments.')
    print('Synthetic transcription passed: timed speech segments returned; no recording imported.')


def native_inference(cfg):
    audio = cfg.repo_root / 'scripts/dev-harness/fixtures/speech-check.wav'
    manifest = local_stt.inspect_audio(audio)
    engine = local_stt.EngineConfig.load(cfg)
    # Share the existing worker lock; do not compete with a recording's final STT.
    root = cfg.layout.services_dir / 'local-transcripts'
    if root.is_symlink():
        raise ValueError('Unsafe transcription lock directory.')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(root / '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Transcription is busy. Retry doctor --inference after processing finishes.') from None
        adapter = {'whisperkit': local_stt.run_whisperkit, 'whisperx': local_stt.run_whisperx,
                   'parakeet-mlx': local_stt.run_parakeet, 'openai-compatible': local_stt.run_openai}[engine.engine]
        with tempfile.TemporaryDirectory(prefix='omiloc-probe-') as folder:
            raw = adapter(engine, audio, Path(folder), manifest)
        require_segments(raw, manifest['duration_seconds'])


def docker_inference(root, expected_device):
    # The model loads before /health becomes available; inference still needs a real request.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open('http://127.0.0.1:10301/health', timeout=5) as response:
        health = json.load(response)
    if expected_device not in {'cpu', 'cuda'} or health.get('device') != expected_device:
        raise ValueError('The running STT device differs from .env.docker; stop and bootstrap the selected mode.')
    audio = root / 'scripts/dev-harness/fixtures/speech-check.wav'
    manifest = local_stt.inspect_audio(audio)
    raw = local_openai_stt.transcribe('http://127.0.0.1:10301/v1', os.environ['STT_MODEL'],
                                      'auto', audio, manifest['duration_seconds'])
    require_segments(raw, manifest['duration_seconds'])
    print('Verified STT device: ' + expected_device)
