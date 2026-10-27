"""Default-deny outbound network boundary for the offline backend process."""

from __future__ import annotations

import ipaddress
import logging
import os
import sys
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlparse

from utils.env_loader import is_offline_runtime

logger = logging.getLogger(__name__)

_PRIVATE_NETWORKS = (
    ipaddress.ip_network("0.0.0.0/32"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)
_LOCAL_NAMES = frozenset({"localhost", "host.docker.internal"})
_ENDPOINT_ENV = (
    ("FIRESTORE_EMULATOR_HOST", None),
    ("FIREBASE_AUTH_EMULATOR_HOST", None),
    ("REDIS_DB_HOST", "REDIS_DB_PORT"),
    ("TYPESENSE_HOST", "TYPESENSE_HOST_PORT"),
    ("BASE_API_URL", None),
    ("API_BASE_URL", None),
    ("OMI_LLM_GATEWAY_URL", None),
    ("OMI_LOCAL_LIVE_PREVIEW_URL", None),
    ("OMI_LOCAL_PROCESSING_URL", None),
    ("OMI_OFFLINE_ALLOWED_ENDPOINTS", None),
)


class OfflineEgressBlocked(RuntimeError):
    """Raised before an offline process can resolve/connect to a denied target."""


def _normalized_host(value: object) -> str:
    if isinstance(value, bytes):
        value = value.decode("ascii", errors="strict")
    return str(value or "").strip().lower().strip("[]")


def _is_private_ip(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(address in network for network in _PRIVATE_NETWORKS)


def _parse_endpoint(value: str, separate_port: str | None = None) -> tuple[str, int | None]:
    raw = value.strip()
    if not raw:
        return "", None
    parsed = urlparse(raw if "://" in raw else f"//{raw}")
    host = _normalized_host(parsed.hostname)
    try:
        port = parsed.port
    except ValueError as exc:
        raise OfflineEgressBlocked(f"invalid offline endpoint configuration for {host or '<empty>'}") from exc
    if port is None and separate_port:
        try:
            port = int(separate_port)
        except ValueError as exc:
            raise OfflineEgressBlocked("invalid offline endpoint port") from exc
    if port is None and parsed.scheme:
        port = 443 if parsed.scheme in {"https", "wss"} else 80
    return host, port


@dataclass(frozen=True)
class OfflineEgressPolicy:
    endpoints: frozenset[tuple[str, int | None]]

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] | None = None) -> "OfflineEgressPolicy":
        source = os.environ if environ is None else environ
        endpoints: set[tuple[str, int | None]] = set()
        for key, port_key in _ENDPOINT_ENV:
            raw = source.get(key, "")
            if not raw:
                continue
            values = raw.split(",") if key == "OMI_OFFLINE_ALLOWED_ENDPOINTS" else [raw]
            for value in values:
                host, port = _parse_endpoint(value, source.get(port_key, "") if port_key else None)
                if not host:
                    continue
                if host not in _LOCAL_NAMES and not _is_private_ip(host):
                    raise OfflineEgressBlocked(f"configured offline endpoint host {host!r} is not local/private")
                endpoints.add((host, port))
        return cls(frozenset(endpoints))

    def require(self, host_value: object, port_value: object = None) -> None:
        host = _normalized_host(host_value)
        if not host:
            raise OfflineEgressBlocked("offline network target has no host")
        try:
            port = int(port_value) if port_value not in (None, "") else None
        except (TypeError, ValueError) as exc:
            raise OfflineEgressBlocked(f"offline network target {host!r} has an invalid port") from exc
        if (host, port) in self.endpoints or (host, None) in self.endpoints:
            return
        raise OfflineEgressBlocked(f"outbound socket to {host}:{port or '<unknown>'} is blocked in offline runtime")

    def audit(self, event: str, args: tuple[object, ...]) -> None:
        if event == "socket.getaddrinfo" and len(args) >= 2:
            self.require(args[0], args[1])
            return
        if event != "socket.connect" or len(args) < 2:
            return
        address = args[1]
        if isinstance(address, str):
            # Unix-domain sockets are local filesystem IPC.
            return
        if isinstance(address, tuple) and len(address) >= 2:
            self.require(address[0], address[1])


_installed_policy: OfflineEgressPolicy | None = None


def install_offline_egress_guard(environ: Mapping[str, str] | None = None) -> OfflineEgressPolicy | None:
    """Install a non-removable process audit hook exactly once in offline mode."""

    global _installed_policy
    source = os.environ if environ is None else environ
    if not is_offline_runtime(dict(source)):
        return None
    if _installed_policy is not None:
        return _installed_policy
    policy = OfflineEgressPolicy.from_environ(source)
    sys.addaudithook(policy.audit)
    _installed_policy = policy
    logger.info("Offline egress guard installed allowed_endpoints=%d", len(policy.endpoints))
    return policy
