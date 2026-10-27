"""Paired phone controls for the harness-owned processing registry.

The backend only contacts one configured loopback library. Provider protocols,
credentials and process lifecycle remain owned by the harness.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from utils.env_loader import is_offline_runtime
from utils.local_transport_auth import LocalTransportAuthError, local_tunnel_enabled, verify_local_authorization
from utils.offline_network_policy import OfflineEgressBlocked

router = APIRouter()
REQUEST_LIMIT = 64 * 1024
RESPONSE_LIMIT = 2 * 1024 * 1024


def require_processing(request: Request) -> None:
    if not is_offline_runtime() or not local_tunnel_enabled():
        raise HTTPException(status_code=404, detail='Local processing is unavailable')
    try:
        verify_local_authorization(request.scope['headers'])
    except LocalTransportAuthError:
        raise HTTPException(status_code=401, detail='local_auth_required') from None


def library_origin() -> str:
    value = os.environ.get('OMI_LOCAL_PROCESSING_URL', '')
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port
                or value != f'http://127.0.0.1:{parsed.port}'):
            raise ValueError()
    except ValueError:
        raise HTTPException(status_code=503, detail='Local processing is not configured') from None
    return value


def error(message: str, status: int, *, outcome: str | None = None) -> JSONResponse:
    return JSONResponse({'error': message, **({'outcome': outcome} if outcome else {})},
                        status_code=status, headers={'Cache-Control': 'no-store'})


async def forward(request: Request, suffix: str = '') -> JSONResponse:
    origin = library_origin()
    body = None
    if request.method != 'GET':
        if (request.headers.get('content-type', '').split(';')[0].strip() != 'application/json'
                or request.headers.get('transfer-encoding')):
            return error('Ожидается запрос JSON.', 400)
        data = bytearray()
        try:
            if int(request.headers.get('content-length', '0')) > REQUEST_LIMIT:
                return error('Запрос настроек слишком большой.', 413)
            async with asyncio.timeout(15):
                async for chunk in request.stream():
                    data.extend(chunk)
                    if len(data) > REQUEST_LIMIT:
                        return error('Запрос настроек слишком большой.', 413)
        except (TimeoutError, ValueError):
            return error('Не удалось прочитать запрос настроек.', 400)
        try:
            body = json.loads(data)
            if not isinstance(body, dict):
                raise ValueError()
        except (ValueError, UnicodeError):
            return error('Некорректный запрос настроек.', 400)
    activating = suffix == '/activate'
    timeout = 700 if activating else 45
    try:
        async with asyncio.timeout(timeout):
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=timeout) as client:
                async with client.stream(request.method, origin + '/api/processing' + suffix,
                                         headers={'Origin': origin, 'X-Omiloc-Request': 'processing'},
                                         **({'json': body} if body is not None else {})) as response:
                    if response.status_code not in {200, 400, 403, 404, 409, 413, 503}:
                        raise ValueError()
                    data = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        data.extend(chunk)
                        if len(data) > RESPONSE_LIMIT:
                            raise ValueError()
                    result = json.loads(data)
                    if not isinstance(result, dict):
                        raise ValueError()
                    return JSONResponse(result, status_code=response.status_code, headers={'Cache-Control': 'no-store'})
    except (TimeoutError, httpx.HTTPError, OSError, ValueError, OfflineEgressBlocked):
        # A connection can end after activation has published. Never retry writes.
        if activating:
            return error('Результат переключения не подтверждён. Обновите настройки и проверьте активный профиль.',
                         503, outcome='indeterminate')
        return error('Сервер настроек недоступен. Проверьте подключение и обновите настройки.', 503)


@router.get('/v1/local/processing', dependencies=[Depends(require_processing)])
async def read_processing(request: Request) -> JSONResponse:
    return await forward(request)


@router.post('/v1/local/processing/save', dependencies=[Depends(require_processing)])
@router.post('/v1/local/processing/probe', dependencies=[Depends(require_processing)])
@router.post('/v1/local/processing/models', dependencies=[Depends(require_processing)])
@router.post('/v1/local/processing/activate', dependencies=[Depends(require_processing)])
async def change_processing(request: Request) -> JSONResponse:
    return await forward(request, '/' + request.url.path.rsplit('/', 1)[-1])


@router.delete('/v1/local/processing/profiles/{profile_id}', dependencies=[Depends(require_processing)])
async def delete_processing(profile_id: str, request: Request) -> JSONResponse:
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', profile_id):
        return error('Профиль не найден.', 404)
    return await forward(request, '/profiles/' + profile_id)
