import io
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dev_harness import local_setup, local_mac


class Terminal(io.StringIO):
    def isatty(self):
        return True


def test_start_stops_at_first_failed_precondition(monkeypatch):
    events = []
    monkeypatch.setattr(local_setup.sys, 'stdin', Terminal())
    monkeypatch.setattr(local_setup.sys, 'stdout', Terminal())
    def fail(_cfg, **_kwargs):
        raise local_setup.SetupError('preflight failed')
    monkeypatch.setattr(local_setup, 'check', fail)
    monkeypatch.setattr(local_setup.config, 'load_config', lambda *a, **k: events.append('configure'))
    monkeypatch.setattr(local_mac, 'up', lambda *_: events.append('up'))
    with pytest.raises(local_setup.SetupError, match='preflight failed'):
        local_setup.run(SimpleNamespace(repo_root=Path('.')))
    assert events == []


@pytest.mark.parametrize('operation', ['bootstrap', 'up'])
def test_bootstrap_and_start_have_separate_effects(monkeypatch, tmp_path, operation):
    output, events = Terminal(), []
    (tmp_path / 'pairing.json').write_text('{}')
    cfg = SimpleNamespace(repo_root=tmp_path, backend_port=20000,
                          layout=SimpleNamespace(state_root=tmp_path))
    monkeypatch.setattr(local_setup.sys, 'stdout', output)
    monkeypatch.setattr(local_setup, 'check', lambda _, **kwargs: events.append('check'))
    monkeypatch.setattr(local_setup.local_live, 'require_backend_environment', lambda _: None)
    monkeypatch.setattr(local_setup.local_live, 'preflight_start', lambda _: None)
    monkeypatch.setattr(local_setup.local_transcription, 'prepare', lambda _: events.append('prepare'))
    monkeypatch.setattr(local_setup.local_transcription, 'configure_defaults', lambda _: events.append('defaults'))
    monkeypatch.setattr(local_setup, 'require_transcription_ready', lambda _: events.append('ready'))
    monkeypatch.setattr(local_setup.local_launcher, 'install', lambda _: events.append('install'))
    monkeypatch.setattr(local_setup.config, 'load_config', lambda *a, **k: cfg)
    monkeypatch.setattr(local_mac, 'configure', lambda _: events.append('configure'))
    monkeypatch.setattr(local_mac, 'up', lambda _: events.append('up') or 0)
    monkeypatch.setattr(local_setup.local_library, 'start', lambda _: events.append('library'))
    monkeypatch.setattr(local_setup.webbrowser, 'open', lambda _: pytest.fail('browser must be explicit'))
    if operation == 'bootstrap':
        assert local_setup.bootstrap(cfg) == 0
        assert events == ['check', 'prepare', 'defaults', 'configure', 'install']
    else:
        assert local_setup.run(cfg) == 0
        assert events == ['check', 'up', 'ready', 'library']
        assert 'http://127.0.0.1:20001' in output.getvalue()
        assert 'Native services ready' in output.getvalue()
    assert len(output.getvalue().splitlines()) < 20


def test_pair_refuses_noninteractive_secret_output(monkeypatch):
    monkeypatch.setattr(local_setup.sys, 'stdin', io.StringIO())
    with pytest.raises(local_setup.SetupError, match='interactive terminal'):
        local_setup.pair(SimpleNamespace())


def test_setup_does_not_open_library_or_claim_success_when_live_is_unavailable(monkeypatch, tmp_path):
    from dev_harness import config

    cfg = config.load_config(tmp_path, {'PROVIDER_MODE': 'offline', 'OMI_LOCAL_TRANSPORT': 'ngrok',
                                       'OMI_DEV_BIND_HOST': '127.0.0.1'}, create_layout=True)
    (cfg.layout.state_root / 'pairing.json').write_text('{}')
    output = Terminal()
    monkeypatch.setattr(local_setup.sys, 'stdin', Terminal())
    monkeypatch.setattr(local_setup.sys, 'stdout', output)
    monkeypatch.setattr(local_setup, 'check', lambda *_a, **_k: None)
    monkeypatch.setattr(local_setup.local_live, 'require_backend_environment', lambda _: None)
    monkeypatch.setattr(local_setup.local_live, 'preflight_start', lambda _: None)
    monkeypatch.setattr(local_setup.local_transcription, 'prepare', lambda _: None)
    monkeypatch.setattr(local_setup.local_transcription, 'configure_defaults', lambda _: None)
    monkeypatch.setattr(local_setup.local_launcher, 'install', lambda _: None)
    monkeypatch.setattr(local_setup.config, 'load_config', lambda *a, **k: cfg)
    monkeypatch.setattr(local_mac, 'configure', lambda _: None)
    monkeypatch.setattr(local_mac, 'up', lambda _: 0)
    monkeypatch.setattr(local_setup.local_live, 'health', lambda _: {'ready': False, 'active': False})
    monkeypatch.setattr(local_setup.local_library, 'start', lambda _: pytest.fail('cannot publish readiness'))
    monkeypatch.setattr(local_setup.webbrowser, 'open', lambda _: pytest.fail('cannot open successful setup'))
    with pytest.raises(local_setup.local_live.LocalLiveError, match='not ready'):
        local_setup.run(cfg)
    assert 'Native services ready' not in output.getvalue()


def test_first_pairing_shows_box_once_and_repeat_preserves_hash(monkeypatch, tmp_path):
    output = Terminal()
    monkeypatch.setattr(local_mac.sys, 'stdin', Terminal())
    monkeypatch.setattr(local_mac.sys, 'stdout', output)
    monkeypatch.setattr('builtins.input', lambda _: 'https://example.ngrok.app')
    monkeypatch.setattr(local_mac.getpass, 'getpass', lambda _: 'synthetic-agent-token-123456')
    monkeypatch.setattr(local_mac.cli, '_service_record', lambda *a: None)
    monkeypatch.setattr(local_mac.secrets, 'token_urlsafe', lambda _: 'x' * 43)
    cfg = SimpleNamespace(repo_root=tmp_path, layout=SimpleNamespace(state_root=tmp_path), backend_port=20000)
    local_mac.configure(cfg)
    saved = (tmp_path / 'pairing.json').read_bytes()
    assert '┌' in output.getvalue() and 'x' * 43 in output.getvalue()
    assert b'x' * 43 not in saved
    output.seek(0)
    output.truncate()
    local_mac.configure(cfg)
    assert (tmp_path / 'pairing.json').read_bytes() == saved
    assert 'x' * 43 not in output.getvalue()
    assert 'Address: https://example.ngrok.app' in output.getvalue()


@pytest.mark.parametrize('system, architecture, translated, accepted, reexec', [
    ('Darwin', 'arm64', '0', True, False),
    ('Darwin', 'x86_64', '1', True, True),
    ('Darwin', 'x86_64', '0', False, False),
    ('Linux', 'aarch64', '0', False, False),
])
def test_mac_entry_accepts_native_or_translated_apple_silicon(
    system, architecture, translated, accepted, reexec,
):
    import subprocess

    helper = Path(__file__).resolve().parents[2] / 'macos-runtime.sh'
    result = subprocess.run([
        'bash', '-c', '''
        set -euo pipefail
        source "$1"
        platform_name=$2
        platform_arch=$3
        translated=$4
        uname() { if [[ "$1" == -s ]]; then echo "$platform_name"; else echo "$platform_arch"; fi; }
        sysctl() { echo "$translated"; }
        exec() { printf '<%s>\\n' "$@"; exit 0; }
        omi_require_apple_silicon '/tmp/Другой Mac/start.command' --check
        echo native
        ''', 'test', str(helper), system, architecture, translated,
    ], text=True, capture_output=True)
    assert (result.returncode == 0) == accepted
    if reexec:
        assert result.stdout.splitlines() == [
            '</usr/bin/arch>', '<-arm64>', '</bin/bash>',
            '</tmp/Другой Mac/start.command>', '<--check>',
        ]
    elif accepted:
        assert result.stdout.strip() == 'native'
    else:
        assert 'Apple Silicon' in result.stderr
        assert not result.stdout


@pytest.mark.parametrize('failed', [False, True])
def test_quiet_installer_keeps_private_log_and_stops_on_failure(tmp_path, failed):
    import os
    import shutil
    import subprocess

    root = Path(__file__).resolve().parents[3]
    repo = tmp_path / 'project with spaces'
    (repo / 'scripts').mkdir(parents=True)
    for name in ('install-local-mac.sh', 'macos-runtime.sh'):
        shutil.copy2(root / 'scripts' / name, repo / 'scripts' / name)
    for name in ('backend/.python-version', 'backend/pylock.macos.toml', 'package.json', 'package-lock.json', 'firebase.json'):
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('fixture')
    sync = repo / 'backend/scripts/sync-python-deps.sh'
    sync.parent.mkdir()
    sync.write_text('echo dependency-output\necho dependency-diagnostic >&2\nexit ' + ('9' if failed else '0'))
    python = repo / 'backend/.venv/bin/python'
    python.parent.mkdir(parents=True)
    python.write_text('#!/bin/bash\necho emulator-prepared\n')
    python.chmod(0o700)
    (repo / 'scripts/local-mac.sh').write_text('echo runtime-checked\n')
    prefix = tmp_path / 'Other Brew'
    shell_env = tmp_path / 'shell-env'
    shell_env.write_text('''
    uname() { case "$1" in -s) echo Darwin;; -m) echo arm64;; esac; }
    lockf() { :; }
    brew() { if [[ "$1" == --prefix ]]; then echo "$OMI_TEST_PREFIX"; fi; }
    ngrok() { :; }
    npm() { mkdir -p node_modules; echo npm-output; }
    ''')
    result = subprocess.run(
        ['bash', 'scripts/install-local-mac.sh', '--quiet'], cwd=repo,
        env={**os.environ, 'BASH_ENV': str(shell_env), 'OMI_TEST_PREFIX': str(prefix)},
        capture_output=True, text=True,
    )
    assert result.returncode == (9 if failed else 0)
    assert 'dependency-output' not in result.stdout + result.stderr
    assert 'dependency-diagnostic' not in result.stdout + result.stderr
    assert len((result.stdout + result.stderr).splitlines()) <= 4
    if failed:
        assert 'Failed: prepare Python' in result.stderr
        assert '.local/install.log' in result.stderr
    else:
        assert 'Checking native service prerequisites' in result.stdout
        assert not result.stderr
    log = repo / '.local/install.log'
    assert log.stat().st_mode & 0o777 == 0o600
    text = log.read_text()
    assert 'dependency-output' in text and 'dependency-diagnostic' in text
    assert f'Installation exit status: {result.returncode}' in text
    assert ('runtime-checked' in text) != failed
    assert ('npm-output' in text) != failed
    assert (repo / '.local/install.ready').is_file() != failed


def test_mac_runtime_discovers_homebrew_prefix(tmp_path):
    import os
    import subprocess

    helper = Path(__file__).resolve().parents[2] / 'macos-runtime.sh'
    prefix = tmp_path / 'Other Brew'
    for directory, name in [('opt/node@22/bin', 'node'), ('opt/openjdk@21/bin', 'java')]:
        binary = prefix / directory / name
        binary.parent.mkdir(parents=True)
        binary.write_text('#!/bin/bash\nexit 0\n')
        binary.chmod(0o700)
    result = subprocess.run([
        'bash', '-c', '''
        set -euo pipefail
        source "$1"
        brew() { echo "$OMI_TEST_PREFIX"; }
        omi_macos_path
        command -v node
        command -v java
        ''', 'test', str(helper),
    ], env={**os.environ, 'OMI_TEST_PREFIX': str(prefix)}, capture_output=True, text=True, check=True)
    assert result.stdout.splitlines() == [str(prefix / 'opt/node@22/bin/node'), str(prefix / 'opt/openjdk@21/bin/java')]


@pytest.mark.parametrize('version, status, accepted', [
    ('openjdk version "17.0.1"', 0, False),
    ('java version "1.8.0_451"', 0, False),
    ('openjdk version "21.0.6"', 0, True),
    ('openjdk version "25-ea"', 0, True),
    ('', 0, False),
    ('openjdk version "21.0.6"', 1, False),
])
def test_mac_entry_requires_firebase_supported_java(version, status, accepted):
    import subprocess

    helper = Path(__file__).resolve().parents[2] / 'macos-runtime.sh'
    result = subprocess.run([
        'bash', '-c', '''
        source "$1"
        version=$2
        status=$3
        java() { printf '%s\\n' "$version" >&2; return "$status"; }
        omi_java_ready
        ''', 'test', str(helper), version, str(status),
    ], capture_output=True, text=True)
    assert (result.returncode == 0) == accepted
    assert result.stdout == result.stderr == ''


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS terminal recorder')
def test_homebrew_terminal_logging_keeps_tty_and_exit_status(tmp_path):
    import os
    import subprocess

    log = tmp_path / 'install.log'
    log.touch(mode=0o600)
    result = subprocess.run([
        '/usr/bin/script', '-q', '-a', str(log), '/bin/bash', '-c',
        '[[ -t 0 && -t 1 ]] || exit 8; printf "bootstrap-prompt\\n"; exit 7',
    ], stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
       env={**os.environ, 'TERM': 'dumb'})
    assert result.returncode == 7
    assert b'bootstrap-prompt' in result.stdout
    assert 'bootstrap-prompt' in log.read_text()
    assert log.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("found", [False, True])
def test_lan_environment_finds_jdk_in_another_brew_prefix(tmp_path, found):
    import os
    import shutil
    import subprocess

    root = Path(__file__).resolve().parents[3]
    loader = tmp_path / 'repo/scripts/dev-harness/_source_local_dev_env.sh'
    loader.parent.mkdir(parents=True)
    shutil.copy2(root / 'scripts/dev-harness/_source_local_dev_env.sh', loader)
    jdk = tmp_path / 'Other Brew/openjdk@21'
    java = jdk / 'bin/java'
    java.parent.mkdir(parents=True)
    java.write_text('#!/bin/bash\nexit 0\n')
    java.chmod(0o700)
    result = subprocess.run([
        'bash', '-c', '''
        set -euo pipefail
        brew() { [[ "$*" == '--prefix openjdk@21' ]] || exit 2; [[ "$OMI_TEST_FOUND" == 1 ]] || return 1; echo "$OMI_TEST_JDK"; }
        source "$1"
        command -v java
        ''', 'test', str(loader),
    ], env={**os.environ, 'OMI_TEST_JDK': str(jdk), 'OMI_ENV_STAGE': 'offline',
            'OMI_TEST_FOUND': '1' if found else '0',
            'PATH': os.environ['PATH'] if found else str(java.parent) + os.pathsep + os.environ['PATH']},
       capture_output=True, text=True, check=True)
    assert result.stdout.strip() == str(java)
