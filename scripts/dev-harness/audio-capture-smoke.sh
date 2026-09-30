#!/usr/bin/env bash
set -euo pipefail

# Synthetic CV1 Opus or phone PCM16 -> real /v4/listen -> WAV smoke test.
# shellcheck source=_source_local_dev_env.sh
source "$(dirname "$0")/_source_local_dev_env.sh"
cd "$(dirname "$0")/../.."

if command -v brew >/dev/null 2>&1; then
  opus_prefix="$(brew --prefix opus 2>/dev/null || true)"
  if [ -f "$opus_prefix/lib/libopus.dylib" ]; then
    export DYLD_LIBRARY_PATH="$opus_prefix/lib${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}"
  fi
fi

PYTHON_BIN="${PYTHON:-backend/venv/bin/python}"
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="python3"
fi

PYTHONPATH="scripts/dev-harness${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" - <<'PY'
from __future__ import annotations

import asyncio
import json
import math
import os
import struct
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path
from urllib.parse import urlencode, urlparse

import opuslib
import websockets

from dev_harness import config, safety
from dev_harness.memory_scenarios import ALICE_USER_ID, USERS


def fail(message: str) -> None:
    raise SystemExit(message)


repo = Path.cwd()
cfg = config.load_config(repo, create_layout=False)
if cfg.provider_mode != 'offline' or os.environ.get('OMI_ENV_STAGE') != 'offline':
    fail('Audio capture smoke is available only with PROVIDER_MODE=offline')
if not cfg.layout.sentinel_path.is_file():
    fail('Local harness sentinel is missing; run PROVIDER_MODE=offline make dev-up first')
safety.read_and_validate_sentinel(cfg.layout.state_root, repo_root=repo, instance=cfg.instance)
if not safety.is_loopback_host(urlparse(cfg.backend_url).netloc):
    fail('Audio capture smoke refuses a non-loopback backend URL')

storage_root = Path(config.child_env_for(cfg)['OMI_LOCAL_STORAGE_ROOT'])
capture_root = storage_root / 'listen-captures'
before = {path.name for path in capture_root.iterdir()} if capture_root.is_dir() else set()

base_url = os.environ.get('OMI_AUDIO_BASE_URL', cfg.backend_url).rstrip('/')
if cfg.local_transport in {'ngrok', 'tailscale'}:
    from dev_harness.local_mac import endpoint, pairing_data
    if base_url != cfg.backend_url:
        if cfg.local_transport == 'tailscale':
            from dev_harness.local_transport import normalize_tailscale_url
            normalize_tailscale_url(base_url)
        else:
            endpoint(base_url)
    key_fd = int(os.environ['OMI_AUDIO_ACCESS_KEY_FD'])
    with os.fdopen(key_fd) as stream:
        token = stream.read(128).strip()
    pairing_data(token)
else:
    alice = next(user for user in USERS if user.uid == ALICE_USER_ID)
    auth_request = urllib.request.Request(
        f'http://{cfg.auth_host}/identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key=local-dev-harness',
        data=json.dumps(
            {'email': alice.email, 'password': alice.password, 'returnSecureToken': True}
        ).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(auth_request, timeout=10) as response:
            auth_body = json.loads(response.read().decode('utf-8'))
    except (OSError, urllib.error.HTTPError, json.JSONDecodeError) as error:
        fail(f'Firebase Auth emulator sign-in failed ({type(error).__name__})')
    token = auth_body.get('idToken') if isinstance(auth_body, dict) else None
    if not isinstance(token, str) or not token:
        fail('Firebase Auth emulator returned no ID token')

sample_rate = 16000
frame_samples = 320
frame_count = 100
amplitude = 6000
audio_source = os.environ.get('OMI_AUDIO_SOURCE', 'omi')
if audio_source not in {'omi', 'phone'}:
    fail('Unsupported synthetic audio source')
input_codec = 'pcm16' if audio_source == 'phone' else 'opus_fs320'
encoder = opuslib.Encoder(sample_rate, 1, opuslib.APPLICATION_AUDIO)
packets: list[bytes] = []
for frame_index in range(frame_count):
    samples = [
        int(amplitude * math.sin(2 * math.pi * 440 * ((frame_index * frame_samples + index) / sample_rate)))
        for index in range(frame_samples)
    ]
    pcm = struct.pack(f'<{frame_samples}h', *samples)
    packets.append(pcm if audio_source == 'phone' else encoder.encode(pcm, frame_samples))


async def send_audio() -> None:
    query = urlencode(
        {
            'language': 'en',
            'sample_rate': sample_rate,
            'codec': input_codec,
            'channels': 1,
            'include_speech_profile': 'false',
            'source': audio_source,
        }
    )
    socket_base = base_url.replace('https://', 'wss://', 1).replace('http://', 'ws://', 1)
    url = f'{socket_base}/v4/listen?{query}'
    async with websockets.connect(
        url,
        extra_headers={'Authorization': f'Bearer {token}', 'X-App-Platform': 'ios'},
        open_timeout=10,
        close_timeout=10,
    ) as websocket:
        while True:
            raw = await asyncio.wait_for(websocket.recv(), timeout=15)
            if isinstance(raw, str):
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict) and event.get('status') == 'ready':
                    if event.get('provider') != 'offline_capture':
                        fail('Listen route did not enter offline_capture mode')
                    break
        for packet in packets:
            await websocket.send(packet)
        await websocket.send(json.dumps({'type': 'finalization_reason', 'reason': 'user_stop'}))


asyncio.run(send_audio())

deadline = time.monotonic() + 15
session_dir = None
while time.monotonic() < deadline:
    candidates = [
        path
        for path in capture_root.iterdir()
        if path.is_dir() and path.name not in before and (path / 'metadata.json').is_file()
    ] if capture_root.is_dir() else []
    matching = []
    for path in candidates:
        try:
            metadata = json.loads((path / 'metadata.json').read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            continue
        if metadata.get('frames_received') == frame_count and metadata.get('encoded_bytes') == sum(map(len, packets)):
            matching.append(path)
    if len(matching) == 1:
        session_dir = matching[0]
        break
    time.sleep(0.1)
if session_dir is None:
    fail('No unique finalized WAV appeared after the /v4/listen smoke session')

metadata = json.loads((session_dir / 'metadata.json').read_text(encoding='utf-8'))
with wave.open(str(session_dir / 'audio.wav'), 'rb') as source:
    actual = (source.getnchannels(), source.getsampwidth(), source.getframerate(), source.getnframes())
expected = (1, 2, sample_rate, frame_count * frame_samples)
if actual != expected:
    fail(f'Unexpected WAV format/count: {actual!r}')
if metadata.get('source') != audio_source or metadata.get('input', {}).get('codec') != input_codec:
    fail('Capture source/codec metadata does not match the stream')
if metadata.get('status') != 'completed' or metadata.get('decode_errors') != 0:
    fail('Capture metadata did not report a clean completed session')
if metadata.get('decoded_pcm_bytes') != frame_count * frame_samples * 2:
    fail('Capture metadata decoded byte count is incorrect')
if abs(float(metadata.get('duration_seconds', 0)) - 2.0) > 0.001:
    fail('Capture metadata duration is incorrect')
if (session_dir / 'audio.pcm.part').exists() or (session_dir / 'metadata.json.part').exists():
    fail('Finalized capture retained partial files')

print(f'PASS: {audio_source}/{input_codec} via real /v4/listen produced a 2.000 s WAV, PCM16 mono 16 kHz, decode_errors=0')
PY
