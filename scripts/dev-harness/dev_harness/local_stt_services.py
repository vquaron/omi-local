"""Owned local STT services. Live and finished-WAV providers are independent."""
from __future__ import annotations

import json
import os
import hashlib
import fcntl
from pathlib import Path
import sys
import subprocess
import time
from urllib.parse import urlsplit

import httpx

from . import config, local_openai_stt


class ServiceError(ValueError):
    """Content-free diagnostics suitable for the local launcher."""


class ActivationIndeterminate(ServiceError):
    """Runtime changed but readiness or restoration could not be verified."""


_UNSET = object()


def registry_exists(cfg):
    from .local_providers import Registry
    return Registry(cfg).exists


def snapshot_settings(snapshot):
    if snapshot is None:
        return {}
    from .local_provider_http import validate_url
    data = dict(snapshot['settings'])
    data.update(enabled=True, provider=snapshot['kind'])
    try:
        data['url'] = validate_url(data['url'], websocket=True)
        parsed = urlsplit(data['url'])
        if data['provider'] not in {'external', 'whisperlivekit'}:
            raise ValueError()
        if data['provider'] == 'whisperlivekit':
            if parsed.scheme != 'ws' or parsed.hostname != '127.0.0.1' or not parsed.port or parsed.path != '/asr':
                raise ValueError()
            if (type(data.get('chunk_seconds', 4)) not in (int, float)
                    or not 1 <= data.get('chunk_seconds', 4) <= 10
                    or data.get('language', 'ru') not in {'ru', 'en', 'auto'}):
                raise ValueError()
        diarization_settings(data)
        return data
    except (KeyError, TypeError, ValueError):
        raise ServiceError('Invalid live provider settings') from None


def settings(cfg, filename):
    path = cfg.layout.state_root / filename
    return json.loads(path.read_text()) if path.exists() else {}


def live_settings(cfg):
    if registry_exists(cfg):
        from .local_providers import Registry
        return snapshot_settings(Registry(cfg).snapshot('live'))
    data = settings(cfg, 'live-stt.json')
    if not data or not data.get('enabled', False):
        return {}
    try:
        url = urlsplit(data['url'])
        if (url.scheme != 'ws' or url.hostname != '127.0.0.1' or not url.port or url.path != '/asr'
                or url.query or url.fragment or url.username or url.password
                or data['provider'] not in {'external', 'whisperlivekit'}):
            raise ValueError()
        if data['provider'] == 'whisperlivekit':
            chunk = data.get('chunk_seconds', 4)
            if type(chunk) not in (int, float) or not 1 <= chunk <= 10:
                raise ValueError()
            if data.get('language', 'ru') not in {'ru', 'en', 'auto'}:
                raise ValueError()
        return data
    except (KeyError, TypeError, ValueError):
        raise ServiceError('Invalid local live STT settings') from None


def diarization_settings(live):
    data = live.get('diarization', {})
    if not isinstance(data, dict):
        raise ServiceError('Invalid local live diarization settings')
    if not data or not data.get('enabled', False):
        return {}
    if (type(data.get('port')) is not int or not 1024 <= data['port'] <= 65535
            or data['port'] == urlsplit(live['url']).port
            or data.get('model', 'pyannote/speaker-diarization-3.1') not in {
                'pyannote/speaker-diarization-3.1', 'pyannote/speaker-diarization-community-1'}
            or data.get('device', 'mps') not in {'cpu', 'mps'}
            or type(data.get('threads', 4)) is not int or not 1 <= data.get('threads', 4) <= 16
            or type(data.get('interval_seconds', 15)) not in (int, float)
            or not 5 <= data.get('interval_seconds', 15) <= 120
            or not isinstance(data.get('python'), str)):
        raise ServiceError('Invalid local live diarization settings')
    return data


def live_url(cfg):
    if registry_exists(cfg):
        from .local_provider_relay import url
        return url(cfg)
    live = live_settings(cfg)
    diarization = diarization_settings(live)
    return f"ws://127.0.0.1:{diarization['port']}/asr" if diarization else live.get('url', '')


def live_runtime_revision(cfg):
    digest = hashlib.sha256()
    for path in sorted((cfg.repo_root / 'scripts/local_live_preview').glob('*.py')):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def health(cfg, service, live=None):
    try:
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=2) as client:
            if service == 'provider-relay':
                from .local_provider_relay import port
                response = client.get(f'http://127.0.0.1:{port(cfg)}/health')
                data = response.json()
                return response.status_code == 200 and data.get('relay') is True and data.get('ready') is True, 'live relay readiness'
            if service in {'live-stt', 'live-diarization'}:
                live = live_settings(cfg) if live is None else live
                diarization = diarization_settings(live)
                endpoint = live.get('url', '') if service == 'live-stt' else f"ws://127.0.0.1:{diarization['port']}/asr"
                origin = urlsplit(endpoint)
                url = origin._replace(scheme='https' if origin.scheme == 'wss' else 'http', path='/health').geturl()
                response = client.get(url)
                data = response.json()
                return response.status_code == 200 and data.get('ready') is True, 'live STT readiness'
            data = settings(cfg, 'argmax-stt.json')
            local_openai_stt.check(f"http://127.0.0.1:{data['port']}/v1", data['model'])
            return True, 'Argmax model ready'
    except (httpx.HTTPError, ValueError, KeyError):
        return False, 'STT service unavailable'


def preflight_argmax(cfg):
    """Check an enabled owned server's files without requiring its HTTP listener."""
    data = settings(cfg, 'argmax-stt.json')
    if not data or data.get('enabled') is False:
        return {}
    try:
        if data.get('enabled') is not True or type(data['port']) is not int or not 1024 <= data['port'] <= 65535:
            raise ValueError()
        if not isinstance(data['model'], str) or not data['model'].strip():
            raise ValueError()
        binary = Path(data['binary'])
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise ValueError()
        for key in ('model_dir', 'tokenizer_dir'):
            if not Path(data[key]).is_dir():
                raise ValueError()
        return data
    except (KeyError, TypeError, ValueError, OSError):
        raise ServiceError('Configured Argmax files are unavailable; preserve the selected model and prepare its local files') from None


def owns_final_engine(cfg, engine):
    if engine.engine != 'openai-compatible':
        return False
    data = preflight_argmax(cfg)
    return bool(data and engine.model == data['model']
                and engine.provider_url.rstrip('/') == f"http://127.0.0.1:{data['port']}/v1")


def start_configured(cfg, *, live_override=_UNSET, include_argmax=True, start_relay=True):
    from . import cli, config
    if cfg.provider_mode != 'offline' or cfg.local_transport not in config.PAIRED_TRANSPORTS:
        return
    argmax = preflight_argmax(cfg) if include_argmax else {}
    # A legacy proxy may be repointed to the relay below. Establish that hop
    # before replacing any owned worker, including on normal harness startup.
    if start_relay and registry_exists(cfg):
        start_provider_relay(cfg)
    live = live_settings(cfg) if live_override is _UNSET else live_override
    diarization = diarization_settings(live)
    commands = []
    if argmax.get('enabled'):
        port = argmax['port']
        local_openai_stt.validate_url(f'http://127.0.0.1:{port}/v1')
        commands.append(('argmax-stt', [sys.executable, str(Path(__file__).with_name('run_argmax.py')),
                                      '--settings', str(cfg.layout.state_root / 'argmax-stt.json'),
                                      '--settings-digest', hashlib.sha256(json.dumps(argmax, sort_keys=True).encode()).hexdigest()],
                         cfg.repo_root, port))
    if live.get('provider') == 'whisperlivekit':
        python = Path(live['python']).expanduser().absolute()
        if not python.is_file():
            raise ServiceError('Live STT Python is unavailable')
        model = Path(live['model_dir']).resolve(strict=True)
        root = cfg.layout.services_dir / 'local-transcripts'
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        (root / '.lock').touch(mode=0o600, exist_ok=True)
        commands.append(('live-stt', [str(python), '-m', 'local_live_preview.serve',
                                    '--model-dir', str(model), '--inference-lock', str(root / '.lock'),
                                    '--port', str(urlsplit(live['url']).port),
                                    '--language', live.get('language', 'ru'),
                                    '--chunk-seconds', str(live.get('chunk_seconds', 4)),
                                    '--runtime-revision', live_runtime_revision(cfg)],
                         cfg.repo_root / 'scripts', urlsplit(live['url']).port))
    if diarization:
        python = Path(diarization['python']).expanduser().absolute()
        if not python.is_file():
            raise ServiceError('Live diarization Python is unavailable')
        upstream = live['url']
        if registry_exists(cfg) or live_override is not _UNSET:
            from .local_provider_relay import url
            upstream = url(cfg).removesuffix('/asr') + '/upstream'
        commands.append(('live-diarization', [str(python), '-m', 'local_live_preview.diarization_proxy',
                         '--upstream', upstream, '--port', str(diarization['port']),
                         '--model', diarization.get('model', 'pyannote/speaker-diarization-3.1'),
                         '--device', diarization.get('device', 'mps'),
                         '--threads', str(diarization.get('threads', 4)),
                         '--interval-seconds', str(diarization.get('interval_seconds', 15)),
                         '--runtime-revision', live_runtime_revision(cfg)],
                         cfg.repo_root / 'scripts', diarization['port']))
    for name, command, cwd, port in commands:
        existing = cli._service_record(cfg, name)
        if existing is not None and existing.get('command') != command:
            lock_path = cfg.layout.services_dir / 'local-transcripts/.lock'
            lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with lock_path.open('a') as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ServiceError('Stop recording and wait for final STT before changing providers') from None
                cli._stop_single_service(cfg, existing)
        env = config.child_env_for(cfg)
        env['OMI_HARNESS_PRIVATE_UMASK'] = '077'
        cli._start_process(cfg, name, command, cwd=cwd, log_name=name + '.log', port=port, env=env)
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            if health(cfg, name, live)[0]:
                break
            record = cli._service_record(cfg, name)
            if record is None:
                raise ServiceError('Local STT process exited before readiness')
            observer = subprocess.run(['ps', '-p', str(record['pid']), '-o', 'stat='],
                                      capture_output=True, text=True, timeout=5)
            if observer.returncode == 0 and observer.stdout.strip().startswith('Z'):
                raise ServiceError('Local STT process exited before readiness')
            time.sleep(1)
        else:
            raise ServiceError('Local STT startup did not become ready; inspect owned service diagnostics')


def start_provider_relay(cfg):
    from . import cli, config
    from .local_provider_relay import port
    command = [sys.executable, '-m', 'dev_harness.local_provider_relay']
    env = config.child_env_for(cfg)
    # Relay owns explicit external connections; no key appears in args or env.
    env.update(OMI_LOCAL_INSTANCE=cfg.instance, OMI_LOCAL_TRANSPORT=cfg.local_transport,
               PROVIDER_MODE=cfg.provider_mode, OMI_LOCAL_STATE_ROOT=str(cfg.layout.state_root.parent))
    for name, value in [('backend', cfg.backend_port), ('firestore', cfg.firestore_port),
                        ('auth', cfg.auth_port), ('redis', cfg.redis_port),
                        ('desktop_backend', cfg.desktop_backend_port), ('typesense', cfg.typesense_port),
                        ('llm_gateway', cfg.llm_gateway_port)]:
        env[config.PORT_OVERRIDE_ENVS[name]] = str(value)
    cli._start_process(cfg, 'provider-relay', command, cwd=cfg.repo_root,
                       log_name='provider-relay.log', port=port(cfg), env=env)
    for _ in range(30):
        if health(cfg, 'provider-relay')[0]:
            return
        time.sleep(0.2)
    raise ServiceError('Live relay did not become ready')


def _capture_status(cfg):
    from . import local_env
    data = local_env.read_env(cfg.repo_root / '.env')
    try:
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=5) as client:
            response = client.get(cfg.backend_url + '/v1/local/status',
                                  headers={'Authorization': 'Bearer ' + data['OMI_LOCAL_APP_KEY']})
            if response.status_code != 200:
                raise ValueError()
            status = response.json()
            if status['capture']['state'] != 'idle':
                raise ServiceError('Stop the current recording before applying live settings')
            return status
    except ServiceError:
        raise
    except (httpx.HTTPError, KeyError, ValueError):
        raise ServiceError('Capture idle state could not be verified') from None


def _legacy_drain_idle(cfg, status, drained=None):
    if status.get('live_transcript', {}).get('configurable'):
        return
    legacy = settings(cfg, 'live-stt.json')
    if not legacy.get('enabled'):
        return
    diarization = diarization_settings(legacy)
    endpoint = (f"ws://127.0.0.1:{diarization['port']}/asr" if diarization else legacy['url'])
    origin = urlsplit(endpoint)
    if origin.scheme != 'ws' or origin.hostname != '127.0.0.1':
        raise ServiceError('Previous live provider drain could not be verified')
    from . import cli
    name = 'live-diarization' if diarization else 'live-stt'
    record = cli._service_record(cfg, name)
    if (drained and drained[0] == name and drained[2] == origin.port and record
            and record.get('pid') != drained[1] and record.get('port') != origin.port):
        # Preparation stopped this exact owned worker and moved its listener.
        # Its old sessions ended with that process; probing the closed old port
        # would reject every valid relocation. Capture is still rechecked above.
        return drained
    try:
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=5) as client:
            result = client.get(origin._replace(scheme='http', path='/health').geturl())
            if result.status_code != 200 or result.json().get('active') is not False:
                raise ValueError()
    except (httpx.HTTPError, KeyError, ValueError):
        raise ServiceError('Wait for the previous live preview to finish before changing providers') from None
    if record and record.get('port') == origin.port:
        return name, record.get('pid'), origin.port


def activate(cfg, snapshot, *, publish, rollback=None):
    """Prepare, publish atomically while sessions are gated, then attach relay once."""
    import asyncio
    from . import cli
    from .local_provider_relay import port, probe, session_gate
    from .local_providers import Registry
    if cfg.provider_mode != 'offline' or cfg.local_transport not in config.PAIRED_TRANSPORTS:
        raise ServiceError('Live provider settings require the owned loopback stack')
    live = snapshot_settings(snapshot)
    if live:
        destination = urlsplit(live['url'])
        endpoints = [destination.port] if destination.hostname in {'127.0.0.1', 'localhost', '::1'} else []
        if diarization_settings(live):
            endpoints.append(live['diarization']['port'])
        if port(cfg) in endpoints:
            raise ServiceError('Live provider ports conflict with its relay')
    with session_gate(cfg, exclusive=True):
        status = _capture_status(cfg)
        drained = _legacy_drain_idle(cfg, status)
        previous = live_settings(cfg)
        published = False
        backend_changed = False
        preparation_started = False
        try:
            start_provider_relay(cfg)
            preparation_started = True
            start_configured(cfg, live_override=live, include_argmax=False, start_relay=False)
            if snapshot is not None:
                asyncio.run(probe(snapshot, cfg, key=Registry(cfg).key(snapshot)))
            status = _capture_status(cfg)
            _legacy_drain_idle(cfg, status, drained)
            configurable = status.get('live_transcript', {}).get('configurable')
            restart_cfg = cfg if configurable else cli.backend_restart_config(cfg)
            published = True
            publish()
            if configurable:
                return
            record = cli._service_record(cfg, 'backend')
            if record is None:
                raise ServiceError('Owned backend is unavailable')
            backend_changed = True
            cli._stop_single_service(cfg, record)
            cli._start_app_services(restart_cfg)
            for _ in range(30):
                try:
                    if _capture_status(cfg).get('live_transcript', {}).get('configurable'):
                        return
                except ServiceError:
                    pass
                time.sleep(1)
            raise ActivationIndeterminate('Backend readiness is indeterminate after applying live settings')
        except Exception:
            if published and rollback is not None:
                rollback()
            try:
                if preparation_started:
                    start_configured(cfg, live_override=previous, include_argmax=False, start_relay=False)
            except Exception:
                raise ActivationIndeterminate('Live provider restoration is indeterminate; inspect owned services') from None
            if backend_changed:
                raise ActivationIndeterminate('Backend readiness is indeterminate after applying live settings') from None
            raise


def apply(cfg):
    """Validate idle capture before restarting only the owned backend."""
    from . import cli, local_env
    data = local_env.read_env(cfg.repo_root / '.env')
    # Credentials remain transient and never enter process arguments or diagnostics.
    def require_idle():
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=5) as client:
            response = client.get(cfg.backend_url + '/v1/local/status',
                                  headers={'Authorization': 'Bearer ' + data['OMI_LOCAL_APP_KEY']})
            if response.status_code != 200 or response.json()['capture']['state'] != 'idle':
                raise ServiceError('Stop the current recording before applying STT settings')
    require_idle()
    start_configured(cfg)
    require_idle()  # Model startup may take minutes; recheck immediately before restart.
    cfg = cli.backend_restart_config(cfg)
    record = cli._service_record(cfg, 'backend')
    if record is None:
        raise ServiceError('Owned backend is unavailable')
    cli._stop_single_service(cfg, record)
    cli._start_app_services(cfg)
    for _ in range(30):
        if cli._service_health(cfg, "backend")[0]:
            return
        time.sleep(1)
    raise ServiceError("Backend readiness is indeterminate after applying STT")
