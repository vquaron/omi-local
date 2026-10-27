"""Provision the installed local iPhone app without embedding secrets in its binary."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import tempfile

from .local_env import LocalEnvError
from .server_presets import clean_environment, launch_environment, load_presets


def capture(args, *, env=None, cwd=None):
    result = subprocess.run(args, env=env, cwd=cwd, capture_output=True)
    if result.returncode:
        raise LocalEnvError('iPhone operation failed; check device connection, unlock and signing')
    return result.stdout


def select_iphone(require_unlocked=True):
    """Resolve one paired phone, then establish its current connection and lock state."""
    try:
        devices = json.loads(capture([
            'xcrun', 'devicectl', 'list', 'devices', '--timeout', '15',
            '--json-output', '/dev/stdout', '--quiet',
        ]))['result']['devices']
        # Wi-Fi discovery can retain a disconnected tunnel until a live request opens it.
        candidates = [device for device in devices
                      if device['hardwareProperties'].get('deviceType') == 'iPhone'
                      and device['connectionProperties'].get('pairingState') == 'paired']
        wired = [device for device in candidates if device['connectionProperties'].get('transportType') == 'wired']
        phones = wired or [device for device in candidates
                           if device['connectionProperties'].get('transportType') == 'localNetwork']
        if len(phones) != 1:
            raise LocalEnvError('Connect exactly one paired iPhone')
        device_id = phones[0]['hardwareProperties']['udid']
        device = json.loads(capture([
            'xcrun', 'devicectl', 'device', 'info', 'details', '--device', device_id,
            '--timeout', '15', '--json-output', '/dev/stdout', '--quiet',
        ]))['result']
        if device['connectionProperties'].get('tunnelState') != 'connected':
            raise LocalEnvError('iPhone is offline; connect it by USB or the paired Wi-Fi network')
        if require_unlocked:
            lock = json.loads(capture([
                'xcrun', 'devicectl', 'device', 'info', 'lockState', '--device', device_id,
                '--timeout', '15', '--json-output', '/dev/stdout', '--quiet',
            ]))['result']
            if lock.get('unlockedSinceBoot') is not True or lock.get('passcodeRequired') is not False:
                raise LocalEnvError('Unlock the iPhone and keep its screen awake, then retry')
        return device
    except LocalEnvError:
        raise
    except (ValueError, KeyError, TypeError) as error:
        raise LocalEnvError('Could not verify iPhone connection and lock state') from error


def _launch(app, phone, env, bundle):
    result = json.loads(capture([
        'xcrun', 'devicectl', 'device', 'process', 'launch',
        '--device', phone['hardwareProperties']['udid'], '--terminate-existing',
        '--json-output', '/dev/stdout', '--quiet', bundle,
    ], env=env))
    if result.get('info', {}).get('outcome') != 'success':
        raise LocalEnvError('iPhone launch was not confirmed')


def launch(env_file, app):
    env_file = Path(env_file).absolute()
    env, _hidden = launch_environment(env_file.parent, single_env_file=env_file, required=True)
    app = Path(app)
    capture(['codesign', '--verify', '--deep', '--strict', str(app)])
    info = plistlib.loads((app / 'Info.plist').read_bytes())
    bundle = info['CFBundleIdentifier']
    if not bundle.startswith('com.omi.local.'):
        raise LocalEnvError('Only the separately signed local iPhone app can receive these settings')
    phone = select_iphone()
    _launch(app, phone, env, bundle)
    print('Local iPhone app restarted with private server presets; select and verify a server in Local Mac.')


def _profile_inputs(root, env, flutter_args):
    names = capture(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard', '--',
                     'app/lib', 'app/ios', 'app/assets', 'app/pubspec.yaml', 'app/pubspec.lock',
                     'app/setup.sh', 'app/scripts', 'scripts/dev-harness/dev_harness/launch_iphone.py'],
                    env=env, cwd=root).decode().split('\0')
    names += ['app/.dev.env', 'app/lib/env/dev_env.g.dart', 'app/ios/Pods/Manifest.lock']
    names += [str(path.relative_to(root)) for path in (root / 'app/ios/Flutter').glob('*.xcconfig')]
    digest = hashlib.sha256(json.dumps(['Profile-dev / local_dev / offline', *flutter_args]).encode())
    for command in (['flutter', '--version', '--machine'], ['xcodebuild', '-version'], ['pod', '--version']):
        digest.update(capture(command, env=env, cwd=root / 'app'))
    # Flutter rewrites this derived file for Profile mode. Canonical defines,
    # SDK versions and every user-authored xcconfig remain fingerprinted.
    for name in sorted(set(names) - {'', 'app/ios/Flutter/Generated.xcconfig'}):
        path = root / name
        digest.update(name.encode())
        if path.is_file():
            digest.update(path.read_bytes())
        else:
            digest.update(b'<missing>')
    return digest.hexdigest()


def _attest_profile(root, app, phone, env):
    settings_file = root / 'app/ios/Flutter/PersonalTeam.xcconfig'
    if settings_file.is_symlink() or settings_file.stat().st_mode & 0o077:
        raise LocalEnvError('Private local signing settings are required')
    settings = dict(re.findall(r'^([A-Z_]+)=(.*)$', settings_file.read_text(), re.M))
    team, bundle = settings.get('OMI_APPLE_TEAM_ID', ''), settings.get('APP_BUNDLE_IDENTIFIER', '')
    if (not re.fullmatch(r'[A-Z0-9]{10}', team) or not bundle
            or settings.get('OMI_PERSONAL_LOCAL') != 'YES' or settings.get('OMI_RUNTIME_MODE') != 'offline'):
        raise LocalEnvError('Profile signing configuration is not the local offline overlay')
    capture(['codesign', '--verify', '--deep', '--strict', str(app)], env=env)
    info = plistlib.loads((app / 'Info.plist').read_bytes())
    entitlements = plistlib.loads(capture(['codesign', '-d', '--entitlements', ':-', str(app)], env=env))
    profile = plistlib.loads(capture(['security', 'cms', '-D', '-i', str(app / 'embedded.mobileprovision')], env=env))
    if (info['CFBundleIdentifier'] != bundle
            or entitlements.get('application-identifier') != team + '.' + bundle
            or entitlements.get('com.apple.developer.team-identifier') != team
            or type(entitlements.get('get-task-allow', False)) is not bool):
        raise LocalEnvError('Profile signature and entitlements do not match the local configuration')
    if (profile['ExpirationDate'].replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc)
            or team not in profile.get('TeamIdentifier', [])
            or phone['hardwareProperties']['udid'] not in profile.get('ProvisionedDevices', [])):
        raise LocalEnvError('Provisioning profile is expired or does not cover this iPhone/team')
    if (app / 'Frameworks/App.framework/flutter_assets/kernel_blob.bin').is_file():
        raise LocalEnvError('Expected the Profile artifact, not a Debug bundle')
    executable = app / info['CFBundleExecutable']
    if not executable.resolve().is_relative_to(app.resolve()):
        raise LocalEnvError('Invalid app executable path')
    return bundle, hashlib.sha256(executable.read_bytes()).hexdigest(), entitlements


def build_profile(env_file, app, flutter_args):
    env_file, app = Path(env_file).absolute(), Path(app).absolute()
    root = env_file.parent
    if app != root / 'app/build/ios/Profile-dev-iphoneos/Runner.app':
        raise LocalEnvError('Profile installation requires the exact Profile-dev-iphoneos artifact')
    load_presets(root, single_env_file=env_file)  # Cheap validation before any build.
    env = clean_environment(os.environ)
    phone = select_iphone()
    expected = env.get('OMI_IOS_DEVICE_ID')
    if expected and expected not in {phone.get('identifier'), phone['hardwareProperties']['udid']}:
        raise LocalEnvError('Connected iPhone differs from the selected build destination')
    record = root / '.local/ios-profile.json'
    if record.is_symlink() or record.parent.is_symlink():
        raise LocalEnvError('Unsafe Profile attestation record')
    inputs = _profile_inputs(root, env, flutter_args)
    try:
        saved = json.loads(record.read_text()) if record.is_file() else {}
    except ValueError:
        saved = {}
    reuse = isinstance(saved, dict) and saved.get('inputs') == inputs and app.is_dir()
    if reuse:
        try:
            reuse = _attest_profile(root, app, phone, env)[1] == saved.get('executable_sha256')
        except (LocalEnvError, ValueError, OSError, KeyError):
            reuse = False
    print('Decision:', 'reuse' if reuse else 'build', '; configuration: Profile-dev / local_dev / offline', flush=True)
    commit = capture(['git', 'rev-parse', 'HEAD'], env=env, cwd=root).decode().strip()
    if not reuse:
        capture(['flutter', 'build', 'ios', '--profile', '--no-pub', '--flavor', 'dev', *flutter_args],
                env=env, cwd=root / 'app')
    if inputs != _profile_inputs(root, env, flutter_args):
        raise LocalEnvError('Profile build inputs changed; no installation performed')
    bundle, digest, entitlements = _attest_profile(root, app, phone, env)
    source_commit = saved.get('commit', commit) if reuse else commit
    data = {'inputs': inputs, 'commit': source_commit, 'executable_sha256': digest,
            'configuration': 'Profile-dev / local_dev / offline', 'entitlements': sorted(entitlements),
            'get_task_allow': entitlements.get('get-task-allow', False)}
    record.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=record.parent, delete=False) as output:
        temporary = Path(output.name)
        os.fchmod(output.fileno(), 0o600)
        json.dump(data, output)
    temporary.replace(record)
    print('Source commit:', source_commit)
    print('Artifact:', app)
    print('Executable SHA256:', digest)
    print('Signature: passed; entitlements:', ', '.join(sorted(entitlements)),
          '; get-task-allow=' + str(entitlements.get('get-task-allow', False)).lower(), flush=True)
    installed = json.loads(capture(['xcrun', 'devicectl', 'device', 'install', 'app', '--device',
                                   phone['hardwareProperties']['udid'], '--timeout', '120',
                                   '--json-output', '/dev/stdout', '--quiet', str(app)], env=env))
    if installed.get('info', {}).get('outcome') != 'success':
        raise LocalEnvError('iPhone installation was not confirmed')
    runtime_env, _hidden = launch_environment(root, environ=env, single_env_file=env_file)
    _launch(app, phone, runtime_env, bundle)
    print('Profile app installed and launched; saved server presets are available in Local Mac.')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--env', type=Path, default=Path('.env'))
    parser.add_argument('--app', type=Path, default=Path('app/build/ios/Profile-dev-iphoneos/Runner.app'))
    parser.add_argument('--build-profile', action='store_true')
    parser.add_argument('flutter_args', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        if args.build_profile:
            build_profile(args.env, args.app, args.flutter_args[1:] if args.flutter_args[:1] == ['--'] else args.flutter_args)
        elif args.flutter_args:
            parser.error('Flutter arguments require --build-profile')
        else:
            launch(args.env, args.app)
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(str(error) if isinstance(error, LocalEnvError) else 'Invalid local app or settings; secrets not displayed')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
