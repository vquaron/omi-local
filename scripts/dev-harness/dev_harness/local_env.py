"""Private, non-executable settings for the local Mac and iPhone installer."""

import json
import os
from pathlib import Path
import re
import secrets

FIELDS = ('OMI_LOCAL_TRANSPORT', 'OMI_TAILSCALE_IP', 'NGROK_URL', 'OMI_NGROK_URL', 'NGROK_AUTHTOKEN', 'OMI_LOCAL_APP_KEY')


class LocalEnvError(ValueError):
    """Messages contain field names only, never values."""


def read_env(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise LocalEnvError('Private .env required (chmod 600); symlinks are not allowed')
    if path.stat().st_size > 16384:
        raise LocalEnvError('Local .env is too large')
    result = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        name, separator, value = line.partition('=')
        name, value = name.strip(), value.strip()
        if not separator or name not in FIELDS or name in result:
            raise LocalEnvError('Use each documented local .env field exactly once')
        if value.startswith(('"', "'")) and value.endswith(value[0]):
            value = value[1:-1]
        result[name] = value
    from .local_mac import endpoint, pairing_data
    from .local_transport import validate_tailscale_ip
    mode = result.setdefault('OMI_LOCAL_TRANSPORT', 'ngrok')
    if mode not in {'ngrok', 'tailscale'}:
        raise LocalEnvError('OMI_LOCAL_TRANSPORT must be tailscale or ngrok')
    if mode == 'ngrok':
        if result.get('NGROK_URL') and result.get('OMI_NGROK_URL'):
            raise LocalEnvError('Use only one ngrok address field')
        if 'NGROK_URL' in result:
            result['OMI_NGROK_URL'] = result.pop('NGROK_URL')
        if not all(result.get(key) for key in ('OMI_NGROK_URL', 'NGROK_AUTHTOKEN')):
            raise LocalEnvError('Local .env requires OMI_NGROK_URL and NGROK_AUTHTOKEN')
        result['OMI_NGROK_URL'] = endpoint(result['OMI_NGROK_URL'])
        if not re.fullmatch(r'[A-Za-z0-9_-]{16,256}', result['NGROK_AUTHTOKEN']):
            raise LocalEnvError('Invalid NGROK_AUTHTOKEN format')
    if result.get('OMI_TAILSCALE_IP'):
        result['OMI_TAILSCALE_IP'] = validate_tailscale_ip(result['OMI_TAILSCALE_IP'])
    if result.get('OMI_LOCAL_APP_KEY'):
        pairing_data(result['OMI_LOCAL_APP_KEY'])
    return result


def initialize(cfg):
    """Create once; never attempt to recover a key from the existing pairing hash."""
    import yaml
    path = cfg.repo_root / '.env'
    if path.exists() or path.is_symlink():
        raise LocalEnvError('.env already exists; it was not replaced')
    url_path = cfg.layout.state_root / 'ngrok.json'
    url = json.loads(url_path.read_text())['url'] if url_path.is_file() else ''
    token = ''
    for candidate in (
        cfg.layout.state_root / 'ngrok-agent.yml',
        Path.home() / 'Library/Application Support/ngrok/ngrok.yml',
        Path.home() / '.config/ngrok/ngrok.yml',
    ):
        if candidate.is_file() and not candidate.is_symlink():
            saved = yaml.safe_load(candidate.read_text()) or {}
            token = saved.get('agent', {}).get('authtoken') or saved.get('authtoken') or ''
            if token:
                break
    mode = getattr(cfg, 'local_transport', 'tailscale')
    values = {'OMI_LOCAL_TRANSPORT': mode}
    if mode == 'ngrok':
        values.update(OMI_NGROK_URL=url, NGROK_AUTHTOKEN=token)
    else:
        values['OMI_TAILSCALE_IP'] = ''
    # An existing hash cannot recover the user's key; never rotate it implicitly.
    paired = (cfg.layout.state_root / 'pairing.json').exists()
    values['OMI_LOCAL_APP_KEY'] = '' if paired else secrets.token_urlsafe(32)
    if any('\n' in value or '\r' in value for value in values.values()):
        raise LocalEnvError('Existing connection configuration has invalid values')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write('# Private local settings. Never commit or share this file.\n')
        for name, value in values.items():
            stream.write(f'{name}={value}\n')
    print('Created private .env; existing pairing preserved' if paired else
          'Created private .env; app key generated. Stop the local stack before first application.')


def apply(cfg, values):
    from .local_mac import cli, ngrok_port, pairing_data, private_json
    mode = getattr(cfg, 'local_transport', values.get('OMI_LOCAL_TRANSPORT', 'ngrok'))
    desired = {}
    if mode == 'ngrok':
        desired.update({
            'ngrok.json': {'url': values['OMI_NGROK_URL']},
            'ngrok-agent.yml': {'version': '3', 'agent': {
                'authtoken': values['NGROK_AUTHTOKEN'], 'web_addr': f'127.0.0.1:{ngrok_port(cfg)}'}},
        })
    if values.get('OMI_LOCAL_APP_KEY'):
        desired['pairing.json'] = pairing_data(values['OMI_LOCAL_APP_KEY'])
    elif not (cfg.layout.state_root / 'pairing.json').is_file():
        raise LocalEnvError('OMI_LOCAL_APP_KEY is required for first pairing')
    changed = []
    for name, value in desired.items():
        path = cfg.layout.state_root / name
        if path.is_symlink() or (path.exists() and path.stat().st_mode & 0o077):
            raise LocalEnvError('Existing local settings must be private regular files')
        if not path.exists() or json.loads(path.read_text()) != value:
            changed.append((path, value))
    if changed and any(cli._service_record(cfg, name) for name in ('backend', 'ngrok')):
        raise LocalEnvError('Stop the local stack before applying changed .env settings; current settings preserved')
    for path, value in changed:
        private_json(path, value)
    print('Private .env settings applied; secrets are not printed')
