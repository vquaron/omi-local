"""One offline ASGI process on loopback and one verified Tailscale interface."""

from __future__ import annotations

from contextlib import ExitStack
import ipaddress
import os
import socket
import sys
from collections.abc import Mapping

import uvicorn

BACKLOG = 2048


class ListenerError(ValueError):
    """Diagnostics never contain the private interface or system error text."""


def listener_addresses(environ: Mapping[str, str]) -> tuple[tuple[str, int], tuple[str, int]]:
    if (environ.get('PROVIDER_MODE') != 'offline' or environ.get('OMI_ENV_STAGE') != 'offline'
            or environ.get('OMI_LOCAL_TRANSPORT') != 'tailscale'
            or environ.get('OMI_DEV_BIND_HOST', '127.0.0.1') != '127.0.0.1'):
        raise ListenerError('Tailscale backend requires the paired offline loopback stack')
    try:
        address = ipaddress.IPv4Address(environ.get('OMI_TAILSCALE_IP', ''))
        port = int(environ.get('PORT', ''))
    except (ValueError, TypeError):
        raise ListenerError('Invalid Tailscale backend listener configuration') from None
    if address not in ipaddress.IPv4Network('100.64.0.0/10') or not 1024 <= port <= 65535:
        raise ListenerError('Invalid Tailscale backend listener configuration')
    return ('127.0.0.1', port), (str(address), port)


def run(environ=None, *, socket_factory=None, server_factory=None) -> None:
    """Acquire both interfaces before application startup; release both on every exit."""
    addresses = listener_addresses(os.environ if environ is None else environ)
    socket_factory = socket_factory or socket.socket
    server_factory = server_factory or uvicorn.Server
    with ExitStack() as cleanup:
        sockets = []
        for address in addresses:
            sock = socket_factory(socket.AF_INET, socket.SOCK_STREAM)
            cleanup.callback(sock.close)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(address)
            sock.listen(BACKLOG)
            sock.setblocking(False)
            sockets.append(sock)
        server = server_factory(uvicorn.Config(
            'main:app', host='127.0.0.1', port=addresses[0][1], workers=1,
            access_log=False, log_level='warning', proxy_headers=False, backlog=BACKLOG,
        ))
        server.run(sockets=sockets)
        if not server.started:
            raise ListenerError('Tailscale backend application startup failed')


def main() -> int:
    try:
        run()
    except (Exception, SystemExit):
        print('Tailscale backend could not start; check the local interface and owned port.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
