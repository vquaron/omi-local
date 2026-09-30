import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def installation(tmp_path):
    root = Path(__file__).resolve().parents[3]
    repo = tmp_path / 'project with spaces'
    (repo / 'scripts').mkdir(parents=True)
    for name in ('scripts/install-local-mac.sh', 'scripts/macos-runtime.sh', 'scripts/native-runtime.sh', 'omiloc', 'start.command'):
        shutil.copy2(root / name, repo / name)
    for name in ('backend/.python-version', 'backend/pylock.macos.toml', 'package.json',
                 'package-lock.json', 'firebase.json', 'web-local/index.html'):
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('fixture input')
    # Executables can already exist in an interrupted, incomplete installation.
    for name in ('backend/.venv/bin/python', 'node_modules/.bin/firebase'):
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('#!/bin/bash\nif [[ "$2" == dev_harness.local_transport ]]; then echo ngrok; exit; fi\nif [[ "$2" == dev_harness.local_setup ]]; then echo "$3" >> "$OMI_TEST_EVENTS"; exit; fi\necho emulator >> "$OMI_TEST_EVENTS"\n')
        target.chmod(0o700)
    sync = repo / 'backend/scripts/sync-python-deps.sh'
    sync.parent.mkdir()
    sync.write_text('''
echo sync >> "$OMI_TEST_EVENTS"
[[ "$OMI_TEST_FAIL" != sync ]] || exit 9
[[ "$OMI_TEST_FAIL" != kill ]] || kill -KILL $$
''')
    (repo / 'scripts/local-mac.sh').write_text('''
echo "$1" >> "$OMI_TEST_EVENTS"
[[ "$1" != check || "$OMI_TEST_FAIL" != check ]] || exit 9
''')
    shell_env = tmp_path / 'shell-env'
    shell_env.write_text('''
uname() { case "$1" in -s) echo Darwin;; -m) echo arm64;; esac; }
brew() { if [[ "$1" == --prefix ]]; then echo "$OMI_TEST_PREFIX"; fi; }
java() { echo 'openjdk version "21.0.6"' >&2; }
uv() { :; }
node() { :; }
redis-server() { :; }
ffmpeg() { :; }
ngrok() { :; }
jq() { :; }
npm() {
  echo npm >> "$OMI_TEST_EVENTS"
  [[ "$OMI_TEST_FAIL" != npm ]] || return 9
}
''')
    events = tmp_path / 'events'
    env = {**os.environ, 'BASH_ENV': str(shell_env), 'OMI_TEST_EVENTS': str(events),
           'OMI_TEST_PREFIX': str(tmp_path / 'brew'), 'OMI_TEST_FAIL': '', 'PYTHON': str(repo / 'backend/.venv/bin/python')}
    return repo, env, events


def start(repo, env):
    import pty
    master, slave = pty.openpty()
    try:
        process = subprocess.Popen(['bash', 'start.command'], cwd=repo, env=env,
                                   stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        slave = None
        # A failed interactive setup now offers retry; choose exit for this test.
        os.write(master, b'q\n')
        return process.wait(timeout=15)
    finally:
        os.close(master)
        if slave is not None:
            os.close(slave)


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS terminal and native lockf')
@pytest.mark.parametrize('failure', ['sync', 'npm', 'check', 'kill'])
def test_start_recovers_interruption_before_marking_install_complete(installation, failure):
    repo, env, events = installation
    recording = repo / '.local/recordings/preserved.txt'
    recording.parent.mkdir(parents=True)
    recording.write_text('synthetic data must survive setup')
    assert start(repo, {**env, 'OMI_TEST_FAIL': failure}) != 0
    assert not (repo / '.local/install.ready').exists()
    assert 'up' not in events.read_text().splitlines()
    assert start(repo, env) == 0
    assert (repo / '.local/install.ready').is_file()
    assert events.read_text().splitlines()[-3:] == ['bootstrap', 'up', 'open']
    before = events.read_text()
    assert start(repo, env) == 0
    assert events.read_text() == before + 'bootstrap\nup\nopen\n'
    (repo / 'backend/pylock.macos.toml').write_text('updated dependencies')
    assert start(repo, env) == 0
    assert events.read_text()[len(before):].splitlines().count('sync') == 1
    assert recording.read_text() == 'synthetic data must survive setup'


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS terminal and native lockf')
def test_manual_retry_invalidates_previous_success_before_changes(installation):
    repo, env, _ = installation
    assert start(repo, env) == 0
    failed = subprocess.run(['bash', 'scripts/install-local-mac.sh', '--quiet'], cwd=repo,
                            env={**env, 'OMI_TEST_FAIL': 'sync'}, capture_output=True)
    assert failed.returncode == 9
    assert not (repo / '.local/install.ready').exists()
    assert start(repo, env) == 0


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS native lockf')
def test_second_installer_does_not_invalidate_an_active_install(installation):
    import fcntl
    repo, env, events = installation
    local = repo / '.local'
    local.mkdir()
    ready = local / 'install.ready'
    ready.write_text('existing completed installation')
    with (local / 'install.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        blocked = subprocess.run(['bash', 'scripts/install-local-mac.sh', '--quiet'],
                                 cwd=repo, env=env, capture_output=True, text=True)
        assert blocked.returncode != 0
        assert 'already running' in blocked.stderr
        assert not events.exists()
        assert ready.read_text() == 'existing completed installation'


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS terminal and native lockf')
def test_start_waits_then_retries_installation_and_checks_before_launch(installation):
    import pty
    import select
    import time

    repo, env, events = installation
    (repo / 'backend/scripts/sync-python-deps.sh').write_text('''
echo sync >> "$OMI_TEST_EVENTS"
if [[ ! -f .local/retry-allowed ]]; then exit 9; fi
''')
    master, slave = pty.openpty()
    process = subprocess.Popen(['bash', 'start.command'], cwd=repo, env=env,
                               stdin=slave, stdout=slave, stderr=slave)
    os.close(slave)
    output = b''
    try:
        deadline = time.monotonic() + 10
        while 'q to exit: '.encode() not in output and time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                output += os.read(master, 65536)
        assert 'Enter to retry'.encode() in output
        assert not (repo / '.local/install.ready').exists()
        assert 'up' not in events.read_text().splitlines()
        (repo / '.local/retry-allowed').touch()
        os.write(master, b'\n')
        assert process.wait(timeout=10) == 0
        assert (repo / '.local/install.ready').is_file()
        assert events.read_text().splitlines() == ['sync', 'sync', 'npm', 'emulator', 'check', 'bootstrap', 'up', 'open']
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)
