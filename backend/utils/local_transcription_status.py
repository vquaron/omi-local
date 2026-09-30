"""Bounded, credential-free observations for the paired phone's setup screen."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from utils.env_loader import is_offline_runtime
from utils.local_transport_auth import local_tunnel_enabled
from utils.local_live_preview import preview_url


def _status(status, reason=None):
    return {'status': status, 'reason': reason}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def live_status():
    try:
        endpoint = preview_url()
        if endpoint is None:
            return _status('disabled', 'not_configured')
        port = urlsplit(endpoint).port
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        with opener.open(f'http://127.0.0.1:{port}/health', timeout=1) as response:
            if response.status != 200:
                return _status('unavailable', 'adapter_unavailable')
            raw = response.read(65537)
            if len(raw) > 65536:
                return _status('unavailable', 'invalid_health')
            health = json.loads(raw)
        if not isinstance(health, dict) or health.get('ready') is not True:
            return _status('unavailable', 'model_not_ready')
        if health.get('active') is True:
            return _status('busy', 'session_active')
        return _status('ready')
    except (ValueError, OSError, urllib.error.URLError):
        return _status('unavailable', 'adapter_unavailable')


def final_status():
    root_value = os.environ.get('OMI_HARNESS_STATE_ROOT')
    if not root_value:
        return _status('unavailable', 'missing_configuration')
    root = Path(root_value)
    try:
        settings_path = root / 'stt-watch.json'
        fd = os.open(settings_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return _status('unavailable', 'invalid_configuration')
            raw = stream.read(65537)
        if len(raw) > 65536:
            return _status('unavailable', 'invalid_configuration')
        settings = json.loads(raw)
        if not isinstance(settings, dict) or not isinstance(settings.get('enabled'), bool):
            return _status('unavailable', 'invalid_configuration')
        if not settings['enabled']:
            return _status('disabled', 'not_enabled')
        lock_path = root / 'services' / 'local-transcripts' / '.watch.lock'
        fd = os.open(lock_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd) as lock:
            if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
                return _status('unavailable', 'invalid_configuration')
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                # The owned watcher acquires this lock only after model validation.
                return _status('ready')
            fcntl.flock(lock, fcntl.LOCK_UN)
        return _status('unavailable', 'worker_stopped')
    except FileNotFoundError:
        return _status('unavailable', 'missing_configuration')
    except (ValueError, OSError):
        return _status('unavailable', 'invalid_configuration')


def local_transcription_status():
    if not is_offline_runtime() or not local_tunnel_enabled():
        return None
    return {'live': live_status(), 'final': final_status()}
