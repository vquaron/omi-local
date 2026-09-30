"""Hermetic checks for the native backend's two-socket ownership boundary."""

from pathlib import Path
import socket
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dev_harness import tailscale_backend


ENV = {
    'OMI_LOCAL_TRANSPORT': 'tailscale', 'PROVIDER_MODE': 'offline', 'OMI_ENV_STAGE': 'offline',
    'OMI_DEV_BIND_HOST': '127.0.0.1', 'OMI_TAILSCALE_IP': '100.64.0.1', 'PORT': '20000',
}


@pytest.mark.parametrize('change', [
    {'OMI_TAILSCALE_IP': ''}, {'OMI_TAILSCALE_IP': '192.168.1.2'}, {'OMI_TAILSCALE_IP': '100.128.0.1'},
    {'OMI_TAILSCALE_IP': 'fd7a:115c:a1e0::1'}, {'OMI_DEV_BIND_HOST': '0.0.0.0'},
    {'PROVIDER_MODE': 'real'}, {'OMI_ENV_STAGE': 'local'}, {'OMI_LOCAL_TRANSPORT': 'lan'}, {'PORT': '0'},
])
def test_invalid_native_configuration_never_opens_a_socket(change):
    def forbidden(*args):
        raise AssertionError('validation must precede sockets')
    with pytest.raises(tailscale_backend.ListenerError):
        tailscale_backend.run({**ENV, **change}, socket_factory=forbidden)


class FakeSocket:
    def __init__(self, events, index, fail_bind=False, fail_listen=False):
        self.events, self.index, self.fail_bind = events, index, fail_bind
        self.fail_listen = fail_listen

    def setsockopt(self, *args):
        assert args == (socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    def bind(self, address):
        self.events.append(('bind', self.index, address))
        if self.fail_bind:
            raise OSError('private interface unavailable')

    def listen(self, backlog):
        assert backlog == tailscale_backend.BACKLOG
        self.events.append(('listen', self.index))
        if self.fail_listen:
            raise OSError('private listener unavailable')

    def setblocking(self, value):
        assert value is False

    def close(self):
        self.events.append(('close', self.index))


def test_one_server_shares_both_sockets_and_closes_them_after_exit():
    events, sockets = [], []
    def socket_factory(family, kind):
        assert (family, kind) == (socket.AF_INET, socket.SOCK_STREAM)
        sock = FakeSocket(events, len(sockets))
        sockets.append(sock)
        return sock
    def server_factory(cfg):
        assert events == [('bind', 0, ('127.0.0.1', 20000)), ('listen', 0),
                          ('bind', 1, ('100.64.0.1', 20000)), ('listen', 1)]
        assert cfg.app == 'main:app' and cfg.workers == 1
        assert cfg.proxy_headers is False and cfg.access_log is False
        assert cfg.log_level == 'warning'
        def run(**kwargs):
            assert kwargs['sockets'] == sockets
            events.append(('serve',))
        return SimpleNamespace(run=run, started=True)
    tailscale_backend.run(ENV, socket_factory=socket_factory, server_factory=server_factory)
    assert events[-3:] == [('serve',), ('close', 1), ('close', 0)]


@pytest.mark.parametrize('failure', ['bind', 'listen'])
def test_second_socket_failure_closes_both_without_application_startup(failure):
    events, sockets = [], []
    def factory(*args):
        sock = FakeSocket(events, len(sockets), fail_bind=bool(sockets) and failure == 'bind',
                          fail_listen=bool(sockets) and failure == 'listen')
        sockets.append(sock)
        return sock
    def forbidden(*args):
        raise AssertionError('application must not start after failed bind')
    with pytest.raises(OSError):
        tailscale_backend.run(ENV, socket_factory=factory, server_factory=forbidden)
    assert events[-2:] == [('close', 1), ('close', 0)]


def test_application_failure_closes_both_listeners():
    events = []
    def server_factory(_cfg):
        def run(**_kwargs):
            raise RuntimeError('application rejected configuration')
        return SimpleNamespace(run=run)
    with pytest.raises(RuntimeError):
        tailscale_backend.run(ENV, socket_factory=lambda *_: FakeSocket(events, 0), server_factory=server_factory)
    assert len([event for event in events if event[0] == 'close']) == 2


def test_main_redacts_native_error(monkeypatch, capsys):
    def fail():
        raise OSError('address 100.64.0.1 and other private system details')
    monkeypatch.setattr(tailscale_backend, 'run', fail)
    assert tailscale_backend.main() == 1
    output = capsys.readouterr()
    assert not output.out and '100.' not in output.err and 'private system' not in output.err
