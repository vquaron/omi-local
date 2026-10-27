"""Explicit provider destinations; never inherit proxy credentials or redirect audio."""
from __future__ import annotations

import ipaddress
import math
import time
from urllib.parse import urlsplit

import httpx


PRIVATE_NETWORKS = tuple(ipaddress.ip_network(value) for value in (
    '127.0.0.0/8', '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16',
    '100.64.0.0/10', '::1/128', 'fc00::/7',
))


def private_address(value):
    address = ipaddress.ip_address(value)
    return any(address.version == network.version and address in network for network in PRIVATE_NETWORKS)


def validate_url(value, *, websocket=False):
    """Allow TLS publicly and plain transport only to explicit local/VPN addresses."""
    message = 'Укажите HTTPS/WSS адрес сервера; HTTP/WS доступны только для Mac и LAN/VPN.'
    try:
        if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 33 for c in value):
            raise ValueError()
        parsed = urlsplit(value)
        schemes = {'ws', 'wss'} if websocket else {'http', 'https'}
        if (parsed.scheme not in schemes or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment or '\\' in value
                or parsed.port == 0):
            raise ValueError()
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            address = None
        if address is not None and (address.is_link_local or address.is_multicast or address.is_unspecified):
            raise ValueError()
        if parsed.scheme in {'http', 'ws'}:
            if address is not None:
                if not private_address(str(address)):
                    raise ValueError()
            elif parsed.hostname != 'localhost':
                # A second DNS lookup by the client must not change this decision.
                # Use a private IP for plain LAN/VPN transport; names require TLS.
                raise ValueError()
        return value.rstrip('/')
    except (ValueError, TypeError, OSError):
        raise ValueError(message) from None


def auth_headers(key):
    if not isinstance(key, str) or len(key) > 8192 or any(ord(c) < 32 or ord(c) > 126 for c in key):
        raise ValueError('Недопустимый API-ключ.')
    return {'Authorization': 'Bearer ' + key} if key else {}


def client(*, key='', timeout=10):
    return httpx.Client(trust_env=False, follow_redirects=False, timeout=timeout, headers=auth_headers(key))


def metadata_get(client, url, *, limit=2 * 1024 * 1024, deadline_seconds=15):
    chunks, size = [], 0
    deadline = time.monotonic() + deadline_seconds
    with client.stream('GET', url) as response:
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > limit or time.monotonic() > deadline:
                raise ValueError('Ответ сервера слишком большой.')
            chunks.append(chunk)
        return httpx.Response(response.status_code, content=b''.join(chunks))


def post_json(client, url, *, limit=2 * 1024 * 1024, deadline_seconds=180, **kwargs):
    """Read a bounded provider response before parsing it or retaining its content.

    HTTPX's timeout is per I/O operation. Check elapsed time between chunks as
    well, so a provider cannot keep a job alive by indefinitely trickling bytes.
    Request identity encoding: decoding a compressed chunk before checking its
    size could allocate an arbitrarily large intermediate buffer.
    """
    if type(limit) is not int or limit <= 0 or not math.isfinite(deadline_seconds) or deadline_seconds <= 0:
        raise ValueError('Недопустимые ограничения ответа сервера.')
    deadline = time.monotonic() + deadline_seconds
    timeout = {name: min(value, deadline_seconds) if value is not None else deadline_seconds
               for name, value in client.timeout.as_dict().items()}
    try:
        with client.stream('POST', url, headers={'Accept-Encoding': 'identity'},
                           timeout=httpx.Timeout(**timeout), follow_redirects=False, **kwargs) as response:
            if not 200 <= response.status_code < 300:
                raise ValueError('Сервер отклонил запрос. Проверьте адрес, ключ и модель.')
            if response.headers.get('Content-Encoding', 'identity').lower().strip() not in {'', 'identity'}:
                raise ValueError('Сервер вернул неподдерживаемый ответ.')
            body = bytearray()
            for chunk in response.iter_bytes():
                if time.monotonic() > deadline:
                    raise ValueError('Превышено время ожидания ответа сервера.')
                if len(chunk) > limit - len(body):
                    raise ValueError('Ответ сервера слишком большой.')
                body.extend(chunk)
            if time.monotonic() > deadline:
                raise ValueError('Превышено время ожидания ответа сервера.')
            # Deliberately discard upstream headers, request URL and credentials.
            return json_response(httpx.Response(response.status_code, content=bytes(body)), limit=limit)
    except (httpx.HTTPError, OSError):
        raise ValueError('Не удалось получить ответ сервера.') from None


def json_response(response, *, limit=2 * 1024 * 1024):
    if not 200 <= response.status_code < 300:
        raise ValueError('Сервер отклонил запрос. Проверьте адрес, ключ и модель.')
    if len(response.content) > limit:
        raise ValueError('Ответ сервера слишком большой.')
    try:
        value = response.json()
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, TypeError):
        raise ValueError('Сервер вернул неподдерживаемый ответ.') from None


def model_ids(data):
    rows = data.get('data')
    if not isinstance(rows, list):
        raise ValueError('Сервер не предоставил список моделей.')
    return sorted({row['id'] for row in rows[:2000] if isinstance(row, dict)
                   and isinstance(row.get('id'), str) and 0 < len(row['id']) <= 256
                   and row['id'].strip() and row['id'].isprintable()})
