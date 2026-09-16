"""User command for the owned, loopback audio library."""

import argparse
import contextlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import webbrowser

from . import config, local_library, safety


class LauncherError(ValueError):
    pass


def install_plan(repo, *, home=None):
    home = home or Path.home()
    source = repo.resolve() / 'omiloc'
    target = home / '.local/bin/omiloc'
    if not source.is_file() or not os.access(source, os.X_OK):
        raise LauncherError('The executable omiloc entrypoint is missing from this checkout.')
    if os.path.lexists(target) and not (target.is_symlink() and target.resolve() == source):
        raise LauncherError('The path ~/.local/bin/omiloc is already occupied. Existing file preserved.')
    existing = shutil.which('omiloc')
    if existing and Path(existing).resolve() != source:
        raise LauncherError('Another omiloc command is already on PATH. Existing command preserved.')
    shell = Path(os.environ.get('SHELL', '/bin/zsh')).name
    profile = home / ('.bash_profile' if shell == 'bash' else '.zprofile')
    on_path = str(target.parent) in os.environ.get('PATH', '').split(os.pathsep)
    line = 'export PATH="$HOME/.local/bin:$PATH"'
    profile_text = profile.read_text() if profile.exists() else ''
    if not on_path and shell not in {'bash', 'zsh'}:
        raise LauncherError('Add ~/.local/bin to your shell PATH and retry installation.')
    # Check both destinations before changing either of them.
    for destination in (target.parent, profile if profile.exists() else home):
        parent = destination
        while not parent.exists():
            parent = parent.parent
        if not os.access(parent, os.W_OK):
            raise LauncherError('Cannot install the command in the user home directory.')
    return source, target, profile, line if not on_path and line not in profile_text.splitlines() else None


def install(repo, *, home=None):
    source, target, profile, line = install_plan(repo, home=home)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.is_symlink():
        target.symlink_to(source)
    if line:
        with profile.open('a') as stream:
            stream.write('\n# omiloc\n' + line + '\n')
    return target


def open_library(cfg):
    # This path never provisions a key, starts ngrok, or prints private settings.
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        local_library.start(cfg)
    address = local_library.url(cfg)
    print('omiloc · ' + address)
    if sys.stdout.isatty() and not os.environ.get('SSH_CONNECTION') and not os.environ.get('SSH_TTY'):
        if not webbrowser.open(address):
            print('Open this address in your browser.')
    return 0


def main():
    parser = argparse.ArgumentParser(prog='omiloc', description='Open the local audio library in a browser.')
    parser.add_argument('--install', action='store_true', help='Install the command for the current user.')
    args = parser.parse_args()
    try:
        repo = Path.cwd()
        if args.install:
            install(repo)
            print('omiloc installed. Open a new terminal if needed.')
        else:
            cfg = config.load_config(repo, create_layout=False)
            return open_library(cfg)
        return 0
    except (ValueError, OSError, RuntimeError, safety.SafetyError, subprocess.SubprocessError) as error:
        message = str(error) if isinstance(error, LauncherError) else (
            'Unable to open the library. Run from the checkout: ./omiloc doctor')
        print(message, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
