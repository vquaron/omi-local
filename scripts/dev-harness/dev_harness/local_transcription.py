"""Prepare configured Mac models before enabling first-install transcription."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

from . import (config, local_live, local_live_install, local_stt, local_stt_services, local_stt_watch, local_whisperkit,
               safety, transcription_lock, whisperkit_install)


class TranscriptionSetupError(ValueError):
    """Fixed diagnostics; configuration paths and native error output stay private."""


DEFAULT_ENGINE = {
    'engine': 'whisperkit', 'model': local_whisperkit.MODEL,
    'language': 'auto', 'diarization_model': 'none',
}

_ERRORS = (OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.SubprocessError, safety.SafetyError)


def _checked(call, message):
    try:
        return call()
    except _ERRORS:
        raise TranscriptionSetupError(message) from None


def _safe_path(cfg, path):
    """Reject redirects before writing setup state or acquiring its shared lock."""
    for item in (path, *path.parents):
        if item == cfg.repo_root:
            break
        if item.is_symlink():
            raise TranscriptionSetupError('Небезопасный путь локальных настроек распознавания.')


def _validate_state(cfg, *, required=False):
    if cfg.provider_mode != 'offline' or cfg.local_transport not in config.PAIRED_TRANSPORTS:
        raise TranscriptionSetupError('Подготовка распознавания требует локальный Mac в парном режиме offline.')
    root = cfg.layout.state_root
    _safe_path(cfg, root)
    if cfg.layout.services_dir != root / 'services':
        raise TranscriptionSetupError('Неверный каталог локальных сервисов распознавания.')
    _checked(lambda: safety.validate_safe_state_root(root, cfg.repo_root),
             'Небезопасный каталог локального окружения.')
    if root.exists() or required:
        _safe_path(cfg, root / safety.HARNESS_SENTINEL_FILENAME)
        _checked(lambda: safety.read_and_validate_sentinel(root, repo_root=cfg.repo_root, instance=cfg.instance),
                 'Не подтверждён владелец локального окружения; подготовка остановлена.')


def _settings_path(cfg, name):
    path = cfg.layout.state_root / name
    _safe_path(cfg, path)
    return path


def final_enabled(cfg):
    """A new installation opts in; an existing explicit off choice wins."""
    path = _settings_path(cfg, 'stt-watch.json')
    if not path.exists():
        return True
    data = _checked(lambda: json.loads(path.read_text()), 'Не удалось прочитать stt-watch.json.')
    if not isinstance(data, dict) or type(data.get('enabled')) is not bool:
        raise TranscriptionSetupError('В stt-watch.json требуется enabled: true или false.')
    return data['enabled']


def _engine(cfg):
    path = _settings_path(cfg, 'stt-engine.json')
    return _checked(lambda: local_stt.EngineConfig.load(cfg, profile=None if path.exists() else DEFAULT_ENGINE),
                    'Cannot verify the selected final engine; check stt-engine.json.')


def _live_settings(cfg):
    _settings_path(cfg, 'live-preview.json')
    return _checked(lambda: local_live.settings(cfg), 'Cannot verify live-preview.json.')


def _managed_live(cfg, data):
    return data['enabled'] and _checked(lambda: local_live.managed(cfg), 'Cannot verify live transcription settings.')


def _installed(check, missing_error):
    try:
        check()
    except missing_error:
        return False
    except _ERRORS:
        raise TranscriptionSetupError('Cannot verify installed transcription files.') from None
    return True


def _check_final(cfg, engine):
    if _checked(lambda: local_stt_services.owns_final_engine(cfg, engine),
                'Selected Argmax files are not ready; check argmax-stt.json.'):
        return  # HTTP readiness belongs after the lifecycle starts this owned service.
    return _checked(lambda: local_stt.check_model(engine),
                    'Выбранный финальный движок не готов. Подготовьте его по docs/LOCAL_STT.md; выбор сохранён.')


def check_models(cfg):
    """Read-only model checks; service/HTTP readiness belongs to the lifecycle."""
    _validate_state(cfg)
    _checked(lambda: local_stt_services.preflight_argmax(cfg),
             'Selected Argmax files are not ready; check argmax-stt.json.')
    if final_enabled(cfg):
        _check_final(cfg, _engine(cfg))
    data = _live_settings(cfg)
    if _managed_live(cfg, data):
        _checked(lambda: local_live_install.installed(cfg.repo_root),
                 'Live transcription is not prepared. Run: ./omiloc bootstrap')


def check_service_models(cfg):
    """Probe the selected owned final model only after its service has started."""
    if final_enabled(cfg):
        engine = _engine(cfg)
        if local_stt_services.owns_final_engine(cfg, engine):
            _checked(lambda: local_stt.check_model(engine),
                     'Selected final engine is not ready after starting its local service.')


def prepare(cfg):
    """Install only missing managed models; never change engine or opt-in state."""
    _validate_state(cfg)
    _checked(lambda: local_stt_services.preflight_argmax(cfg),
             'Selected Argmax files are not ready; check argmax-stt.json.')
    final_root = None
    if final_enabled(cfg):
        engine = _engine(cfg)
        managed_root = cfg.repo_root / '.local/whisperkit'
        if engine.engine == 'whisperkit' and Path(engine.assets_path) == managed_root:
            _safe_path(cfg, managed_root)
            if not _installed(lambda: local_whisperkit.installed(managed_root), local_whisperkit.WhisperKitError):
                final_root = managed_root
            else:
                _check_final(cfg, engine)
        else:
            # Prepared explicit providers/paths are supported without replacing
            # the user's chosen engine or writing outside managed model roots.
            _check_final(cfg, engine)
    live = _live_settings(cfg)
    needs_live = False
    if _managed_live(cfg, live):
        needs_live = not _installed(lambda: local_live_install.installed(cfg.repo_root), local_live_install.LiveInstallError)
    if final_root is None and not needs_live:
        return

    # All prerequisites precede either install and even first-run lock/layout
    # creation: a failed second model preflight cannot leave a partial setup.
    if final_root is not None:
        _checked(lambda: whisperkit_install.preflight(final_root),
                 'WhisperKit: check Xcode/Swift, macOS SDK, free disk space and sandbox availability.')
    if needs_live:
        _checked(lambda: local_live_install.preflight(cfg.repo_root),
                 'Live: check pinned sources, Xcode/Swift, macOS SDK, free disk space and sandbox.')

    if not cfg.layout.state_root.exists():
        _checked(lambda: safety.create_state_layout(
            cfg.repo_root, cfg.instance, {'OMI_LOCAL_STATE_ROOT': str(cfg.layout.state_root.parent)}),
            'Cannot create the local transcription environment.')
    _validate_state(cfg, required=True)
    try:
        lock_context = transcription_lock.acquire(cfg.repo_root, cfg=cfg)
        with lock_context as lock:
            # Another setup may have completed while prerequisites were checked.
            if final_root is not None and not _installed(
                    lambda: local_whisperkit.installed(final_root), local_whisperkit.WhisperKitError):
                _checked(lambda: whisperkit_install.install(final_root, shared_lock=lock),
                         'WhisperKit preparation stopped; check .local/whisperkit/build.log and retry bootstrap.')
                _checked(lambda: local_whisperkit.installed(final_root),
                         'WhisperKit readiness is unverified after installation; settings preserved.')
            if needs_live and not _installed(lambda: local_live_install.installed(cfg.repo_root), local_live_install.LiveInstallError):
                _checked(lambda: local_live_install.install(cfg.repo_root, shared_lock=lock),
                         'Live preparation stopped; check .local/parakeet-live/build.log and retry bootstrap.')
                _checked(lambda: local_live_install.installed(cfg.repo_root),
                         'Live readiness is unverified after installation; settings preserved.')
    except transcription_lock.TranscriptionLockBusy as error:
        raise TranscriptionSetupError(str(error)) from None
    except transcription_lock.TranscriptionLockError as error:
        raise TranscriptionSetupError(str(error)) from None


def configure_defaults(cfg):
    """Commit first defaults only after preparation and harness ownership checks."""
    _validate_state(cfg, required=True)
    engine_path = _settings_path(cfg, 'stt-engine.json')
    watch_path = _settings_path(cfg, 'stt-watch.json')
    live_path = _settings_path(cfg, 'live-preview.json')
    # Validate all existing settings and gather defaults before the first write.
    if final_enabled(cfg) and engine_path.exists():
        _engine(cfg)
    live = _live_settings(cfg)
    defaults = []
    if not engine_path.exists():
        defaults.append((engine_path, DEFAULT_ENGINE))
    if not watch_path.exists():
        _safe_path(cfg, cfg.layout.services_dir / 'storage/listen-captures')
        excluded = _checked(lambda: sorted(local_stt_watch.captures(cfg)),
                            'Cannot inspect existing recordings; automatic processing remains disabled.')
        defaults.append((watch_path, {'enabled': True, 'excluded': excluded}))
    if not live_path.exists():
        defaults.append((live_path, live))
    for path, data in defaults:
        _checked(lambda: local_stt.atomic_json(path, data),
                 'Cannot save initial transcription settings; retry bootstrap.')
