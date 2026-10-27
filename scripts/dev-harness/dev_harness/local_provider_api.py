"""Settings operations served exclusively by the library's same-origin admin routes."""
from copy import deepcopy
import re

from .local_providers import Registry, SettingsError
from .local_provider_probe import probe


class Settings:
    def __init__(self, cfg):
        self.cfg = cfg
        self.registry = Registry(cfg)

    def read(self):
        return self.registry.read()

    def dispatch(self, method, path, body):
        if not isinstance(body, dict):
            raise SettingsError('Ожидается объект настроек.')
        if method == 'POST':
            if path == '/api/settings/save':
                return self.registry.save(body)
            if path == '/api/settings/activate':
                return self.registry.activate(body)
            if path in {'/api/settings/probe', '/api/settings/models'}:
                profile, key = self.registry.draft(body, models=path.endswith('/models'))
                return probe(self.cfg, profile, key=key, models=path.endswith('/models'))
        if method == 'DELETE' and path.startswith('/api/settings/profiles/'):
            return self.registry.delete(path.rsplit('/', 1)[-1], body.get('revision'))
        raise SettingsError('Не найдено.', status=404)


class Processing(Settings):
    """Paired-phone controls: select prepared engines, edit network adapters only."""

    STAGES = {'live', 'stt', 'diarization'}
    FIELDS = {
        ('live', 'external'): {'url'},
        ('stt', 'openai-compatible'): {'provider_url', 'model', 'language', 'device', 'diarization_model'},
        ('diarization', 'mycelia'): {'base_url', 'min_speakers', 'max_speakers'},
    }
    PRIVATE_FIELDS = {'python', 'model_dir', 'library_path', 'assets_path', 'speaker_python'}

    @classmethod
    def can_edit(cls, profile):
        if not isinstance(profile, dict):
            return False
        fields = cls.FIELDS.get((profile.get('stage'), profile.get('kind')))
        settings = profile.get('settings', {})
        return (fields is not None and isinstance(settings, dict) and not set(settings) - fields
                and settings.get('diarization_model', 'none') == 'none')

    @classmethod
    def public(cls, state):
        result = deepcopy(state)
        for profile in [*result['profiles'], *result['effective'].values()]:
            if profile is None:
                continue
            profile['can_edit'] = cls.can_edit(profile)
            settings = profile.get('settings', {})
            for key in cls.PRIVATE_FIELDS:
                settings.pop(key, None)
            if isinstance(settings.get('diarization'), dict):
                settings['diarization'].pop('python', None)
        return result

    def read(self):
        return self.public(self.registry.read())

    def dispatch(self, method, path, body):
        if not isinstance(body, dict):
            raise SettingsError('Ожидается объект настроек.')
        operation = path.removeprefix('/api/processing/')
        if method == 'POST' and operation in {'save', 'probe', 'models'}:
            profile = body.get('profile')
            if not self.can_edit(profile):
                raise SettingsError('Этот профиль настраивается на сервере. В приложении можно выбрать подготовленный профиль.', status=403)
            previous = next((p for p in self.registry.read()['profiles'] if p['id'] == profile.get('id')), None)
            if previous and not self.can_edit(previous):
                raise SettingsError('Подготовленный локальный профиль доступен только для выбора.', status=403)
        elif method == 'POST' and operation == 'activate':
            if body.get('stage') not in self.STAGES:
                raise SettingsError('Выберите этап распознавания или диаризации.')
        elif method == 'DELETE' and re.fullmatch(r'profiles/[A-Za-z0-9_-]{1,64}', operation):
            previous = next((p for p in self.registry.read()['profiles'] if p['id'] == operation.rsplit('/', 1)[-1]), None)
            if previous and not self.can_edit(previous):
                raise SettingsError('Подготовленный локальный профиль удаляется на сервере.', status=403)
        else:
            raise SettingsError('Не найдено.', status=404)
        result = super().dispatch(method, '/api/settings/' + operation, body)
        return self.public(result) if 'profiles' in result else result
