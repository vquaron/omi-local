"""One interactive iPhone entry point; never replace an existing local session."""

import argparse
from contextlib import contextmanager
import errno
import fcntl
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys

from .local_env import LocalEnvError


class SessionBusy(Exception):
    pass


@contextmanager
def session_lock(path=None):
    # A common path protects separate worktrees and Terminal windows too.
    path = path or Path('/tmp') / f'omi-iphone-session-{os.getuid()}.lock'
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise LocalEnvError('Cannot verify the iPhone launch lock.')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN):
                raise SessionBusy from error
            raise
        # Keep the inode: deleting a lock file can split simultaneous callers.
        yield fd
    finally:
        os.close(fd)


def _capture(args):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise LocalEnvError('Cannot inspect running processes; duplicate startup refused.') from error
    if result.returncode:
        raise LocalEnvError('Cannot inspect running processes; duplicate startup refused.')
    return result.stdout


def _candidate(command, executable=None):
    try:
        args = shlex.split(command)
    except ValueError:
        args = command.split()
    if not args:
        return False
    executable = executable or Path(args[0]).name
    if executable in ('dart', 'dartvm', 'flutter'):
        for index, arg in enumerate(args):
            if arg.endswith('flutter_tools.snapshot') or (index == 0 and executable == 'flutter'):
                return args[index + 1:index + 2] in (['run'], ['attach']) or args[index + 1:index + 3] == ['build', 'ios']
    if executable == 'xcodebuild':
        return True
    if executable.startswith('python'):
        return 'dev_harness.ios_debug' in args or any(
            arg.endswith('/run-live-iphone.py') for arg in args[1:])
    if executable in ('bash', 'sh', 'zsh'):
        return any(Path(arg).name == 'dev-iphone.command' for arg in args[1:]) or (
            'ios' in args and any(Path(arg).name == 'setup.sh' for arg in args[1:]))
    return False


def _omi_checkout(directory):
    path = Path(directory)
    return any((parent / 'docs/LOCAL_SETUP.md').is_file()
               and (parent / 'scripts/dev-harness/dev_harness/ios_debug.py').is_file()
               for parent in (path, *path.parents))


def active_sessions():
    found = []
    # Read executable names separately: ps does not quote paths containing spaces.
    executables = {}
    for line in _capture(['ps', '-axo', 'pid=,comm=']).splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            executables[int(parts[0])] = Path(parts[1]).name
    # Process arguments can contain private device details. Never return/log them.
    for line in _capture(['ps', '-axo', 'pid=,command=']).splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        pid = int(parts[0])
        if pid == os.getpid() or not _candidate(parts[1], executables.get(pid)):
            continue
        try:
            cwd = _capture(['lsof', '-a', '-p', str(pid), '-d', 'cwd', '-Fn'])
        except LocalEnvError:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue  # It exited between ps and lsof.
            raise
        directories = [value[1:] for value in cwd.splitlines() if value.startswith('n')]
        if not directories:
            raise LocalEnvError('Cannot identify the running process checkout; launch cancelled.')
        if any(_omi_checkout(directory) for directory in directories):
            found.append(pid)
    return found


def command(root, mode, *, check=False, build_only=False):
    if mode == 'debug':
        args = [sys.executable, '-m', 'dev_harness.ios_debug']
        return args + (['--check'] if check else ['--build-only'] if build_only else [])
    if build_only:
        raise LocalEnvError('--build-only is supported only in Debug mode.')
    return ['bash', str(root / 'app/setup.sh'), 'ios', 'personal'] + (['--check'] if check else [])


def launch(root, mode, *, check=False, build_only=False, lock_path=None):
    try:
        with session_lock(lock_path) as fd:
            existing = active_sessions()
            if existing:
                print('An Omi iPhone session or build is already running. Duplicate launch skipped.')
                return 0
            if mode == 'status':
                print('No active Omi iPhone sessions or builds found on this Mac.')
                return 0
            if not (check or build_only) and not sys.stdin.isatty():
                raise LocalEnvError('Run iphone.command in your own terminal for an interactive session.')
            args = command(root, mode, check=check, build_only=build_only)
            print('Omi Local Dev: Debug with hot reload.' if mode == 'debug'
                  else 'Omi Local: Profile for everyday launches from the app icon; paired local server.', flush=True)
            # The direct child retains the lock even if this wrapper exits first.
            return subprocess.run(args, cwd=root, pass_fds=(fd,)).returncode
    except SessionBusy:
        print('Another terminal owns the iPhone launcher. Duplicate launch skipped.')
        return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', nargs='?', choices=('debug', 'dev', 'profile', 'prod', 'status'))
    options = parser.add_mutually_exclusive_group()
    options.add_argument('--check', action='store_true')
    options.add_argument('--build-only', action='store_true')
    args = parser.parse_args(argv)
    mode = args.mode
    if mode is None:
        if args.check or args.build_only:
            mode = 'debug'
        elif not sys.stdin.isatty():
            parser.error('Choose debug, profile or status.')
        else:
            print('1 - Omi Local Dev: Debug with hot reload\n2 - Omi Local: Profile for everyday use\n3 - Status')
            try:
                choice = input('Choose a mode [1]: ').strip() or '1'
            except (EOFError, KeyboardInterrupt):
                print('\nLaunch cancelled.')
                return 0
            mode = {'1': 'debug', '2': 'profile', '3': 'status'}.get(choice)
            if mode is None:
                parser.error('Choose 1, 2 or 3.')
    mode = {'dev': 'debug', 'prod': 'profile'}.get(mode, mode)
    if mode == 'profile' and args.build_only:
        parser.error('--build-only is supported only in Debug mode.')
    if mode == 'status' and (args.check or args.build_only):
        parser.error('status does not accept --check or --build-only.')
    try:
        return launch(Path.cwd(), mode, check=args.check, build_only=args.build_only)
    except (LocalEnvError, OSError) as error:
        print(str(error) if isinstance(error, LocalEnvError) else 'Unable to verify iPhone launch readiness.')
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
