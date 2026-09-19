"""Owner-scoped diagnostics and loopback-only drafts for the local listen pipeline."""

from __future__ import annotations

import asyncio
import json
import os
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException, Request

from routers.listen import registry
from utils.env_loader import is_offline_runtime
from utils.http_client import get_local_preview_client, get_local_preview_semaphore
from utils.local_live_preview import preview_url
from utils.offline_audio_capture import OUTPUT_CHANNELS, OUTPUT_SAMPLE_RATE, OUTPUT_SAMPLE_WIDTH_BYTES
from utils.offline_network_policy import OfflineEgressBlocked


def require_local_status() -> None:
    if not is_offline_runtime() or os.environ.get('OMI_LOCAL_TRANSPORT') != 'ngrok':
        raise HTTPException(status_code=404, detail='Local status is unavailable')


def require_local_preview(request: Request) -> None:
    """Draft text is readable by the authenticated loopback library only."""
    require_local_status()
    if (request.client is None or request.client.host not in {'127.0.0.1', '::1'}
            or any(name == 'forwarded' or name.startswith('x-forwarded-') for name in request.headers)):
        raise HTTPException(status_code=404, detail='Local preview is unavailable')


async def _worker_status(url: str) -> dict:
    unavailable = {'state': 'unavailable', 'diarization': {'state': 'unavailable', 'labeled_segments': 0}}
    health_url = urlsplit(url)._replace(scheme='http', path='/health').geturl()
    try:
        # Bound semaphore wait, connect, and the entire response, including slow
        # chunked bodies. Never follow a worker redirect or inherit proxy env.
        async with asyncio.timeout(1.0):
            async with get_local_preview_semaphore():
                async with get_local_preview_client().stream(
                    'GET', health_url, follow_redirects=False, timeout=0.8
                ) as response:
                    if response.status_code != 200:
                        return unavailable
                    body = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=4097):
                        body.extend(chunk)
                        if len(body) > 4096:
                            return unavailable
                    health = json.loads(body)
        if not isinstance(health, dict) or health.get('ready') is not True:
            return unavailable
        if health.get('enabled') is False:
            return {'state': 'disabled', 'diarization': {'state': 'disabled', 'labeled_segments': 0}}
        if type(health.get('active')) is not bool or type(health.get('busy', False)) is not bool:
            return unavailable
        busy = health['active'] or health.get('busy', False)
        diarization = health.get('diarization')
        if isinstance(diarization, dict) and health.get('relay') is True:
            state = diarization.get('state')
            if state not in {'disabled', 'ready', 'busy', 'unavailable', 'unknown'}:
                state = 'unknown'
        elif diarization is True:
            state = 'busy' if busy else 'ready'
        elif diarization is False:
            state = 'disabled'
        else:
            state = 'unknown'
        return {'state': 'busy' if busy else 'ready',
                'diarization': {'state': state, 'labeled_segments': 0}}
    except (TimeoutError, httpx.HTTPError, OSError, ValueError, OfflineEgressBlocked):
        return unavailable


async def snapshot(uid: str) -> dict:
    """Read only this owner's active sessions; counters are not flow liveness."""
    sessions = [
        session for session in registry._sessions_for(uid)
        if session.state.active and not session.state.shutdown_event.is_set()
    ]
    sinks = [session.capture_sink for session in sessions if session.capture_sink is not None]
    frames = sum(sink.frames_received for sink in sinks)
    pcm_bytes = sum(sink.decoded_pcm_bytes for sink in sinks)
    capture_state = 'waiting_audio' if sessions else 'idle'
    if frames:
        capture_state = 'received'
    if any(sink.decode_errors for sink in sinks):
        capture_state = 'decode_error'
    previews = [session.local_preview for session in sessions if session.local_preview is not None]
    updates = sum(preview.updates for preview in previews)
    diarization = {'state': 'unknown', 'labeled_segments': 0}
    if previews and all(getattr(preview, 'disabled', False) for preview in previews):
        live_state = 'disabled'
    elif any(preview.failed for preview in previews):
        live_state = 'failed'
    elif updates:
        live_state = 'streaming'
    else:
        try:
            url = preview_url()
        except ValueError:
            live_state = 'unavailable'
        else:
            worker = await _worker_status(url) if url else {
                'state': 'disabled', 'diarization': {'state': 'disabled', 'labeled_segments': 0}}
            live_state, diarization = worker['state'], worker['diarization']
    if previews:
        evidence = [getattr(preview, 'diarization_status', {'state': 'unknown', 'labeled_segments': 0})
                    for preview in previews]
        # A failing or waiting session must not be hidden by another one's labels.
        order = ('failed', 'degraded', 'pending', 'unknown', 'labeled', 'disabled')
        diarization = {'state': next(state for state in order if any(item['state'] == state for item in evidence)),
                       'labeled_segments': sum(item['labeled_segments'] for item in evidence)}
    elif live_state == 'unavailable':
        diarization = {'state': 'unavailable', 'labeled_segments': 0}
    return {
        'backend': 'ready',
        'diarization': diarization,
        'capture': {
            'state': capture_state,
            'audio_seconds': round(pcm_bytes / (OUTPUT_SAMPLE_RATE * OUTPUT_CHANNELS * OUTPUT_SAMPLE_WIDTH_BYTES), 3),
            'frames_received': frames,
        },
        'live_transcript': {'state': live_state, 'updates': updates,
                            **({'configurable': True} if os.getenv('OMI_LOCAL_LIVE_PROVIDER_RELAY') == '1' else {})},
    }


async def preview_snapshot(uid: str) -> dict:
    """RAM-only drafts, separate from the content-free phone status contract."""
    result = await snapshot(uid)
    result['sessions'] = []
    for session in registry._sessions_for(uid):
        if not session.state.active or session.state.shutdown_event.is_set() or session.capture_sink is None:
            continue
        sink, preview = session.capture_sink, session.local_preview
        text = preview.segment.text if preview is not None else ''
        result['sessions'].append({
            'source': sink.source, 'codec': sink.input_codec,
            'audio_seconds': round(sink.decoded_pcm_bytes / 32000, 3),
            'frames_received': sink.frames_received, 'decode_errors': sink.decode_errors,
            # This ephemeral preview token already identifies the draft on iPhone;
            # it is not the persisted recording or owner's identifier.
            'preview_id': preview.segment.id if preview is not None else None,
            'text': text[-100000:], 'text_truncated': len(text) > 100000,
            'segments': preview.segment.segments if preview is not None and len(text) <= 100000 else [],
            'revision': preview.segment.revision if preview is not None else 0,
            'updates': preview.updates if preview is not None else 0,
        })
    return result
