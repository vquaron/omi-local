"""Pinned FluidAudio live worker installation; no pairing, recordings or active services."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import select
import selectors
import shutil
import struct
import subprocess
import time
import wave

from . import stt_install, transcription_lock


class LiveInstallError(ValueError):
    """Fixed diagnostics: native output and filesystem identifiers stay private."""


PACKAGE = Path('scripts/local_live_preview')
BUILD_FILES = ('Package.swift', 'Package.resolved', 'ParakeetWorker.swift')
MANIFEST = Path('scripts/dev-harness/parakeet-live-models.json')
FIXTURE = Path('scripts/dev-harness/fixtures/speech-check.wav')
BINARY = 'bin/ParakeetWorker'
BUNDLE = 'bin/FluidAudio_FluidAudio.bundle'
# FluidAudio v0.15.6 resolves load(from:) through the parent plus this repo name.
# A generic models/ basename makes it look beside, rather than inside, that root.
MODEL_DIR = 'models/parakeet-tdt-0.6b-v3'


def runtime_root(repo_root: Path) -> Path:
    return repo_root / '.local/parakeet-live'


def recipe(repo_root: Path) -> dict:
    return json.loads((repo_root / MANIFEST).read_text())


def revision(repo_root: Path) -> str:
    inputs = [MANIFEST, FIXTURE, *(PACKAGE / name for name in BUILD_FILES)]
    return hashlib.sha256(''.join(stt_install.digest(repo_root / name) for name in inputs).encode()).hexdigest()


def environment() -> dict:
    return {key: os.environ[key] for key in ('HOME', 'PATH', 'TMPDIR', 'LANG', 'DEVELOPER_DIR') if key in os.environ}


def offline(command: list[str]) -> list[str]:
    sandbox = shutil.which('sandbox-exec')
    if not sandbox:
        raise LiveInstallError('Live requires macOS sandbox-exec for offline execution.')
    return [sandbox, '-p', '(version 1)(allow default)(deny network*)', *command]


def safe_path(repo_root: Path, path: Path) -> None:
    if not path.is_relative_to(repo_root) or '..' in path.parts:
        raise LiveInstallError('Unsafe live installation path.')
    for item in (path, *path.parents):
        if item == repo_root:
            break
        if item.is_symlink():
            raise LiveInstallError('Unsafe live installation path.')


def preflight(repo_root: Path) -> str:
    """Read-only prerequisites, before either native compilation or model download."""
    data = recipe(repo_root)
    if (platform.system() != 'Darwin' or platform.machine() != 'arm64'
            or int(platform.mac_ver()[0].split('.')[0]) < data['macos_min']):
        raise LiveInstallError('Live requires Apple Silicon and macOS 14 or later.')
    for tool in ('xcrun', 'git', 'sandbox-exec'):
        if not shutil.which(tool):
            raise LiveInstallError('Live requires Xcode, Git and sandbox-exec.')
    for name in BUILD_FILES:
        if not (repo_root / PACKAGE / name).is_file():
            raise LiveInstallError('Pinned live sources are missing; restore the project checkout.')
    if stt_install.digest(repo_root / FIXTURE) != data['smoke_sha256']:
        raise LiveInstallError('The synthetic live fixture is missing or changed.')
    root = runtime_root(repo_root)
    paths = ['.install.lock', 'build.log', 'ready.json', 'build-receipt.json', 'source', BINARY]
    paths += [asset['path'] for asset in data['assets']]
    paths += [asset['path'] + '.part' for asset in data['assets']]
    for name in paths:
        safe_path(repo_root, root / name)
    swift = subprocess.run(['xcrun', 'swift', '--version'], env=environment(), capture_output=True, text=True, timeout=30)
    version = re.search(r'Swift version (\d+)\.(\d+)', swift.stdout)
    if swift.returncode or not version or tuple(map(int, version.groups())) < tuple(data['swift_min']):
        raise LiveInstallError('Live requires Xcode with Swift 6.0 or later.')
    sdk = subprocess.run(['xcrun', '--sdk', 'macosx', '--show-sdk-path'],
                         env=environment(), capture_output=True, text=True, timeout=30)
    if sdk.returncode or not sdk.stdout.strip() or not Path(sdk.stdout.strip()).is_dir():
        raise LiveInstallError('macOS SDK unavailable; complete Xcode setup.')
    sdk_version = subprocess.run(['xcrun', '--sdk', 'macosx', '--show-sdk-version'],
                                 env=environment(), capture_output=True, text=True, timeout=30)
    if sdk_version.returncode or not re.fullmatch(r'\d+(?:\.\d+)+', sdk_version.stdout.strip()):
        raise LiveInstallError('Cannot determine the macOS SDK version.')
    observer = subprocess.run(offline(['/usr/bin/true']), env=environment(), capture_output=True, timeout=10)
    if observer.returncode:
        raise LiveInstallError('macOS sandbox unavailable for live transcription; installation stopped.')
    parent = root
    while not parent.exists():
        parent = parent.parent
    if shutil.disk_usage(parent).free < 4 * 1024**3:
        raise LiveInstallError('Free at least 4 GiB of disk space before preparing live transcription.')
    return swift.stdout.strip() + '; macOS SDK ' + sdk_version.stdout.strip()


def build_inputs(repo_root: Path, toolchain: str) -> dict:
    return {'files': {name: stt_install.digest(repo_root / PACKAGE / name) for name in BUILD_FILES},
            'toolchain': toolchain, 'configuration': 'release', 'product': 'ParakeetWorker', 'jobs': 2}


def verified_build(repo_root: Path, receipt: dict) -> bool:
    try:
        files = receipt['files']
        if BINARY not in files or not any(name.startswith(BUNDLE + '/') for name in files):
            return False
        for name, checksum in files.items():
            if name != BINARY and not name.startswith(BUNDLE + '/'):
                return False
            path = runtime_root(repo_root) / name
            safe_path(repo_root, path)
            if not path.is_file() or stt_install.digest(path) != checksum:
                return False
        return os.access(runtime_root(repo_root) / BINARY, os.X_OK)
    except (OSError, KeyError, TypeError, ValueError):
        return False


def installed(repo_root: Path) -> tuple[Path, Path]:
    """Cheap receipt/stat checks plus native-output hashes; raises when not ready."""
    root = runtime_root(repo_root)
    try:
        safe_path(repo_root, root / 'ready.json')
        safe_path(repo_root, root / 'build-receipt.json')
        ready = json.loads((root / 'ready.json').read_text())
        build = json.loads((root / 'build-receipt.json').read_text())
        expected = {asset['path'] for asset in recipe(repo_root)['assets']} | set(build['files'])
        if (ready['revision'] != revision(repo_root) or ready['offline_speech_check'] != 'passed'
                or set(ready['files']) != expected or not verified_build(repo_root, build)):
            raise ValueError('receipt')
        for name, stamp in ready['files'].items():
            path = root / name
            safe_path(repo_root, path)
            if not path.is_file() or [path.stat().st_size, path.stat().st_mtime_ns] != stamp:
                raise ValueError('changed')
        return root / BINARY, root / MODEL_DIR
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise LiveInstallError('Live is incomplete or changed; run ./omiloc bootstrap.') from None


def prepare_source(repo_root: Path, root: Path, inputs: dict) -> Path:
    # A new recipe gets a separate build directory; no clean/rebuild of a legacy worker.
    name = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    source = root / 'source' / name
    for filename in BUILD_FILES:
        target = source / filename
        safe_path(repo_root, target)
        content = (repo_root / PACKAGE / filename).read_bytes()
        if target.exists() and target.read_bytes() != content:
            raise LiveInstallError('Live build sources changed; inspect them before retrying.')
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
    return source


def build(repo_root: Path, inputs: dict, lock) -> dict:
    root = runtime_root(repo_root)
    source = prepare_source(repo_root, root, inputs)
    print('Live: building the pinned Parakeet worker...', flush=True)
    with (root / 'build.log').open('w') as log:
        os.chmod(root / 'build.log', 0o600)
        result = subprocess.run(['xcrun', 'swift', 'build', '-c', 'release', '--product', 'ParakeetWorker',
                                 '--disable-automatic-resolution', '-j', '2'], cwd=source, env=environment(),
                                stdout=log, stderr=log, timeout=1800, pass_fds=(lock.fileno(),))
    if result.returncode:
        raise LiveInstallError('Live build failed; check .local/parakeet-live/build.log.')
    output = source / '.build/release'
    products = [output / 'ParakeetWorker']
    bundle = output / 'FluidAudio_FluidAudio.bundle'
    products += sorted(path for path in bundle.rglob('*') if path.is_file())
    if len(products) < 2 or not all(path.is_file() and not path.is_symlink() for path in products):
        raise LiveInstallError('Live build did not produce all required executables and resources.')
    files = {}
    for product in products:
        name = 'bin/' + product.relative_to(output).as_posix()
        target = root / name
        safe_path(repo_root, target)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + '.part')
        safe_path(repo_root, temporary)
        shutil.copy2(product, temporary)
        os.replace(temporary, target)
        files[name] = stt_install.digest(target)
    receipt = {'inputs': inputs, 'files': files}
    stt_install.atomic_json(root / 'build-receipt.json', receipt)
    return receipt


def smoke(repo_root: Path, binary: Path, models: Path, *, timeout: float = 120) -> None:
    """Exercise model load, Start, PCM and Stop with synthetic speech, denying network."""
    with wave.open(str(repo_root / FIXTURE)) as fixture:
        if (fixture.getnchannels(), fixture.getsampwidth(), fixture.getframerate(), fixture.getcomptype()) != (1, 2, 16000, 'NONE'):
            raise LiveInstallError('Invalid synthetic live fixture format.')
        pcm = fixture.readframes(fixture.getnframes())
    process = subprocess.Popen(offline([str(binary), str(models)]), stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               env={**environment(), 'OS_ACTIVITY_MODE': 'disable'}, bufsize=0)
    deadline = time.monotonic() + timeout
    buffered = bytearray()
    selector = selectors.DefaultSelector()
    try:
        selector.register(process.stdout, selectors.EVENT_READ)
        os.set_blocking(process.stdin.fileno(), False)

        def read():
            while b'\n' not in buffered:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise LiveInstallError('Live probe timed out waiting for the worker.')
                chunk = os.read(process.stdout.fileno(), 8192)
                if not chunk or len(buffered) + len(chunk) > 1024 * 1024:
                    raise LiveInstallError('Live probe received an incomplete worker response.')
                buffered.extend(chunk)
            line, _, rest = buffered.partition(b'\n')
            buffered[:] = rest
            message = json.loads(line)
            if not isinstance(message, dict) or message.get('type') == 'error':
                raise LiveInstallError('Live probe reported a worker error.')
            return message

        def send(kind, audio=b''):
            frame = struct.pack('!I', len(audio) + 1) + bytes([kind]) + audio
            # FileIO may perform a short write; preserve the complete wire frame.
            view = memoryview(frame)
            while view:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([], [process.stdin], [], remaining)[1]:
                    raise LiveInstallError('Live timed out receiving synthetic audio.')
                try:
                    written = os.write(process.stdin.fileno(), view)
                except BlockingIOError:
                    continue
                if not written:
                    raise LiveInstallError('Cannot send synthetic audio to the live worker.')
                view = view[written:]

        if read().get('type') != 'model_ready':
            raise LiveInstallError('Live did not confirm model loading.')
        send(1)
        if read().get('type') != 'started':
            raise LiveInstallError('Live did not confirm recording startup.')
        for offset in range(0, len(pcm), 32000):
            send(2, pcm[offset:offset + 32000])
        send(3)
        text = ''
        while True:
            message = read()
            if message.get('type') == 'finished':
                break
            if message.get('type') != 'snapshot' or not isinstance(message.get('text'), str):
                raise LiveInstallError('Live probe received an invalid event.')
            text = message['text'].lower()
        if not all(word in text for word in ('проверка', 'погода', 'текст')):
            raise LiveInstallError('Live did not transcribe the synthetic speech fixture.')
    except (OSError, ValueError) as error:
        if isinstance(error, LiveInstallError):
            raise
        raise LiveInstallError('Live probe did not complete the worker exchange.') from None
    finally:
        selector.close()
        if process.poll() is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        process.wait(timeout=10)
        process.stdin.close()
        process.stdout.close()


def install(repo_root: Path, *, shared_lock=None) -> None:
    toolchain = preflight(repo_root)
    try:
        with transcription_lock.acquire(repo_root, shared_lock=shared_lock):
            root = runtime_root(repo_root)
            data = recipe(repo_root)
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            with (root / '.install.lock').open('a') as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise LiveInstallError('Live preparation is already running.') from None
                expected = build_inputs(repo_root, toolchain)
                try:
                    receipt = json.loads((root / 'build-receipt.json').read_text())
                except (OSError, ValueError):
                    receipt = {}
                reuse = receipt.get('inputs') == expected and verified_build(repo_root, receipt)
                print('Live: ' + ('reuse' if reuse else 'build') + ' from sources and toolchain.', flush=True)
                # A failed attempt must never leave a previous smoke receipt marked ready.
                (root / 'ready.json').unlink(missing_ok=True)
                if not reuse:
                    receipt = build(repo_root, expected, lock)
                print('Live: checking and downloading the model if needed (~483 MB)...', flush=True)
                for asset in data['assets']:
                    stt_install.download(asset, root)
                print('Live: verifying synthetic speech transcription offline...', flush=True)
                smoke(repo_root, root / BINARY, root / MODEL_DIR)
                names = {asset['path'] for asset in data['assets']} | set(receipt['files'])
                stt_install.atomic_json(root / 'ready.json', {
                    'revision': revision(repo_root), 'offline_speech_check': 'passed',
                    'files': {name: [(root / name).stat().st_size, (root / name).stat().st_mtime_ns]
                              for name in sorted(names)},
                })
                installed(repo_root)
                print('Live prepared; running services unchanged.', flush=True)
    except transcription_lock.TranscriptionLockBusy as error:
        raise LiveInstallError(str(error)) from None
    except transcription_lock.TranscriptionLockError:
        raise LiveInstallError('Cannot safely open the shared transcription lock.') from None


def main() -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description='Prepare local Parakeet live transcription.')
    parser.add_argument('--check', action='store_true', help='Check the installed worker without making changes.')
    args = parser.parse_args()
    repo_root = Path(__file__).resolve().parents[3]
    try:
        if args.check:
            installed(repo_root)
            print('Live: files and transcription probe verified.')
        else:
            install(repo_root)
        return 0
    except (LiveInstallError, transcription_lock.TranscriptionLockError,
            stt_install.InstallError, OSError, subprocess.SubprocessError) as error:
        print(str(error) if isinstance(error, (LiveInstallError, stt_install.InstallError)) else
              'Live preparation stopped. Check the environment and retry.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
