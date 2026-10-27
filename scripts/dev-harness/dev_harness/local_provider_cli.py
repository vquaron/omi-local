"""Local operator CLI for the same guarded provider registry as the library."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys

from . import config, safety
from .local_provider_api import Settings
from .local_providers import STAGES, SettingsError
from .local_transport import TransportError, prepare_environment
from .local_stt_services import ActivationIndeterminate, ServiceError

LIMIT = 512 * 1024
PROFILE_FIELDS = {'id', 'name', 'stage', 'kind', 'settings'}


def parser():
    root = argparse.ArgumentParser(description='Processing profiles: save a draft, then explicitly use it.')
    commands = root.add_subparsers(dest='command', required=True)
    commands.add_parser('list', help='Show saved and selected profiles')
    export = commands.add_parser('export', help='Export editable JSON without API keys; does not change selection')
    export.add_argument('profile', nargs='?', help='Profile ID or unique name; omit to export all drafts')
    export.add_argument('--file', default='-', help='New output file (0600), or - for stdout')
    save = commands.add_parser('save', help='Save JSON drafts; does not activate them')
    save.add_argument('--file', required=True, help='Profile or exported document; - reads stdin')
    key = save.add_mutually_exclusive_group()
    key.add_argument('--api-key-env', metavar='NAME', help='Environment variable containing key (single profile only)')
    key.add_argument('--clear-key', action='store_true', help='Remove the saved key (single profile only)')
    use = commands.add_parser('use', help='Check and activate a saved profile for new work')
    use.add_argument('stage', choices=STAGES)
    use.add_argument('profile', help='Profile ID or unique name within the stage')
    disable = commands.add_parser('disable', help='Disable an optional stage')
    disable.add_argument('stage', choices=[stage for stage in STAGES if stage != 'stt'])
    check = commands.add_parser('check', help='Check readiness without sending audio')
    check.add_argument('profile', help='Profile ID or unique name')
    return root


def select(state, name, stage=None):
    profiles = [p for p in state['profiles'] if stage is None or p['stage'] == stage]
    matches = [p for p in profiles if p['id'] == name] or [p for p in profiles if p['name'] == name]
    if len(matches) != 1:
        raise SettingsError('Profile not found or name is ambiguous; use its ID from providers export.')
    return matches[0]


def editable(profile):
    return {key: value for key, value in profile.items() if key in PROFILE_FIELDS}


def read_profiles(filename, stdin):
    try:
        if filename == '-':
            raw = stdin.read(LIMIT + 1)
        else:
            with open(filename) as source:
                raw = source.read(LIMIT + 1)
        if len(raw.encode('utf-8')) > LIMIT:
            raise ValueError()
        value = json.loads(raw)
    except (OSError, ValueError, UnicodeError):
        raise SettingsError('Cannot read profile JSON (maximum 512 KiB).') from None
    if isinstance(value, dict) and 'profiles' in value:
        if value.get('version') != 1 or set(value) - {'version', 'profiles', 'active'}:
            raise SettingsError('Unsupported profile document; expected version 1.')
        profiles = value['profiles']
    else:
        profiles = [value]
    if not isinstance(profiles, list) or not 1 <= len(profiles) <= 64:
        raise SettingsError('Provide between 1 and 64 profiles.')
    for profile in profiles:
        if not isinstance(profile, dict) or set(profile) - PROFILE_FIELDS:
            raise SettingsError('Only id, name, stage, kind and settings belong in a profile; supply keys via --api-key-env.')
    ids = [p['id'] for p in profiles if 'id' in p]
    if any(not isinstance(item, str) for item in ids) or len(set(ids)) != len(ids):
        raise SettingsError('Profile IDs must be unique strings.')
    return profiles


def execute(args, settings, *, stdin, stdout, environ):
    state = settings.read()
    if args.command == 'list':
        for stage in STAGES:
            current = state['effective'][stage]
            print(f'{stage}: {current["name"] if current else "off"}', file=stdout)
            for profile in state['profiles']:
                if profile['stage'] == stage:
                    selected = current and current['id'] == profile['id']
                    changed = selected and current != profile
                    marker = 'selected; draft changed' if changed else 'selected' if selected else 'saved'
                    print(f'  {profile["name"]} [{profile["kind"]}] — {marker}', file=stdout)
        return 0
    if args.command == 'export':
        payload = editable(select(state, args.profile)) if args.profile else {
            'version': 1, 'profiles': [editable(p) for p in state['profiles']], 'active': state['active']}
        content = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
        if args.file == '-':
            stdout.write(content)
        else:
            try:
                fd = os.open(args.file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'w') as target:
                    target.write(content)
            except OSError:
                raise SettingsError('Cannot create export file; choose a new writable path.') from None
        return 0
    if args.command == 'save':
        profiles = read_profiles(args.file, stdin)
        extra = {}
        if args.api_key_env or args.clear_key:
            if len(profiles) != 1:
                raise SettingsError('Key options require exactly one profile.')
            if args.clear_key:
                extra['clear_key'] = True
            else:
                if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', args.api_key_env):
                    raise SettingsError('Invalid key environment variable name.')
                key = environ.get(args.api_key_env)
                if not key:
                    raise SettingsError('Key environment variable is empty or unset.')
                extra['api_key'] = key
        # Validate the whole input before the first save. Every save still checks
        # its revision; concurrent edits are not retried or overwritten silently.
        for profile in profiles:
            settings.registry.draft({'profile': profile, **extra})
        for profile in profiles:
            state = settings.dispatch('POST', '/api/settings/save', {
                'revision': state['revision'], 'profile': profile, **extra})
            print(f'Saved: {profile["name"]}. Selection unchanged.', file=stdout)
        return 0
    if args.command == 'check':
        result = settings.dispatch('POST', '/api/settings/probe', {'profile': select(state, args.profile)})
        print(f'{result["state"]}: {result["message"]}', file=stdout)
        return 0 if result['state'] in {'ready', 'available'} else 1
    profile = select(state, args.profile, args.stage) if args.command == 'use' else None
    settings.dispatch('POST', '/api/settings/activate', {
        'revision': state['revision'], 'stage': args.stage, 'id': profile['id'] if profile else None})
    print(f'{args.stage}: {profile["name"] if profile else "off"}. Applied to new work.', file=stdout)
    return 0


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        repo = Path.cwd()
        if os.environ.get('OMI_CONTAINER_RUNTIME') != '1':
            prepare_environment(repo, verify=False)
        cfg = config.load_config(repo, create_layout=False)
        return execute(args, Settings(cfg), stdin=sys.stdin, stdout=sys.stdout, environ=os.environ)
    except ActivationIndeterminate as exc:
        print(f'{exc}. Inspect providers list and owned service status before retrying.', file=sys.stderr)
        return 1
    except (SettingsError, safety.SafetyError, TransportError, ServiceError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (OSError, ValueError):
        print('Processing configuration is unavailable; check the selected instance and runtime.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
