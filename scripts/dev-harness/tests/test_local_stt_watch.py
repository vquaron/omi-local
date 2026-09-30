import json
import sys
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dev_harness import local_stt, local_stt_watch as watch


def test_worker_readiness_follows_validation_in_actual_child(tmp_path, monkeypatch):
    from dev_harness import config

    cfg = config.load_config(tmp_path, {'PROVIDER_MODE': 'offline', 'OMI_LOCAL_TRANSPORT': 'ngrok',
                                       'OMI_DEV_BIND_HOST': '127.0.0.1'}, create_layout=True)
    (cfg.layout.state_root / 'stt-watch.json').write_text('{"enabled": true, "excluded": []}')
    monkeypatch.setattr(watch.config, 'load_config', lambda *_: cfg)
    monkeypatch.setattr(local_stt.EngineConfig, 'load', lambda _: SimpleNamespace())

    def fail(_):
        assert not watch.worker_ready(cfg)
        raise local_stt.TranscriptionError('Synthetic model not ready')

    monkeypatch.setattr(local_stt, 'check_model', fail)
    assert watch.main() == 1
    assert not (watch.queue_path(cfg).parent / '.watch.lock').exists()
    validated = []
    monkeypatch.setattr(local_stt, 'check_model', lambda _: validated.append(True))
    monkeypatch.setattr(watch.signal, 'signal', lambda *_: None)

    def worker(_):
        assert validated == [True]
        assert watch.worker_ready(cfg)
        raise KeyboardInterrupt

    monkeypatch.setattr(watch, 'Worker', worker)
    assert watch.main() == 0
    assert not watch.worker_ready(cfg)


def test_existing_worker_can_finish_model_validation_after_five_seconds(tmp_path, monkeypatch):
    from dev_harness import cli, config

    cfg = config.load_config(tmp_path, {'PROVIDER_MODE': 'offline', 'OMI_LOCAL_TRANSPORT': 'ngrok',
                                       'OMI_DEV_BIND_HOST': '127.0.0.1'}, create_layout=True)
    (cfg.layout.state_root / 'stt-watch.json').write_text('{"enabled": true, "excluded": []}')
    elapsed = [0.0]
    monkeypatch.setattr(watch.time, 'monotonic', lambda: elapsed[0])
    monkeypatch.setattr(watch.time, 'sleep', lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds))
    monkeypatch.setattr(cli, '_service_record', lambda *_: {'service': 'stt-worker'})
    monkeypatch.setattr(watch, 'worker_ready', lambda _: elapsed[0] >= 8.0)
    starts = []
    monkeypatch.setattr(cli, '_start_process', lambda *a, **k: starts.append(True))
    watch.start_if_enabled(cfg, checked=True)
    assert elapsed[0] >= 8.0
    assert not starts, 'An owned worker validating its model must not be restarted'


@pytest.fixture(params=["ngrok", "tailscale"])
def setup(tmp_path, monkeypatch, request):
    state = tmp_path / 'state'
    state.mkdir()
    cfg = SimpleNamespace(provider_mode='offline', local_transport=request.param,
                          layout=SimpleNamespace(state_root=state, services_dir=state / 'services'))
    monkeypatch.setattr(watch, 'preflight', lambda *_: None)
    monkeypatch.setattr(watch, 'start_if_enabled', lambda *a, **kw: None)
    monkeypatch.setattr(watch, 'status', lambda *_: 0)
    calls, documents = [], set()

    def infer(engine, *_):
        calls.append(engine.model)
        return {'segments': [{'text': 'Synthetic fixture', 'start': 0, 'end': 1, 'speaker': 'SPEAKER_00'}]}

    def backend(cfg, folder=None):
        if folder:
            documents.add(folder.name)  # Model the existing DB create-if-absent boundary.
        return {'import': 'passed'}

    monkeypatch.setattr(local_stt, 'run_whisperx', infer)
    monkeypatch.setattr(local_stt, 'backend_step', backend)
    return cfg, calls, documents, backend


def capture(cfg, name, *, completed=True):
    folder = cfg.layout.services_dir / 'storage' / 'listen-captures' / name
    folder.mkdir(parents=True, exist_ok=True)
    with wave.open(str(folder / 'audio.wav'), 'wb') as wav:
        wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        wav.writeframes(bytes([len(name), 0]) * 16000)
    (folder / 'metadata.json').write_text(json.dumps({
        'status': 'completed' if completed else 'recording', 'decode_errors': 0,
        'duration_seconds': 1, 'source': 'phone', 'started_at': '2026-01-01T00:00:00Z',
    }))
    if not completed:
        (folder / 'audio.pcm.part').touch()
    else:
        (folder / 'audio.pcm.part').unlink(missing_ok=True)
    return folder


def test_opt_in_skips_archive_and_existing_capture_waits_for_finalization_then_processes_once(setup):
    cfg, calls, documents, _ = setup
    capture(cfg, 'archive')
    capture(cfg, 'previous-active', completed=False)
    watch.enable(cfg)
    worker = watch.Worker(cfg)
    capture(cfg, 'previous-active')
    capture(cfg, 'new', completed=False)
    worker.tick(0)
    assert not calls
    folder = capture(cfg, 'new')
    (folder / 'metadata.json.part').touch()  # Proven harmless stale sidecar.
    worker.tick(1)
    watch.Worker(cfg).tick(2)
    assert calls == ['large-v3-turbo']
    assert len(documents) == 1
    assert next(iter(watch.read_queue(cfg).values()))['state'] == 'completed'
    assert 'new' not in watch.queue_path(cfg).read_text()
    assert watch.queue_path(cfg).stat().st_mode & 0o777 == 0o600


def test_disabled_selected_stt_pauses_admission_but_keeps_existing_job(setup, monkeypatch):
    cfg, calls, documents, _ = setup
    watch.enable(cfg)
    capture(cfg, 'admitted')
    worker = watch.Worker(cfg)
    transcribe = local_stt.transcribe
    def busy(*args, **kwargs):
        raise local_stt.TranscriptionBusy('Synthetic occupied inference slot')
    monkeypatch.setattr(local_stt, 'transcribe', busy)
    worker.tick(0)
    (cfg.layout.state_root / 'providers.json').write_text(json.dumps({
        'version': 1, 'revision': 1, 'profiles': [],
        'effective': {'stt': None, 'diarization': None, 'summary': None}}))
    capture(cfg, 'new-while-disabled')
    monkeypatch.setattr(local_stt, 'transcribe', transcribe)
    worker.tick(3)
    assert calls == ['large-v3-turbo'] and len(documents) == 1
    assert len(worker.jobs) == 1 and next(iter(worker.jobs.values()))['state'] == 'completed'


@pytest.mark.parametrize('new_settings', [{'model': 'small'}, {'model': 'small', 'diarization_model': 'none'}])
def test_import_outage_keeps_json_and_pins_model_across_restart(setup, monkeypatch, new_settings):
    cfg, calls, documents, backend = setup
    watch.enable(cfg)
    capture(cfg, 'first')
    def unavailable(cfg, folder=None):
        if folder:
            raise local_stt.TranscriptionError('Synthetic import failure')
        return {}
    monkeypatch.setattr(local_stt, 'backend_step', unavailable)
    worker = watch.Worker(cfg)
    worker.tick(0)
    assert calls == ['large-v3-turbo'] and not documents
    (cfg.layout.state_root / 'stt-engine.json').write_text(json.dumps(new_settings))
    monkeypatch.setattr(local_stt, 'backend_step', backend)
    worker = watch.Worker(cfg)
    worker.tick(59)
    assert not documents
    worker.tick(61)
    capture(cfg, 'second')
    worker.tick(62)
    assert calls == ['large-v3-turbo', 'small']  # Retry reused JSON, new capture uses new model.
    assert len(documents) == 2
    assert all(job['state'] == 'completed' for job in watch.read_queue(cfg).values())


def test_crash_after_database_import_replays_without_second_inference_or_document(setup, monkeypatch):
    cfg, calls, documents, _ = setup
    watch.enable(cfg)
    capture(cfg, 'new')
    worker = watch.Worker(cfg)
    save = worker.save
    def lose_power():
        if any(job['state'] == 'completed' for job in worker.jobs.values()):
            raise KeyboardInterrupt  # Disk still says processing, DB already has result.
        save()
    monkeypatch.setattr(worker, 'save', lose_power)
    with pytest.raises(KeyboardInterrupt):
        worker.tick(0)
    assert next(iter(watch.read_queue(cfg).values()))['state'] == 'processing'
    watch.Worker(cfg).tick(1)
    assert len(documents) == 1 and calls == ['large-v3-turbo']
    assert next(iter(watch.read_queue(cfg).values()))['state'] == 'completed'


def test_retry_after_runtime_upgrade_records_actual_version_and_keeps_model_settings(setup, monkeypatch):
    cfg, _, _, _ = setup
    watch.enable(cfg)
    capture(cfg, 'pending')
    key = next(iter(watch.captures(cfg)))
    worker = watch.Worker(cfg)
    old = local_stt.EngineConfig(runtime_revision='old', python='/old/python', diarization_model='none')
    worker.jobs[key] = {'state': 'pending', 'attempts': 1, 'retry_at': 0, 'profile': old.profile()}
    worker.save()
    (cfg.layout.state_root / 'stt-engine.json').write_text(json.dumps({
        'runtime_revision': 'new', 'python': '/new/python', 'batch_size': 2, 'diarization_model': 'none'}))
    used = []
    monkeypatch.setattr(local_stt, 'transcribe', lambda *a, engine: used.append(engine) or 0)
    worker.tick(61)
    assert used[0].python == '/new/python'
    assert used[0].runtime_revision == 'new'
    assert used[0].batch_size == 1
    assert used[0].profile() != old.profile()
    assert watch.read_queue(cfg)[key]['profile'] == used[0].profile()


def test_busy_and_interrupt_do_not_exhaust_retries_and_failures_are_bounded(setup, monkeypatch):
    cfg, _, documents, _ = setup
    watch.enable(cfg)
    folder = capture(cfg, 'new')
    worker = watch.Worker(cfg)
    def fail(error):
        def run(*a, **kw):
            raise error
        monkeypatch.setattr(local_stt, 'transcribe', run)
    fail(local_stt.TranscriptionBusy('Synthetic contention'))
    for now in (0, 2, 4, 6):
        worker.tick(now)
    assert next(iter(worker.jobs.values()))['attempts'] == 0
    fail(KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        worker.tick(8)
    assert next(iter(worker.jobs.values()))['state'] == 'pending'
    assert next(iter(worker.jobs.values()))['attempts'] == 0
    fail(local_stt.TranscriptionError('Synthetic model failure'))
    for now in (10, 70, 190, 1000):
        worker.tick(now)
    job = next(iter(watch.read_queue(cfg).values()))
    assert job['state'] == 'failed' and job['attempts'] == 3
    assert (folder / 'audio.wav').is_file() and not documents


def test_reenable_preserves_backlog_boundary(setup):
    cfg, calls, _, _ = setup
    capture(cfg, 'archive')
    watch.enable(cfg)
    saved = watch.settings(cfg)
    local_stt.atomic_json(cfg.layout.state_root / 'stt-watch.json', {**saved, 'enabled': False})
    capture(cfg, 'new')
    watch.Worker(cfg).tick(0)
    assert not calls
    watch.enable(cfg)
    assert watch.settings(cfg)['excluded'] == saved['excluded']
    watch.Worker(cfg).tick(1)
    assert calls == ['large-v3-turbo']


@pytest.mark.parametrize('job_state', ['pending', 'completed', 'failed', 'no_speech'])
def test_deleted_recording_leaves_queue_without_retry(setup, monkeypatch, job_state):
    cfg, _, _, _ = setup
    watch.enable(cfg)
    folder = capture(cfg, 'deleted')
    key = next(iter(watch.captures(cfg)))
    worker = watch.Worker(cfg)
    worker.jobs[key] = {'state': job_state, 'attempts': 1, 'retry_at': 0,
                        'profile': local_stt.EngineConfig.load(cfg).profile()}
    worker.save()
    # Deletion can leave a harmless sidecar directory behind.
    (folder / 'audio.wav').unlink()
    (folder / 'metadata.json').unlink()
    monkeypatch.setattr(local_stt, 'transcribe', lambda *a, **k: pytest.fail('Deleted audio retried'))
    worker.tick(10)
    assert worker.jobs == {} and watch.read_queue(cfg) == {}


def test_missing_capture_root_preserves_queue(setup, monkeypatch):
    cfg, _, _, _ = setup
    watch.enable(cfg)
    folder = capture(cfg, 'stored')
    key = next(iter(watch.captures(cfg)))
    worker = watch.Worker(cfg)
    worker.jobs[key] = {'state': 'pending', 'attempts': 1, 'retry_at': 0,
                        'profile': local_stt.EngineConfig.load(cfg).profile()}
    worker.save()
    before = watch.queue_path(cfg).read_bytes()
    folder.parent.rename(folder.parent.with_name('temporarily-unavailable'))
    monkeypatch.setattr(local_stt, 'transcribe', lambda *a, **k: pytest.fail('Unavailable storage retried'))
    worker.tick(10)
    assert watch.queue_path(cfg).read_bytes() == before


def test_retry_delay_starts_after_processing_failure(setup, monkeypatch):
    cfg, _, _, _ = setup
    watch.enable(cfg)
    capture(cfg, 'new')
    clock = [0]
    monkeypatch.setattr(watch.time, 'time', lambda: clock[0])
    def long_failure(*a, **kw):
        clock[0] = 3600
        raise local_stt.TranscriptionError('Synthetic long failure')
    monkeypatch.setattr(local_stt, 'transcribe', long_failure)
    watch.Worker(cfg).tick()
    assert next(iter(watch.read_queue(cfg).values()))['retry_at'] == 3660


def test_standard_up_starts_opted_in_worker_after_endpoint_ready(tmp_path, monkeypatch):
    import io
    from dev_harness import cli, local_mac

    monkeypatch.setenv('PROVIDER_MODE', 'offline')
    monkeypatch.setenv('OMI_ENV_STAGE', 'offline')
    monkeypatch.setenv('OMI_LOCAL_TRANSPORT', 'ngrok')
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / 'backend'))
    cfg = SimpleNamespace(repo_root=Path(__file__).resolve().parents[3],
                          layout=SimpleNamespace(state_root=tmp_path), backend_url='http://127.0.0.1:20000',
                          provider_mode='offline', local_transport='ngrok')
    local_mac.private_json(tmp_path / 'pairing.json', local_mac.pairing_data('a' * 43))
    monkeypatch.setattr(local_mac, 'read_config', lambda _: {'url': 'https://synthetic.ngrok.app'})
    monkeypatch.setattr(local_mac, 'check_agent', lambda _: None)
    lifecycle = []
    monkeypatch.setattr(local_mac.local_live, 'require_backend_environment', lambda _: None)
    monkeypatch.setattr(local_mac.local_live, 'preflight_start', lambda _: None)
    monkeypatch.setattr(local_mac.local_transcription, 'check_models', lambda _: lifecycle.append('models'))
    monkeypatch.setattr(local_mac.local_live, 'start', lambda _: lifecycle.append('live'))
    monkeypatch.setattr(local_mac.local_live, 'require_ready', lambda _: lifecycle.append('ready'))
    monkeypatch.setattr(local_mac, 'ensure_owner_profile', lambda *a: None)
    monkeypatch.setattr(local_mac, 'require_auth_boundary', lambda _: None)
    monkeypatch.setattr(local_mac, 'ngrok_port', lambda _: 16040)
    monkeypatch.setattr(cli, 'cmd_check', lambda _: 0)
    monkeypatch.setattr(cli, 'cmd_up', lambda _: lifecycle.append('backend') or 0)
    monkeypatch.setattr(cli, '_start_process', lambda *a, **kw: None)
    monkeypatch.setattr(cli, '_service_health', lambda *a: (True, 'ready'))
    class Response(io.BytesIO):
        status = 200
    monkeypatch.setattr(local_mac.urllib.request, 'urlopen', lambda *a, **kw: Response())
    starts = []
    monkeypatch.setattr(watch, 'start_if_enabled', starts.append)
    assert local_mac.up(cfg) == 0
    assert lifecycle == ['models', 'live', 'backend', 'ready']
    assert starts == [cfg]


@pytest.mark.parametrize('crash_before_queue_save', [False, True])
def test_no_speech_is_cached_terminal_without_import_or_retry(setup, monkeypatch, tmp_path, crash_before_queue_save):
    from dev_harness import local_whisperkit, local_library
    cfg, calls, documents, _ = setup
    cfg.repo_root = tmp_path
    (cfg.layout.state_root / 'stt-engine.json').write_text(json.dumps({'engine': 'whisperkit', 'language': 'auto'}))
    monkeypatch.setattr(local_stt, 'check_model', lambda *_: None)
    monkeypatch.setattr(local_whisperkit.shutil, 'which', lambda _: '/usr/bin/sandbox-exec')
    def infer(command, **kwargs):
        calls.append('whisperkit')
        assert '(deny network*)' in command[2] and kwargs['capture_output']
        output = Path(command[command.index('--report-path') + 1])
        (output / 'audio.json').write_text(json.dumps({'language': 'en', 'segments': []}))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(local_whisperkit.subprocess, 'run', infer)
    watch.enable(cfg)
    folder = capture(cfg, 'short-synthetic')
    original = (folder / 'audio.wav').read_bytes()
    worker = watch.Worker(cfg)
    save = worker.save
    def lose_power():
        if any(job['state'] == 'no_speech' for job in worker.jobs.values()):
            raise KeyboardInterrupt
        save()
    if crash_before_queue_save:
        monkeypatch.setattr(worker, 'save', lose_power)
        with pytest.raises(KeyboardInterrupt):
            worker.tick(0)
    else:
        worker.tick(0)
    watch.Worker(cfg).tick(1000)
    job = next(iter(watch.read_queue(cfg).values()))
    assert job['state'] == 'no_speech' and job['attempts'] == 1 and job['retry_at'] == 0
    with pytest.raises(local_stt.NoSpeechDetected):
        local_stt.transcribe(cfg, str(folder / 'audio.wav'))
    assert calls == ['whisperkit'] and not documents
    assert (folder / 'audio.wav').read_bytes() == original
    assert local_library.Library(cfg.layout.services_dir).scan()[0]['status'] == 'no_speech'
