import test from 'node:test';
import assert from 'node:assert/strict';
import {draftPayload, mountSettings, profileMatches, providerDefaults, providerFields, settingsRequest} from './settings.mjs';
import {processingRows, renderProcessing} from './recording-processing.mjs';

test('adapters expose supported settings and preserve independent diarization', () => {
  assert.deepEqual(providerFields(providerDefaults('live', 'external')).map(field => field.key), ['url']);
  assert.deepEqual(providerFields(providerDefaults('summary', 'openai-compatible')).map(field => field.key), ['base_url', 'model', 'reasoning']);
  assert.equal(providerDefaults('summary', 'openai-compatible').settings.reasoning, 'default');
  assert.deepEqual(providerFields(providerDefaults('translation')).map(field => field.key), ['base_url', 'model', 'reasoning']);
  assert.equal(providerDefaults('translation').kind, 'openai-compatible');
  for (const kind of ['whisperkit', 'whisperx', 'parakeet-mlx', 'openai-compatible']) {
    assert.equal(providerDefaults('stt', kind).settings.diarization_model, 'none');
    assert.ok(!providerFields(providerDefaults('stt', kind)).some(field => field.key.startsWith('speaker_')));
  }
  assert.equal(providerDefaults('stt', 'parakeet-mlx').settings.language, 'auto');
});

test('draft requests retain local settings and explicitly replace or clear a secret', () => {
  const profile = {...providerDefaults('summary'), id: 'summary-1', name: '  Local model  ', has_key: true};
  profile.settings.advanced = {preserved: true};
  const body = draftPayload(profile);
  assert.equal(body.profile.name, 'Local model');
  assert.equal(body.api_key, undefined);
  assert.equal(body.profile.has_key, undefined);
  assert.equal(body.profile.settings.advanced.preserved, true);
  assert.notEqual(body.profile.settings, profile.settings);
  assert.equal(draftPayload(profile, 'synthetic-secret').api_key, 'synthetic-secret');
  assert.deepEqual(Object.keys(draftPayload(profile, 'synthetic-secret', true)), ['profile', 'clear_key']);
});

test('saved profile differs from effective configuration until applied', () => {
  const saved = {...providerDefaults('summary'), id: 'summary-1'};
  const effective = structuredClone(saved);
  assert.equal(profileMatches(saved, effective), true);
  effective.settings.model = 'previous';
  assert.equal(profileMatches(saved, effective), false);
  effective.settings = Object.fromEntries(Object.entries(saved.settings).reverse());
  assert.equal(profileMatches(saved, effective), true);
  assert.equal(profileMatches(saved, null), false);
  saved.has_key = effective.has_key = true;
  saved.configuration_revision = 2; effective.configuration_revision = 1;
  assert.equal(profileMatches(saved, effective), false, 'replaced credential remains unapplied');
});

test('independent speaker settings do not create a false pending STT configuration', () => {
  const saved = {...providerDefaults('stt', 'whisperx'), id: 'stt-1', configuration_revision: 2};
  const effective = structuredClone(saved);
  effective.settings.diarization_model = 'pyannote/speaker-diarization-community-1';
  assert.equal(profileMatches(saved, effective), true);
  effective.settings.model = 'different-model';
  assert.equal(profileMatches(saved, effective), false);
});

test('requests use same-origin mutation guard and preserve actionable validation errors', async () => {
  let request;
  await settingsRequest('/save', {revision: 7}, 'POST', async (url, options) => {
    request = {url, options}; return {ok: true, json: async () => ({revision: 8})};
  });
  assert.equal(request.url, '/api/settings/save');
  assert.equal(request.options.headers['X-Omiloc-Request'], 'settings');
  assert.deepEqual(JSON.parse(request.options.body), {revision: 7});
  await assert.rejects(settingsRequest('/activate', {}, 'POST', async () => ({ok: false, status: 409,
    json: async () => ({error: 'Recording is active', field: 'settings.url'})})), error =>
    error.status === 409 && error.field === 'settings.url' && error.message === 'Recording is active');
  await assert.rejects(settingsRequest('/activate', {}, 'POST', async () => ({ok: false, status: 503,
    json: async () => ({error: 'Readiness unresolved', outcome: 'indeterminate'})})), error => error.outcome === 'indeterminate');
});

function dom(t) {
  class Element {
    children = []; attrs = {}; listeners = {}; dataset = {}; className = ''; value = ''; disabled = false; content = '';
    constructor(tag) { this.tag = tag; }
    append(...children) { for (const child of children) { child.parentElement = this; this.children.push(child); } }
    replaceChildren(...children) { this.children = []; this.append(...children); }
    set textContent(value) { this.content = value; this.children = []; }
    get textContent() { return this.content + this.children.map(child => child.textContent).join(''); }
    setAttribute(key, value) { this.attrs[key] = value; }
    removeAttribute(key) { delete this.attrs[key]; }
    addEventListener(event, fn) { this.listeners[event] = fn; }
    dispatch(event) { return this.listeners[event]?.({preventDefault() {}}); }
    focus() { this.focused = true; }
    reportValidity() { return true; }
    remove() { this.parentElement.children = this.parentElement.children.filter(child => child !== this); }
    querySelectorAll(selector) {
      return this.children.flatMap(child => [child, ...child.querySelectorAll('*')]).filter(child => selector === '*' || selector.split(',').some(item =>
        item.startsWith('.') ? child.className.split(' ').includes(item.slice(1)) : child.tag === item));
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  }
  const old = Object.getOwnPropertyDescriptor(globalThis, 'document');
  Object.defineProperty(globalThis, 'document', {configurable: true, value: {
    createElement: tag => new Element(tag), createDocumentFragment: () => new Element('fragment'),
  }});
  t.after(() => { if (old) Object.defineProperty(globalThis, 'document', old); else delete globalThis.document; });
  const root = new Element('section');
  return {root, click: text => { const target = root.querySelectorAll('button').find(button => button.textContent === text);
    assert.ok(target, `button ${text} exists`); assert.equal(target.disabled, false); return target.dispatch('click'); },
    input: (name, value) => { const target = root.querySelectorAll('input,select').find(input => input.name === name);
      assert.ok(target, `field ${name} exists`); target.value = value; target.dispatch('input'); return target; }};
}
async function settle() { for (let i = 0; i < 10; i++) await Promise.resolve(); }
function serverState() {
  const profile = {...providerDefaults('summary'), id: 'summary-1', name: 'Local model', has_key: true};
  profile.settings = {base_url: 'http://127.0.0.1:11434/v1', model: 'synthetic-model'};
  return {version: 1, revision: 7, profiles: [profile], active: {live: null, stt: null, diarization: null, summary: profile.id},
    effective: {live: null, stt: null, diarization: null, summary: structuredClone(profile)}};
}

test('translation settings explain the server model used automatically', async t => {
  const ui = dom(t), state = serverState();
  state.active.translation = null;
  state.effective.translation = {...state.effective.summary, stage: 'translation', inherited_from: 'summary'};
  const settings = mountSettings(ui.root, {request: async () => structuredClone(state)});
  await settings.show();
  ui.click('Перевод');
  assert.match(ui.root.textContent, /Автоматически: используется модель суммаризации этого сервера/);
  assert.match(ui.root.textContent, /Действует: Local model/);
  const automatic = ui.root.querySelectorAll('button').find(b => b.textContent === 'Использовать модель суммаризации');
  assert.equal(automatic.disabled, true);
});

test('finished-recording speaker selection stays independent of the live server', async t => {
  const ui = dom(t), calls = [], state = serverState();
  const live = {...providerDefaults('live', 'external'), id: 'live-1', name: 'Live server'};
  live.settings.url = 'wss://speech.example/asr';
  const speakers = {...providerDefaults('diarization', 'mycelia'), id: 'speakers-1', name: 'Recording speakers'};
  speakers.settings.base_url = 'https://speakers.example';
  state.profiles.push(live, speakers);
  state.active.live = live.id; state.effective.live = structuredClone(live);
  const settings = mountSettings(ui.root, {request: async (path, body) => {
    calls.push({path, body});
    if (path === '/activate') {
      state.active[body.stage] = body.id;
      state.effective[body.stage] = structuredClone(state.profiles.find(profile => profile.id === body.id));
      state.revision++;
    }
    return structuredClone(state);
  }});
  await settings.show();
  assert.match(ui.root.textContent, /Mac или контейнер/);
  assert.match(ui.root.textContent, /разделение говорящих в реальном времени/i);
  ui.click('Диаризация');
  assert.match(ui.root.textContent, /в готовой записи/);
  ui.click('Использовать'); await settle();
  assert.deepEqual(calls.at(-1), {path: '/activate', body: {revision: 7, stage: 'diarization', id: speakers.id}});
  assert.match(ui.root.textContent, /Действует: Recording speakers/);
  ui.click('Live STT');
  assert.match(ui.root.textContent, /Действует: Live server/);
  assert.equal(calls.filter(call => call.path === '/activate').length, 1);
});

test('editing and checking a draft does not save or activate; save and use are distinct actions', async t => {
  const ui = dom(t), calls = [], state = serverState();
  const settings = mountSettings(ui.root, {request: async (path, body, method) => {
    calls.push({path, body, method});
    if (path === '/probe') return {state: 'ready', message: 'Synthetic metadata ready'};
    if (path === '/save') { state.profiles[0] = {...body.profile, has_key: true}; state.revision++; }
    if (path === '/activate') { state.effective.summary = structuredClone(state.profiles[0]); state.revision++; }
    return structuredClone(state);
  }});
  await settings.show();
  ui.click('Суммаризация'); ui.click('Редактировать');
  ui.input('model', 'changed-model'); ui.input('api-key', 'synthetic-secret');
  assert.equal(calls.length, 1);
  ui.click('Проверить подключение'); await settle();
  assert.equal(calls.at(-1).path, '/probe');
  assert.match(ui.root.textContent, /не отправляет записи/);
  assert.equal(ui.root.querySelectorAll('input').find(input => input.name === 'api-key').value, 'synthetic-secret');
  assert.equal(ui.root.querySelectorAll('button').find(button => button.textContent === 'Удалить').disabled, true);
  ui.root.querySelector('form').dispatch('submit'); await settle();
  assert.equal(calls.at(-1).path, '/save');
  assert.equal(calls.at(-1).body.revision, 7);
  assert.equal(calls.at(-1).body.api_key, 'synthetic-secret');
  assert.match(ui.root.textContent, /параметры отличаются от действующих/);
  assert.equal(ui.root.querySelector('form'), null);
  assert.doesNotMatch(ui.root.textContent, /synthetic-secret/);
  ui.click('Использовать'); await settle();
  assert.equal(calls.at(-1).path, '/activate');
  assert.equal(calls.at(-1).body.revision, 8);
  assert.equal(calls.at(-1).body.id, 'summary-1');
  assert.doesNotMatch(ui.root.textContent, /параметры отличаются/);
});

test('failed activation keeps effective profile and re-enables retry controls', async t => {
  const ui = dom(t), state = serverState();
  const settings = mountSettings(ui.root, {request: async path => {
    if (path === '/activate') throw new Error('Завершите текущую запись.');
    return structuredClone(state);
  }});
  await settings.show(); ui.click('Суммаризация'); ui.click('Выключить этап'); await settle();
  assert.match(ui.root.textContent, /Действует: Local model/);
  assert.match(ui.root.textContent, /Завершите текущую запись/);
  assert.equal(ui.root.querySelectorAll('button').find(button => button.textContent === 'Выключить этап').disabled, false);
  assert.equal(ui.root.querySelectorAll('button').find(button => button.textContent === 'Удалить').disabled, true);
});

for (const outcome of ['timeout', 'indeterminate']) test(`activation ${outcome} rereads effective settings and never repeats the mutation`, async t => {
  const ui = dom(t), state = serverState(), paths = [];
  const settings = mountSettings(ui.root, {request: async path => {
    paths.push(path);
    if (path === '/activate') {
      state.active.summary = null; state.effective.summary = null;
      const error = new Error('Readiness unresolved');
      if (outcome === 'timeout') error.name = 'TimeoutError'; else error.outcome = 'indeterminate';
      throw error;
    }
    return structuredClone(state);
  }});
  await settings.show(); ui.click('Суммаризация'); ui.click('Выключить этап'); await settle();
  assert.deepEqual(paths, ['', '/activate', '']);
  assert.match(ui.root.textContent, /Результат применения не подтверждён/);
  assert.match(ui.root.textContent, /Действует: Выключено/);
  assert.match(ui.root.textContent, /Применение не повторялось/);
});

test('clearing optional speaker counts omits the properties in saved settings', async t => {
  const ui = dom(t), state = serverState(), calls = [];
  const settings = mountSettings(ui.root, {request: async (path, body) => {
    calls.push({path, body}); return structuredClone(state);
  }});
  await settings.show(); ui.click('Диаризация'); ui.click('+ Добавить провайдера');
  const kind = ui.root.querySelectorAll('select').find(input => input.name === 'kind');
  kind.value = 'mycelia'; kind.dispatch('change');
  ui.input('name', 'Speaker server'); ui.input('base_url', 'http://127.0.0.1:21003');
  ui.input('min_speakers', '2'); ui.input('max_speakers', '4');
  ui.input('min_speakers', ''); ui.input('max_speakers', '');
  ui.root.querySelector('form').dispatch('submit'); await settle();
  assert.equal(calls.at(-1).path, '/save');
  assert.equal('min_speakers' in calls.at(-1).body.profile.settings, false);
  assert.equal('max_speakers' in calls.at(-1).body.profile.settings, false);
});

test('model discovery works before choosing a model and leaves manual entry available', async t => {
  const ui = dom(t), calls = [], state = serverState();
  const settings = mountSettings(ui.root, {request: async (path, body) => {
    calls.push({path, body});
    if (path === '/models') return {state: 'ready', message: 'Models found', models: ['first-model', 'second-model']};
    return structuredClone(state);
  }});
  await settings.show(); ui.click('Суммаризация'); ui.click('+ Добавить провайдера');
  ui.input('name', 'New server'); ui.input('base_url', 'http://127.0.0.1:11434/v1');
  const model = ui.root.querySelectorAll('input').find(input => input.name === 'model');
  ui.root.querySelector('form').reportValidity = () => {
    assert.equal(model.required, false, 'discovery validates URL while allowing an empty model'); return true;
  };
  ui.click('Загрузить модели'); await settle();
  assert.equal(calls.at(-1).path, '/models');
  assert.equal(calls.at(-1).body.profile.settings.model, '');
  assert.equal(model.required, true, 'saving still requires a chosen model');
  assert.equal(ui.root.querySelector('datalist').children.length, 2);
  ui.input('model', 'manual-model');
  assert.equal(model.value, 'manual-model');
});

test('processing display handles old records and renders untrusted summaries as plain text', t => {
  assert.deepEqual(processingRows(undefined), []);
  const ui = dom(t), summary = document.createElement('section');
  renderProcessing(ui.root, summary, {summary: {overview: '<script>synthetic</script>'}, processing: {
    stt: {status: 'ready', model: 'local'}, diarization: {status: 'failed', provider_name: 'Speaker server'},
    summary: {status: 'pending'}, credentials: {secret: 'must not appear'},
  }});
  assert.equal(summary.hidden, false);
  assert.match(summary.textContent, /<script>synthetic<\/script>/);
  assert.equal(summary.querySelectorAll('script').length, 0);
  assert.match(ui.root.textContent, /Ошибка/);
  assert.doesNotMatch(ui.root.textContent, /must not appear/);
  renderProcessing(ui.root, summary, {});
  assert.equal(summary.hidden, true);
  assert.equal(ui.root.hidden, true);
});
