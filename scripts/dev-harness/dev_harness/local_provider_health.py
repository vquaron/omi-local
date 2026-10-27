"""Periodic readiness of the selected providers, written for the phone status endpoint.

The stt-worker is the one owned process allowed to reach selected providers, so it
runs the same probe the library's Check button uses and stores only names and
states. The backend reads the file; it never contacts providers itself.
"""
from __future__ import annotations

import time

from .local_provider_probe import probe as default_probe
from .local_providers import STAGES, Registry, SettingsError
from .local_stt import atomic_json

FILE_NAME = 'provider-health.json'
INTERVAL_SECONDS = 60
REMOTE_KINDS = {'openai-compatible', 'mycelia', 'external', 'whisperlivekit'}
STATES = {'ready': 'ready', 'available': 'ready', 'unavailable': 'unavailable', 'misconfigured': 'misconfigured'}


def path(cfg):
    return cfg.layout.state_root / FILE_NAME


def collect(cfg, *, probe=default_probe, now=None):
    registry = Registry(cfg)
    stages = {}
    if registry.exists:
        for stage in STAGES:
            snapshot = registry.snapshot(stage)
            if snapshot is None:
                stages[stage] = {'status': 'disabled'}
                continue
            entry = {'kind': snapshot['kind'], 'name': snapshot['name']}
            if snapshot['kind'] not in REMOTE_KINDS:
                # Local engines are validated by the worker's own preflight at start.
                stages[stage] = {'status': 'ready', **entry}
                continue
            try:
                key = registry.key(snapshot)
            except SettingsError:
                key = ''
            try:
                result = probe(cfg, snapshot, key=key)
                status = STATES.get(result.get('state'), 'unavailable')
                latency = result.get('latency_ms')
            except Exception:  # noqa: BLE001 - a broken probe must not stop transcription.
                status, latency = 'unavailable', None
            stages[stage] = {'status': status, **entry, **({'latency_ms': latency} if type(latency) is int else {})}
    return {'version': 1, 'checked_at': int(time.time() if now is None else now), 'stages': stages}


def write(cfg, report):
    atomic_json(path(cfg), report)


class Monitor:
    def __init__(self, cfg, *, interval=INTERVAL_SECONDS, collect=None):
        self.cfg = cfg
        self.interval = interval
        self._collect = collect or (lambda cfg, now: globals()['collect'](cfg, now=now))
        self._last = None

    def tick(self, now=None):
        now = time.time() if now is None else now
        if self._last is not None and now - self._last < self.interval:
            return
        self._last = now
        try:
            write(self.cfg, self._collect(self.cfg, now))
        except OSError:
            pass
