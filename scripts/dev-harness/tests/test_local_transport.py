"""Behavioral contracts for native connection selection and pairing preservation."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dev_harness import local_transport as transport, local_env, local_mac


def settings(tmp_path, content):
    path = tmp_path / '.env'
    path.write_text(content)
    path.chmod(0o600)
    return path


@pytest.mark.parametrize('value,expected', [
    ('100.64.0.1', 'http://100.64.0.1:20000'),
    ('100.127.255.254:21000', 'http://100.127.255.254:21000'),
    ('http://100.64.0.1:21000/', 'http://100.64.0.1:21000'),
    ('http://100.64.0.1', 'http://100.64.0.1:80'),
])
def test_connection_address(value, expected):
    assert transport.normalize_tailscale_url(value) == expected


@pytest.mark.parametrize('value', ['100.63.255.255', '100.128.0.1', '192.168.1.2',
    '100.64.0.1:0', '100.64.0.1:65536', '100.64.0.1:', '100.64.0.1/path',
    'http://user@100.64.0.1', 'https://100.64.0.1', '100.64.0.1?', '100.64.0.1#',
    '100.064.0.1', '100.64.0.1.evil.test'])
def test_reject_non_tailnet_authorities(value):
    with pytest.raises(transport.TransportError):
        transport.normalize_tailscale_url(value)


def test_selection_preserves_existing_ngrok_and_explicit_switch(tmp_path):
    assert transport.selection(tmp_path, {}) == 'tailscale'
    settings(tmp_path, 'NGROK_URL=https://synthetic.ngrok.app\nNGROK_AUTHTOKEN=synthetic\n')
    assert transport.selection(tmp_path, {}) == 'ngrok'
    assert transport.selection(tmp_path, {'OMI_LOCAL_TRANSPORT': 'tailscale'}) == 'tailscale'
    settings(tmp_path, 'OMI_LOCAL_TRANSPORT=tailscale\n')
    assert transport.selection(tmp_path, {}) == 'tailscale'
    with pytest.raises(transport.TransportError):
        transport.selection(tmp_path, {'OMI_LOCAL_TRANSPORT': 'lan'})


def test_selection_uses_saved_instance_without_env(tmp_path):
    saved = tmp_path / '.local/dev-harness/ngrok/ngrok.json'
    saved.parent.mkdir(parents=True)
    saved.write_text('{}')
    assert transport.selection(tmp_path, {}) == 'ngrok'


def test_discovery_verifies_running_self_and_does_not_leak(monkeypatch, capsys):
    monkeypatch.setattr(transport.shutil, 'which', lambda _: 'tailscale')
    status = {'BackendState': 'Running', 'Self': {'Online': True, 'TailscaleIPs': ['100.64.0.1']}}
    monkeypatch.setattr(transport.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps(status)))
    assert transport.tailscale_ip() == '100.64.0.1'
    with pytest.raises(transport.TransportError, match='does not belong'):
        transport.tailscale_ip('100.64.0.2')
    status['BackendState'] = 'Stopped'
    with pytest.raises(transport.TransportError, match='Cannot verify'):
        transport.tailscale_ip()
    assert not capsys.readouterr().out


def test_tailscale_env_needs_no_ngrok_preserves_key_and_files(tmp_path, monkeypatch):
    cfg = SimpleNamespace(repo_root=tmp_path, local_transport='tailscale',
                          layout=SimpleNamespace(state_root=tmp_path/'state'), backend_port=20000)
    monkeypatch.setattr(local_mac.cli, '_service_record', lambda *a: None)
    path = settings(tmp_path, 'OMI_LOCAL_TRANSPORT=tailscale\nOMI_LOCAL_APP_KEY=' + 'a'*43 + '\n')
    local_mac.private_json(cfg.layout.state_root/'ngrok.json', {'url': 'https://synthetic.ngrok.app'})
    local_env.apply(cfg, local_env.read_env(path))
    pairing = (cfg.layout.state_root/'pairing.json').read_bytes()
    settings(tmp_path, 'OMI_LOCAL_TRANSPORT=tailscale\n')
    local_env.apply(cfg, local_env.read_env(path))
    assert (cfg.layout.state_root/'pairing.json').read_bytes() == pairing
    assert not (cfg.layout.state_root/'ngrok-agent.yml').exists()
    assert json.loads((cfg.layout.state_root/'ngrok.json').read_text())['url'] == 'https://synthetic.ngrok.app'


def test_init_env_preserves_existing_hash_without_rotating(tmp_path):
    cfg = SimpleNamespace(repo_root=tmp_path, local_transport='tailscale',
                          layout=SimpleNamespace(state_root=tmp_path/'state'))
    local_mac.private_json(cfg.layout.state_root/'pairing.json', local_mac.pairing_data('a'*43))
    before = (cfg.layout.state_root/'pairing.json').read_bytes()
    local_env.initialize(cfg)
    assert local_env.read_env(tmp_path/'.env')['OMI_LOCAL_APP_KEY'] == ''
    assert (cfg.layout.state_root/'pairing.json').read_bytes() == before


def test_tailscale_new_pairing_without_key_fails(tmp_path, monkeypatch):
    cfg = SimpleNamespace(repo_root=tmp_path, local_transport='tailscale',
                          layout=SimpleNamespace(state_root=tmp_path/'state'))
    path = settings(tmp_path, 'OMI_LOCAL_TRANSPORT=tailscale\n')
    with pytest.raises(local_env.LocalEnvError, match='first pairing'):
        local_env.apply(cfg, local_env.read_env(path))


def test_custom_state_root_retains_transport(tmp_path):
    state = tmp_path / 'separate-state/custom'
    state.mkdir(parents=True)
    (state / 'ngrok.json').write_text('{}')
    assert transport.selection(tmp_path, {'OMI_LOCAL_STATE_ROOT': str(state.parent),
                                          'OMI_LOCAL_INSTANCE': 'custom'}) == 'ngrok'


def test_ngrok_env_can_reuse_existing_pairing_without_plaintext_key(tmp_path, monkeypatch):
    cfg = SimpleNamespace(repo_root=tmp_path, local_transport='ngrok', backend_port=20000,
                          layout=SimpleNamespace(state_root=tmp_path/'state'))
    local_mac.private_json(cfg.layout.state_root/'pairing.json', local_mac.pairing_data('a'*43))
    before = (cfg.layout.state_root/'pairing.json').read_bytes()
    path = settings(tmp_path, 'OMI_LOCAL_TRANSPORT=ngrok\nOMI_NGROK_URL=https://synthetic.ngrok.app\n'
                             'NGROK_AUTHTOKEN=synthetic-token-1234\nOMI_LOCAL_APP_KEY=\n')
    monkeypatch.setattr(local_mac.cli, '_service_record', lambda *a: None)
    local_env.apply(cfg, local_env.read_env(path))
    assert (cfg.layout.state_root/'pairing.json').read_bytes() == before
