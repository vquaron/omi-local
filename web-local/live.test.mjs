import test from 'node:test';
import assert from 'node:assert/strict';
import {audioActivity, mountLiveMonitor} from './live.mjs';

const received = frames => ({backend: 'ready', capture: {state: 'received', frames_received: frames}});

test('audio badge requires increasing counters and reports a stalled stream', () => {
  const first = audioActivity(received(10), null, 1000, 0);
  assert.equal(first.tone, 'active');
  assert.equal(audioActivity(received(10), received(10), 6500, first.progress).tone, 'waiting');
  assert.equal(audioActivity(received(11), received(10), 7000, first.progress).tone, 'active');
});

test('disconnect, stop, and decoding errors do not masquerade as receiving audio', () => {
  assert.equal(audioActivity({backend: 'unavailable'}, received(10), 10, 0).tone, 'error');
  assert.equal(audioActivity({backend: 'ready', capture: {state: 'idle'}}, received(10), 10, 0).tone, 'idle');
  assert.equal(audioActivity({backend: 'ready', capture: {state: 'decode_error'}}, received(10), 10, 0).tone, 'error');
});

function monitor(t) {
  class Element {
    children = [];
    dataset = {};
    content = '';
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.children = children; }
    set textContent(value) { this.content = value; this.children = []; }
    get textContent() { return this.content + this.children.map(child => child.textContent).join(''); }
  }
  const root = new Element(), listeners = new Map(), timers = new Map(), requests = [];
  const document = {hidden: false, createElement: () => new Element(), createDocumentFragment: () => new Element()};
  for (const [key, value] of Object.entries({document, addEventListener: (event, listener) => listeners.set(event, listener)})) {
    const old = Object.getOwnPropertyDescriptor(globalThis, key);
    Object.defineProperty(globalThis, key, {value, configurable: true});
    t.after(() => { if (old) Object.defineProperty(globalThis, key, old); else delete globalThis[key]; });
  }
  let nextTimer = 0;
  t.mock.method(globalThis, 'setTimeout', callback => { timers.set(++nextTimer, callback); return nextTimer; });
  t.mock.method(globalThis, 'clearTimeout', id => timers.delete(id));
  t.mock.method(AbortSignal, 'timeout', () => new AbortController().signal);
  t.mock.method(globalThis, 'fetch', (url, options) => new Promise((resolve, reject) => {
    assert.equal(url, '/api/runtime');
    assert.equal(options.cache, 'no-store');
    requests.push({signal: options.signal, reject, reply: (text, diarization) => resolve({ok: true, json: async () => ({
      backend: 'ready', capture: {state: 'received', frames_received: requests.length},
      sessions: [{source: 'omi', preview_id: 'synthetic-draft', text}],
      live_transcript: {state: 'streaming', updates: requests.length}, final_stt: {state: 'ready'}, events: [],
      observed_at: '2026-09-10T12:00:00Z', diarization,
    })})});
  }));
  mountLiveMonitor(root);
  return {root, document, timers, requests,
    event: name => listeners.get(name)?.({persisted: true}),
    tick: () => {
      assert.equal(timers.size, 1, 'exactly one polling timer');
      const [[id, callback]] = timers;
      timers.delete(id);
      callback();
    },
  };
}

async function settle() {
  for (let step = 0; step < 6; step++) await Promise.resolve();
}

test('live monitor resumes after page restoration, skips hidden tabs, and recovers from fetch failure', async t => {
  const ui = monitor(t);
  ui.requests[0].reply('Первый черновик');
  await settle();
  assert.match(ui.root.textContent, /Первый черновик/);
  assert.match(ui.root.textContent, /Live-текст получен/);
  ui.event('pagehide');
  assert.equal(ui.timers.size, 0);
  ui.event('pageshow');
  ui.event('pageshow');
  assert.equal(ui.requests.length, 2, 'restoration starts one request');
  ui.requests[1].reply('Свежий черновик');
  await settle();
  assert.match(ui.root.textContent, /Свежий черновик/);
  assert.doesNotMatch(ui.root.textContent, /Первый черновик/);
  ui.tick();
  ui.requests[2].reject(new Error('Synthetic connection failure'));
  await settle();
  assert.match(ui.root.textContent, /Нет связи с сервером/);
  assert.doesNotMatch(ui.root.textContent, /Свежий черновик/);
  ui.document.hidden = true;
  ui.tick();
  assert.equal(ui.requests.length, 3);
  ui.document.hidden = false;
  ui.tick();
  ui.requests[3].reply('Связь восстановлена');
  await settle();
  assert.match(ui.root.textContent, /Связь восстановлена/);
  assert.equal(ui.timers.size, 1);
});

test('restoration during a pending fetch discards stale results without overlapping polling chains', async t => {
  const ui = monitor(t);
  ui.event('pagehide');
  assert.equal(ui.requests[0].signal.aborted, true);
  ui.event('pageshow');
  assert.equal(ui.requests.length, 1, 'wait for aborted request to settle');
  ui.requests[0].reply('Устаревший черновик');
  await settle();
  assert.doesNotMatch(ui.root.textContent, /Устаревший черновик/);
  ui.tick();
  assert.equal(ui.requests.length, 2);
  ui.event('pagehide');
  ui.requests[1].reject(new Error('Synthetic aborted request'));
  await settle();
  assert.equal(ui.timers.size, 0, 'hidden page does not restart its timer');
  assert.doesNotMatch(ui.root.textContent, /Нет связи с сервером/);
  ui.event('pageshow');
  ui.requests[2].reply('Текущий черновик');
  await settle();
  assert.match(ui.root.textContent, /Текущий черновик/);
  assert.equal(ui.timers.size, 1);
});


test('diarization distinguishes service readiness, labels, degradation and lost status', async t => {
  const ui = monitor(t);
  for (const [state, label, count] of [
    ['ready', 'Сервис готов', 0], ['pending', 'ожидаем метки', 0],
    ['labeled', 'Метки получены', 3], ['degraded', 'Ошибка диаризации', 0],
    ['disabled', 'Выключена', 0], ['unknown', 'Неизвестно', 0],
  ]) {
    if (ui.requests.length > 1 || state !== 'ready') ui.tick();
    ui.requests.at(-1).reply('Текст продолжает поступать', {state, labeled_segments: count});
    await settle();
    assert.ok(ui.root.textContent.includes(label));
    assert.match(ui.root.textContent, /Live-текст получен/);
    if (state === 'labeled') assert.match(ui.root.textContent, /текущем тексте: 3/);
    else assert.doesNotMatch(ui.root.textContent, /текущем тексте: 3/);
  }
  ui.tick();
  ui.requests.at(-1).reject(new Error('Disconnected'));
  await settle();
  assert.doesNotMatch(ui.root.textContent, /Метки получены|Сервис готов/);
});
