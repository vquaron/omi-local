"""Short terminal UX over the existing checked, owned Mac lifecycle."""

import argparse
import contextlib
import io
import os
import shutil
import sys
import subprocess
from pathlib import Path
import webbrowser

from . import cli, config, local_launcher, local_library, local_stt_watch, local_live, local_transcription


class SetupError(ValueError):
    pass


def frame(title, lines):
    width = max(len(title), *(len(line) for line in lines)) + 4
    border = '─' * width
    return '\n'.join(['┌' + border + '┐', '│  ' + title.ljust(width - 2) + '│',
                      '├' + border + '┤', *['│  ' + line.ljust(width - 2) + '│' for line in lines],
                      '└' + border + '┘'])


def show_frame(title, lines):
    output = frame(title, lines)
    if sys.stdout.isatty() and 'NO_COLOR' not in os.environ and os.environ.get('TERM') != 'dumb':
        output = '\033[1m' + output + '\033[0m'
    print(output)


def check(cfg, *, preparing=False):
    if getattr(cfg, 'local_transport', 'ngrok') == 'ngrok' and not shutil.which('ngrok'):
        raise SetupError('ngrok is missing. Run: ./omiloc bootstrap')
    # Capture diagnostic chatter in memory; credentials are never provisioned here.
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        missing, _warnings = cli.prerequisite_report(cfg)
    if missing:
        raise SetupError('Native prerequisites failed. Run: ./omiloc bootstrap')
    if not all((cfg.repo_root / 'web-local' / f).is_file() for f in ('index.html', 'style.css', 'app.js', 'player.mjs', 'live.mjs', 'reload.mjs')):
        raise SetupError('Library assets are missing. Restore the project checkout.')
    try:
        local_launcher.install_plan(cfg.repo_root)
    except local_launcher.LauncherError as error:
        raise SetupError(str(error)) from error
    if not preparing:
        local_transcription.check_models(cfg)
    return 0


def require_transcription_ready(cfg):
    local_live.require_ready(cfg)
    if local_stt_watch.settings(cfg)['enabled'] and not local_stt_watch.worker_ready(cfg):
        raise SetupError('Final transcription is not ready. Run: ./omiloc status and ./omiloc logs')


def bootstrap(cfg):
    """Prepare dependencies/models/settings without leaving services running."""
    from . import local_mac

    print('Checking native prerequisites...', flush=True)
    check(cfg, preparing=True)
    local_live.require_backend_environment(cfg)
    local_live.preflight_start(cfg)
    local_transcription.prepare(cfg)
    cfg = config.load_config(cfg.repo_root, create_layout=True)
    local_transcription.configure_defaults(cfg)
    local_mac.configure(cfg)
    local_launcher.install(cfg.repo_root)
    print('Native bootstrap complete. Run: ./omiloc up')
    return 0


def run(cfg, *, open_browser=False):
    """Start already-prepared services. No installs, downloads or new pairing."""
    from . import local_mac

    print('Checking native readiness...', flush=True)
    check(cfg)
    if not (cfg.layout.state_root / 'pairing.json').is_file():
        raise SetupError('Pairing is not prepared. Run: ./omiloc bootstrap')
    print('Starting native services...', flush=True)
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        if local_mac.up(cfg):
            raise SetupError('Startup failed. Run: ./omiloc doctor and ./omiloc logs')
        require_transcription_ready(cfg)
        local_library.start(cfg)
    print('Native services ready. You can close this terminal.')
    show_frame('AUDIO LIBRARY', ['omiloc', local_library.url(cfg)])
    print('Stop services: ./omiloc down')
    if open_browser and sys.stdout.isatty() and not os.environ.get('SSH_CONNECTION'):
        webbrowser.open(local_library.url(cfg))
    return 0


def pair(cfg):
    from . import local_env, local_mac

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise SetupError('Run pair in your own interactive terminal; keys must not enter logs.')
    values = local_env.read_env(cfg.repo_root / '.env')
    key = values.get('OMI_LOCAL_APP_KEY')
    if not key:
        raise SetupError('The existing key is stored only on your phone. It cannot be recovered from its hash.')
    # Never show a draft key that does not match this runtime's existing identity.
    import json
    saved = json.loads((cfg.layout.state_root / 'pairing.json').read_text())
    if local_mac.pairing_data(key) != saved:
        raise SetupError('Private settings differ from the prepared pairing. Run bootstrap while stopped.')
    show_frame('IPHONE PAIRING', ['Address: ' + local_mac.read_config(cfg)['url'], 'App key: ' + key])
    return 0


def status(cfg):
    records = cli._process_records(cfg)
    if not records:
        print('Native services: stopped. Run: ./omiloc up')
        return 1
    healthy = True
    for record in records:
        name = record['service']
        ok, _ = cli._service_health(cfg, name)
        print(f'{name}: ' + ('ready' if ok else 'unavailable'))
        healthy = healthy and ok
    try:
        require_transcription_ready(cfg)
    except (ValueError, OSError):
        healthy = False
        print('Configured transcription: unavailable')
    return 0 if healthy else 1


def logs(cfg, follow=False):
    paths = sorted(cfg.layout.logs_dir.glob('*.log'))
    paths = [p for p in paths if p.is_file() and not p.is_symlink()]
    if not paths:
        print('No native service logs yet.')
        return 0
    return subprocess.call(['tail', '-n', '80', *(['-f'] if follow else []), *map(str, paths)])


def main(argv=None):
    parser = argparse.ArgumentParser(description='Native Omi Local lifecycle.')
    parser.add_argument('command', choices=('bootstrap', 'up', 'down', 'status', 'logs', 'doctor', 'open', 'pair', 'install'))
    parser.add_argument('-f', action='store_true', help='Follow service logs.')
    parser.add_argument('--inference', action='store_true', help='Transcribe synthetic speech without adding a recording.')
    args = parser.parse_args(['install'] if argv == ['--install'] else argv)
    if args.f and args.command != 'logs' or args.inference and args.command != 'doctor':
        parser.error('Use -f with logs and --inference with doctor.')
    try:
        from .local_transport import prepare_environment
        prepare_environment(Path.cwd(), verify=args.command in {'bootstrap', 'up', 'doctor'})
        cfg = config.load_config(Path.cwd(), create_layout=False)
        if args.command == 'bootstrap':
            return bootstrap(cfg)
        if args.command == 'up':
            return run(cfg)
        if args.command == 'down':
            return cli.cmd_down(argparse.Namespace())
        if args.command == 'status':
            return status(cfg)
        if args.command == 'logs':
            return logs(cfg, args.f)
        if args.command == 'pair':
            return pair(cfg)
        if args.command == 'install':
            local_launcher.install(cfg.repo_root)
            print('omiloc installed. Open a new terminal if needed.')
            return 0
        if args.command == 'open':
            return local_launcher.open_library(cfg)
        check(cfg)
        print('Native prerequisites and configured models passed.')
        if args.inference:
            from .runtime_probe import native_inference
            native_inference(cfg)
        else:
            print('Run doctor --inference to verify synthetic transcription.')
        return 0
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        # Application errors are deliberately static; low-level exceptions may contain secrets.
        message = str(error) if isinstance(error, ValueError) else 'Native operation failed. Run ./omiloc doctor and inspect private logs.'
        print(message, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
