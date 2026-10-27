"""Private provider drafts and immutable active snapshots for the local audio library.

The registry owns selection, credentials and optimistic edits. It never sends audio.
Legacy engine files are imported once, when the first explicit edit is saved.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile
import uuid

from .local_provider_http import auth_headers, validate_url


STAGES = ('live', 'stt', 'diarization', 'summary', 'translation')
KINDS = {'live': {'external', 'whisperlivekit'},
         'stt': {'whisperkit', 'whisperx', 'parakeet-mlx', 'openai-compatible'},
         'diarization': {'pyannote', 'mycelia'}, 'summary': {'openai-compatible'},
         'translation': {'openai-compatible'}}
ENGINE_FIELDS = {'provider_url', 'model', 'language', 'compute_type', 'batch_size', 'python',
                 'library_path', 'assets_path', 'runtime_revision', 'device', 'chunk_duration',
                 'overlap_duration', 'diarization_model'}
SPEAKER_FIELDS = {'speaker_model', 'speaker_python', 'speaker_device', 'speaker_threads', 'speaker_count', 'speaker_revision'}
LIVE_FIELDS = {'url', 'python', 'model_dir', 'chunk_seconds', 'language', 'diarization'}
NAME_LIMIT = 100


class SettingsError(ValueError):
    def __init__(self, message, *, status=400, field=None):
        super().__init__(message)
        self.status = status
        self.field = field


def read_json(path, default=None):
    if not path.exists():
        return deepcopy(default)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise SettingsError('Файл настроек недоступен.', status=503)
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        raise SettingsError('Не удалось прочитать настройки.', status=503) from None


def atomic_json(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise SettingsError('Файл настроек недоступен.', status=503)
    fd, temporary = tempfile.mkstemp(prefix='.providers-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def identity(profile):
    """Keys cannot follow a draft URL to a different host or protocol."""
    from urllib.parse import urlsplit
    settings = profile.get('settings', {})
    endpoint = settings.get('provider_url') or settings.get('base_url') or settings.get('url') or ''
    parsed = urlsplit(endpoint)
    return profile.get('stage'), profile.get('kind'), parsed.scheme, parsed.netloc


def number(settings, key, default, low, high, *, integer=True):
    value = settings.get(key, default)
    if type(value) not in ((int,) if integer else (int, float)) or not low <= value <= high:
        raise SettingsError('Недопустимое значение: ' + key, field=key)
    settings[key] = value


def text_field(settings, key, default='', *, required=False, limit=2048):
    value = settings.get(key, default)
    if not isinstance(value, str) or len(value) > limit or (value and not value.isprintable()):
        raise SettingsError('Недопустимое значение: ' + key, field=key)
    value = value.strip()
    if required and not value:
        raise SettingsError('Заполните поле: ' + key, field=key)
    if value or key in settings or default:
        settings[key] = value


def validate_profile(value, *, models=False):
    if not isinstance(value, dict):
        raise SettingsError('Некорректная карточка провайдера.')
    stage, kind = value.get('stage'), value.get('kind')
    if stage not in KINDS or kind not in KINDS[stage]:
        raise SettingsError('Выберите поддерживаемый тип провайдера.')
    name = value.get('name', '')
    if not isinstance(name, str) or not name.strip() or len(name) > NAME_LIMIT or any(ord(c) < 32 for c in name):
        raise SettingsError('Введите название провайдера (до 100 символов).', field='name')
    data = deepcopy(value.get('settings', {}))
    if not isinstance(data, dict):
        raise SettingsError('Некорректные параметры провайдера.')
    allowed = (ENGINE_FIELDS if stage == 'stt' else LIVE_FIELDS if stage == 'live' else
               SPEAKER_FIELDS if kind == 'pyannote' else
               {'base_url', 'min_speakers', 'max_speakers'} if stage == 'diarization' else
               {'base_url', 'model', 'reasoning'})
    if set(data) - allowed:
        raise SettingsError('Параметры не соответствуют выбранному типу провайдера.')
    for key in data:
        if key not in {'diarization', 'batch_size', 'chunk_duration', 'overlap_duration', 'speaker_threads',
                       'speaker_count', 'chunk_seconds', 'min_speakers', 'max_speakers'}:
            text_field(data, key)
    if stage == 'stt':
        text_field(data, 'model', required=not models, limit=256)
        text_field(data, 'language', 'ru' if kind == 'whisperx' else 'auto', limit=32)
        if kind == 'openai-compatible':
            data['provider_url'] = endpoint(data, 'provider_url')
            if not data['provider_url'].endswith('/v1'):
                raise SettingsError('Адрес STT должен оканчиваться на /v1.', field='provider_url')
        if 'batch_size' in data:
            number(data, 'batch_size', 1, 1, 8)
        if 'chunk_duration' in data:
            number(data, 'chunk_duration', 60, 10, 60)
        if 'overlap_duration' in data:
            number(data, 'overlap_duration', 5, 1, data.get('chunk_duration', 60) - 1)
        if (not re.fullmatch(r'[a-z]{2,3}|auto', data['language'])
                or kind == 'whisperx' and data['language'] == 'auto'
                or kind == 'parakeet-mlx' and data['language'] != 'auto'):
            raise SettingsError('Язык не поддерживается выбранным движком.', field='language')
    elif stage == 'live':
        data['url'] = endpoint(data, 'url', websocket=True)
        if kind == 'whisperlivekit':
            from urllib.parse import urlsplit
            parsed = urlsplit(data['url'])
            if parsed.scheme != 'ws' or parsed.hostname != '127.0.0.1' or not parsed.port or parsed.path != '/asr':
                raise SettingsError('Управляемый WhisperLiveKit должен работать на 127.0.0.1 с путём /asr.', field='url')
            text_field(data, 'python', required=True)
            text_field(data, 'model_dir', required=True)
            number(data, 'chunk_seconds', 4, 1, 10, integer=False)
            if data.get('language', 'ru') not in {'ru', 'en', 'auto'}:
                raise SettingsError('Выберите язык ru, en или auto.')
        if data.get('diarization'):
            from .local_stt_services import diarization_settings
            diarization_settings(data)
    elif kind == 'pyannote':
        if data.get('speaker_model') not in {'pyannote/speaker-diarization-community-1', 'pyannote/speaker-diarization-3.1'}:
            raise SettingsError('Выберите подготовленную модель Pyannote.', field='speaker_model')
        if data.get('speaker_device', 'cpu') not in {'cpu', 'mps'}:
            raise SettingsError('Выберите CPU или MPS.')
        number(data, 'speaker_threads', 4, 1, 16)
        number(data, 'speaker_count', 0, 0, 32)
    else:
        data['base_url'] = endpoint(data, 'base_url')
        if stage in {'summary', 'translation'}:
            text_field(data, 'model', required=not models, limit=256)
            if data.get('reasoning', 'default') not in {'default', 'off'}:
                raise SettingsError('Режим рассуждений: по умолчанию или отключён.', field='reasoning')
            if data.get('reasoning') == 'default':
                data.pop('reasoning')
        else:
            for key in ('min_speakers', 'max_speakers'):
                if key in data:
                    number(data, key, 1, 1, 32)
            if data.get('min_speakers', 1) > data.get('max_speakers', 32):
                raise SettingsError('Минимум говорящих превышает максимум.')
    result = {'name': name.strip(), 'stage': stage, 'kind': kind, 'settings': data}
    if value.get('id') is not None:
        if not isinstance(value['id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value['id']):
            raise SettingsError('Некорректная карточка провайдера.')
        result['id'] = value['id']
    return result


def endpoint(settings, key, *, websocket=False):
    try:
        return validate_url(settings.get(key, ''), websocket=websocket)
    except ValueError as error:
        raise SettingsError(str(error), field=key) from None


class Registry:
    def __init__(self, cfg):
        self.cfg = cfg
        self.root = cfg.layout.state_root
        self.path = self.root / 'providers.json'
        self.secrets_path = self.root / 'provider-secrets.json'

    @property
    def exists(self):
        return self.path.exists()

    def _legacy(self):
        engine = read_json(self.root / 'stt-engine.json', {})
        live = read_json(self.root / 'live-stt.json', {})
        if not isinstance(engine, dict) or not isinstance(live, dict):
            raise SettingsError('Текущие настройки движков недоступны.', status=503)
        # Resolve exactly the defaults/runtime paths used before the registry exists.
        # Raw omissions differ between managed WhisperX and older engine profiles.
        from .local_stt import EngineConfig, TranscriptionError
        try:
            resolved = EngineConfig.load(self.cfg)
        except (TranscriptionError, ValueError, TypeError):
            raise SettingsError('Текущие настройки движка недоступны.', status=503) from None
        engine = {key: getattr(resolved, key) for key in ENGINE_FIELDS | SPEAKER_FIELDS}
        kind = resolved.engine
        profile = {'id': 'current-stt', 'name': {'whisperkit': 'WhisperKit', 'whisperx': 'WhisperX',
                   'parakeet-mlx': 'Parakeet MLX', 'openai-compatible': 'Текущий STT-сервер'}.get(kind, kind),
                   'stage': 'stt', 'kind': kind,
                   'settings': {k: v for k, v in engine.items() if k in ENGINE_FIELDS}}
        state = {'version': 1, 'revision': 0, 'profiles': [profile],
                 'effective': dict.fromkeys(STAGES)}
        state['effective']['stt'] = deepcopy(profile)
        if engine.get('speaker_model'):
            speakers = {'id': 'current-diarization', 'name': 'Pyannote на этом Mac',
                        'stage': 'diarization', 'kind': 'pyannote',
                        'settings': {k: v for k, v in engine.items() if k in SPEAKER_FIELDS}}
            state['profiles'].append(speakers)
            state['effective']['diarization'] = deepcopy(speakers)
        elif profile['settings'].get('diarization_model', 'none') != 'none':
            speakers = {'id': 'current-diarization', 'name': 'Встроенная диаризация STT',
                        'stage': 'diarization', 'kind': 'pyannote', 'embedded': True,
                        'settings': {'speaker_model': profile['settings']['diarization_model']}}
            if engine.get('python'):
                speakers['settings']['speaker_python'] = engine['python']
            state['profiles'].append(speakers)
            state['effective']['diarization'] = deepcopy(speakers)
        if live.get('url'):
            snapshot = {'id': 'current-live', 'name': 'Текущий Live STT', 'stage': 'live',
                        'kind': live.get('provider', 'external'),
                        'settings': {k: v for k, v in live.items() if k in LIVE_FIELDS}}
            state['profiles'].append(snapshot)
            if live.get('enabled'):
                state['effective']['live'] = deepcopy(snapshot)
        return state

    def _load(self):
        state = read_json(self.path) if self.exists else self._legacy()
        if (not isinstance(state, dict) or state.get('version') != 1
                or not isinstance(state.get('profiles'), list) or not isinstance(state.get('effective'), dict)):
            raise SettingsError('Версия настроек не поддерживается.', status=503)
        return state

    @contextmanager
    def _edit(self, revision):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_path = self.root / '.providers.lock'
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SettingsError('Настройки сейчас применяются. Повторите позже.', status=409) from None
            state = self._load()
            if type(revision) is not int or revision != state['revision']:
                raise SettingsError('Настройки изменились в другом окне. Обновите страницу.', status=409)
            yield state

    @staticmethod
    def _public(profile):
        if profile is None:
            return None
        return {**{k: deepcopy(v) for k, v in profile.items() if k != 'credential_ref'},
                'has_key': bool(profile.get('credential_ref'))}

    def read(self):
        state = self._load()
        effective = {stage: self._public(state['effective'].get(stage)) for stage in STAGES}
        effective['translation'] = self._public(self._translation(state))
        return {'version': 1, 'revision': state['revision'],
                'profiles': [self._public(p) for p in state['profiles']], 'effective': effective,
                'active': {stage: p['id'] if (p := state['effective'].get(stage)) else None for stage in STAGES}}

    @staticmethod
    def _translation(state):
        """An explicit translator overrides the connected server's active text LLM.

        Resolve only applied settings, never drafts or speech-recognition models.
        Keep the credential reference pinned to that exact model/endpoint version.
        """
        effective = state['effective']
        if effective.get('translation'):
            return deepcopy(effective['translation'])
        summary = effective.get('summary')
        if summary and summary['kind'] == 'openai-compatible':
            return {**deepcopy(summary), 'stage': 'translation', 'inherited_from': 'summary'}
        return None

    def snapshot(self, stage):
        if stage not in STAGES:
            raise SettingsError('Неизвестный этап обработки.')
        state = self._load()
        return self._translation(state) if stage == 'translation' else deepcopy(state['effective'].get(stage))

    def pipeline(self):
        effective = self._load()['effective']
        return {stage: deepcopy(effective.get(stage)) for stage in ('stt', 'diarization', 'summary')}

    def key(self, snapshot):
        ref = snapshot.get('credential_ref') if snapshot else None
        if not ref:
            return ''
        vault = read_json(self.secrets_path, {})
        if ref not in vault:
            raise SettingsError('API-ключ этого задания недоступен. Настройте провайдера снова.', status=409)
        key = vault[ref]
        auth_headers(key)
        return key

    def _draft(self, body, state, *, models=False):
        profile = validate_profile(body.get('profile'), models=models)
        previous = next((p for p in state['profiles'] if p['id'] == profile.get('id')), None)
        if previous and (previous['stage'], previous['kind']) != (profile['stage'], profile['kind']):
            raise SettingsError('Для другого типа создайте новую карточку.')
        # Embedded execution is registry-owned metadata, not an editable field.
        # Editing/exporting a legacy card must not turn it into a separate worker.
        if previous and previous.get('embedded'):
            profile['embedded'] = True
        if previous and identity(previous) == identity(profile) and previous.get('credential_ref'):
            profile['credential_ref'] = previous['credential_ref']
        if body.get('clear_key'):
            profile.pop('credential_ref', None)
        key = body.get('api_key')
        if key is not None:
            auth_headers(key)
        return profile, key

    def draft(self, body, *, models=False):
        profile, key = self._draft(body, self._load(), models=models)
        return profile, key if key else self.key(profile)

    def save(self, body):
        with self._edit(body.get('revision')) as state:
            profile, key = self._draft(body, state)
            profile.setdefault('id', uuid.uuid4().hex)
            profile['configuration_revision'] = state['revision'] + 1
            if key:
                vault = read_json(self.secrets_path, {})
                ref = uuid.uuid4().hex
                vault[ref] = key
                atomic_json(self.secrets_path, vault)
                profile['credential_ref'] = ref
            found = next((i for i, p in enumerate(state['profiles']) if p['id'] == profile['id']), None)
            if found is None:
                if len(state['profiles']) >= 64:
                    raise SettingsError('Достигнут предел: 64 провайдера.')
                state['profiles'].append(profile)
            else:
                state['profiles'][found] = profile
            state['revision'] += 1
            atomic_json(self.path, state)
        return self.read()

    def activate(self, body, *, prepare=None):
        stage = body.get('stage')
        if stage not in STAGES or (stage == 'stt' and body.get('id') is None):
            raise SettingsError('Выберите провайдера распознавания.')
        with self._edit(body.get('revision')) as state:
            selected = next((p for p in state['profiles'] if p['id'] == body.get('id') and p['stage'] == stage), None)
            if body.get('id') is not None and selected is None:
                raise SettingsError('Провайдер не найден.', status=404)
            selected = deepcopy(selected)
            next_stt = selected if stage == 'stt' else state['effective'].get('stt')
            next_diarization = selected if stage == 'diarization' else state['effective'].get('diarization')
            if (stage in {'stt', 'diarization'} and next_diarization and next_diarization.get('embedded')
                    and (not next_stt or next_stt['kind'] not in {'whisperx', 'parakeet-mlx'})):
                raise SettingsError('Сначала выберите отдельного провайдера диаризации или выключите её: '
                                    'встроенная диаризация недоступна для этого STT.', status=409)
            if stage == 'stt':
                selected['settings']['diarization_model'] = (
                    next_diarization['settings']['speaker_model']
                    if next_diarization and next_diarization.get('embedded') else 'none')
            original = deepcopy(state)
            existed = self.exists
            def publish():
                state['effective'][stage] = selected
                if stage in {'stt', 'diarization'} and state['effective'].get('stt'):
                    state['effective']['stt']['settings']['diarization_model'] = (
                        next_diarization['settings']['speaker_model']
                        if next_diarization and next_diarization.get('embedded') else 'none')
                state['revision'] = original['revision'] + 1
                atomic_json(self.path, state)
            try:
                if prepare:
                    prepare(selected, publish)
                elif stage == 'live':
                    from .local_stt_services import activate
                    activate(self.cfg, selected, publish=publish, rollback=lambda: atomic_json(self.path, original))
                else:
                    from .local_provider_probe import probe
                    if selected:
                        result = probe(self.cfg, selected, key=self.key(selected))
                        if result['state'] not in {'ready', 'available'}:
                            raise SettingsError(result['message'], status=409)
                    publish()
            except Exception:
                # Preparation can publish before verifying the backend bootstrap.
                # Restore the effective selection while preserving the saved draft.
                if existed or self.exists:
                    atomic_json(self.path, original)
                raise
        return self.read()

    def delete(self, profile_id, revision):
        with self._edit(revision) as state:
            if any(p and p['id'] == profile_id for p in state['effective'].values()):
                raise SettingsError('Сначала выберите другой активный провайдер или выключите этап.', status=409)
            profiles = [p for p in state['profiles'] if p['id'] != profile_id]
            if len(profiles) == len(state['profiles']):
                raise SettingsError('Провайдер не найден.', status=404)
            state['profiles'] = profiles
            state['revision'] += 1
            # Credential versions remain available for immutable queued snapshots.
            atomic_json(self.path, state)
        return self.read()
