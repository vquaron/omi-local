import asyncio
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
import httpx
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dev_harness import local_provider_relay as relay
from dev_harness import local_stt_services as services


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
def cfg(tmp_path):
    return SimpleNamespace(repo_root=tmp_path, backend_port=20000, backend_url='http://127.0.0.1:20000',
                           provider_mode='offline', local_transport='ngrok',
                           layout=SimpleNamespace(state_root=tmp_path))


def profile(url='wss://speech.example/asr', **settings):
    return {'id': 'synthetic-live', 'name': 'Synthetic', 'stage': 'live', 'kind': 'external',
            'settings': {'url': url, **settings}, 'credential_ref': 'synthetic-key-ref'}


class Registry:
    def __init__(self, snapshot):
        self.value = snapshot
        self.lookups = []

    def snapshot(self, stage):
        assert stage == 'live'
        return copy.deepcopy(self.value)

    def key(self, snapshot):
        self.lookups.append(snapshot['credential_ref'])
        return 'synthetic-bearer'


class Provider:
    def __init__(self, *, broken=False):
        self.incoming = asyncio.Queue()
        self.sent = []
        self.broken = broken

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def recv(self):
        return json.dumps({'type': 'config', 'useAudioWorklet': not self.broken, 'diarization': True})

    async def send(self, pcm):
        self.sent.append(pcm)
        if pcm:
            self.incoming.put_nowait(json.dumps({'lines': [{'text': 'initial', 'start': 0, 'end': .1}],
                                                 'buffer_transcription': 'draft'}))
        else:
            self.incoming.put_nowait(json.dumps({'lines': [], 'buffer_transcription': 'corrected',
                                                 'diarization_status': 'degraded'}))
            self.incoming.put_nowait(json.dumps({'type': 'ready_to_stop'}))

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.incoming.get()


def test_real_asgi_relay_pins_key_preserves_corrections_and_drains(cfg):
    registry = Registry(profile())
    connections = []
    def connect(endpoint, *, key):
        provider = Provider()
        connections.append((endpoint, key, provider))
        return provider
    async def health_probe(*a, **kw):
        return {'state': 'ready'}
    with TestClient(relay.create_app(cfg, registry=registry, connector=connect, health_probe=health_probe), client=('127.0.0.1', 1234)) as client:
        with client.websocket_connect('/asr') as socket:
            assert socket.receive_json()['useAudioWorklet'] is True
            health = client.get('/health').json()
            assert health['active_sessions'] == 1
            assert health['diarization'] == {'state': 'ready'}
            with pytest.raises(relay.RelayError, match='Stop recording'):
                with relay.session_gate(cfg, exclusive=True):
                    pass
            registry.value = profile('wss://next.example/asr')
            registry.value['credential_ref'] = 'changed-ref'
            socket.send_bytes(b'\x01\x00' * 100)
            assert socket.receive_json()['lines'][0]['text'] == 'initial'
            socket.send_bytes(b'')
            corrected = socket.receive_json()
            assert corrected == {'lines': [], 'buffer_transcription': 'corrected', 'diarization_status': 'degraded'}
            assert socket.receive_json() == {'type': 'ready_to_stop'}
        assert connections[0][:2] == ('wss://speech.example/asr', 'synthetic-bearer')
        assert registry.lookups == ['synthetic-key-ref', 'synthetic-key-ref']
        assert connections[0][2].sent == [b'\x01\x00' * 100, b'']
        with client.websocket_connect('/asr') as socket:
            socket.receive_json()
            socket.send_bytes(b'')
            socket.receive_json()
            socket.receive_json()
        assert connections[1][0] == 'wss://next.example/asr'
        assert registry.lookups == ['synthetic-key-ref', 'synthetic-key-ref', 'changed-ref']


def test_disabled_handshake_never_resolves_key_or_opens_upstream(cfg):
    registry = Registry(None)
    with TestClient(relay.create_app(cfg, registry=registry, connector=lambda *_a, **_k: pytest.fail('no provider')),
                    client=('127.0.0.1', 1234)) as client:
        assert client.get('/health').json()['enabled'] is False
        with client.websocket_connect('/asr') as socket:
            assert socket.receive_json() == {'type': 'config', 'useAudioWorklet': True,
                                              'enabled': False, 'diarization': False}
    assert registry.lookups == []


def test_gate_prevents_handshake_during_switch(cfg):
    with TestClient(relay.create_app(cfg, registry=Registry(profile()), connector=lambda *_a, **_k: pytest.fail('gated')),
                    client=('127.0.0.1', 1234)) as client:
        with relay.session_gate(cfg, exclusive=True):
            with client.websocket_connect('/asr') as socket:
                assert socket.receive_json() == {'type': 'error', 'code': 'live_provider_failed'}


@pytest.mark.parametrize('headers', [{'Origin': 'https://evil.example'}, {'X-Forwarded-For': '127.0.0.1'}])
def test_browser_or_forwarded_socket_is_rejected(cfg, headers):
    from starlette.websockets import WebSocketDisconnect
    with TestClient(relay.create_app(cfg, registry=Registry(None)), client=('127.0.0.1', 1234)) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect('/asr', headers=headers):
                pass


@pytest.mark.anyio
async def test_disconnect_still_drains_provider_without_delivering(cfg):
    provider = Provider()
    downstream = SimpleNamespace(receive=lambda: asyncio.sleep(0, result={'type': 'websocket.disconnect'}),
                                 send_text=lambda raw: asyncio.sleep(0))
    await relay.relay_session(downstream, profile(), cfg, connector=lambda *a, **k: provider)
    assert provider.sent == [b'']


@pytest.mark.anyio
async def test_probe_checks_pcm_contract_and_zero_audio_eof(cfg):
    provider = Provider()
    assert (await relay.probe(profile(), cfg, connector=lambda *a, **k: provider))['ready']
    assert provider.sent == [b'']
    with pytest.raises(relay.RelayError, match='protocol'):
        await relay.probe(profile(), cfg, connector=lambda *a, **k: Provider(broken=True))


@pytest.mark.anyio
async def test_missing_eof_is_bounded_and_redirects_never_forward_credentials(cfg, monkeypatch):
    provider = Provider()
    async def send(pcm):
        provider.sent.append(pcm)
    provider.send = send
    monkeypatch.setattr(relay, 'DRAIN_SECONDS', .02)
    downstream = SimpleNamespace(receive=lambda: asyncio.sleep(0, result={'type': 'websocket.receive', 'bytes': b''}),
                                 send_text=lambda raw: asyncio.sleep(0))
    with pytest.raises(TimeoutError):
        await relay.relay_session(downstream, profile(), cfg, connector=lambda *a, **k: provider)
    assert provider.sent == [b'']
    pending = relay.connect('wss://speech.example/asr', key='synthetic-secret')
    with pytest.raises(relay.RelayError, match='redirect'):
        pending.handle_redirect('wss://other.example/asr')


def test_diarization_hop_has_no_cloud_key_and_raw_hop_does(cfg):
    selected = profile(diarization={'enabled': True, 'port': 18091})
    assert relay.upstream(selected, cfg) == ('ws://127.0.0.1:18091/asr', False)
    assert relay.upstream(selected, cfg, raw=True) == ('wss://speech.example/asr', True)
    with pytest.raises(ValueError, match='relay'):
        relay.upstream(profile(relay.url(cfg)), cfg)


def test_activation_rechecks_idle_and_publishes_under_exclusive_gate(cfg, monkeypatch):
    from dev_harness import cli
    from dev_harness.local_providers import Registry as RealRegistry
    events = []
    monkeypatch.setattr(services, '_capture_status', lambda cfg: events.append('idle') or {
        'live_transcript': {'configurable': True}})
    monkeypatch.setattr(services, '_legacy_drain_idle', lambda *a: None)
    monkeypatch.setattr(services, 'live_settings', lambda cfg: {})
    monkeypatch.setattr(services, 'start_configured', lambda *a, **k: events.append('prepare'))
    monkeypatch.setattr(services, 'start_provider_relay', lambda *a: events.append('relay'))
    async def probe(*a, **k):
        events.append('probe')
    monkeypatch.setattr(relay, 'probe', probe)
    monkeypatch.setattr(RealRegistry, 'key', lambda *a: 'synthetic')
    monkeypatch.setattr(cli, '_stop_single_service', lambda *a: pytest.fail('already attached'))
    def publish():
        with pytest.raises(relay.RelayError):
            with relay.session_gate(cfg):
                pass
        events.append('publish')
    services.activate(cfg, profile(), publish=publish)
    assert events == ['idle', 'relay', 'prepare', 'probe', 'idle', 'publish']


def test_failed_first_activation_restores_proxy_only_after_relay_is_ready(cfg, monkeypatch):
    from dev_harness.local_providers import Registry as RealRegistry
    previous = {'provider': 'external', 'url': 'ws://127.0.0.1:18090/asr',
                'diarization': {'enabled': True, 'port': 18091, 'python': '/synthetic/python'}}
    events = []
    monkeypatch.setattr(services, '_capture_status', lambda cfg: {'live_transcript': {}})
    monkeypatch.setattr(services, '_legacy_drain_idle', lambda *a: None)
    monkeypatch.setattr(services, 'live_settings', lambda cfg: previous)
    monkeypatch.setattr(services, 'start_provider_relay', lambda *a: events.append('relay'))
    def start(*args, **kwargs):
        assert 'relay' in events  # The restored proxy can immediately reach its new hop.
        events.append(kwargs['live_override'])
    monkeypatch.setattr(services, 'start_configured', start)
    monkeypatch.setattr(RealRegistry, 'key', lambda *a: '')
    async def fail(*args, **kwargs):
        raise relay.RelayError('Unsupported protocol')
    monkeypatch.setattr(relay, 'probe', fail)
    with pytest.raises(relay.RelayError, match='Unsupported protocol'):
        services.activate(cfg, profile(), publish=lambda: pytest.fail('candidate failed'))
    assert events[0] == 'relay' and events[-1] == previous
    monkeypatch.setattr(services, 'start_provider_relay', lambda *a: (_ for _ in ()).throw(services.ServiceError('relay failed')))
    events.clear()
    with pytest.raises(services.ServiceError, match='relay failed'):
        services.activate(cfg, profile(), publish=lambda: pytest.fail('relay failed'))
    assert events == []  # Original direct proxy is untouched if the new hop failed.


def test_first_nonlive_save_preserves_live_and_bootstraps_relay_before_proxy(cfg, monkeypatch):
    from dev_harness import cli, config
    from dev_harness.local_providers import Registry as RealRegistry
    cfg.layout.services_dir = cfg.repo_root / 'services'
    python = cfg.repo_root / 'synthetic-python'
    python.write_text('synthetic')
    legacy = {'enabled': True, 'provider': 'external', 'url': 'ws://127.0.0.1:18090/asr',
              'diarization': {'enabled': True, 'port': 18091, 'python': str(python)}}
    path = cfg.layout.state_root / 'live-stt.json'
    path.write_text(json.dumps(legacy))
    original = path.read_bytes()
    registry = RealRegistry(cfg)
    saved = registry.save({'revision': 0, 'profile': {
        'name': 'Synthetic summary', 'stage': 'summary', 'kind': 'openai-compatible',
        'settings': {'base_url': 'https://llm.example/v1', 'model': 'synthetic'}}})
    assert saved['active']['live'] == 'current-live' and path.read_bytes() == original
    assert registry.snapshot('live')['settings']['diarization'] == legacy['diarization']
    starts = []
    monkeypatch.setattr(services, 'start_provider_relay', lambda cfg: starts.append(('relay', [])))
    monkeypatch.setattr(config, 'child_env_for', lambda cfg: {})
    monkeypatch.setattr(cli, '_service_record', lambda *a: None)
    monkeypatch.setattr(cli, '_start_process', lambda cfg, name, command, **kwargs: starts.append((name, command)))
    monkeypatch.setattr(services, 'health', lambda *a: (True, 'synthetic ready'))
    services.start_configured(cfg)
    assert [name for name, _ in starts] == ['relay', 'live-diarization']
    command = starts[1][1]
    assert command[command.index('--upstream') + 1] == 'ws://127.0.0.1:20004/upstream'


def test_activation_does_not_publish_when_capture_resumes(cfg, monkeypatch):
    events = []
    checks = iter([{'live_transcript': {'configurable': True}}, None])
    def status(cfg):
        result = next(checks)
        if result is None:
            raise services.ServiceError('Stop the current recording')
        return result
    monkeypatch.setattr(services, '_capture_status', status)
    monkeypatch.setattr(services, '_legacy_drain_idle', lambda *a: None)
    monkeypatch.setattr(services, 'live_settings', lambda cfg: {})
    monkeypatch.setattr(services, 'start_configured', lambda *a, **k: events.append(k['live_override']))
    monkeypatch.setattr(services, 'start_provider_relay', lambda *a: None)
    with pytest.raises(services.ServiceError, match='Stop the current recording'):
        services.activate(cfg, None, publish=lambda: pytest.fail('capture resumed'))
    assert events == [{}, {}]  # Previous configuration is restored under the gate.


@pytest.mark.parametrize('diarization', [False, True])
def test_legacy_recheck_accepts_only_verified_owned_port_relocation(cfg, monkeypatch, diarization):
    from dev_harness import cli
    old_port = 18091 if diarization else 18090
    legacy = {'enabled': True, 'provider': 'whisperlivekit', 'url': 'ws://127.0.0.1:18090/asr'}
    if diarization:
        legacy['diarization'] = {'enabled': True, 'port': old_port, 'python': '/synthetic/python'}
    (cfg.layout.state_root / 'live-stt.json').write_text(json.dumps(legacy))
    record = {'pid': 11, 'port': old_port}
    monkeypatch.setattr(cli, '_service_record', lambda *a: record)
    calls = []
    def respond(request):
        calls.append(request)
        if len(calls) > 1:
            raise httpx.ConnectError('previous listener has closed')
        return httpx.Response(200, json={'ready': True, 'active': False})
    factory = httpx.Client
    monkeypatch.setattr(services.httpx, 'Client', lambda **kwargs: factory(transport=httpx.MockTransport(respond), **kwargs))
    witness = services._legacy_drain_idle(cfg, {})
    record = {'pid': 12, 'port': 19091}
    assert services._legacy_drain_idle(cfg, {}, witness) == witness
    assert len(calls) == 1
    # An unowned/unchanged old endpoint still needs a fresh drain check.
    record = {'pid': 11, 'port': old_port}
    with pytest.raises(services.ServiceError, match='previous live preview'):
        services._legacy_drain_idle(cfg, {}, witness)
    assert len(calls) == 2


def test_activation_rolls_back_under_gate_after_backend_failure(cfg, monkeypatch):
    from dev_harness import cli
    events = []
    monkeypatch.setattr(services, '_capture_status', lambda cfg: {'live_transcript': {}})
    monkeypatch.setattr(services, '_legacy_drain_idle', lambda *a: None)
    monkeypatch.setattr(services, 'live_settings', lambda cfg: {})
    monkeypatch.setattr(services, 'start_configured', lambda *a, **k: None)
    monkeypatch.setattr(services, 'start_provider_relay', lambda *a: None)
    monkeypatch.setattr(cli, '_service_record', lambda *a: {'pid': 123})
    monkeypatch.setattr(cli, '_stop_single_service', lambda *a: None)
    monkeypatch.setattr(cli, '_start_app_services', lambda *a: (_ for _ in ()).throw(RuntimeError('private details')))
    def rollback():
        with pytest.raises(relay.RelayError):
            with relay.session_gate(cfg):
                pass
        events.append('rollback')
    with pytest.raises(services.ActivationIndeterminate, match='indeterminate') as error:
        services.activate(cfg, None, publish=lambda: events.append('publish'), rollback=rollback)
    assert events == ['publish', 'rollback'] and 'private' not in str(error.value)


@pytest.mark.anyio
@pytest.mark.parametrize('body,status,expected', [
    ({'ready': True, 'active': False, 'diarization': True}, 200, 'ready'),
    ({'ready': True, 'active': True, 'diarization': True}, 200, 'busy'),
    ({'ready': True, 'active': False, 'busy': True, 'diarization': True}, 200, 'busy'),
    ({'ready': True, 'active': False, 'busy': True, 'diarization': False}, 200, 'disabled'),
    ({'ready': True, 'active': False, 'busy': 'false', 'diarization': True}, 200, 'unknown'),
    ({'ready': True, 'active': False, 'diarization': False}, 200, 'disabled'),
    ({'ready': True, 'active': False}, 200, 'unknown'),
    ({'ready': False, 'diarization': True}, 200, 'unavailable'),
    ({'ready': True}, 302, 'unavailable'),
    ({'padding': 'x' * 5000}, 200, 'unavailable'),
])
async def test_selected_diarization_health_is_bounded_and_does_not_redirect(cfg, monkeypatch, body, status, expected):
    factory = httpx.AsyncClient
    def respond(request):
        assert str(request.url) == 'https://speech.example/health'
        assert request.headers['authorization'] == 'Bearer synthetic-key'
        return httpx.Response(status, json=body, headers={'Location': 'https://unselected.example'})
    def client(**kwargs):
        assert kwargs['trust_env'] is False and kwargs['follow_redirects'] is False
        return factory(transport=httpx.MockTransport(respond), **kwargs)
    monkeypatch.setattr(relay.httpx, 'AsyncClient', client)
    assert await relay.diarization_health(profile(), cfg, key='synthetic-key') == {'state': expected}


@pytest.mark.anyio
async def test_local_diarization_health_does_not_receive_remote_credentials(cfg, monkeypatch):
    factory = httpx.AsyncClient
    def respond(request):
        assert str(request.url) == 'http://127.0.0.1:18091/health'
        assert 'authorization' not in request.headers
        raise httpx.ConnectError('synthetic private error')
    monkeypatch.setattr(relay.httpx, 'AsyncClient', lambda **kw: factory(transport=httpx.MockTransport(respond), **kw))
    selected = profile(diarization={'enabled': True, 'port': 18091})
    assert await relay.diarization_health(selected, cfg, key='synthetic-key') == {'state': 'unavailable'}
