"""Read-only loopback observation. Keys and drafts never enter logs or files."""

from collections import Counter, deque
from datetime import datetime, timezone
import json
import threading
import time

import httpx

from . import local_env, local_openai_stt, local_stt_watch


class Runtime:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.events = deque(maxlen=40)
        self.previous = None
        self.cached = None
        self.checked_at = 0

    def _settings(self, name):
        path = self.cfg.layout.state_root / name
        return json.loads(path.read_text()) if path.is_file() and not path.is_symlink() else {}

    def _preview(self):
        # Derive the destination from the owned port, never a browser parameter.
        key = local_env.read_env(self.cfg.repo_root / '.env')['OMI_LOCAL_APP_KEY']
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=2) as client:
            response = client.get(f'http://127.0.0.1:{self.cfg.backend_port}/v1/local/preview',
                                  headers={'Authorization': 'Bearer ' + key})
            response.raise_for_status()
            data = response.json()
            if (not isinstance(data, dict) or not isinstance(data.get('sessions'), list)
                    or not isinstance(data.get('capture'), dict) or not isinstance(data.get('live_transcript'), dict)):
                raise ValueError('Invalid runtime response')
            return data

    def _final(self):
        jobs = local_stt_watch.read_queue(self.cfg)
        counts = Counter(job['state'] for job in jobs.values())
        engine = self._settings('stt-engine.json')
        from .local_providers import Registry
        registry = Registry(self.cfg)
        selected = registry.snapshot('stt') if registry.exists else None
        active_job = next((job for job in jobs.values() if job['state'] == 'processing'), None)
        if active_job:
            pinned = active_job.get('profile', {})
            if pinned:
                selected = pinned.get('pipeline', {}).get('stt')
                if not selected:
                    engine = pinned
        if selected:
            engine = {'engine': selected['kind'], **selected['settings']}
        enabled = self._settings('stt-watch.json').get('enabled', False)
        ready = enabled and local_stt_watch.worker_ready(self.cfg)
        state = 'ready' if ready else 'stopped'
        if ready and not selected and engine.get('engine') == 'openai-compatible':
            try:
                local_openai_stt.check(engine['provider_url'], engine['model'], timeout=1)
            except (httpx.HTTPError, ValueError, KeyError):
                state = 'unavailable'
        if ready and counts['processing']:
            state = 'processing'
        label = {'openai-compatible': 'OpenAI-совместимый STT', 'whisperkit': 'WhisperKit',
                 'whisperx': 'WhisperX', 'parakeet-mlx': 'Parakeet MLX'}.get(engine.get('engine'), 'Не настроен')
        if selected:
            label = selected['name']
        argmax = self._settings('argmax-stt.json')
        if (not selected and argmax.get('enabled') and engine.get('engine') == 'openai-compatible'
                and engine.get('provider_url') == f"http://127.0.0.1:{argmax.get('port')}/v1"):
            label = 'Argmax · WhisperKit'
        return {'state': state, 'provider': label,
                'jobs': {name: counts[name] for name in ('pending', 'processing', 'completed', 'failed', 'no_speech')}}

    def _event(self, message):
        self.events.appendleft({'observed_at': datetime.now(timezone.utc).isoformat(), 'message': message})

    def _observe(self, current):
        previous = self.previous
        connected = current['backend'] == 'ready'
        if previous is None or connected != (previous['backend'] == 'ready'):
            self._event('Связь с сервером установлена.' if connected else 'Сервер недоступен. Проверка продолжается.')
        sessions = current.get('sessions', [])
        old_sessions = previous.get('sessions', []) if previous else []
        identity = sorted((s.get('preview_id') or '', s['source']) for s in sessions)
        old_identity = sorted((s.get('preview_id') or '', s['source']) for s in old_sessions)
        if identity and identity != old_identity:
            sources = ', '.join(sorted({'CV1' if s['source'] == 'omi' else 'микрофон iPhone' for s in sessions}))
            self._event('Получена текущая запись: ' + sources + '.')
        if connected and old_sessions and not sessions:
            self._event('Поток записи закрыт. Ожидаем сохранение и финальное распознавание.')
        if current['live_transcript'].get('updates', 0) and (not previous
                or not previous['live_transcript'].get('updates', 0) or identity != old_identity):
            self._event('Live-текст отправлен в приложение.')
        live_state = current['live_transcript']['state']
        if live_state in {'failed', 'unavailable'} and (not previous or previous['live_transcript']['state'] != live_state):
            self._event('Live-распознавание недоступно. Состояние аудиопотока показано отдельно.')
        if previous and previous['final_stt']['state'] != 'unavailable' and current['final_stt']['state'] != 'unavailable':
            jobs, old_jobs = current['final_stt']['jobs'], previous['final_stt']['jobs']
            for state, message in [('processing', 'Началось финальное распознавание.'),
                                   ('completed', 'Финальный транскрипт сохранён и доступен в аудиотеке.'),
                                   ('no_speech', 'Финальная обработка завершена: речь не обнаружена.'),
                                   ('failed', 'Финальное распознавание завершилось с ошибкой.')]:
                if jobs[state] > old_jobs[state]:
                    self._event(message)
        # Retain only counters/ephemeral identity for event comparison, never draft text.
        self.previous = {**current, 'sessions': [{k: v for k, v in s.items() if k != 'text'} for s in sessions]}

    def snapshot(self):
        with self.lock:
            if self.cached is not None and time.monotonic() - self.checked_at < 1:
                return self.cached
            try:
                current = self._preview()
            except (httpx.HTTPError, OSError, ValueError, KeyError):
                current = {'backend': 'unavailable', 'capture': {'state': 'unknown'},
                           'live_transcript': {'state': 'unavailable', 'updates': 0},
                           'diarization': {'state': 'unavailable', 'labeled_segments': 0}, 'sessions': []}
            try:
                current['final_stt'] = self._final()
                live = self._settings('live-stt.json')
                current['live_transcript']['provider'] = ('WhisperLiveKit · MLX'
                    if live.get('provider') == 'whisperlivekit' else 'Внешний live STT')
                from .local_providers import Registry
                registry = Registry(self.cfg)
                if registry.exists:
                    selected = registry.snapshot('live')
                    current['live_transcript']['provider'] = selected['name'] if selected else 'Выключено'
                    if current['live_transcript']['state'] == 'ready':
                        # Relay readiness proves its transport, not upstream inference.
                        current['live_transcript']['state'] = 'configured' if selected else 'disabled'
            except (OSError, ValueError, KeyError, TypeError):
                current['final_stt'] = {'state': 'unavailable', 'provider': 'Недоступен',
                                        'jobs': dict.fromkeys(('pending', 'processing', 'completed', 'failed', 'no_speech'), 0)}
            self._observe(current)
            self.cached = {**current, 'events': list(self.events),
                           'observed_at': datetime.now(timezone.utc).isoformat()}
            self.checked_at = time.monotonic()
            return self.cached
