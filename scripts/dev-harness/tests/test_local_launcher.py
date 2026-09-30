from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dev_harness import local_launcher


@pytest.fixture
def layout(tmp_path, monkeypatch):
    repo = tmp_path / "project with 'quotes' and spaces"
    home = tmp_path / 'home'
    (repo / 'scripts').mkdir(parents=True)
    home.mkdir()
    source = Path(__file__).resolve().parents[3] / 'omiloc'
    shutil.copy2(source, repo / 'omiloc')
    (repo / 'scripts/native-runtime.sh').write_text('printf "%s\\n" "$@"\n')
    monkeypatch.setenv('SHELL', '/bin/zsh')
    monkeypatch.setenv('PATH', '/usr/bin:/bin')
    return repo, home


def test_install_is_idempotent_and_runs_from_unrelated_directory(layout, tmp_path):
    repo, home = layout
    profile = home / '.zprofile'
    profile.write_text('# existing user settings\n')
    target = local_launcher.install(repo, home=home)
    first = profile.read_bytes()
    assert local_launcher.install(repo, home=home) == target
    assert profile.read_bytes() == first
    assert first.startswith(b'# existing user settings\n')
    assert first.count(b'export PATH=') == 1
    result = subprocess.run([str(target), '--help'], cwd=tmp_path, text=True, capture_output=True)
    assert result.returncode == 0
    assert 'Usage: ./omiloc' in result.stdout
    result = subprocess.run([str(target), '--runtime', 'native', 'open'], cwd=tmp_path, text=True, capture_output=True)
    assert result.returncode == 0 and result.stdout.splitlines() == ['open']


def test_install_refuses_existing_command_before_profile_change(layout):
    repo, home = layout
    target = home / '.local/bin/omiloc'
    target.parent.mkdir(parents=True)
    target.write_text('user program')
    with pytest.raises(local_launcher.LauncherError, match='already occupied'):
        local_launcher.install(repo, home=home)
    assert target.read_text() == 'user program'
    assert not (home / '.zprofile').exists()


def test_install_refuses_foreign_command_elsewhere_on_path(layout, monkeypatch):
    repo, home = layout
    other = home / 'other-bin'
    other.mkdir()
    command = other / 'omiloc'
    command.write_text('#!/bin/sh\nexit 0\n')
    command.chmod(0o755)
    monkeypatch.setenv('PATH', str(other))
    with pytest.raises(local_launcher.LauncherError, match='Another omiloc'):
        local_launcher.install(repo, home=home)
    assert not (home / '.local').exists()
    assert not (home / '.zprofile').exists()


def test_open_library_starts_before_browser_and_keeps_output_short(monkeypatch, capsys):
    events = []
    monkeypatch.setattr(sys.stdout, 'isatty', lambda: True)
    monkeypatch.delenv('SSH_CONNECTION', raising=False)
    monkeypatch.delenv('SSH_TTY', raising=False)
    def start(_):
        events.append('start')
        print('internal diagnostics')
    monkeypatch.setattr(local_launcher.local_library, 'start', start)
    monkeypatch.setattr(local_launcher.webbrowser, 'open', lambda url: events.append(url) or True)
    assert local_launcher.open_library(SimpleNamespace(backend_port=20000)) == 0
    assert events == ['start', 'http://127.0.0.1:20001']
    assert capsys.readouterr().out == 'omiloc · http://127.0.0.1:20001\n'


def test_failed_start_does_not_open_browser_or_leak_error(monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', ['omiloc'])
    monkeypatch.setattr(local_launcher.config, 'load_config', lambda *a, **k: object())
    def fail(_):
        raise RuntimeError('internal private details')
    monkeypatch.setattr(local_launcher.local_library, 'start', fail)
    monkeypatch.setattr(local_launcher.webbrowser, 'open', lambda _: pytest.fail('browser opened'))
    assert local_launcher.main() == 1
    result = capsys.readouterr()
    assert result.out == '' and 'internal private details' not in result.err
    assert './omiloc doctor' in result.err


def test_removed_home_option_is_rejected_before_any_action(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["omiloc", "--home"])
    monkeypatch.setattr(local_launcher.config, "load_config", lambda *a, **k: pytest.fail("state accessed"))
    with pytest.raises(SystemExit) as result:
        local_launcher.main()
    assert result.value.code == 2


def test_headless_open_prints_url_without_starting_browser(monkeypatch, capsys):
    monkeypatch.setenv('SSH_CONNECTION', 'fixture')
    monkeypatch.setattr(local_launcher.local_library, 'start', lambda _: None)
    monkeypatch.setattr(local_launcher.webbrowser, 'open', lambda _: pytest.fail('browser opened over SSH'))
    assert local_launcher.open_library(SimpleNamespace(backend_port=20000)) == 0
    assert 'http://127.0.0.1:20001' in capsys.readouterr().out
