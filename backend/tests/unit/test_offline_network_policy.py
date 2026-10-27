from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from utils.offline_network_policy import OfflineEgressBlocked, OfflineEgressPolicy


def test_policy_allows_only_configured_local_endpoint() -> None:
    policy = OfflineEgressPolicy.from_environ(
        {
            "FIREBASE_AUTH_EMULATOR_HOST": "127.0.0.1:9099",
            "API_BASE_URL": "http://192.168.1.20:8000",
        }
    )
    policy.require("127.0.0.1", 9099)
    policy.require("192.168.1.20", 8000)
    with pytest.raises(OfflineEgressBlocked):
        policy.require("192.168.1.21", 8000)
    with pytest.raises(OfflineEgressBlocked):
        policy.require("1.1.1.1", 443)
    with pytest.raises(OfflineEgressBlocked):
        policy.require("api.openai.com", 443)


def test_policy_rejects_public_configured_endpoint() -> None:
    with pytest.raises(OfflineEgressBlocked, match="not local/private"):
        OfflineEgressPolicy.from_environ({"API_BASE_URL": "https://api.omi.me"})


def test_processing_library_adds_only_its_exact_loopback_endpoint() -> None:
    policy = OfflineEgressPolicy.from_environ({'OMI_LOCAL_PROCESSING_URL': 'http://127.0.0.1:20001'})
    policy.require('127.0.0.1', 20001)
    for host, port in [('127.0.0.1', 20002), ('192.168.1.20', 20001), ('provider.example', 443)]:
        with pytest.raises(OfflineEgressBlocked):
            policy.require(host, port)


def test_installed_audit_hook_blocks_before_dns_and_allows_local_socket(tmp_path: Path) -> None:
    reserved = socket.socket()
    reserved.bind(("127.0.0.1", 0))
    local_port = reserved.getsockname()[1]
    reserved.close()
    script = tmp_path / "probe.py"
    script.write_text(
        """
import json
import os
import socket
from utils.offline_network_policy import OfflineEgressBlocked, install_offline_egress_guard

install_offline_egress_guard()
blocked = []
for target in ((\"api.openai.com\", 443), (\"1.1.1.1\", 443)):
    try:
        socket.getaddrinfo(*target)
    except OfflineEgressBlocked:
        blocked.append(target[0])
listener = socket.socket()
port = int(os.environ[\"LOCAL_PROBE_PORT\"])
listener.bind((\"127.0.0.1\", port))
listener.listen(1)
client = socket.socket()
client.connect((\"127.0.0.1\", port))
accepted, _ = listener.accept()
print(json.dumps({\"blocked\": blocked, \"local_connected\": True}))
accepted.close()
client.close()
listener.close()
""",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update(
        {
            "OMI_ENV_STAGE": "offline",
            "PROVIDER_MODE": "offline",
            "API_BASE_URL": "http://127.0.0.1:8000",
            "LOCAL_PROBE_PORT": str(local_port),
            "OMI_OFFLINE_ALLOWED_ENDPOINTS": f"127.0.0.1:{local_port}",
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
        }
    )
    result = subprocess.run([sys.executable, str(script)], env=env, text=True, capture_output=True, check=True)
    payload = json.loads(result.stdout)
    assert payload["blocked"] == ["api.openai.com", "1.1.1.1"]
    assert payload["local_connected"] is True
