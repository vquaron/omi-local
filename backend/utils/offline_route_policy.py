"""Inbound route surface permitted by the current offline audio stage."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from utils.env_loader import is_offline_runtime

_ALLOWED_HTTP = frozenset(
    {
        ("POST", "/v1/local/audio/parts"),
        ("GET", "/openapi.json"),
        ("GET", "/v1/account/cutover/control"),
        ("GET", "/v1/action-items"),
        ("GET", "/v1/conversations"),
        ("GET", "/v1/goals/all"),
        ("GET", "/v1/health"),
        ("GET", "/v1/local/status"),
        ("GET", "/v1/local/preview"),
        ("GET", "/v1/local/processing"),
        ("POST", "/v1/local/processing/save"),
        ("POST", "/v1/local/processing/probe"),
        ("POST", "/v1/local/processing/models"),
        ("POST", "/v1/local/processing/activate"),
        ("HEAD", "/v1/health"),
        ("GET", "/v1/users/available-languages"),
        ("GET", "/v1/users/daily-summaries"),
        ("GET", "/v1/users/me/subscription"),
        ("GET", "/v1/users/people"),
        ("GET", "/v1/users/private-cloud-sync"),
        ("GET", "/v1/users/profile"),
        ("GET", "/v1/users/language"),
        ("PATCH", "/v1/users/language"),
        ("GET", "/v1/users/onboarding"),
        ("GET", "/v1/users/training-data-opt-in"),
        ("GET", "/v1/users/transcription-preferences"),
        ("GET", "/v3/speech-profile"),
        ("PATCH", "/v1/users/onboarding"),
    }
)
_ALLOWED_WEBSOCKETS = frozenset({"/v4/listen"})


def is_offline_http_route_allowed(method: str, path: str) -> bool:
    if method.upper() == 'DELETE' and (
        path == '/v1/local/processing/profiles/{profile_id}'
        or re.fullmatch(r'/v1/local/processing/profiles/[A-Za-z0-9_-]{1,64}', path)
    ):
        return True
    if method.upper() == 'GET' and (
        path == '/v1/conversations/{conversation_id}'
        or re.fullmatch(r'/v1/conversations/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', path)
    ):
        return True
    return (method.upper(), path) in _ALLOWED_HTTP


def is_offline_websocket_route_allowed(path: str) -> bool:
    return path in _ALLOWED_WEBSOCKETS


class OfflineRoutePolicyMiddleware:
    """Reject non-allowlisted routes before body, auth, quota or provider code."""

    def __init__(self, app: Callable[..., Awaitable[Any]]) -> None:
        self.app = app

    async def __call__(
        self,
        scope: MutableMapping[str, Any],
        receive: Callable[[], Awaitable[MutableMapping[str, Any]]],
        send: Callable[[MutableMapping[str, Any]], Awaitable[None]],
    ) -> None:
        if not is_offline_runtime() or scope.get("type") not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path", ""))
        if scope["type"] == "http":
            method = str(scope.get("method", "GET")).upper()
            if is_offline_http_route_allowed(method, path):
                await self.app(scope, receive, send)
                return
            payload = json.dumps(
                {
                    "error": "Route is unavailable in offline runtime",
                    "reason": "offline_route_blocked",
                },
                separators=(",", ":"),
            ).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 503,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())],
                }
            )
            await send({"type": "http.response.body", "body": payload})
            return
        if is_offline_websocket_route_allowed(path):
            await self.app(scope, receive, send)
            return
        await send({"type": "websocket.close", "code": 1008, "reason": "offline_route_blocked"})
