export const stages = {
  live: {title: 'Live STT', description: 'Текст во время записи. Разделение говорящих в реальном времени настраивается на выбранном Live-сервере. Переключайте профиль после завершения записи.'},
  stt: {title: 'Распознавание', description: 'Точный транскрипт готовой записи с таймкодами.'},
  diarization: {title: 'Диаризация', description: 'Кто и когда говорит в готовой записи. Спикеры определяются отдельно для каждой записи. Разделение говорящих во время записи задаётся в Live STT.'},
  summary: {title: 'Суммаризация', description: 'Заголовок и краткое содержание на языке разговора.'},
  translation: {title: 'Перевод', description: 'По умолчанию используется активная модель суммаризации подключённого сервера. Здесь можно выбрать отдельную модель. Язык и включение перевода — в приложении.'},
};
const kinds = {
  live: {'whisperlivekit': 'WhisperLiveKit · на сервере Omi', external: 'Сервер по WebSocket · протокол WLK'},
  stt: {whisperkit: 'WhisperKit · на сервере Omi', whisperx: 'WhisperX · на сервере Omi', 'parakeet-mlx': 'Parakeet MLX · на сервере Omi', 'openai-compatible': 'Сервер по API · OpenAI-совместимый'},
  diarization: {pyannote: 'Pyannote · на сервере Omi', mycelia: 'Сервер по API · Mycelia'},
  summary: {'openai-compatible': 'OpenAI-совместимая LLM'},
  translation: {'openai-compatible': 'OpenAI-совместимая LLM'},
};
const resultLabels = {ready: 'Готов к подключению', available: 'Сервер отвечает', unavailable: 'Недоступен', misconfigured: 'Проверьте настройки'};

export function providerDefaults(stage, kind = Object.keys(kinds[stage])[0]) {
  let settings = {};
  if (stage === 'stt') {
    settings = {diarization_model: 'none'};
    if (kind === 'whisperkit') Object.assign(settings, {model: 'openai_whisper-large-v3-v20240930_626MB', language: 'ru', device: 'cpuAndNeuralEngine'});
    if (kind === 'whisperx') Object.assign(settings, {model: 'large-v3-turbo', language: 'ru', device: 'cpu', compute_type: 'float32', batch_size: 1});
    if (kind === 'parakeet-mlx') Object.assign(settings, {model: 'mlx-community/parakeet-tdt-0.6b-v3', language: 'auto', device: 'gpu', compute_type: 'float32', chunk_duration: 60, overlap_duration: 5});
    if (kind === 'openai-compatible') Object.assign(settings, {provider_url: '', model: '', language: 'auto', device: 'server'});
  }
  if (stage === 'diarization') settings = kind === 'pyannote'
    ? {speaker_model: 'pyannote/speaker-diarization-community-1', speaker_device: 'cpu', speaker_threads: 4, speaker_count: 0}
    : {base_url: ''};
  if (stage === 'summary' || stage === 'translation') settings = {base_url: '', model: '', reasoning: 'default'};
  if (stage === 'live') settings = kind === 'external' ? {url: ''}
    : {url: 'ws://127.0.0.1:10302/asr', python: '', model_dir: '', language: 'ru', chunk_seconds: 4};
  return {name: '', stage, kind, settings};
}

const field = (key, label, extras = {}) => ({key, label, ...extras});
export function providerFields(profile) {
  const {stage, kind} = profile;
  if (stage === 'live') return kind === 'external'
    ? [field('url', 'WebSocket URL', {required: true, placeholder: 'wss://server.example/asr'})]
    : [field('url', 'Локальный WebSocket URL', {required: true}), field('python', 'Python подготовленного сервера', {required: true}), field('model_dir', 'Папка модели', {required: true}),
      field('language', 'Язык', {options: {ru: 'Русский', en: 'English', auto: 'Автоматически'}}), field('chunk_seconds', 'Длина фрагмента, с', {type: 'number', min: 1, max: 10, step: 0.5})];
  if (stage === 'summary' || stage === 'translation') return [field('base_url', 'Базовый URL API', {required: true, placeholder: 'http://127.0.0.1:11434/v1'}), field('model', 'Модель', {required: true, models: true}),
    field('reasoning', 'Рассуждения модели', {options: {default: 'По умолчанию сервера', off: 'Отключить (Qwen3 на llama.cpp/vLLM)'}, hint: 'Отключайте, если модель тратит весь ответ на размышления и результат не приходит.'})];
  if (stage === 'diarization') return kind === 'mycelia'
    ? [field('base_url', 'Базовый URL сервера', {required: true, placeholder: 'https://server.example'}),
      field('min_speakers', 'Минимум говорящих', {type: 'number', min: 1, max: 32, optional: true, placeholder: 'Автоматически'}),
      field('max_speakers', 'Максимум говорящих', {type: 'number', min: 1, max: 32, optional: true, placeholder: 'Автоматически'})]
    : [field('speaker_model', 'Модель', {required: true, models: true}), field('speaker_device', 'Устройство', {options: {cpu: 'CPU', mps: 'Apple GPU (MPS)'}}),
      field('speaker_threads', 'Потоки CPU', {type: 'number', min: 1, max: 16}), field('speaker_count', 'Число говорящих', {type: 'number', min: 0, max: 32, hint: '0 — определить автоматически.'}),
      field('speaker_python', 'Python', {advanced: true, hint: 'Пустое поле — подготовленная среда omiloc.'})];
  const common = [field('model', 'Модель', {required: true, models: true}), field('language', 'Язык', {required: true, hint: 'Код языка: ru, en. Для поддерживаемых моделей: auto.'})];
  if (kind === 'openai-compatible') return [field('provider_url', 'Базовый URL API', {required: true, placeholder: 'https://server.example/v1'}), ...common];
  if (kind === 'whisperkit') return [field('model', 'Подготовленная модель', {readonly: true}), common[1], field('assets_path', 'Папка WhisperKit', {advanced: true, hint: 'Пустое поле — подготовленная среда omiloc.'})];
  if (kind === 'parakeet-mlx') return [common[0], field('language', 'Язык', {readonly: true, hint: 'Parakeet определяет язык автоматически.'}),
    field('chunk_duration', 'Длина фрагмента, с', {type: 'number', min: 10, max: 60, advanced: true}), field('overlap_duration', 'Перекрытие, с', {type: 'number', min: 1, max: 59, advanced: true}),
    field('python', 'Python', {advanced: true}), field('assets_path', 'Папка моделей', {advanced: true})];
  return [...common, field('compute_type', 'Вычисления', {options: {float32: 'Float32', int8: 'Int8', int8_float32: 'Int8 / Float32'}, advanced: true}),
    field('batch_size', 'Размер пакета', {type: 'number', min: 1, max: 8, advanced: true}), field('python', 'Python', {advanced: true}), field('assets_path', 'Папка моделей', {advanced: true})];
}

export function draftPayload(profile, apiKey = '', clearKey = false) {
  const {id, name, stage, kind, settings} = profile;
  const payload = {profile: {...(id ? {id} : {}), name: name.trim(), stage, kind, settings: structuredClone(settings)}};
  if (clearKey) payload.clear_key = true;
  else if (apiKey.trim()) payload.api_key = apiKey.trim();
  return payload;
}

function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === 'object') return Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])]));
  return value;
}
export function profileMatches(saved, effective) {
  const comparable = profile => Object.fromEntries(Object.entries(profile.settings || {}).filter(([key]) => profile.stage !== 'stt' || key !== 'diarization_model'));
  return !!saved && !!effective && saved.id === effective.id && saved.kind === effective.kind &&
    saved.configuration_revision === effective.configuration_revision && saved.has_key === effective.has_key &&
    JSON.stringify(canonical(comparable(saved))) === JSON.stringify(canonical(comparable(effective)));
}
export async function settingsRequest(path, body, method = 'POST', fetcher = fetch) {
  const timeout = path === '/activate' ? 720000 : ['/probe', '/models'].includes(path) ? 130000 : 15000;
  const response = await fetcher(`/api/settings${path}`, {method, cache: 'no-store', signal: AbortSignal.timeout(timeout),
    ...(method === 'GET' ? {} : {headers: {'Content-Type': 'application/json', 'X-Omiloc-Request': 'settings'}, body: JSON.stringify(body)})});
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(data.error || 'Не удалось выполнить действие.');
    error.status = response.status;
    error.field = data.field;
    error.outcome = data.outcome;
    throw error;
  }
  return data;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function button(text, action, className = 'settings-button') {
  const node = el('button', className, text);
  node.type = 'button';
  node.addEventListener('click', action);
  return node;
}
function fingerprint(profile) { return JSON.stringify(canonical(draftPayload(profile))); }
function profileLabel(profile) { return profile ? `${profile.name || kinds[profile.stage]?.[profile.kind] || profile.kind}${profile.settings?.model || profile.settings?.speaker_model ? ` · ${profile.settings.model || profile.settings.speaker_model}` : ''}` : 'Выключено'; }

export function mountSettings(root, {request = settingsRequest} = {}) {
  const state = {data: null, stage: 'live', draft: null, busy: false, models: [], checks: new Map(), loaded: false, generation: 0};
  let feedback, editor, draftKey, clearKey, fieldInputs = new Map(), disabledControls = new WeakMap();
  function message(text = '', tone = '') {
    if (!feedback) return;
    feedback.textContent = text;
    feedback.hidden = !text;
    feedback.dataset.tone = tone;
  }
  function busy(value) {
    state.busy = value;
    root.setAttribute('aria-busy', String(value));
    for (const control of root.querySelectorAll('button,input,select')) {
      if (value) { disabledControls.set(control, control.disabled); control.disabled = true; }
      else if (disabledControls.has(control)) { control.disabled = disabledControls.get(control); disabledControls.delete(control); }
    }
  }
  function fail(error) {
    const text = error.name === 'TimeoutError' ? 'Сервер ещё не ответил. Обновите настройки перед повторным действием.'
      : error instanceof TypeError || error instanceof SyntaxError ? 'Нет связи с аудиотекой. Повторите после восстановления подключения.'
      : error.status === 409 ? `${error.message} Обновите настройки: черновик останется в редакторе.` : error.message;
    message(text, 'error');
    const input = fieldInputs.get(error.field?.replace(/^settings\./, ''));
    if (input) {
      input.setAttribute('aria-invalid', 'true');
      input.parentElement.querySelector('.settings-field-error')?.remove();
      const hint = el('span', 'settings-field-error', error.message);
      hint.id = `${input.id}-error`;
      input.setAttribute('aria-describedby', hint.id);
      input.parentElement.append(hint);
      input.focus();
    }
  }
  async function operation(action, success, progress = 'Выполняем действие…') {
    if (state.busy) return;
    busy(true);
    message(progress);
    try { await action(); if (success) message(success, 'success'); }
    catch (error) { fail(error); }
    finally { busy(false); }
  }
  async function load() {
    await operation(async () => {
      state.data = await request('', undefined, 'GET');
      state.loaded = true;
      render();
    });
  }
  function choose(profile = null) {
    state.draft = profile ? structuredClone(profile) : providerDefaults(state.stage);
    state.models = [];
    state.generation++;
    render();
    fieldInputs.get('name')?.focus();
  }
  function formField(spec) {
    const wrapper = el('label', 'settings-field');
    const id = `provider-${spec.key}`;
    wrapper.htmlFor = id;
    wrapper.append(el('span', 'settings-field-label', spec.label));
    const input = el(spec.options ? 'select' : 'input');
    input.id = id;
    input.name = spec.key;
    if (spec.options) for (const [value, text] of Object.entries(spec.options)) {
      const option = el('option', '', text); option.value = value; input.append(option);
    }
    else input.type = spec.type || 'text';
    input.value = spec.key === 'name' ? state.draft.name : state.draft.settings[spec.key] ?? '';
    input.required = !!spec.required;
    input.readOnly = !!spec.readonly;
    if (spec.placeholder) input.placeholder = spec.placeholder;
    if (spec.min !== undefined) input.min = spec.min;
    if (spec.max !== undefined) input.max = spec.max;
    if (spec.type === 'number') input.step = spec.step || 1;
    if (spec.models) input.setAttribute('list', 'provider-models');
    if (spec.hint) { const hint = el('span', 'settings-field-hint', spec.hint); hint.id = `${id}-hint`; input.setAttribute('aria-describedby', hint.id); wrapper.append(hint); }
    input.addEventListener('input', () => {
      if (spec.key === 'name') state.draft.name = input.value;
      else if (spec.optional && input.value === '') delete state.draft.settings[spec.key];
      else state.draft.settings[spec.key] = spec.type === 'number' ? (input.value === '' ? null : Number(input.value)) : input.value;
      input.removeAttribute('aria-invalid');
      wrapper.querySelector('.settings-field-error')?.remove();
      if (spec.hint) input.setAttribute('aria-describedby', `${id}-hint`);
      else input.removeAttribute('aria-describedby');
      message();
    });
    wrapper.append(input);
    fieldInputs.set(spec.key, input);
    return wrapper;
  }
  function currentPayload() { return draftPayload(state.draft, draftKey?.value || '', clearKey?.checked || false); }
  function editorView() {
    fieldInputs = new Map();
    draftKey = null; clearKey = null;
    if (!state.draft) return null;
    editor = el('form', 'provider-editor');
    editor.setAttribute('aria-label', 'Редактор провайдера');
    editor.append(el('h3', '', state.draft.id ? 'Редактировать провайдера' : 'Новый провайдер'));
    const grid = el('div', 'settings-fields');
    grid.append(formField(field('name', 'Название', {required: true})));
    const type = el('label', 'settings-field');
    type.append(el('span', 'settings-field-label', 'Тип подключения'));
    const select = el('select'); select.name = 'kind'; select.setAttribute('aria-label', 'Тип подключения');
    for (const [value, text] of Object.entries(kinds[state.stage])) { const option = el('option', '', text); option.value = value; select.append(option); }
    select.value = state.draft.kind;
    select.disabled = !!state.draft.id;
    select.addEventListener('change', () => {
      state.draft = {...state.draft, ...providerDefaults(state.stage, select.value), name: state.draft.name};
      state.models = [];
      render();
    });
    type.append(select);
    if (state.draft.id) type.append(el('span', 'settings-field-hint', 'Для другого типа подключения добавьте новую карточку.'));
    grid.append(type);
    const fields = providerFields(state.draft);
    for (const spec of fields.filter(item => !item.advanced)) grid.append(formField(spec));
    editor.append(grid);
    if (fields.some(item => item.advanced)) {
      const advanced = el('details', 'settings-advanced');
      advanced.append(el('summary', '', 'Параметры выполнения'));
      const inner = el('div', 'settings-fields');
      fields.filter(item => item.advanced).forEach(spec => inner.append(formField(spec)));
      advanced.append(inner); editor.append(advanced);
    }
    const remote = state.draft.kind === 'openai-compatible' || state.draft.kind === 'mycelia' || state.draft.kind === 'external';
    if (remote) {
      const key = el('label', 'settings-field');
      key.append(el('span', 'settings-field-label', 'API-ключ · необязательно'));
      draftKey = el('input'); draftKey.type = 'password'; draftKey.autocomplete = 'new-password'; draftKey.name = 'api-key';
      draftKey.placeholder = state.draft.has_key ? 'Ключ сохранён. Введите новый для замены.' : 'Без ключа';
      key.append(draftKey); editor.append(key);
      if (state.draft.has_key) {
        const clear = el('label', 'settings-checkbox'); clearKey = el('input'); clearKey.type = 'checkbox';
        clearKey.addEventListener('change', () => { if (clearKey.checked) draftKey.value = ''; });
        draftKey.addEventListener('input', () => { if (draftKey.value) clearKey.checked = false; });
        clear.append(clearKey, el('span', '', 'Удалить сохранённый ключ')); editor.append(clear);
      }
      editor.append(el('p', 'settings-note', 'URL должен быть доступен серверу Omi. localhost — это Mac или контейнер, где работает Omi. Ключ хранится на сервере Omi. Для HTTP/WS укажите IP сервера в LAN/VPN или localhost; для доменных имён — HTTPS/WSS.'));
    }
    if (state.stage === 'live' && state.draft.kind === 'external') editor.append(el('p', 'settings-note', 'Сервер должен поддерживать протокол WLK /asr. Модель, язык и live-диаризация задаются на самом сервере.'));
    if (state.stage === 'stt' && remote) editor.append(el('p', 'settings-note', 'Для перехода по фразам сервер должен возвращать таймкоды сегментов или слов.'));
    if (state.draft.settings.diarization?.enabled) editor.append(el('p', 'settings-note', 'Для этого live-профиля сохранена диаризация потока. Её параметры сохранятся при редактировании. Вкладка «Диаризация» настраивает обработку готовых записей.'));
    if (state.draft.embedded) editor.append(el('p', 'settings-note', 'Этот профиль сохраняет встроенную диаризацию STT. Для отдельного этапа добавьте новую карточку Pyannote или Mycelia и выберите её после проверки окружения.'));
    const models = el('datalist'); models.id = 'provider-models';
    for (const model of state.models) { const option = el('option'); option.value = model; models.append(option); }
    editor.append(models);
    const actions = el('div', 'settings-actions');
    const save = el('button', 'settings-button primary', 'Сохранить'); save.type = 'submit';
    actions.append(save, button('Проверить подключение', () => check('/probe')));
    if (fields.some(item => item.models)) actions.append(button('Загрузить модели', () => check('/models')));
    actions.append(button('Закрыть', () => { state.draft = null; render(); }));
    editor.append(actions, el('p', 'settings-note', 'Сохранение не переключает обработку. После сохранения нажмите «Использовать» на карточке.'));
    editor.addEventListener('submit', event => {
      event.preventDefault();
      if (!editor.reportValidity()) return;
      const payload = currentPayload();
      operation(async () => {
        state.data = await request('/save', {revision: state.data.revision, ...payload});
        state.checks.delete(fingerprint(payload.profile));
        state.draft = null; render();
      }, 'Профиль сохранён. Выберите «Использовать», чтобы применить его.', 'Сохраняем профиль…');
    });
    return editor;
  }
  async function check(path) {
    const model = fieldInputs.get('model'), required = model?.required;
    if (path === '/models' && model) model.required = false;
    const valid = editor.reportValidity();
    if (model) model.required = required;
    if (!valid) return;
    const payload = currentPayload(), key = fingerprint(payload.profile);
    await operation(async () => {
      const result = await request(path, payload);
      if (!payload.api_key && !payload.clear_key) state.checks.set(key, result);
      if (Array.isArray(result.models)) {
        state.models = result.models.filter(model => typeof model === 'string');
        const list = editor.querySelector('datalist');
        list?.replaceChildren(...state.models.map(model => { const option = el('option'); option.value = model; return option; }));
      }
      const label = resultLabels[result.state] || 'Проверка завершена';
      const suffix = path === '/models' ? ` Моделей: ${state.models.length}. Название можно ввести вручную.` : ' Проверка не отправляет записи и не подтверждает качество распознавания.';
      message(`${label}. ${result.message || ''}${suffix}`, ['ready', 'available'].includes(result.state) ? 'success' : 'error');
    }, '', path === '/models' ? 'Запрашиваем список моделей…' : 'Проверяем подключение и готовность провайдера…');
  }
  function activate(id) {
    operation(async () => {
      try {
        state.data = await request('/activate', {revision: state.data.revision, stage: state.stage, id});
      } catch (error) {
        if (error.name !== 'TimeoutError' && error.outcome !== 'indeterminate') throw error;
        let refreshed = false;
        try { state.data = await request('', undefined, 'GET'); refreshed = true; render(); }
        catch { /* The activation outcome remains unknown until the server can be read. */ }
        throw new Error(`${error.name === 'TimeoutError' ? 'Время ожидания истекло.' : error.message} Результат применения не подтверждён. ${refreshed ? 'Действующие настройки перечитаны.' : 'Не удалось перечитать действующие настройки; нажмите «Обновить» после восстановления связи.'} Применение не повторялось.`);
      }
      render();
    }, id ? 'Профиль применяется к новым задачам и сессиям.' : 'Этап выключен для новых задач и сессий.',
    'Применяем настройки. Подготовка локальной модели может занять до 12 минут. Дождитесь результата; повторный запрос не требуется.');
  }
  function render() {
    const fragment = document.createDocumentFragment();
    const heading = el('header', 'settings-heading');
    const title = el('div'); title.append(el('p', 'eyebrow', 'ОБРАБОТКА АУДИО'), el('h2', '', 'Настройки'));
    heading.append(title, button('Обновить', load)); fragment.append(heading);
    fragment.append(el('p', 'settings-intro', 'Выберите профиль для каждого этапа. Обработка может идти на сервере Omi (Mac или контейнер) либо через API своего или облачного сервера. На каждом этапе действует один профиль.'));
    const guide = el('details', 'settings-advanced');
    guide.append(el('summary', '', 'Как переключить обработку'));
    guide.append(el('p', 'settings-note', 'Локально: выберите карточку подготовленного движка и нажмите «Использовать». Модели и среды сначала подготавливаются на сервере Omi.'));
    guide.append(el('p', 'settings-note', 'По API: добавьте карточку нужного типа, укажите URL, модель (если требуется) и ключ. Сервер API может работать на том же компьютере, в вашей сети или в облаке. Адрес указывается с точки зрения сервера Omi.'));
    guide.append(el('p', 'settings-note', 'Проверить подключение → Сохранить → Использовать. Сохранение оставляет текущий профиль действующим. Выбор применяется к новым записям; обработка старых записей сохраняет свои настройки.'));
    fragment.append(guide);
    const tabs = el('nav', 'settings-tabs'); tabs.setAttribute('aria-label', 'Этап обработки');
    for (const [stage, detail] of Object.entries(stages)) {
      const tab = button(detail.title, () => { state.stage = stage; state.draft = null; state.models = []; render(); }, `settings-tab${state.stage === stage ? ' selected' : ''}`);
      tab.setAttribute('aria-pressed', String(state.stage === stage)); tabs.append(tab);
    }
    fragment.append(tabs);
    feedback = el('p', 'settings-feedback'); feedback.setAttribute('role', 'status'); feedback.hidden = true; fragment.append(feedback);
    if (!state.data) { fragment.append(el('p', 'settings-note', 'Загружаем настройки сервера…')); root.replaceChildren(fragment); return; }
    fragment.append(el('p', 'settings-description', stages[state.stage].description));
    const selected = state.data.profiles.find(profile => profile.id === state.data.active[state.stage]);
    const effective = state.data.effective[state.stage];
    const assignment = el('div', 'settings-assignment');
    assignment.append(el('p', '', `Выбран: ${profileLabel(selected)}`), el('p', '', `Действует: ${profileLabel(effective)}`));
    if (selected && !profileMatches(selected, effective)) assignment.append(el('p', 'settings-pending', 'Сохранённые параметры отличаются от действующих. Нажмите «Использовать», чтобы применить их.'));
    if (effective?.inherited_from === 'summary') assignment.append(el('p', 'settings-note', 'Автоматически: используется модель суммаризации этого сервера.'));
    if (state.stage !== 'stt') { const off = button(state.stage === 'translation' ? 'Использовать модель суммаризации' : 'Выключить этап', () => activate(null)); off.disabled = state.stage === 'translation' ? !selected : !selected && !effective; assignment.append(off); }
    fragment.append(assignment);
    const list = el('div', 'provider-cards');
    for (const profile of state.data.profiles.filter(item => item.stage === state.stage)) {
      const active = profileMatches(profile, effective), card = el('article', `provider-card${active ? ' active' : ''}`);
      const top = el('div', 'provider-card-heading'); top.append(el('h3', '', profile.name));
      if (active || profile.id === selected?.id) top.append(el('span', 'provider-badge', active ? 'Действует' : 'Выбран'));
      card.append(top, el('p', 'provider-kind', kinds[profile.stage]?.[profile.kind] || profile.kind));
      if (profile.settings.model || profile.settings.speaker_model) card.append(el('p', 'provider-model', profile.settings.model || profile.settings.speaker_model));
      const endpoint = profile.settings.base_url || profile.settings.provider_url || profile.settings.url;
      if (endpoint) card.append(el('p', 'provider-endpoint', endpoint));
      const checked = state.checks.get(fingerprint(profile));
      card.append(el('p', 'provider-health', checked ? resultLabels[checked.state] || 'Проверено' : 'Подключение не проверено'));
      const actions = el('div', 'settings-actions');
      actions.append(button('Редактировать', () => choose(profile)), button('Использовать', () => activate(profile.id), 'settings-button primary'));
      const remove = button('Удалить', () => operation(async () => {
        state.data = await request(`/profiles/${encodeURIComponent(profile.id)}`, {revision: state.data.revision}, 'DELETE');
        if (state.draft?.id === profile.id) state.draft = null;
        render();
      }, 'Профиль удалён.'));
      remove.disabled = profile.id === selected?.id || profile.id === effective?.id;
      if (remove.disabled) remove.title = 'Сначала выберите другой профиль или выключите этап.';
      actions.append(remove); card.append(actions); list.append(card);
    }
    if (!list.children.length) list.append(el('p', 'settings-note', 'Провайдеров пока нет. Добавьте первый профиль для этого этапа.'));
    fragment.append(list, button('+ Добавить провайдера', () => choose(), 'settings-button add-provider'));
    const form = editorView(); if (form) fragment.append(form);
    root.replaceChildren(fragment);
  }
  render();
  return {load, show: () => { if (!state.loaded && !state.busy) return load(); }};
}
