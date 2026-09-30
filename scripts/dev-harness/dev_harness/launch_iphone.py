"""Provision the installed local iPhone app without embedding secrets in its binary."""

import argparse
import json
import os
from pathlib import Path
import plistlib
import subprocess

from .local_env import LocalEnvError, read_env


def capture(args, *, env=None):
    result = subprocess.run(args, env=env, capture_output=True)
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
        phones = [device for device in devices
                  if device['hardwareProperties'].get('deviceType') == 'iPhone'
                  and device['connectionProperties'].get('pairingState') == 'paired'
                  and device['connectionProperties'].get('transportType') in ('wired', 'localNetwork')]
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


def launch(env_file, app):
    values = read_env(env_file)
    from .local_transport import normalize_tailscale_url, tailscale_ip
    if values['OMI_LOCAL_TRANSPORT'] == 'tailscale':
        ip = tailscale_ip(values.get('OMI_TAILSCALE_IP', ''))
        port = int(os.environ.get('OMI_HARNESS_BACKEND_PORT') or 8000 + int(os.environ.get('OMI_HARNESS_PORT_OFFSET', '12000')))
        url = normalize_tailscale_url(ip, port)
    else:
        url = values['OMI_NGROK_URL']
    if not values.get('OMI_LOCAL_APP_KEY'):
        raise LocalEnvError('Set the existing app key in .env to configure the phone automatically')
    app = Path(app)
    capture(['codesign', '--verify', '--deep', '--strict', str(app)])
    info = plistlib.loads((app / 'Info.plist').read_bytes())
    bundle = info['CFBundleIdentifier']
    if not bundle.startswith('com.omi.local.'):
        raise LocalEnvError('Only the separately signed local iPhone app can receive these settings')
    phone = select_iphone()
    env = {**os.environ,
           'DEVICECTL_CHILD_OMI_LOCAL_MAC_URL': url,
           'DEVICECTL_CHILD_OMI_LOCAL_MAC_KEY': values['OMI_LOCAL_APP_KEY']}
    # The ngrok agent credential is deliberately not forwarded, even if ambient.
    env.pop('NGROK_AUTHTOKEN', None)
    env.pop('DEVICECTL_CHILD_NGROK_AUTHTOKEN', None)
    result = json.loads(capture([
        'xcrun', 'devicectl', 'device', 'process', 'launch',
        '--device', phone['hardwareProperties']['udid'], '--terminate-existing',
        '--json-output', '/dev/stdout', '--quiet', bundle,
    ], env=env))
    if result.get('info', {}).get('outcome') != 'success':
        raise LocalEnvError('iPhone launch was not confirmed')
    print('Local iPhone app restarted with private pairing settings; ngrok authtoken stayed on Mac')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--env', type=Path, default=Path('.env'))
    parser.add_argument('--app', type=Path, default=Path('app/build/ios/Profile-dev-iphoneos/Runner.app'))
    args = parser.parse_args()
    try:
        launch(args.env, args.app)
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(str(error) if isinstance(error, LocalEnvError) else 'Invalid local app or settings; secrets not displayed')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
