"""Compose lifecycle for the existing loopback harness, with persistent state."""
import fcntl
import getpass
import json
import os
from pathlib import Path
import secrets
import signal
import sys
import time
import threading
import urllib.request

from dev_harness import cli, config, local_env, local_library, local_mac, local_stt_services, local_stt_watch

ROOT = Path('/opt/omiloc')
DATA = Path('/data')
READY = Path('/tmp/omiloc-ready')


def write_once(path, value):
    if not path.exists():
        local_mac.private_json(path, value)


def prepare(cfg, root=ROOT, data=DATA):
    """Initialize a fresh volume; preserve all existing pairing and job settings."""
    private = data / 'connection.env'
    if not private.exists():
        fd = os.open(private, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(f'OMI_LOCAL_TRANSPORT={cfg.local_transport}\n')
            if cfg.local_transport == 'ngrok':
                stream.write('OMI_NGROK_URL=https://local.invalid\nNGROK_AUTHTOKEN=localplaceholderunused\n')
            stream.write('OMI_LOCAL_APP_KEY=' + secrets.token_urlsafe(32) + '\n')
    values = local_env.read_env(private)
    # Compose selects the active transport; changing it never replaces volume identity.
    values['OMI_LOCAL_TRANSPORT'] = cfg.local_transport
    if cfg.local_transport == 'ngrok':
        # Local-only browsing also works for a volume first created in Tailscale mode.
        # The optional tunnel still needs an explicit interactive configure step.
        values.setdefault('OMI_NGROK_URL', 'https://local.invalid')
        values.setdefault('NGROK_AUTHTOKEN', 'localplaceholderunused')
    # local_env deliberately rejects symlinks. Keep its existing private-file contract.
    target = root / '.env'
    target.write_text(''.join(f'{key}={values[key]}\n' for key in local_env.FIELDS if key in values))
    target.chmod(0o600)
    local_env.apply(cfg, values)
    if cfg.local_transport == 'ngrok':
        (data / 'tunnel-url').write_text(values['OMI_NGROK_URL'])
    write_once(cfg.layout.state_root / 'stt-engine.json', {
        'engine': 'openai-compatible', 'provider_url': 'http://127.0.0.1:10301/v1',
        'model': os.environ.get('STT_MODEL', 'Systran/faster-whisper-small'),
        'language': 'auto', 'diarization_model': 'none'})
    # The native Mac Parakeet worker is not part of the container runtime.
    write_once(cfg.layout.state_root / 'live-preview.json', {'enabled': False})
    # Enabling an existing archive must preserve the same opt-in exclusion boundary.
    write_once(cfg.layout.state_root / 'stt-watch.json', {
        'enabled': True, 'excluded': sorted(local_stt_watch.captures(cfg))})
    return values


def backend_command(cfg, reload=False):
    command = [sys.executable, '-m', 'uvicorn', 'main:app', '--host', '127.0.0.1',
               '--port', str(cfg.backend_port), '--no-access-log', '--log-level', 'warning',
               '--no-proxy-headers']
    if reload:
        command += ['--reload', '--reload-dir', str(cfg.repo_root / 'backend')]
    return command


def configure():
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError('Run configure in your own interactive terminal')
    values = local_env.read_env(DATA / 'connection.env')
    values['OMI_LOCAL_TRANSPORT'] = 'ngrok'
    values['OMI_NGROK_URL'] = local_mac.endpoint(input('Ngrok HTTPS domain: ').strip())
    values['NGROK_AUTHTOKEN'] = getpass.getpass('Ngrok token (hidden): ').strip()
    staged = DATA / 'connection.next.env'
    fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(''.join(f'{key}={values[key]}\n' for key in local_env.FIELDS if key in values))
    local_env.read_env(staged)
    staged.replace(DATA / 'connection.env')
    print('Saved. Restart the Docker stack before starting the tunnel.')


def health():
    if not READY.is_file():
        raise ValueError('Docker runtime is not ready yet.')
    cfg = config.load_config(ROOT, create_layout=False)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for url in (cfg.backend_url + '/v1/health', local_library.url(cfg)):
        with opener.open(url, timeout=3) as response:
            if response.status != 200:
                raise ValueError('A Docker service is unavailable.')
    from dev_harness.local_setup import require_transcription_ready
    require_transcription_ready(cfg)


def pair():
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError('Run pair in your own interactive terminal; keys must not enter logs.')
    values = local_env.read_env(DATA / 'connection.env')
    address = os.environ.get('OMI_PAIR_ADDRESS') or values.get('OMI_NGROK_URL')
    if not address or address == 'https://local.invalid':
        raise ValueError('Configure the ngrok connection before pairing.')
    print('Address: ' + address)
    print('App key: ' + values['OMI_LOCAL_APP_KEY'])


def supervisor_running(pid, proc_root=Path('/proc')):
    # kill(pid, 0) also succeeds for exited, unreaped children on Linux.
    try:
        state = (proc_root / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()[0]
        return state not in {'Z', 'X'}
    except FileNotFoundError:
        return False


def stop_owned(cfg):
    # Reap only our recorded supervisors. Otherwise Linux keeps exited children
    # as zombies and the harness mistakes them for services still shutting down.
    pids = [int(record['pid']) for record in cli._process_records(cfg)]
    done = threading.Event()

    def reap():
        while not done.wait(.05):
            for pid in pids:
                try:
                    os.waitpid(pid, os.WNOHANG)
                except (ChildProcessError, ProcessLookupError):
                    pass

    thread = threading.Thread(target=reap, daemon=True)
    thread.start()
    try:
        cli._stop_owned(cfg)
    finally:
        done.set()
        thread.join()


def main():
    os.umask(0o077)
    READY.unlink(missing_ok=True)
    os.chdir(ROOT)
    DATA.mkdir(exist_ok=True)
    # The lock rejects accidental duplicate runtimes on the same named volume.
    with (DATA / '.runtime.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        cfg = config.load_config(ROOT, create_layout=True)
        # Manifests describe PIDs from the previous container, not this namespace.
        cli._save_manifests(cfg, [])
        values = prepare(cfg)
        stopped = False

        def stop(_sig, _frame):
            nonlocal stopped
            stopped = True
            READY.unlink(missing_ok=True)

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        try:
            cli._start_infrastructure(cfg)
            # Wait for Firebase before importing the backend and seeding the local owner.
            deadline = time.monotonic() + 120
            while not all(cli._port_open('127.0.0.1', p) for p in
                          (cfg.firestore_port, cfg.auth_port, cfg.redis_port)):
                if stopped or time.monotonic() > deadline:
                    raise RuntimeError('Infrastructure did not become ready')
                time.sleep(.5)
            pairing = local_mac.pairing_data(values['OMI_LOCAL_APP_KEY'])
            local_mac.ensure_owner_profile(cfg, pairing['owner_uid'])
            local_stt_services.start_configured(cfg)
            cli._start_process(cfg, 'backend', backend_command(cfg, os.environ.get('OMI_DOCKER_RELOAD') == '1'),
                               cwd=ROOT / 'backend', log_name='backend.log', port=cfg.backend_port)
            failures = cli._wait_health(cfg)
            if failures:
                raise RuntimeError('Backend startup failed; inspect private harness logs')
            local_mac.require_auth_boundary(cfg.backend_url)
            local_library.start(cfg)
            # STT can load slowly on CPU. Start the web UI first and retry readiness.
            print('Library: http://127.0.0.1:21001', flush=True)
            deadline = time.monotonic() + 600
            while not stopped:
                try:
                    local_stt_watch.start_if_enabled(cfg)
                    break
                except Exception:
                    if time.monotonic() > deadline:
                        raise RuntimeError('STT startup failed; check model cache and stt container') from None
                    time.sleep(2)
            if stopped:
                return
            READY.touch(mode=0o600)
            print('Docker runtime ready', flush=True)
            while not stopped:
                # Fail visibly so Compose can restart the owned runtime after a child exits.
                records = cli._process_records(cfg)
                if any(not supervisor_running(int(r['pid'])) for r in records):
                    raise RuntimeError('An owned service stopped')
                time.sleep(1)
        finally:
            # Signal each validated supervisor once; Firebase exports before exit.
            READY.unlink(missing_ok=True)
            stop_owned(cfg)


if __name__ == '__main__':
    try:
        if sys.argv[1:] == ['configure']:
            configure()
        elif sys.argv[1:] == ['pair']:
            pair()
        elif sys.argv[1:] == ['health']:
            health()
        elif len(sys.argv) == 3 and sys.argv[1] == 'inference':
            from dev_harness.runtime_probe import docker_inference
            docker_inference(ROOT, sys.argv[2])
        elif len(sys.argv) == 1:
            main()
        else:
            raise ValueError('Unknown Docker runtime operation.')
    except Exception as error:
        print('Docker runtime operation failed: ' + type(error).__name__ +
              '. Inspect private service logs and run doctor.', file=sys.stderr)
        raise SystemExit(1)
