"""Reproducible WhisperKit preparation; never changes the active engine."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import wave

from . import local_whisperkit as kit, stt_install, transcription_lock


def preflight(root):
    if platform.system() != 'Darwin' or platform.machine() != 'arm64' or int(platform.mac_ver()[0].split('.')[0]) < 14:
        raise kit.WhisperKitError('WhisperKit preparation requires Apple Silicon and macOS 14 or newer')
    for tool in ('xcrun', 'git', 'sandbox-exec'):
        if not shutil.which(tool):
            raise kit.WhisperKitError('WhisperKit requires Xcode, Git and sandbox-exec')
    swift = subprocess.run(['xcrun', 'swift', '--version'], capture_output=True, text=True, timeout=30)
    version = re.search(r'Swift version (\d+)\.(\d+)', swift.stdout)
    if swift.returncode or not version or tuple(map(int, version.groups())) < (5, 10):
        raise kit.WhisperKitError('WhisperKit requires Xcode Swift 5.10 or newer')
    sdk = subprocess.run(['xcrun', '--sdk', 'macosx', '--show-sdk-path'], capture_output=True, text=True, timeout=30)
    if sdk.returncode or not Path(sdk.stdout.strip()).is_dir() or not sdk.stdout.strip():
        raise kit.WhisperKitError('The macOS SDK is unavailable; finish Xcode setup')
    parent = root
    while not parent.exists():
        parent = parent.parent
    if shutil.disk_usage(parent).free < 4 * 1024**3:
        raise kit.WhisperKitError('WhisperKit preparation needs at least 4 GiB free')
    # Exercise the observer/policy before model installation or compilation.
    result = subprocess.run(kit.offline(['/usr/bin/true']), capture_output=True, timeout=10)
    if result.returncode:
        raise kit.WhisperKitError('macOS offline sandbox is unavailable')
    return swift.stdout.strip()


def source_files(root, data):
    """Only build inputs; exclude example symlinks from the upstream archive."""
    with tarfile.open(root / data['source_archive']['path']) as archive:
        for entry in archive.getmembers():
            parts = Path(entry.name).parts
            if len(parts) < 2 or parts[1] not in {'Sources', 'Tests', 'Package.swift', 'Package.resolved', 'LICENSE.md'}:
                continue
            if entry.isdir():
                continue
            if not entry.isfile() or '..' in parts or Path(entry.name).is_absolute():
                raise kit.WhisperKitError('Unexpected source archive entry')
            target = root / 'source' / Path(*parts[1:])
            content = archive.extractfile(entry).read()
            if target.exists():
                if target.is_symlink() or target.read_bytes() != content:
                    raise kit.WhisperKitError('WhisperKit source inputs changed; inspect the source before building')
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)


def build_inputs(data, swift):
    return {'source_sha256': data['source_archive']['sha256'], 'swift': swift,
            'configuration': 'release', 'product': 'whisperkit-cli', 'jobs': 2}


def install(root, *, shared_lock=None, lock_path=None):
    swift = preflight(root)
    data = kit.recipe()
    repo_root = kit.SOURCE.parents[1]
    try:
        with transcription_lock.acquire(repo_root, shared_lock=shared_lock, lock_path=lock_path):
            _install_locked(root, swift, data)
    except transcription_lock.TranscriptionLockBusy as error:
        raise kit.WhisperKitError(str(error)) from None
    except transcription_lock.TranscriptionLockError:
        raise kit.WhisperKitError('Cannot safely open the shared transcription lock.') from None


def _install_locked(root, swift, data):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (root / '.install.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stt_install.download(data['source_archive'], root)
        source_files(root, data)
        expected = build_inputs(data, swift)
        receipt_path = root / 'build-receipt.json'
        receipt = json.loads(receipt_path.read_text()) if receipt_path.exists() else {}
        binary = root / 'bin/whisperkit-cli'
        reuse = (receipt.get('inputs') == expected and binary.is_file()
                 and receipt.get('executable_sha256') == stt_install.digest(binary))
        if not reuse:
            print('WhisperKit: building the local worker...', flush=True)
            with (root / 'build.log').open('w') as log:
                result = subprocess.run(['xcrun', 'swift', 'build', '-c', 'release', '--product', 'whisperkit-cli',
                                         '--disable-automatic-resolution', '-j', '2'],
                                        cwd=root / 'source', env=kit.environment(), stdout=log, stderr=log, timeout=1800)
            if result.returncode:
                raise kit.WhisperKitError('WhisperKit build failed; inspect .local/whisperkit/build.log')
            binary.parent.mkdir(exist_ok=True)
            shutil.copy2(root / 'source/.build/release/whisperkit-cli', binary)
            stt_install.atomic_json(receipt_path, {'inputs': expected, 'executable_sha256': stt_install.digest(binary)})
        print('WhisperKit: checking and downloading the model (~630 MB)...', flush=True)
        for asset in data['assets']:
            stt_install.download(asset, root)
        print('WhisperKit: verifying speech and timestamps with networking disabled...', flush=True)
        fixture = kit.SOURCE / 'fixtures/speech-check.wav'
        if stt_install.digest(fixture) != data['smoke_sha256']:
            raise kit.WhisperKitError('Synthetic speech fixture changed')
        with wave.open(str(fixture)) as audio:
            duration = audio.getnframes() / audio.getframerate()
        with tempfile.TemporaryDirectory(dir=root, prefix='.speech-check-') as temporary:
            raw = kit.transcribe(root, fixture, Path(temporary), 'ru', duration)
            text = ' '.join(s['text'].lower() for s in raw['segments'])
            if not all(word in text for word in ('проверка', 'погода', 'текст')) or not all(s['words'] for s in raw['segments']):
                raise kit.WhisperKitError('WhisperKit synthetic speech/word timestamp check failed')
        files = {asset['path'] for asset in data['assets']} | {'bin/whisperkit-cli'}
        stt_install.atomic_json(root / 'ready.json', {
            'revision': kit.revision(), 'executable_sha256': stt_install.digest(binary),
            'files': {name: [(root / name).stat().st_size, (root / name).stat().st_mtime_ns] for name in sorted(files)},
            'offline_speech_check': 'passed', 'swift': swift,
        })
        kit.installed(root)
        print('WhisperKit prepared; active engine unchanged.', flush=True)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    default_root = kit.SOURCE.parents[1] / '.local/whisperkit'
    parser.add_argument('--root', type=Path, default=default_root)
    parser.add_argument('--lock-path', type=Path,
                        help='Shared lock path; required with a custom --root.')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    try:
        if args.check:
            kit.installed(args.root)
            print('WhisperKit: files verified.')
        else:
            root = args.root.resolve()
            if root != default_root.resolve() and args.lock_path is None:
                raise kit.WhisperKitError('Specify the shared --lock-path with a custom --root.')
            install(root, lock_path=args.lock_path.resolve() if args.lock_path else None)
        return 0
    except (kit.WhisperKitError, transcription_lock.TranscriptionLockError,
            stt_install.InstallError, OSError, subprocess.SubprocessError) as error:
        print(str(error) if isinstance(error, (kit.WhisperKitError, stt_install.InstallError)) else
              'WhisperKit preparation stopped. Check the environment and retry.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
