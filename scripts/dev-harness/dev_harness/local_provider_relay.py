"""Loopback WLK transport boundary; selected credentials never enter the offline backend."""
import asyncio
from contextlib import contextmanager, suppress
import copy
import fcntl
import json
import os

import httpx
from pathlib import Path
from urllib.parse import urlsplit

from .local_provider_http import auth_headers, validate_url


MAX_MESSAGE_BYTES = 16 * 1024 * 1024
DRAIN_SECONDS = 60


class RelayError(ValueError):
    """Content-free provider errors."""


def port(cfg):
    value = cfg.backend_port + 4
    if not 1024 <= value <= 65535:
        raise RelayError('Live relay port is outside the supported range')
    return value


def url(cfg):
    return f'ws://127.0.0.1:{port(cfg)}/asr'


@contextmanager
def session_gate(cfg, *, exclusive=False):
    """Pin complete sessions, including final upstream drain, across process boundaries."""
    path = cfg.layout.state_root / 'live-provider-session.lock'
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RelayError('Stop recording and wait for live STT to finish before changing providers') from None
        yield
    finally:
        os.close(descriptor)


def upstream(snapshot, cfg, *, raw=False):
    settings = snapshot['settings']
    endpoint = validate_url(settings['url'], websocket=True)
    if urlsplit(endpoint).hostname in {'127.0.0.1', 'localhost', '::1'} and urlsplit(endpoint).port == port(cfg):
        raise RelayError('Live provider cannot point to its relay')
    diarization = settings.get('diarization') or {}
    if not raw and diarization.get('enabled'):
        return f"ws://127.0.0.1:{diarization['port']}/asr", False
    return endpoint, True


def connect(url, *, key=''):
    # The legacy connector has no environment-proxy support. Redirects must
    # never move audio or Authorization to an endpoint the operator did not choose.
    from websockets.legacy.client import Connect

    class ExplicitEndpointConnect(Connect):
        def handle_redirect(self, uri):
            raise RelayError('Live provider redirects are unsupported')

    return ExplicitEndpointConnect(url, extra_headers=auth_headers(key), open_timeout=5,
                                   close_timeout=2, max_size=MAX_MESSAGE_BYTES, max_queue=8)


def validate_config(raw):
    try:
        value = json.loads(raw)
        if (not isinstance(value, dict) or value.get('type') != 'config'
                or value.get('useAudioWorklet') is not True or value.get('enabled') is False):
            raise ValueError()
        return value
    except (ValueError, TypeError):
        raise RelayError('Server does not implement the WLK PCM16 preview protocol') from None


async def probe(snapshot, cfg, *, key='', connector=connect):
    endpoint, _ = upstream(snapshot, cfg, raw=True)
    try:
        async with connector(endpoint, key=key) as socket:
            validate_config(await asyncio.wait_for(socket.recv(), 5))
            # Complete a zero-audio session; readiness must not leave a worker busy.
            await socket.send(b'')
            async with asyncio.timeout(DRAIN_SECONDS):
                async for raw in socket:
                    message = json.loads(raw)
                    if message.get('type') == 'ready_to_stop':
                        return {'ready': True, 'protocol': 'wlk-pcm16'}
                    if message.get('type') == 'error' or message.get('status') == 'error':
                        break
            raise RelayError('Live provider did not acknowledge end of input')
    except Exception:
        raise RelayError('Live provider is unavailable or its streaming protocol is unsupported') from None


async def relay_session(downstream, snapshot, cfg, *, key='', raw_upstream=False, connector=connect):
    """Forward authoritative snapshots unchanged and retain upstream until explicit EOF."""
    endpoint, authenticated = upstream(snapshot, cfg, raw=raw_upstream)
    sent_eof = asyncio.Event()
    tasks = []
    disconnected = False
    eof_ack = False

    async def send(raw):
        nonlocal disconnected
        if not disconnected:
            try:
                await downstream.send_text(raw)
            except Exception:
                disconnected = True

    try:
        async with connector(endpoint, key=key if authenticated else '') as provider:
            config = validate_config(await asyncio.wait_for(provider.recv(), 5))
            await send(json.dumps(config))

            async def feed():
                nonlocal disconnected
                try:
                    while True:
                        event = await asyncio.wait_for(downstream.receive(), 60)
                        if event['type'] == 'websocket.disconnect':
                            disconnected = True
                            break
                        pcm = event.get('bytes')
                        if not isinstance(pcm, bytes) or len(pcm) % 2 or len(pcm) > MAX_MESSAGE_BYTES:
                            raise RelayError('Live input must be bounded PCM16 binary frames')
                        if not pcm:
                            break
                        await provider.send(pcm)
                finally:
                    sent_eof.set()
                    await provider.send(b'')

            async def receive():
                nonlocal eof_ack
                async for message in provider:
                    decoded = json.loads(message)
                    if not isinstance(decoded, dict):
                        raise RelayError('Invalid live provider message')
                    if decoded.get('type') == 'ready_to_stop':
                        if not sent_eof.is_set():
                            raise RelayError('Live provider ended before input completed')
                        eof_ack = True
                        await send(message)
                        return
                    if decoded.get('type') == 'error' or decoded.get('status') == 'error':
                        raise RelayError('Live provider stream failed')
                    # Corrections, retractions, pending buffers, diarization state
                    # and final snapshots all keep their original WLK shape.
                    await send(message)
                raise RelayError('Live provider closed without end acknowledgement')

            feeder = asyncio.create_task(feed(), name='provider-relay-feed')
            receiver = asyncio.create_task(receive(), name='provider-relay-receive')
            tasks = [feeder, receiver]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            await asyncio.wait_for(asyncio.shield(receiver), DRAIN_SECONDS)
            if not eof_ack:
                raise RelayError('Live provider final drain failed')
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=2)
        for task in tasks:
            if task.done():
                with suppress(Exception, asyncio.CancelledError):
                    task.result()


async def diarization_health(snapshot, cfg, *, key=''):
    endpoint, use_key = upstream(snapshot, cfg)
    parsed = urlsplit(endpoint)
    target = parsed._replace(scheme='https' if parsed.scheme == 'wss' else 'http', path='/health').geturl()
    try:
        async with asyncio.timeout(0.6):
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=0.5,
                                         headers=auth_headers(key if use_key else '')) as client:
                async with client.stream('GET', target) as response:
                    if response.status_code != 200:
                        return {'state': 'unavailable'}
                    body = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=4097):
                        body.extend(chunk)
                        if len(body) > 4096:
                            return {'state': 'unavailable'}
                    data = json.loads(body)
        if not isinstance(data, dict) or data.get('ready') is not True:
            return {'state': 'unavailable'}
        if data.get('diarization') is False:
            return {'state': 'disabled'}
        if (data.get('diarization') is not True or type(data.get('active')) is not bool
                or type(data.get('busy', False)) is not bool):
            return {'state': 'unknown'}
        return {'state': 'busy' if data['active'] or data.get('busy', False) else 'ready'}
    except (TimeoutError, httpx.HTTPError, OSError, ValueError):
        return {'state': 'unavailable'}


def create_app(cfg, *, registry=None, connector=connect, health_probe=diarization_health):
    from fastapi import FastAPI, WebSocket
    from fastapi.responses import JSONResponse
    if registry is None:
        from .local_providers import Registry
        registry = Registry(cfg)
    app = FastAPI()
    active = 0

    @app.get('/health')
    async def health():
        try:
            snapshot = await asyncio.to_thread(registry.snapshot, 'live')
            diarization = {'state': 'disabled'}
            if snapshot is not None:
                key = await asyncio.to_thread(registry.key, snapshot)
                diarization = await health_probe(snapshot, cfg, key=key)
            return {'ready': True, 'active': bool(active), 'active_sessions': active,
                    'enabled': snapshot is not None, 'relay': True, 'diarization': diarization}
        except Exception:
            return JSONResponse({'ready': False, 'active': bool(active), 'relay': True}, status_code=503)

    async def serve(socket, *, raw=False):
        nonlocal active
        if (socket.client is None or socket.client.host not in {'127.0.0.1', '::1'}
                or 'origin' in socket.headers or any(name == 'forwarded' or name.startswith('x-forwarded-')
                                                    for name in socket.headers)):
            await socket.close(code=1008)
            return
        await socket.accept()
        try:
            with session_gate(cfg):
                active += 1
                try:
                    snapshot = copy.deepcopy(await asyncio.to_thread(registry.snapshot, 'live'))
                    if snapshot is None:
                        await socket.send_json({'type': 'config', 'useAudioWorklet': True,
                                                'enabled': False, 'diarization': False})
                        await socket.close(code=1000)
                        return
                    key = await asyncio.to_thread(registry.key, snapshot)
                    await relay_session(socket, snapshot, cfg, key=key, raw_upstream=raw, connector=connector)
                finally:
                    active -= 1
        except Exception:
            with suppress(Exception):
                await socket.send_json({'type': 'error', 'code': 'live_provider_failed'})
        finally:
            with suppress(Exception):
                await socket.close()

    @app.websocket('/asr')
    async def asr(socket: WebSocket):
        await serve(socket)

    @app.websocket('/upstream')
    async def raw_asr(socket: WebSocket):
        await serve(socket, raw=True)

    return app


def main():
    import uvicorn
    from . import config, safety
    os.umask(0o077)
    cfg = config.load_config(Path.cwd(), create_layout=False)
    safety.read_and_validate_sentinel(cfg.layout.state_root, repo_root=cfg.repo_root, instance=cfg.instance)
    uvicorn.run(create_app(cfg), host='127.0.0.1', port=port(cfg), access_log=False, log_level='warning')


if __name__ == '__main__':
    main()
