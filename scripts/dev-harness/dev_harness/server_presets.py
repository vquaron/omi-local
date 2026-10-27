"""Private runtime-only server presets for the local iPhone launchers."""

import base64
import json
import os
from pathlib import Path
import re
import stat

from .local_env import LocalEnvError, read_env

FILE_ENV = 'OMI_LOCAL_SERVER_PRESETS_FILE'
PAYLOAD_ENV = 'OMI_LOCAL_SERVER_PRESETS_B64'
CHILD_ENV = 'DEVICECTL_CHILD_' + PAYLOAD_ENV
MAX_BYTES = 32768
MAX_SERVERS = 16
FIELD = re.compile(r'OMI_SERVER_([a-z][a-z0-9_-]{0,31})_(NAME|URL|KEY)')
HANDOFF_FIELDS = (
    'NGROK_AUTHTOKEN', 'OMI_LOCAL_APP_KEY', 'OMI_LOCAL_MAC_KEY', 'OMI_LOCAL_MAC_URL',
    PAYLOAD_ENV, 'DEVICECTL_CHILD_NGROK_AUTHTOKEN', 'DEVICECTL_CHILD_OMI_LOCAL_APP_KEY',
    'DEVICECTL_CHILD_OMI_LOCAL_MAC_KEY', 'DEVICECTL_CHILD_OMI_LOCAL_MAC_URL', CHILD_ENV,
    'DEVICECTL_CHILD_' + FILE_ENV,
)


def clean_environment(environ):
    """Build/check processes must not inherit a previous provisioning payload."""
    result = dict(environ)
    for name in HANDOFF_FIELDS:
        result.pop(name, None)
    return result


def normalize_url(value):
    """Match the paired app's HTTP/Tailscale or public HTTPS origin shape."""
    from .local_transport import normalize_https_url, normalize_tailscale_url

    try:
        if value.startswith('https://'):
            normalized = normalize_https_url(value)
        else:
            normalized = normalize_tailscale_url(value)
        return normalized.rstrip('/') + '/'
    except (ValueError, TypeError, AttributeError):
        raise LocalEnvError('Invalid preset server address; use a Tailscale IP or HTTPS origin.') from None


def private_text(path):
    """Read one owner-only regular file without following a final symlink."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as source:
            info = os.fstat(source.fileno())
            if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_uid != os.getuid() or info.st_nlink != 1):
                raise LocalEnvError('Server presets require an owner-only regular file (chmod 600).')
            if info.st_size > MAX_BYTES:
                raise LocalEnvError('Server preset file exceeds 32 KiB.')
            content = source.read(MAX_BYTES + 1)
        if len(content) > MAX_BYTES:
            raise LocalEnvError('Server preset file exceeds 32 KiB.')
        return content.decode('utf-8')
    except LocalEnvError:
        raise
    except (OSError, UnicodeError):
        raise LocalEnvError('Cannot read private server presets; check the file and its permissions.') from None


def parse_presets(text):
    profiles = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        name, separator, value = line.partition('=')
        match = FIELD.fullmatch(name.strip())
        if not separator or match is None:
            raise LocalEnvError('Use OMI_SERVER_<id>_NAME, _URL and _KEY fields in the server preset file.')
        identity, field = match.groups()
        profile = profiles.setdefault(identity, {})
        if field in profile:
            raise LocalEnvError('Duplicate server preset field; each server ID has one NAME, URL and KEY.')
        value = value.strip()
        if value[:1] in ('"', "'") and value[-1:] == value[:1]:
            value = value[1:-1]
        profile[field] = value
        if len(profiles) > MAX_SERVERS:
            raise LocalEnvError('Server preset file supports at most 16 servers.')
    if not profiles:
        raise LocalEnvError('Server preset file must contain at least one server.')
    result = []
    for identity, profile in profiles.items():
        if set(profile) != {'NAME', 'URL', 'KEY'}:
            raise LocalEnvError('Every server preset requires NAME, URL and KEY.')
        name, key = profile['NAME'], profile['KEY']
        if not name.strip() or len(name) > 80 or any(ord(char) < 32 or ord(char) == 127 for char in name):
            raise LocalEnvError('Preset names must contain 1 to 80 printable characters.')
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}', key):
            raise LocalEnvError('Each server preset requires a 43-character app key.')
        result.append({'id': identity, 'name': name, 'url': normalize_url(profile['URL']), 'key': key})
    return result


def load_presets(root, *, environ=None, single_env_file=None, required=False):
    source = os.environ if environ is None else environ
    root = Path(root)
    selected = source.get(FILE_ENV)
    if selected is not None:
        if not selected.strip():
            raise LocalEnvError('OMI_LOCAL_SERVER_PRESETS_FILE must name a private file.')
        path = Path(selected).expanduser()
        if not path.is_absolute():
            path = root / path
        return parse_presets(private_text(path))
    path = root / '.env.servers'
    if path.exists() or path.is_symlink():
        return parse_presets(private_text(path))
    # Existing single-server settings remain a source for the same catalog
    # payload. No second native handoff protocol or embedded credentials.
    path = Path(single_env_file) if single_env_file is not None else root / '.env'
    if not path.exists() and not path.is_symlink():
        if required:
            raise LocalEnvError('Prepare private .env.servers or existing .env pairing settings first.')
        return []
    try:
        values = read_env(path)
        key = values.get('OMI_LOCAL_APP_KEY')
        if not key:
            if required:
                raise LocalEnvError('Set the existing app key in the private settings to provision the phone.')
            return []
        if values['OMI_LOCAL_TRANSPORT'] == 'tailscale':
            from .local_transport import normalize_tailscale_url, tailscale_ip
            address = tailscale_ip(values.get('OMI_TAILSCALE_IP', ''))
            port = int(source.get('OMI_HARNESS_BACKEND_PORT') or
                       8000 + int(source.get('OMI_HARNESS_PORT_OFFSET', '12000')))
            url = normalize_tailscale_url(address, port)
        else:
            url = values['OMI_NGROK_URL']
        return [{'id': 'local', 'name': 'Local Mac', 'url': normalize_url(url), 'key': key}]
    except LocalEnvError:
        raise
    except (ValueError, TypeError, KeyError):
        raise LocalEnvError('Cannot prepare the existing private pairing settings.') from None


def launch_environment(root, *, environ=None, single_env_file=None, required=False):
    """Return runtime child environment and redactions; never print either."""
    source = os.environ if environ is None else environ
    profiles = load_presets(root, environ=source, single_env_file=single_env_file, required=required)
    result = clean_environment(source)
    hidden = []
    if profiles:
        raw = json.dumps({'version': 1, 'servers': profiles}, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        if len(raw) > MAX_BYTES:
            raise LocalEnvError('Server preset payload exceeds 32 KiB.')
        payload = base64.b64encode(raw).decode('ascii')
        result[CHILD_ENV] = payload
        hidden = [payload, *[value for profile in profiles for value in (profile['url'], profile['key'])]]
    return result, hidden
