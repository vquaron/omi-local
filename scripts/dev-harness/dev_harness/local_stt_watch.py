"""Owned, resumable processing of captures created after the first opt-in."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import signal
import sys
import time
from collections import Counter
from pathlib import Path

from . import config, local_stt, safety

POLL_SECONDS = 2
MAX_ATTEMPTS = 3
START_TIMEOUT_SECONDS = 120  # Includes the configured engine's bounded 30–90s probe.


def settings(cfg) -> dict:
    path = cfg.layout.state_root / 'stt-watch.json'
    return json.loads(path.read_text()) if path.exists() else {'enabled': False}


def captures(cfg, *, require_root=False) -> dict[str, Path]:
    root = cfg.layout.services_dir / 'storage' / 'listen-captures'
    # Persist opaque local job keys, never capture paths or private identifiers.
    try:
        folders = list(root.iterdir())
    except FileNotFoundError:
        if require_root:
            raise
        return {}
    return {hashlib.sha256(p.name.encode()).hexdigest(): p for p in folders if p.is_dir()}


def queue_path(cfg) -> Path:
    return cfg.layout.services_dir / 'local-transcripts' / 'watch-queue.json'


def read_queue(cfg) -> dict:
    path = queue_path(cfg)
    return json.loads(path.read_text()) if path.exists() else {}


def worker_ready(cfg) -> bool:
    """Observe a held worker lock; a saved enabled flag is not readiness."""
    try:
        with (queue_path(cfg).parent / '.watch.lock').open('r') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(lock, fcntl.LOCK_UN)
    except FileNotFoundError:
        pass
    return False


def preflight(cfg) -> None:
    if cfg.provider_mode != 'offline' or cfg.local_transport not in config.PAIRED_TRANSPORTS:
        raise local_stt.TranscriptionError('Use the paired local Mac offline stack')
    safety.read_and_validate_sentinel(cfg.layout.state_root, repo_root=cfg.repo_root, instance=cfg.instance)
    engine = local_stt.EngineConfig.load(cfg)
    local_stt.backend_step(cfg)
    local_stt.check_model(engine)


def start_if_enabled(cfg, *, checked: bool = False) -> None:
    if not settings(cfg)['enabled']:
        return
    from . import cli

    if not cli._service_record(cfg, 'stt-worker'):
        if not checked:
            preflight(cfg)
        env = config.child_env_for(cfg)
        env.update({'OMI_LOCAL_STATE_ROOT': str(cfg.layout.state_root.parent),
                    'OMI_LOCAL_INSTANCE': cfg.instance, 'OMI_HARNESS_PRIVATE_UMASK': '077',
                    'OMI_DEV_BIND_HOST': cfg.dev_bind_host,
                    'OMI_HARNESS_PORT_OFFSET': str(cfg.backend_port - 8000)})
        cli._start_process(cfg, 'stt-worker', [sys.executable, '-m', 'dev_harness.local_stt_watch'],
                           cwd=cfg.repo_root, log_name='stt-worker.log', port=0, env=env)
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not cli._service_record(cfg, 'stt-worker'):
            raise local_stt.TranscriptionError('Automatic transcription worker stopped during model validation')
        if worker_ready(cfg):
            return
        time.sleep(0.2)
    raise local_stt.TranscriptionError('Automatic transcription readiness is indeterminate; inspect the owned worker')


def enable(cfg) -> int:
    preflight(cfg)
    data = settings(cfg)
    if 'excluded' not in data:
        # Include in-progress directories: opt-in never silently admits an old archive.
        data['excluded'] = sorted(captures(cfg))
    data['enabled'] = True
    local_stt.atomic_json(cfg.layout.state_root / 'stt-watch.json', data)
    start_if_enabled(cfg, checked=True)
    return status(cfg)


def disable(cfg) -> int:
    from . import cli

    safety.read_and_validate_sentinel(cfg.layout.state_root, repo_root=cfg.repo_root, instance=cfg.instance)
    data = settings(cfg)
    data['enabled'] = False
    local_stt.atomic_json(cfg.layout.state_root / 'stt-watch.json', data)
    record = cli._service_record(cfg, 'stt-worker')
    if record:
        cli._stop_single_service(cfg, record)
    if worker_ready(cfg):
        raise local_stt.TranscriptionError('Worker is still running; inspect owned service status')
    return status(cfg)


def status(cfg) -> int:
    from . import cli

    running = bool(cli._service_record(cfg, 'stt-worker')) and worker_ready(cfg)
    counts = Counter(job['state'] for job in read_queue(cfg).values())
    print(json.dumps({'automatic_transcription': settings(cfg)['enabled'], 'worker_running': running,
                      **{state: counts[state] for state in ('pending', 'processing', 'completed', 'no_speech', 'failed')}}))
    return 0


class Worker:
    def __init__(self, cfg):
        self.cfg = cfg
        self.jobs = read_queue(cfg)
        for job in self.jobs.values():
            if job['state'] == 'processing':
                # A crash/interrupt may happen after DB import but before the queue write.
                # Replaying transcribe reuses its JSON and create-if-absent Conversation.
                job.update(state='pending', retry_at=0, attempts=max(0, job['attempts'] - 1))
        self.save()

    def save(self):
        path = queue_path(self.cfg)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        local_stt.atomic_json(path, self.jobs)

    def tick(self, now: float | None = None) -> None:
        clock = time.time if now is None else lambda: now
        data = settings(self.cfg)
        if not data['enabled']:
            return
        try:
            paths = captures(self.cfg, require_root=True)
        except FileNotFoundError:
            return  # Missing storage is not evidence that individual recordings were deleted.
        retained = {
            key: job for key, job in self.jobs.items()
            if key in paths and ((paths[key] / 'audio.wav').exists() or (paths[key] / 'audio.pcm.part').exists())
        }
        if retained.keys() != self.jobs.keys():
            self.jobs = retained
            self.save()
        excluded = set(data['excluded'])
        for key, folder in sorted(paths.items(), key=lambda item: item[1].name):
            if key in excluded or key in self.jobs or not (folder / 'audio.wav').is_file():
                continue
            if (folder / 'audio.pcm.part').exists():
                continue
            try:
                metadata = json.loads((folder / 'metadata.json').read_text())
            except (OSError, ValueError):
                continue  # Capture publishes final metadata after WAV, atomically.
            if metadata.get('status') != 'completed':
                continue
            try:
                profile = local_stt.EngineConfig.load(self.cfg).profile()
            except local_stt.TranscriptionDisabled:
                break  # Existing queued snapshots still finish with their selected provider.
            self.jobs[key] = {'state': 'pending', 'attempts': 0, 'retry_at': 0, 'profile': profile}
            self.save()

        for key, job in self.jobs.items():
            if job['state'] != 'pending' or job['retry_at'] > clock():
                continue
            job.update(state='processing', attempts=job['attempts'] + 1)
            self.save()
            try:
                # Model settings stay pinned; cache provenance must describe the runtime actually used.
                profile = dict(job['profile'])
                profile.pop('runtime_revision', None)
                if profile['engine'] == 'whisperx':
                    # Legacy WhisperX jobs always ran diarization; do not inherit
                    # a later temporary single-speaker setting on their retries.
                    profile.setdefault('diarization_model', local_stt.EngineConfig().diarization_model)
                engine = local_stt.EngineConfig.load(self.cfg, profile=profile)
                job['profile'] = engine.profile()
                result = local_stt.transcribe(self.cfg, str(paths[key] / 'audio.wav'), engine=engine)
                if result != 0:
                    raise local_stt.TranscriptionError('Local transcription failed')
            except local_stt.NoSpeechDetected:
                job.update(state='no_speech', retry_at=0)
                self.save()
            except (local_stt.TranscriptionBusy, KeyboardInterrupt) as error:
                job.update(state='pending', attempts=job['attempts'] - 1, retry_at=clock() + POLL_SECONDS)
                self.save()
                if isinstance(error, KeyboardInterrupt):
                    raise
            except Exception as error:
                job.update(state='failed' if job['attempts'] >= MAX_ATTEMPTS else 'pending',
                           retry_at=clock() + 60 * job['attempts'])
                self.save()
                print(json.dumps({'transcription_job': job['state'], 'attempt': job['attempts'],
                                  'error_type': type(error).__name__}), flush=True)
            else:
                job.update(state='completed', retry_at=0)
                self.save()
            return  # One inference per turn; shutdown/config changes are observed between jobs.


def interrupt(_signum, _frame):
    # subprocess.run kills and reaps its ML child when KeyboardInterrupt unwinds it.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    raise KeyboardInterrupt


def main() -> int:
    os.umask(0o077)
    try:
        cfg = config.load_config(Path.cwd())
        safety.read_and_validate_sentinel(cfg.layout.state_root, repo_root=cfg.repo_root, instance=cfg.instance)
        if cfg.provider_mode != 'offline' or cfg.local_transport not in config.PAIRED_TRANSPORTS or not settings(cfg)['enabled']:
            raise local_stt.TranscriptionError('Automatic transcription is not enabled for this local stack')
        # The held readiness lock must follow validation in the actual child.
        local_stt.check_model(local_stt.EngineConfig.load(cfg))
        root = queue_path(cfg).parent
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (root / '.watch.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            signal.signal(signal.SIGINT, interrupt)
            signal.signal(signal.SIGTERM, interrupt)
            worker = Worker(cfg)
            while settings(cfg)['enabled']:
                worker.tick()
                time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        print(json.dumps({'worker': 'stopped', 'error_type': type(error).__name__}), flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
