"""Single-owner credentials for the explicit paired local transports.

Only the random key's SHA-256 is persisted. The pairing file is reread for each
new request, so replacing it revokes the previous key without a code change.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
from pathlib import Path

from utils.env_loader import is_offline_runtime

logger = logging.getLogger(__name__)


class LocalTransportAuthError(ValueError):
    pass


def local_tunnel_enabled() -> bool:
    mode = os.environ.get("OMI_LOCAL_TRANSPORT", "lan")
    if mode not in {"lan", "ngrok", "tailscale"}:
        raise LocalTransportAuthError("Invalid local transport configuration")
    return mode in {"ngrok", "tailscale"}


def load_pairing() -> dict[str, str]:
    if not is_offline_runtime():
        raise LocalTransportAuthError("Local tunnel requires offline runtime")
    try:
        path = Path(os.environ["OMI_LOCAL_PAIRING_FILE"])
        if not path.is_absolute() or path.is_symlink() or path.stat().st_mode & 0o077:
            raise ValueError("Pairing file must be private")
        data = json.loads(path.read_text())
        if data.get("version") != 1 or not re.fullmatch(r"[0-9a-f]{64}", data.get("key_sha256", "")):
            raise ValueError("Invalid pairing digest")
        # Harness identities are synthetic. Never accept a caller-supplied UID.
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", data.get("owner_uid", "")):
            raise ValueError("Invalid local owner")
        return data
    except (OSError, KeyError, ValueError, TypeError, AttributeError) as error:
        raise LocalTransportAuthError("Local pairing is unavailable") from error


def verify_local_key(token: str) -> str:
    pairing = load_pairing()
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        raise LocalTransportAuthError("Invalid local access key")
    digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    if not hmac.compare_digest(digest, pairing["key_sha256"]):
        raise LocalTransportAuthError("Invalid local access key")
    return pairing["owner_uid"]


def verify_local_authorization(headers: list[tuple[bytes, bytes]]) -> str:
    values = [value for name, value in headers if name.lower() == b"authorization"]
    if len(values) != 1:
        raise LocalTransportAuthError("Invalid local authorization")
    try:
        scheme, token = values[0].decode("ascii").split(" ")
        if scheme.lower() != "bearer":
            raise ValueError("Invalid scheme")
    except (UnicodeError, ValueError) as error:
        raise LocalTransportAuthError("Invalid local authorization") from error
    return verify_local_key(token)


class LocalTransportAuthMiddleware:
    """Authenticate before downstream middleware, request bodies and WS accept."""

    def __init__(self, app):
        self.app = app
        if local_tunnel_enabled():
            load_pairing()  # Fail startup if the public ingress has no boundary.

    async def __call__(self, scope, receive, send):
        if not local_tunnel_enabled() or scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        if scope["type"] == "http" and scope.get("path") == "/v1/health" and scope.get("method") in {"GET", "HEAD"}:
            return await self.app(scope, receive, send)
        if scope["type"] == "http" and scope.get("path") == "/v1/users/profile" and scope.get("method") == "GET":
            original_send = send

            async def profile_send(message):
                if message["type"] == "http.response.start":
                    # Pairing diagnostics contain no headers, URL/query, owner or body.
                    logger.warning("Local Mac profile check: status=%d", message["status"])
                await original_send(message)

            send = profile_send
        if scope["type"] == "websocket" and scope.get("path") == "/v4/listen":
            return await self._observe_listen(scope, receive, send)
        await self._authenticate(scope, receive, send)

    async def _observe_listen(self, scope, receive, send):
        # Only fixed events, codes and byte/frame counts. Never log headers,
        # query parameters, close reasons, identifiers or audio payloads.
        frames = 0
        byte_count = 0
        logger.warning("Local Mac listen: opened")

        async def observed_receive():
            nonlocal frames, byte_count
            message = await receive()
            if message["type"] == "websocket.receive" and message.get("bytes") is not None:
                frames += 1
                byte_count += len(message["bytes"])
                if frames == 1:
                    logger.warning("Local Mac listen: first_binary_frame")
            elif message["type"] == "websocket.disconnect":
                logger.warning(
                    "Local Mac listen: peer_closed code=%d frames=%d bytes=%d",
                    message.get("code", 1005),
                    frames,
                    byte_count,
                )
            return message

        async def observed_send(message):
            if message["type"] == "websocket.accept":
                logger.warning("Local Mac listen: accepted")
            elif message["type"] == "websocket.close":
                logger.warning("Local Mac listen: server_closed code=%d", message.get("code", 1000))
            await send(message)

        await self._authenticate(scope, observed_receive, observed_send)

    async def _authenticate(self, scope, receive, send):
        try:
            scope["omi.local_uid"] = verify_local_authorization(scope.get("headers", []))
        except LocalTransportAuthError:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008, "reason": "local_auth_required"})
            else:
                payload = b'{"detail":"local_auth_required"}'
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(payload)).encode()),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": payload})
            return
        await self.app(scope, receive, send)
