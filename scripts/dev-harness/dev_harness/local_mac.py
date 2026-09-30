"""Paired ngrok and Tailscale lifecycle on the owned offline harness."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import ipaddress
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlsplit

from . import cli, config, safety, local_stt, local_stt_watch, local_library, local_live, local_transcription


class LocalMacError(ValueError):
    """Static, credential-free diagnostics safe to show in the local terminal."""


def endpoint(value: str) -> str:
    uri = urlsplit(value.strip())
    if (
        uri.scheme != "https"
        or not uri.hostname
        or uri.username
        or uri.password
        or uri.port not in (None, 443)
        or uri.query
        or uri.fragment
        or uri.path not in ("", "/")
    ):
        raise LocalMacError("Enter an HTTPS server address without a path or credentials")
    if not re.fullmatch(r"[a-zA-Z0-9.-]+", uri.hostname) or "." not in uri.hostname:
        raise LocalMacError("Enter the HTTPS domain assigned by ngrok")
    try:
        ipaddress.ip_address(uri.hostname)
    except ValueError:
        pass
    else:
        raise LocalMacError("Enter the domain assigned by ngrok, not an IP address")
    return f"https://{uri.hostname.lower()}"


def private_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + ".new")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream)
        stream.write("\n")
    temp.replace(path)


def pairing_data(key: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", key):
        raise LocalMacError("Invalid access key")
    return {"version": 1, "owner_uid": "alice", "key_sha256": hashlib.sha256(key.encode()).hexdigest()}


def ngrok_port(cfg) -> int:
    # Follow the harness's existing port offset, without another public listener.
    return 4040 + cfg.backend_port - config.BACKEND_PORT


def read_config(cfg) -> dict:
    if getattr(cfg, 'local_transport', 'ngrok') == 'tailscale':
        from .local_transport import normalize_tailscale_url
        return {'url': normalize_tailscale_url(cfg.tailscale_ip, cfg.backend_port)}
    path = cfg.layout.state_root / "ngrok.json"
    if not path.is_file() or path.is_symlink() or path.stat().st_mode & 0o077:
        raise LocalMacError("Run configure in a local terminal first")
    data = json.loads(path.read_text())
    data["url"] = endpoint(data["url"])
    return data


def ngrok_env(cfg) -> dict[str, str]:
    """Read only connection inputs; never export secrets into child environments."""
    from dotenv import dotenv_values

    path = cfg.repo_root / ".env"
    try:
        if path.is_symlink():
            raise LocalMacError(".env must be a regular file, not a symlink")
        if not path.exists():
            return {}
        if not path.is_file() or path.stat().st_mode & 0o077:
            raise LocalMacError(".env must be a private regular file; run chmod 600 .env")
        values = dotenv_values(path, interpolate=False)
    except (OSError, UnicodeError):
        raise LocalMacError("Unable to read .env; check its permissions and UTF-8 encoding") from None
    return {name: (values.get(name) or "").strip() for name in ("NGROK_URL", "NGROK_AUTHTOKEN")}


def configure(cfg, *, rotate: bool = False, edit: bool = False) -> None:
    from .local_setup import show_frame
    from . import local_env

    repo = getattr(cfg, 'repo_root', None)
    env_path = repo / '.env' if repo is not None else None
    if getattr(cfg, 'local_transport', 'ngrok') == 'tailscale':
        if rotate or edit:
            raise LocalMacError('Edit the private .env while the local stack is stopped')
        if env_path is not None and env_path.is_file():
            fields = local_env.read_env(env_path) if 'OMI_LOCAL_APP_KEY' in env_path.read_text() or 'OMI_LOCAL_TRANSPORT' in env_path.read_text() else {}
            if fields:
                local_env.apply(cfg, fields)
                return
        if (cfg.layout.state_root / 'pairing.json').is_file():
            if sys.stdout.isatty():
                show_frame('IPHONE PAIRING', ['Address: ' + read_config(cfg)['url'],
                           'App key: unchanged; already saved in the app'])
            return
        if env_path is not None and not env_path.exists():
            local_env.initialize(cfg)
            local_env.apply(cfg, local_env.read_env(env_path))
            if sys.stdout.isatty():
                show_frame('IPHONE PAIRING', ['Address: ' + read_config(cfg)['url'],
                           'App key: ' + local_env.read_env(env_path)['OMI_LOCAL_APP_KEY']])
            return
        raise LocalMacError('Set OMI_LOCAL_APP_KEY in the private .env before first pairing')
    env_values = None
    if env_path is not None and (env_path.exists() or env_path.is_symlink()):
        # PR #7's generated env contains the pairing key and is applied as a
        # complete immutable bundle. The newer two-field env only supplies the
        # ngrok URL/token; pairing remains owned by the local state directory.
        raw_names = {
            line.partition('=')[0].strip()
            for line in env_path.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith('#') and '=' in line
        }
        if raw_names & {'OMI_NGROK_URL', 'OMI_LOCAL_APP_KEY'}:
            if rotate or edit:
                raise LocalMacError('Edit the private .env while the local stack is stopped')
            local_env.apply(cfg, local_env.read_env(env_path))
            return
        env_values = ngrok_env(cfg)

    needs_pairing_terminal = not (cfg.layout.state_root / 'pairing.json').is_file() or rotate
    if (env_values is None or needs_pairing_terminal) and (not sys.stdin.isatty() or not sys.stdout.isatty()):
        raise LocalMacError("Configure requires a local interactive terminal; credentials must not enter logs")
    current = cfg.layout.state_root / "ngrok.json"
    if edit and (cli._service_record(cfg, "backend") or cli._service_record(cfg, "ngrok")):
        raise LocalMacError("Stop this local stack before changing its connection")
    if not current.exists() or edit:
        values = env_values or ngrok_env(cfg)
        existing = read_config(cfg)["url"] if current.exists() else ""
        print('Ngrok: https://dashboard.ngrok.com - address under Domains, token under Your Authtoken.')
        print('For account setup, see docs/NGROK.md.')
        url = endpoint(values.get("NGROK_URL") or input(f"Ngrok HTTPS address [{existing}]: ").strip() or existing)
        token = values.get("NGROK_AUTHTOKEN") or getpass.getpass(
            "Ngrok authtoken (hidden; Enter to reuse the saved token): "
        ).strip()
        if not token:
            import yaml

            candidates = [
                cfg.layout.state_root / "ngrok-agent.yml",
                Path.home() / "Library/Application Support/ngrok/ngrok.yml",
                Path.home() / ".config/ngrok/ngrok.yml",
            ]
            for candidate in candidates:
                if candidate.is_file() and not candidate.is_symlink():
                    saved = yaml.safe_load(candidate.read_text()) or {}
                    token = saved.get("agent", {}).get("authtoken") or saved.get("authtoken") or ""
                    if token:
                        break
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", token):
            raise LocalMacError("Invalid ngrok authtoken format")
        # JSON is a YAML subset accepted by the agent, with restrictive permissions.
        private_json(
            cfg.layout.state_root / "ngrok-agent.yml",
            {
                "version": "3",
                "agent": {"authtoken": token, "web_addr": f"127.0.0.1:{ngrok_port(cfg)}"},
            },
        )
        private_json(current, {"url": url})
    pairing = cfg.layout.state_root / "pairing.json"
    if pairing.exists() and not rotate:
        show_frame('IPHONE PAIRING', ['Address: ' + read_config(cfg)['url'],
                   'App key: unchanged; already saved in the app'])
        return
    if cli._service_record(cfg, "backend") or cli._service_record(cfg, "ngrok"):
        raise LocalMacError("Stop this local stack before replacing its pairing key")
    key = secrets.token_urlsafe(32)
    private_json(pairing, pairing_data(key))
    # Deliberate one-time terminal provisioning. Never written to files or logs.
    show_frame('IPHONE PAIRING', ['Address: ' + read_config(cfg)['url'], 'App key: ' + key])
    print('Enter the address and app key under Local Mac on your iPhone.')
    print('This key is shown once. The Mac stores only its verification hash.')


def prepare_emulator(repo: Path) -> None:
    meta_path = repo / "node_modules/firebase-tools/lib/emulator/downloadableEmulatorInfo.json"
    meta = json.loads(meta_path.read_text())["firestore"]
    cache_dir = Path(os.environ.get("FIREBASE_EMULATORS_PATH") or Path.home() / ".cache/firebase/emulators")
    target = cache_dir / meta["downloadPathRelativeToCacheDir"]

    def valid(path):
        return (
            path.is_file()
            and path.stat().st_size == meta["expectedSize"]
            and hashlib.md5(path.read_bytes()).hexdigest() == meta["expectedChecksum"]
        )

    if valid(target):
        print("Pinned Firestore emulator verified; reuse")
        return
    url = meta["remoteUrl"]
    if not url.startswith("https://storage.googleapis.com/firebase-preview-drop/emulator/"):
        raise LocalMacError("Unexpected emulator download source")
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(".jar.part")
    with urllib.request.urlopen(url, timeout=30) as response, part.open("wb") as output:
        while chunk := response.read(1024 * 1024):
            output.write(chunk)
    if not valid(part):
        raise LocalMacError("Emulator archive failed upstream size/checksum verification")
    part.replace(target)
    print("Pinned Firestore emulator verified")


def check_agent(cfg) -> None:
    if not shutil.which("ngrok"):
        raise LocalMacError("ngrok is missing; run install")
    if not 1024 <= ngrok_port(cfg) <= 65535:
        raise LocalMacError("ngrok agent port is outside the supported range")
    agent_config = cfg.layout.state_root / "ngrok-agent.yml"
    if not agent_config.is_file() or agent_config.is_symlink() or agent_config.stat().st_mode & 0o077:
        raise LocalMacError("Private ngrok agent configuration is missing")
    agent = json.loads(agent_config.read_text()).get("agent", {})
    if agent.get("web_addr") != f"127.0.0.1:{ngrok_port(cfg)}":
        raise LocalMacError("Agent diagnostics must bind the owned loopback port")
    result = subprocess.run(
        ["ngrok", "config", "check", "--config", str(agent_config)], capture_output=True, timeout=15
    )
    if result.returncode:
        raise LocalMacError("ngrok configuration check failed")


def ensure_owner_profile(cfg, owner_uid: str) -> None:
    """Check live emulator state, creating only a missing paired-owner profile."""
    if cfg.local_transport not in {"ngrok", "tailscale"} or cfg.provider_mode != "offline" or cfg.dev_bind_host != "127.0.0.1":
        raise LocalMacError("Owner profile preparation requires the paired loopback stack")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", owner_uid):
        raise LocalMacError("Invalid local owner")
    collection = f"http://{cfg.firestore_host}/v1/projects/{cfg.project_id}/databases/{cfg.database_id}/documents/users"
    url = collection + "/" + quote(owner_uid, safe="")
    headers = {"Authorization": "Bearer owner", "Content-Type": "application/json"}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=5) as response:
            if response.status == 200:
                return
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise LocalMacError("Cannot check the local owner profile") from None
    # A stale scenario manifest cannot prove that Firestore still has the user.
    # Create is atomic and never overwrites an existing profile or its settings.
    payload = {"fields": {
        "uid": {"stringValue": owner_uid},
        "display_name": {"stringValue": "Local Mac"},
        "synthetic": {"booleanValue": True},
        "local_harness": {"booleanValue": True},
    }}
    request = urllib.request.Request(
        collection + "?documentId=" + quote(owner_uid, safe=""),
        data=json.dumps(payload).encode(), headers=headers, method="POST",
    )
    try:
        urllib.request.urlopen(request, timeout=5).close()
    except urllib.error.HTTPError as error:
        if error.code != 409:
            raise LocalMacError("Cannot prepare the local owner profile") from None
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=5) as response:
        if response.status != 200:
            raise LocalMacError("Local owner profile readiness is indeterminate")
    print("Local owner profile restored; existing data and pairing preserved")


def up(cfg) -> int:
    data = read_config(cfg)
    if cfg.local_transport == 'ngrok':
        check_agent(cfg)
    local_live.require_backend_environment(cfg)
    local_transcription.check_models(cfg)
    from .local_stt_services import health as stt_health
    from .local_stt_services import live_url, start_configured
    configured_live_url = live_url(cfg)
    if not configured_live_url:
        local_live.preflight_start(cfg)
    sys.path.insert(0, str(cfg.repo_root / "backend"))
    from utils.local_transport_auth import load_pairing

    os.environ["OMI_LOCAL_PAIRING_FILE"] = str(cfg.layout.state_root / "pairing.json")
    pairing = load_pairing()
    if cli.cmd_check(argparse.Namespace()):
        return 1
    if not configured_live_url:
        local_live.start(cfg)
    start_configured(cfg)
    local_transcription.check_service_models(cfg)
    if cli.cmd_up(argparse.Namespace()):
        return 1
    ensure_owner_profile(cfg, pairing["owner_uid"])
    # Require actual readiness, not a process declaration.
    with urllib.request.urlopen(cfg.backend_url + "/v1/health", timeout=5) as response:
        if response.status != 200:
            raise LocalMacError("Backend is not ready")
    require_auth_boundary(cfg.backend_url)
    if cfg.local_transport == 'tailscale':
        # Bypass system proxies; a redirect is not evidence of this listener.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        # The second listener is on the verified tailnet address, never a wildcard.
        with opener.open(data['url'] + '/v1/health', timeout=5) as response:
            if response.status != 200:
                raise LocalMacError('Tailscale backend is not ready')
        require_auth_boundary(data['url'], open_url=opener.open)
        local_stt_watch.start_if_enabled(cfg)
        if configured_live_url:
            if not stt_health(cfg, 'live-stt')[0]:
                raise LocalMacError('Configured live STT is not ready; inspect its owned service')
        else:
            local_live.require_ready(cfg)
        print('Tailscale listener and key boundary passed; phone recording remains a separate check')
        return 0
    cli._start_process(
        cfg,
        "ngrok",
        [
            "ngrok",
            "http",
            cfg.backend_url,
            "--url",
            data["url"],
            "--inspect=false",
            "--config",
            str(cfg.layout.state_root / "ngrok-agent.yml"),
            "--log",
            "stdout",
            "--log-level",
            "warn",
        ],
        cwd=cfg.repo_root,
        log_name="ngrok.log",
        port=ngrok_port(cfg),
    )
    for _ in range(20):
        if cli._service_health(cfg, "ngrok")[0]:
            try:
                with urllib.request.urlopen(data["url"] + "/v1/health", timeout=5) as response:
                    if response.status == 200:
                        require_auth_boundary(data["url"])
                        print("Public HTTPS health passed; run audio-smoke to verify authenticated WSS")
                        local_stt_watch.start_if_enabled(cfg)
                        if configured_live_url:
                            if not stt_health(cfg, "live-stt")[0]:
                                raise LocalMacError("Configured live STT is not ready; inspect its owned service")
                        else:
                            local_live.require_ready(cfg)
                        return 0
            except (OSError, urllib.error.URLError):
                pass
        time.sleep(1)
    raise LocalMacError("Tunnel readiness is indeterminate; inspect the ngrok account and owned process")


def require_auth_boundary(base_url: str, *, open_url=None) -> None:
    """Observe the live boundary; a healthy legacy dev server is insufficient."""
    open_url = open_url or urllib.request.urlopen
    for headers in ({}, {"Authorization": "Bearer " + secrets.token_urlsafe(32)}):
        request = urllib.request.Request(base_url + "/openapi.json", headers=headers)
        try:
            open_url(request, timeout=5).close()
        except urllib.error.HTTPError as error:
            if error.code == 401 and json.load(error).get("detail") == "local_auth_required":
                continue
        raise LocalMacError("Live local key boundary was not verified; tunnel startup stopped")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=[
            "prepare-emulator",
            "init-env",
            "launch-iphone",
            "check",
            "configure",
            "edit-connection",
            "rotate-key",
            "up",
            "status",
            "down",
            "audio-smoke",
            "transcribe",
            "auto-transcribe-on",
            "auto-transcribe-off",
            "transcription-status",
            "apply-stt",
            "library",
            "start",
            "setup-check",
        ],
    )
    parser.add_argument('audio', nargs='?')
    args = parser.parse_args()
    if (args.command == 'transcribe') != (args.audio is not None):
        parser.error('transcribe requires a WAV path; other commands take no audio argument')
    try:
        repo = Path.cwd()
        if args.command == "prepare-emulator":
            prepare_emulator(repo)
            return 0
        from .local_transport import prepare_environment
        prepare_environment(repo, verify=args.command in {'configure', 'up', 'start', 'check', 'setup-check', 'audio-smoke'})
        cfg = config.load_config(repo, create_layout=args.command in {"configure", "edit-connection", "rotate-key"})
        if args.command == 'init-env':
            from . import local_env
            local_env.initialize(cfg)
        elif args.command == 'launch-iphone':
            from . import launch_iphone
            launch_iphone.launch(cfg.repo_root / '.env', cfg.repo_root / 'app/build/ios/Profile-dev-iphoneos/Runner.app')
        elif args.command == "check":
            if cfg.local_transport == "ngrok" and not shutil.which("ngrok"):
                raise LocalMacError("ngrok is missing; run install")
            return cli.cmd_check(argparse.Namespace())
        if args.command in {"configure", "edit-connection", "rotate-key"}:
            configure(cfg, rotate=args.command == "rotate-key", edit=args.command == "edit-connection")
        elif args.command == "up":
            return up(cfg)
        elif args.command == "status":
            return cli.cmd_status(argparse.Namespace(write_summary=False))
        elif args.command == "library":
            local_library.start(cfg)
            print(local_library.url(cfg))
        elif args.command in {"start", "setup-check"}:
            from . import local_setup
            try:
                if args.command == "setup-check":
                    local_setup.check(cfg)
                    local_setup.require_transcription_ready(cfg)
                    print('Native readiness verified. No changes made.')
                    return 0
                return local_setup.run(cfg)
            except local_setup.SetupError as error:
                print(str(error), file=sys.stderr)
                return 1
        elif args.command == "down":
            return cli.cmd_down(argparse.Namespace())
        elif args.command == "transcribe":
            return local_stt.transcribe(cfg, args.audio)
        elif args.command == "auto-transcribe-on":
            return local_stt_watch.enable(cfg)
        elif args.command == "auto-transcribe-off":
            return local_stt_watch.disable(cfg)
        elif args.command == "apply-stt":
            from .local_stt_services import apply
            apply(cfg)
            return 0
        elif args.command == "transcription-status":
            return local_stt_watch.status(cfg)
        elif args.command == "audio-smoke":
            key = getpass.getpass("App access key (hidden): ")
            pairing_data(key)
            read_fd, write_fd = os.pipe()
            try:
                os.write(write_fd, key.encode())
                os.close(write_fd)
                env = {
                    **os.environ,
                    "OMI_AUDIO_ACCESS_KEY_FD": str(read_fd),
                    "OMI_AUDIO_BASE_URL": read_config(cfg)["url"],
                }
                return subprocess.run(
                    ["bash", "scripts/dev-harness/audio-capture-smoke.sh"], env=env, pass_fds=(read_fd,)
                ).returncode
            finally:
                os.close(read_fd)
        return 0
    except local_stt.NoSpeechDetected:
        print('No speech detected. Recording preserved.')
        return 0
    except (ValueError, TypeError, OSError, KeyError, safety.SafetyError, subprocess.SubprocessError) as error:
        # Error text from external tools can contain credentials or account IDs.
        from .local_env import LocalEnvError
        from .local_transport import TransportError
        from .local_stt_services import ServiceError
        message = (
            str(error)
            if isinstance(error, (LocalMacError, LocalEnvError, TransportError, local_stt.TranscriptionError, ServiceError,
                                  local_live.LocalLiveError, local_transcription.TranscriptionSetupError))
            else f"Check prerequisites and local configuration ({type(error).__name__})"
        )
        print(f"Local Mac operation failed: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
