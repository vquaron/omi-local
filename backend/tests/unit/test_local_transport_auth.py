import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import httpx
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from utils.local_transport_auth import LocalTransportAuthError, LocalTransportAuthMiddleware, verify_local_key
from utils.offline_route_policy import OfflineRoutePolicyMiddleware

KEY = "a" * 43


@pytest.fixture(params=["ngrok", "tailscale"])
def pairing(monkeypatch, tmp_path, request):
    path = tmp_path / "pairing.json"
    path.write_text(
        json.dumps(
            {"version": 1, "owner_uid": "synthetic-owner", "key_sha256": hashlib.sha256(KEY.encode()).hexdigest()}
        )
    )
    path.chmod(0o600)
    monkeypatch.setenv("OMI_ENV_STAGE", "offline")
    monkeypatch.setenv("OMI_LOCAL_TRANSPORT", request.param)
    monkeypatch.setenv("OMI_LOCAL_PAIRING_FILE", str(path))
    return path


def client():
    app = FastAPI()

    @app.get("/v1/users/profile")
    def profile():
        return {"uid": "synthetic-owner"}

    @app.websocket("/v4/listen")
    async def listen(ws: WebSocket):
        await ws.accept()
        await ws.send_json({"owner": ws.scope["omi.local_uid"]})
        await ws.close()

    app.add_middleware(OfflineRoutePolicyMiddleware)
    app.add_middleware(LocalTransportAuthMiddleware)
    return TestClient(app)


def test_http_ws_share_key_and_ignore_query_uid(pairing):
    headers = {"Authorization": f"Bearer {KEY}"}
    with client() as c:
        assert c.get("/v1/users/profile", headers=headers).status_code == 200
        with c.websocket_connect("/v4/listen?uid=attacker", headers=headers) as ws:
            assert ws.receive_json() == {"owner": "synthetic-owner"}
        assert c.post("/v2/sync", headers=headers, content=b"blocked").status_code == 503


@pytest.mark.parametrize("value", [None, "Bearer wrong", "Basic " + KEY, "Bearer " + KEY + " "])
def test_invalid_auth_rejected_before_handlers(pairing, value):
    headers = {} if value is None else {"Authorization": value}
    with client() as c:
        assert c.get("/v1/users/profile", headers=headers).status_code == 401
        with pytest.raises(WebSocketDisconnect) as exc:
            with c.websocket_connect("/v4/listen", headers=headers):
                pass
        assert exc.value.code == 1008


def test_rotation_revokes_previous_key_and_permissions_fail_closed(pairing):
    assert verify_local_key(KEY) == "synthetic-owner"
    data = json.loads(pairing.read_text())
    data["key_sha256"] = hashlib.sha256(("b" * 43).encode()).hexdigest()
    pairing.write_text(json.dumps(data))
    with pytest.raises(LocalTransportAuthError):
        verify_local_key(KEY)
    assert verify_local_key("b" * 43) == "synthetic-owner"
    pairing.chmod(0o644)
    with pytest.raises(LocalTransportAuthError):
        verify_local_key("b" * 43)


@pytest.mark.parametrize("transport", ["ngrok", "tailscale"])
def test_missing_configuration_blocks_startup(monkeypatch, transport):
    monkeypatch.setenv("OMI_ENV_STAGE", "offline")
    monkeypatch.setenv("OMI_LOCAL_TRANSPORT", transport)
    monkeypatch.delenv("OMI_LOCAL_PAIRING_FILE", raising=False)
    with pytest.raises(LocalTransportAuthError):
        with client():
            pass


def test_existing_lan_client_remains_unchanged(monkeypatch):
    monkeypatch.setenv("OMI_ENV_STAGE", "offline")
    monkeypatch.setenv("OMI_LOCAL_TRANSPORT", "lan")
    with client() as c:
        assert c.get("/v1/users/profile").status_code == 200


def test_pairing_diagnostics_only_include_response_status(pairing, caplog):
    with client() as c:
        accepted = c.get("/v1/users/profile?private=synthetic-private", headers={"Authorization": f"Bearer {KEY}"})
        rejected = c.get(
            "/v1/users/profile?private=synthetic-private", headers={"Authorization": "Bearer rejected-secret"}
        )
        assert accepted.status_code == 200
        assert rejected.status_code == 401
        assert c.get("/openapi.json").status_code == 401
    messages = [record.getMessage() for record in caplog.records if record.name == "utils.local_transport_auth"]
    assert messages == ["Local Mac profile check: status=200", "Local Mac profile check: status=401"]


@pytest.fixture
def processing_client(pairing, monkeypatch):
    from routers import local_processing
    monkeypatch.setenv('OMI_LOCAL_PROCESSING_URL', 'http://127.0.0.1:20001')
    calls = []
    responses = []
    def handle(request):
        calls.append(request)
        reply = responses.pop(0) if responses else httpx.Response(200, json={'revision': 0, 'profiles': [], 'effective': {}})
        if isinstance(reply, Exception):
            raise reply
        return reply
    original = httpx.AsyncClient
    monkeypatch.setattr(local_processing.httpx, 'AsyncClient',
                        lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))
    app = FastAPI()
    app.include_router(local_processing.router)
    app.add_middleware(OfflineRoutePolicyMiddleware)
    app.add_middleware(LocalTransportAuthMiddleware)
    with TestClient(app) as test_client:
        yield test_client, calls, responses


def test_processing_phone_is_paired_and_only_forwards_to_fixed_library(processing_client):
    client, calls, responses = processing_client
    assert client.get('/v1/local/processing').status_code == 401
    headers = {'Authorization': 'Bearer ' + KEY, 'Host': 'untrusted.example'}
    response = client.get('/v1/local/processing?uid=attacker&url=https://untrusted.example', headers=headers)
    assert response.status_code == 200 and response.headers['Cache-Control'] == 'no-store'
    assert str(calls[0].url) == 'http://127.0.0.1:20001/api/processing'
    assert calls[0].headers['Origin'] == 'http://127.0.0.1:20001'
    assert calls[0].headers['X-Omiloc-Request'] == 'processing'
    assert 'authorization' not in calls[0].headers
    body = {'revision': 2, 'profile': {'stage': 'live', 'kind': 'external'}, 'api_key': 'synthetic-provider-secret'}
    response = client.post('/v1/local/processing/save', headers=headers, json=body)
    assert response.status_code == 200 and json.loads(calls[-1].content) == body
    assert 'synthetic-provider-secret' not in response.text
    assert str(calls[-1].url).endswith('/api/processing/save')
    response = client.request('DELETE', '/v1/local/processing/profiles/synthetic-id', headers=headers, json={'revision': 2})
    assert response.status_code == 200 and str(calls[-1].url).endswith('/api/processing/profiles/synthetic-id')


def test_processing_bounds_bodies_and_preserves_safe_conflicts(processing_client):
    client, calls, responses = processing_client
    headers = {'Authorization': 'Bearer ' + KEY, 'Content-Type': 'application/json'}
    assert client.post('/v1/local/processing/save', headers=headers, content='[]').status_code == 400
    assert client.post('/v1/local/processing/save', headers=headers, content='invalid').status_code == 400
    assert client.post('/v1/local/processing/save', headers=headers, content='x' * 65537).status_code == 413
    assert not calls
    responses.append(httpx.Response(409, json={'error': 'Настройки изменились в другом окне.'}))
    response = client.post('/v1/local/processing/activate', headers=headers, json={'revision': 0, 'stage': 'live', 'id': None})
    assert response.status_code == 409 and response.json()['error'] == 'Настройки изменились в другом окне.'


def test_processing_unknown_outcome_and_redirect_never_retry(processing_client):
    client, calls, responses = processing_client
    headers = {'Authorization': 'Bearer ' + KEY}
    responses.append(httpx.ReadTimeout('synthetic-private-server-details'))
    response = client.post('/v1/local/processing/activate', headers=headers, json={'revision': 0, 'stage': 'live', 'id': None})
    assert response.status_code == 503 and response.json()['outcome'] == 'indeterminate'
    assert len(calls) == 1 and 'synthetic-private' not in response.text
    responses.append(httpx.Response(307, headers={'Location': 'https://untrusted.example'}))
    assert client.get('/v1/local/processing', headers=headers).status_code == 503
    assert len(calls) == 2


@pytest.mark.parametrize('origin', ['', 'http://192.168.1.20:20001', 'https://127.0.0.1:20001',
                                    'http://127.0.0.1:20001/api/settings', 'http://key@127.0.0.1:20001'])
def test_processing_rejects_non_loopback_configuration(processing_client, monkeypatch, origin):
    client, calls, _ = processing_client
    monkeypatch.setenv('OMI_LOCAL_PROCESSING_URL', origin)
    assert client.get('/v1/local/processing', headers={'Authorization': 'Bearer ' + KEY}).status_code == 503
    assert not calls


def test_processing_disabled_for_legacy_lan_and_standard_runtime(monkeypatch):
    from routers.local_processing import router
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        monkeypatch.setenv('OMI_ENV_STAGE', 'offline')
        monkeypatch.setenv('OMI_LOCAL_TRANSPORT', 'lan')
        assert client.get('/v1/local/processing', headers={'Authorization': 'Bearer ' + KEY}).status_code == 404
        monkeypatch.setenv('OMI_ENV_STAGE', 'local')
        assert client.get('/v1/local/processing').status_code == 404


def test_listen_diagnostics_observe_frames_and_close_without_private_content(pairing, caplog):
    app = FastAPI()

    @app.websocket("/v4/listen")
    async def listen(ws: WebSocket):
        await ws.accept()
        assert await ws.receive_bytes() == b"synthetic-private-audio"
        await ws.send_text("received")
        await ws.receive()

    app.add_middleware(LocalTransportAuthMiddleware)
    with TestClient(app) as c:
        with c.websocket_connect(
            "/v4/listen?private=synthetic-private-query",
            headers={"Authorization": f"Bearer {KEY}", "X-Private": "synthetic-private-header"},
        ) as ws:
            ws.send_bytes(b"synthetic-private-audio")
            assert ws.receive_text() == "received"
            ws.close(code=1000, reason="synthetic-private-close")
    messages = [record.getMessage() for record in caplog.records if record.name == "utils.local_transport_auth"]
    assert messages == [
        "Local Mac listen: opened",
        "Local Mac listen: accepted",
        "Local Mac listen: first_binary_frame",
        "Local Mac listen: peer_closed code=1000 frames=1 bytes=23",
    ]


def test_listen_auth_rejection_is_observed_before_accept(pairing, caplog):
    with client() as c:
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("/v4/listen?private=synthetic-private", headers={"Authorization": "Bearer wrong"}):
                pass
    messages = [record.getMessage() for record in caplog.records if record.name == "utils.local_transport_auth"]
    assert messages == ["Local Mac listen: opened", "Local Mac listen: server_closed code=1008"]


def test_local_status_route_auth_and_runtime_boundary(pairing, tmp_path):
    # Exercise the actual public router/auth dependency without importing the
    # full backend application or requiring unrelated cloud SDKs in unit CI.
    backend = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env.update({
        'PROVIDER_MODE': 'offline',
        'OMI_HARNESS_STATE_ROOT': str(tmp_path),
        'OMI_LOCAL_STORAGE_ROOT': str(tmp_path / 'services/storage'),
        'FIRESTORE_EMULATOR_HOST': '127.0.0.1:8085',
        'FIREBASE_AUTH_EMULATOR_HOST': '127.0.0.1:9099',
        'FIREBASE_AUTH_PROJECT_ID': 'demo-omi-local',
        'FIREBASE_PROJECT_ID': 'demo-omi-local',
        'REDIS_DB_HOST': '127.0.0.1',
        'REDIS_DB_PORT': '6380',
        'BASE_API_URL': 'http://127.0.0.1:8000',
        'API_BASE_URL': 'http://127.0.0.1:8000',
        'PYTHONPATH': str(backend),
    })
    env.pop('OMI_LOCAL_LIVE_PREVIEW_URL', None)
    probe = '''
import os
from fastapi import FastAPI
from fastapi.testclient import TestClient
from testing.import_isolation import AutoMockModule, stub_modules
from utils.local_transport_auth import LocalTransportAuthMiddleware
from utils.offline_route_policy import OfflineRoutePolicyMiddleware

def forbidden(*args, **kwargs):
    raise AssertionError('Local status must not contact cloud auth or run a listen session')

def run_probe():
    # Keep route admission, local key verification, and status snapshot real.
    # Stub only unrelated persistence, telemetry, and WebSocket runtime seams;
    # the scoped helper and subprocess prevent these fakes leaking to tests.
    fakes = {
        name: AutoMockModule(name)
        for name in (
            'firebase_admin', 'firebase_admin.auth', 'redis',
            'database.redis_db', 'database.users', 'utils.byok',
            'utils.account_cutover.access', 'routers.listen.runtime',
        )
    }
    for name in ('CertificateFetchError', 'ExpiredIdTokenError', 'InvalidIdTokenError', 'RevokedIdTokenError'):
        setattr(fakes['firebase_admin.auth'], name, type(name, (Exception,), {}))
    fakes['firebase_admin.auth'].verify_id_token = forbidden
    fakes['firebase_admin.auth'].get_user = forbidden
    fakes['database.users'].get_user_deletion_wipe_status.return_value = None
    fakes['utils.account_cutover.access'].cutover_enforcement_enabled.return_value = False
    fakes['routers.listen.runtime'].run_listen_session = forbidden
    with stub_modules(fakes):
        from routers import transcribe
        from utils.other import endpoints as auth

        app = FastAPI()
        app.include_router(transcribe.router)
        app.add_middleware(OfflineRoutePolicyMiddleware)
        app.add_middleware(LocalTransportAuthMiddleware)
        with TestClient(app, base_url='http://127.0.0.1:8000', client=('127.0.0.1', 50000)) as client:
            for headers in ({}, {'Authorization': 'Bearer wrong'}, {'Authorization': 'Basic ' + 'a' * 43}):
                assert client.get('/v1/local/status', headers=headers).status_code == 401
                assert client.get('/v1/local/preview', headers=headers).status_code == 401
            response = client.get('/v1/local/status?uid=synthetic-attacker', headers={'Authorization': 'Bearer ' + 'a' * 43})
            assert response.status_code == 200, response.status_code
            assert response.json() == {
                'backend': 'ready',
                'capture': {'state': 'idle', 'audio_seconds': 0, 'frames_received': 0},
                'live_transcript': {'state': 'disabled', 'updates': 0},
                'diarization': {'state': 'disabled', 'labeled_segments': 0},
            }
            headers = {'Authorization': 'Bearer ' + 'a' * 43}
            preview = client.get('/v1/local/preview?uid=synthetic-attacker', headers=headers)
            assert preview.status_code == 200 and preview.json()['sessions'] == []
            assert preview.headers['cache-control'] == 'no-store'
            for forwarded in ('Forwarded', 'X-Forwarded-For', 'X-Forwarded-Proto'):
                assert client.get('/v1/local/preview', headers={**headers, forwarded: 'synthetic-proxy'}).status_code == 404
            with TestClient(app, base_url='http://100.64.0.1:8000', client=('127.0.0.1', 50000)) as tailnet:
                assert tailnet.get('/v1/local/preview', headers={**headers, 'Host': '127.0.0.1:8000'}).status_code == 404
                assert tailnet.get('/v1/local/status', headers=headers).status_code == 200
            with TestClient(app, base_url='http://127.0.0.1:8000', client=('192.0.2.1', 50000)) as remote:
                assert remote.get('/v1/local/preview', headers=headers).status_code == 404
            app.dependency_overrides[auth.get_current_user_uid] = forbidden
            os.environ['OMI_LOCAL_TRANSPORT'] = 'lan'
            assert client.get('/v1/local/status').status_code == 404
            assert client.get('/v1/local/preview').status_code == 404
            os.environ['OMI_ENV_STAGE'] = 'prod'
            assert client.get('/v1/local/status').status_code == 404
            assert client.get('/v1/local/preview').status_code == 404

run_probe()
print('local_status_auth_passed')
'''
    result = subprocess.run([sys.executable, '-c', probe], cwd=backend, env=env,
                            text=True, capture_output=True, timeout=45)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'local_status_auth_passed'
