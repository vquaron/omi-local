"""Build, attest and run an existing local iOS setup with Flutter hot reload."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile

from .launch_iphone import select_iphone
from .local_env import LocalEnvError

DEFINES = (
    '--dart-define=OMI_APP_PROFILE=local_dev', '--dart-define=OMI_RUNTIME_MODE=offline',
    '--dart-define=OMI_LOCAL_TEST_USER=alice', '--dart-define=OMI_API_BASE_URL=http://127.0.0.1:8000/',
    '--dart-define=OMI_FIREBASE_AUTH_EMULATOR_HOST=127.0.0.1',
)
CONFIGURATION = 'Debug-dev / local_dev / offline'


def capture(args, *, env=None, cwd=None):
    result = subprocess.run(args, env=env, cwd=cwd, capture_output=True, timeout=45)
    if result.returncode:
        raise LocalEnvError('Tool check failed: ' + Path(args[0]).name + '; private output suppressed')
    return result.stdout


def tool_environment(root):
    env = os.environ.copy()
    paths = [str(root / '.local/toolchains/flutter/bin'), str(root / 'scripts/ios-tools')]
    brew = shutil.which('brew')
    gem_paths = [str(root / '.local/toolchains/gems')]
    if brew:
        prefix = capture([brew, '--prefix']).decode().strip()
        paths.append(str(Path(prefix) / 'opt/ruby/bin'))
        gem_paths.append(str(Path(prefix) / 'opt/cocoapods/libexec'))
    env['PATH'] = os.pathsep.join(paths + [env.get('PATH', '')])
    ruby = shutil.which('ruby', path=env['PATH'])
    if not ruby:
        raise LocalEnvError('Ruby missing; prepare iOS tools first')
    gem_paths.extend(capture([ruby, '-e', 'puts Gem.path'], env=env).decode().splitlines())
    env['GEM_PATH'] = os.pathsep.join(dict.fromkeys(gem_paths))
    env.update(OMI_RUNTIME_MODE='offline', OMI_APP_PROFILE='local_dev')
    # Each app owns its pairing in Keychain. Do not inherit a provisioning handoff.
    for name in ('NGROK_AUTHTOKEN', 'OMI_LOCAL_APP_KEY', 'OMI_LOCAL_MAC_KEY', 'OMI_LOCAL_MAC_URL',
                 'DEVICECTL_CHILD_NGROK_AUTHTOKEN', 'DEVICECTL_CHILD_OMI_LOCAL_MAC_KEY',
                 'DEVICECTL_CHILD_OMI_LOCAL_MAC_URL'):
        env.pop(name, None)
    return env


def prepare(root):
    if sys.platform != 'darwin':
        raise LocalEnvError('Debug iPhone sessions require macOS')
    env = tool_environment(root)
    app = root / 'app'
    try:
        capture(['bash', '-c', 'source ./setup.sh; check_flutter_version'], env=env, cwd=app)
    except LocalEnvError:
        raise LocalEnvError('Select the Flutter SDK declared in app/pubspec.yaml and repeat the check') from None
    required = ('ios/Flutter/PersonalTeam.xcconfig', 'ios/Pods/Manifest.lock', '.dart_tool/package_config.json',
                'lib/env/dev_env.g.dart', '.dev.env')
    if any(not (app / name).is_file() for name in required):
        raise LocalEnvError('Prepare iOS first: cd app && bash setup.sh ios personal')
    settings = dict(re.findall(r'^([A-Z_]+)=(.*)$', (app / required[0]).read_text(), re.M))
    team, bundle = settings.get('OMI_APPLE_TEAM_ID', ''), settings.get('APP_BUNDLE_IDENTIFIER', '')
    if not re.fullmatch(r'[A-Z0-9]{10}', team) or not bundle.startswith('com.omi.local.'):
        raise LocalEnvError('An existing local signing configuration is required')
    if settings.get('OMI_PERSONAL_LOCAL') != 'YES' or settings.get('OMI_RUNTIME_MODE') != 'offline':
        raise LocalEnvError('Signing configuration is not the local offline overlay')
    env['OMI_APPLE_TEAM_ID'] = team
    versions = [capture(['flutter', '--version', '--machine'], env=env), capture(['xcodebuild', '-version']),
                capture(['pod', '--version'], env=env)]
    capture(['xcodebuild', '-checkFirstLaunchStatus'])
    capture(['bash', 'scripts/validate_mobile_build_config.sh', '--flavor', 'dev', '--profile', 'local_dev',
             '--runtime-mode', 'offline'], env=env, cwd=app)
    identities = capture(['security', 'find-identity', '-v', '-p', 'codesigning']).decode()
    if not re.search(r'\b[1-9][0-9]* valid identities found', identities):
        raise LocalEnvError('No valid signing identity with a private key')
    phone = select_iphone()
    properties = phone['deviceProperties']
    if properties.get('developerModeStatus') != 'enabled' or properties.get('ddiServicesAvailable') is not True:
        raise LocalEnvError('Enable Developer Mode and wait for Xcode to prepare the iPhone')
    debug_bundle = bundle + '.dev'
    hidden = [team, bundle, debug_bundle, capture(['hostname', '-s']).decode().strip()]
    hidden.extend(re.findall(r'"([^"\n]+)"', identities))
    hidden.extend(str(v) for section in ('hardwareProperties', 'deviceProperties')
                  for k, v in phone.get(section, {}).items() if k in ('udid', 'serialNumber', 'name'))
    hidden.append(phone.get('identifier', ''))
    return env, team, debug_bundle, phone['hardwareProperties']['udid'], hidden, b'\n'.join(versions)


def configure_debug_identity(root, bundle):
    """Upgrade the ignored signing config without changing the daily app's identity."""
    path = root / 'app/ios/Flutter/PersonalTeam.xcconfig'
    if path.is_symlink():
        raise LocalEnvError('Personal signing configuration must not be a symlink')
    original = path.read_text()
    overrides = {
        'APP_BUNDLE_IDENTIFIER[config=Debug-dev]': bundle,
        'BUNDLE_NAME[config=Debug-dev]': 'Omi Local Dev',
        'BUNDLE_DISPLAY_NAME[config=Debug-dev]': 'Omi Local Dev',
    }
    lines = [line for line in original.splitlines()
             if not any(re.match(re.escape(key) + r'\s*=', line.strip()) for key in overrides)]
    updated = '\n'.join(lines + [key + '=' + value for key, value in overrides.items()]) + '\n'
    if updated == original:
        return
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(updated)
            stream.flush()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def fingerprint(root, versions):
    tracked = capture(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard', '--',
                       'app/lib', 'app/ios', 'app/assets', 'app/pubspec.yaml', 'app/pubspec.lock',
                       'app/setup.sh', 'app/scripts', 'scripts/ios-tools',
                       'scripts/dev-harness/dev_harness/ios_debug.py'], cwd=root).decode().split('\0')
    generated = ['app/.dev.env', 'app/lib/env/dev_env.g.dart', 'app/ios/Pods/Manifest.lock']
    generated += [str(p.relative_to(root)) for p in (root / 'app/ios/Flutter').glob('*.xcconfig')]
    digest = hashlib.sha256(versions + '\n'.join(DEFINES).encode())
    for name in sorted(set(tracked + generated) - {''}):
        path = root / name
        digest.update(name.encode())
        if not path.is_file():
            digest.update(b'<missing>')
            continue
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
    return digest.hexdigest()


def attest(artifact, team, bundle, device):
    capture(['codesign', '--verify', '--deep', '--strict', str(artifact)])
    info = plistlib.loads((artifact / 'Info.plist').read_bytes())
    ent = plistlib.loads(capture(['codesign', '-d', '--entitlements', ':-', str(artifact)]))
    profile = plistlib.loads(capture(['security', 'cms', '-D', '-i', str(artifact / 'embedded.mobileprovision')]))
    if (info['CFBundleIdentifier'] != bundle or ent.get('application-identifier') != team + '.' + bundle
            or ent.get('com.apple.developer.team-identifier') != team or ent.get('get-task-allow') is not True):
        raise LocalEnvError('Debug signature/entitlements do not match the configured local app')
    if (profile['ExpirationDate'].replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc)
            or team not in profile.get('TeamIdentifier', [])
            or device not in profile.get('ProvisionedDevices', [])):
        raise LocalEnvError('Provisioning profile is expired or does not cover this iPhone/team')
    if not (artifact / 'Frameworks/App.framework/flutter_assets/kernel_blob.bin').is_file():
        raise LocalEnvError('Expected a Flutter Debug bundle, not Profile/Release')
    executable = artifact / info['CFBundleExecutable']
    if not executable.resolve().is_relative_to(artifact.resolve()):
        raise LocalEnvError('Invalid app executable path')
    return hashlib.sha256(executable.read_bytes()).hexdigest(), sorted(ent)


def redact(line, hidden):
    for value in sorted(filter(None, hidden), key=len, reverse=True):
        line = line.replace(value, '<private>')
    line = re.sub(r'\b[0-9A-Fa-f]{8}-[0-9A-Fa-f-]{16,}\b', '<device>', line)
    line = re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '<account>', line)
    return re.sub(r'(?:https?|wss?)://[^\s]+', '<local-url>', line)


def flutter_command(artifact=None, device=None):
    args = ['flutter', 'build', 'ios', '--debug', '--no-pub', '--flavor', 'dev']
    if artifact is not None:
        args = ['flutter', 'run', '--debug', '--no-pub', '--flavor', 'dev', '-d', device,
                '--use-application-binary=' + str(artifact)]
    return args + list(DEFINES)


def run_flutter(args, root, env, hidden):
    # stdin remains the user's TTY, including Flutter's r/R/q controls.
    process = subprocess.Popen(args, cwd=root / 'app', env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True)
    try:
        for line in process.stdout:
            print(redact(line, hidden), end='', flush=True)
        if process.wait():
            raise LocalEnvError('Flutter exited unsuccessfully; see the redacted output above')
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def session(root, *, check=False, build_only=False):
    env, team, bundle, device, hidden, versions = prepare(root)
    if check:
        print('Debug prerequisites: passed; no build or installation')
        return
    if not build_only and not sys.stdin.isatty():
        raise LocalEnvError('Open dev-iphone.command in Terminal for hot reload, or use --build-only')
    configure_debug_identity(root, bundle)
    artifact = root / 'app/build/ios/Debug-dev-iphoneos/Runner.app'
    record = root / '.local/ios-debug.json'
    if record.is_symlink():
        raise LocalEnvError('Debug attestation record must not be a symlink')
    current = fingerprint(root, versions)
    saved = json.loads(record.read_text()) if record.is_file() else {}
    reuse = saved.get('inputs') == current and artifact.is_dir()
    if reuse:
        try:
            reuse = attest(artifact, team, bundle, device)[0] == saved.get('executable_sha256')
        except (LocalEnvError, ValueError, OSError, KeyError):
            reuse = False
    print('Decision:', 'reuse' if reuse else 'build', '; configuration:', CONFIGURATION, flush=True)
    commit = capture(['git', 'rev-parse', 'HEAD'], cwd=root).decode().strip()
    if not reuse:
        run_flutter(flutter_command(), root, env, hidden)
    digest, entitlements = attest(artifact, team, bundle, device)
    print('Source commit:', saved.get('commit', commit) if reuse else commit)
    print('Artifact:', artifact)
    print('Executable SHA256:', digest)
    print('Signature: passed; entitlements:', ', '.join(entitlements), '; get-task-allow=true')
    record.parent.mkdir(parents=True, exist_ok=True)
    data = {'inputs': fingerprint(root, versions), 'commit': saved.get('commit', commit) if reuse else commit,
            'executable_sha256': digest, 'configuration': CONFIGURATION}
    # Contains hashes/provenance only; never phone, account, team or key values.
    with record.open('w') as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(data, stream)
    if not build_only:
        print('Flutter controls: r = hot reload, R = hot restart, q = quit. Keep this terminal open.', flush=True)
        run_flutter(flutter_command(artifact, device), root, env, hidden)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--check', action='store_true')
    group.add_argument('--build-only', action='store_true')
    args = parser.parse_args()
    try:
        session(Path.cwd(), check=args.check, build_only=args.build_only)
        return 0
    except (LocalEnvError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print('Decision: blocked;', str(error) if isinstance(error, LocalEnvError) else type(error).__name__)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
