"""Local connection selection and verified Tailscale host discovery.

Only explicit paired transports enter here; the development LAN profile is separate.
Diagnostics never include CLI output, device names, addresses or account details.
"""

import ipaddress
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.parse import urlsplit


class TransportError(ValueError):
    pass


def validate_tailscale_ip(value):
    try:
        address = ipaddress.IPv4Address(value)
        if address not in ipaddress.IPv4Network('100.64.0.0/10'):
            raise ValueError
        return str(address)
    except (ValueError, TypeError):
        raise TransportError('Enter a Tailscale IPv4 address in 100.64.0.0/10') from None


def normalize_tailscale_url(value, default_port=20000):
    try:
        value = value.strip()
        uri = urlsplit(value if '://' in value else 'http://' + value)
        if (uri.scheme != 'http' or uri.username is not None or uri.password is not None
                or uri.query or uri.fragment or uri.path not in ('', '/')
                or '?' in value or '#' in value):
            raise ValueError
        host = validate_tailscale_ip(uri.hostname)
        port = uri.port if uri.port is not None else (80 if '://' in value else default_port)
        if not 1 <= port <= 65535 or uri.netloc.endswith(':'):
            raise ValueError
        return f'http://{host}:{port}'
    except (ValueError, TypeError, AttributeError):
        raise TransportError('Enter a Tailscale IPv4 address with an optional valid port') from None


def connection_fields(repo):
    """Read selection before dependencies; never execute or interpolate .env."""
    path = Path(repo) / '.env'
    if not path.exists() and not path.is_symlink():
        return {}
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise TransportError('Local .env must be a private regular file (chmod 600)')
    if path.stat().st_size > 16384:
        raise TransportError('Local .env is too large')
    fields = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        name, sep, value = line.partition('=')
        name, value = name.strip(), value.strip()
        if not sep or name in fields:
            raise TransportError('Use each local .env field once')
        if value[:1] in ('"', "'") and value[-1:] == value[:1]:
            value = value[1:-1]
        fields[name] = value
    return fields


def selection(repo, environ=None):
    env = os.environ if environ is None else environ
    fields = connection_fields(repo)
    state_base = Path(env.get('OMI_LOCAL_STATE_ROOT') or Path(repo) / '.local/dev-harness')
    saved = state_base / env.get('OMI_LOCAL_INSTANCE', 'ngrok') / 'ngrok.json'
    legacy = bool(fields.keys() & {'NGROK_URL', 'OMI_NGROK_URL', 'NGROK_AUTHTOKEN'}) or saved.is_file()
    mode = env.get('OMI_LOCAL_TRANSPORT') or fields.get('OMI_LOCAL_TRANSPORT') or ('ngrok' if legacy else 'tailscale')
    if mode not in {'ngrok', 'tailscale'}:
        raise TransportError('OMI_LOCAL_TRANSPORT must be tailscale or ngrok')
    return mode


def tailscale_ip(value=''):
    executable = shutil.which('tailscale')
    bundled = Path('/Applications/Tailscale.app/Contents/MacOS/Tailscale')
    if not executable and bundled.is_file():
        executable = str(bundled)
    if not executable:
        raise TransportError('Install Tailscale and connect this Mac before starting')
    try:
        result = subprocess.run([executable, 'status', '--json'], capture_output=True, text=True, timeout=15)
        status = json.loads(result.stdout) if result.returncode == 0 else {}
        if status.get('BackendState') != 'Running' or not (status.get('Self') or {}).get('Online'):
            raise ValueError
        addresses = (status.get('Self') or {}).get('TailscaleIPs', [])
        address = next(ip for ip in addresses if ':' not in ip)
        address = validate_tailscale_ip(address)
        if value and validate_tailscale_ip(value) != address:
            raise TransportError('OMI_TAILSCALE_IP does not belong to this connected device')
        return address
    except TransportError:
        raise
    except (OSError, ValueError, TypeError, AttributeError, StopIteration, subprocess.SubprocessError):
        raise TransportError('Cannot verify Tailscale; open Tailscale and check its connection') from None


def prepare_environment(repo, *, verify=False):
    mode = selection(repo)
    os.environ['OMI_LOCAL_TRANSPORT'] = mode
    if mode == 'tailscale':
        fields = connection_fields(repo)
        requested = os.environ.get('OMI_TAILSCALE_IP') or fields.get('OMI_TAILSCALE_IP', '')
        if requested:
            requested = validate_tailscale_ip(requested)
        if verify:
            requested = tailscale_ip(requested)
        if requested:
            os.environ['OMI_TAILSCALE_IP'] = requested
    return mode


if __name__ == '__main__':
    try:
        print(selection(Path.cwd()))
    except (TransportError, OSError, UnicodeError) as error:
        print(str(error) if isinstance(error, TransportError) else 'Cannot read local settings', file=sys.stderr)
        raise SystemExit(1)
