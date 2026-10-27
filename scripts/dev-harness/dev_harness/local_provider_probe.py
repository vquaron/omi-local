"""Metadata-only checks. A reachable server is not evidence of successful inference."""
from __future__ import annotations

import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

from . import local_provider_http as transport


def probe(cfg, profile, *, key='', models=False):
    started = time.monotonic()
    def result(state, message, **extra):
        return {'state': state, 'message': message,
                'latency_ms': round((time.monotonic() - started) * 1000), **extra}
    kind, stage, settings = profile['kind'], profile['stage'], profile['settings']
    try:
        if stage == 'stt' and kind != 'openai-compatible':
            from .local_stt import EngineConfig, check_model
            engine = EngineConfig.load(cfg, profile={'engine': kind, **settings})
            check_model(engine)
            return result('available', 'Локальные файлы и зависимости проверены. Обработка записи проверяется отдельно.',
                          models=[engine.model])
        if kind == 'pyannote':
            python = Path(settings.get('speaker_python') or cfg.repo_root / '.local/diarization/venv/bin/python').expanduser()
            if not python.is_file():
                return result('misconfigured', 'Не найден Python локальной диаризации. Укажите подготовленное окружение.')
            program = ("import sys; import importlib.util; "
                       "sys.addaudithook(lambda event,args: (_ for _ in ()).throw(RuntimeError()) "
                       "if event in {'socket.connect','socket.getaddrinfo'} else None); "
                       "assert importlib.util.find_spec('pyannote.audio'); "
                       "from huggingface_hub import snapshot_download; "
                       "snapshot_download(sys.argv[1],local_files_only=True)")
            checked = subprocess.run([str(python), '-c', program, settings['speaker_model']],
                                     capture_output=True, timeout=30)
            if checked.returncode:
                return result('misconfigured', 'Окружение или кэш модели Pyannote не готовы. Загрузки не выполнялись.')
            return result('available', 'Кэш Pyannote найден. Работа модели проверяется на записи.',
                          models=[settings['speaker_model']])
        if stage in {'stt', 'summary', 'translation'}:
            base = transport.validate_url(settings.get('provider_url') or settings.get('base_url', ''))
            with transport.client(key=key, timeout=8) as client:
                response = transport.metadata_get(client, base + '/models')
                if response.status_code in {404, 405}:
                    return result('available', 'Сервер доступен, список моделей не поддерживается. Укажите модель вручную.', models=[])
                catalogue = transport.model_ids(transport.json_response(response))
            model = settings.get('model')
            if model and model not in catalogue and not models:
                return result('misconfigured', 'Выбранная модель отсутствует в списке сервера.', models=catalogue)
            return result('ready', 'Сервер и список моделей доступны. Выполнение запроса ещё не проверялось.', models=catalogue)
        if stage == 'live':
            base = transport.validate_url(settings['url'], websocket=True)
            parsed = urlsplit(base)
            endpoint = urlunsplit(('https' if parsed.scheme == 'wss' else 'http', parsed.netloc,
                                  parsed.path.rsplit('/', 1)[0] + '/health', '', ''))
        else:
            base = transport.validate_url(settings['base_url'])
            endpoint = base + '/ready'
        with transport.client(key=key, timeout=8) as client:
            response = transport.metadata_get(client, endpoint)
            if stage == 'diarization' and response.status_code in {404, 405}:
                response = transport.metadata_get(client, base + '/health')
            if stage == 'live' and response.status_code in {404, 405}:
                return result('available', 'Сервер отвечает, но не предоставляет /health. Протокол будет проверен при подключении.')
            if response.status_code == 503:
                return result('unavailable', 'Сервер ещё не готов: модель загружается или недоступна.')
            data = transport.json_response(response)
        if data.get('ready') is True:
            return result('ready', 'Сервер сообщает о готовности модели.')
        if data.get('ready') is False:
            return result('unavailable', 'Сервер сообщает, что модель ещё не готова.')
        return result('available', 'Сервер отвечает, готовность модели не подтверждена.')
    except subprocess.TimeoutExpired:
        return result('unavailable', 'Проверка локального окружения превысила время ожидания.')
    except (httpx.HTTPError, OSError):
        return result('unavailable', 'Нет соединения с сервером. Проверьте адрес и доступность.')
    except (ValueError, KeyError, TypeError):
        return result('misconfigured', 'Проверьте адрес, ключ, модель и совместимость протокола.')
