import {formatTime} from './player.mjs';

const states = {ready: 'Готов', configured: 'Провайдер выбран', busy: 'Ожидаем текст', streaming: 'Live-текст получен', disabled: 'Выключен',
  failed: 'Ошибка', unavailable: 'Недоступен', stopped: 'Остановлен', processing: 'Распознаёт запись'};

export function audioActivity(current, previous, now, lastProgress) {
  const capture = current.capture || {};
  if (current.backend !== 'ready') return {label: 'Нет связи с сервером', progress: now, tone: 'error'};
  if (capture.state === 'idle') return {label: 'Ожидаем запись', progress: now, tone: 'idle'};
  if (capture.state === 'decode_error') return {label: 'Есть ошибки декодирования', progress: now, tone: 'error'};
  if (capture.state === 'waiting_audio') return {label: 'Подключено · ждём звук', progress: now, tone: 'waiting'};
  if (capture.state !== 'received') return {label: 'Состояние звука неизвестно', progress: now, tone: 'error'};
  const changed = !previous || capture.frames_received !== previous.capture?.frames_received;
  const progress = changed ? now : lastProgress;
  return {label: now - progress >= 5000 ? 'Аудио не поступает более 5 с' : 'Аудио поступает',
    progress, tone: now - progress >= 5000 ? 'waiting' : 'active'};
}

function element(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

export function mountLiveMonitor(root) {
  const title = element('div', 'monitor-heading');
  const heading = element('div');
  heading.append(element('p', 'eyebrow', 'РАСПОЗНАВАНИЕ НА MAC'), element('h2', '', 'Сейчас'));
  const badge = element('span', 'monitor-badge', 'Подключаемся…');
  title.append(heading, badge);
  const note = element('p', 'monitor-note', 'Начните запись на CV1 или с микрофона iPhone. Текст появится здесь автоматически.');
  const cards = element('div', 'monitor-cards');
  const fields = {};
  for (const [id, label] of [['audio', 'Источник звука'], ['live', 'Live STT'], ['diarization', 'Диаризация · во время записи'], ['final', 'После остановки']]) {
    const card = element('div', 'monitor-card');
    const value = element('strong', '', '—');
    const detail = element('span', '', 'Проверяем состояние…');
    card.append(element('span', 'monitor-card-label', label), value, detail);
    fields[id] = {value, detail};
    cards.append(card);
  }
  const transcriptHeading = element('div', 'transcript-heading');
  transcriptHeading.append(element('h3', '', 'Live-транскрипт'), element('span', '', 'Черновик уточняется во время речи'));
  const text = element('div', 'live-draft');
  const journal = element('details', 'monitor-journal');
  const summary = element('summary', '', 'События');
  const journalNote = element('p', 'monitor-note', 'Время наблюдения на Mac. Журнал начинается с открытия монитора; содержимое речи в него не попадает.');
  const events = element('ol', 'monitor-events');
  journal.append(summary, journalNote, events);
  const freshness = element('p', 'monitor-freshness');
  root.append(title, note, cards, transcriptHeading, text, journal, freshness);
  let previous = null, lastProgress = Date.now(), lastDraft = '', lastEvents = '', timer;
  let paused = false, request = null, generation = 0;

  function render(data) {
    const activity = audioActivity(data, previous, Date.now(), lastProgress);
    lastProgress = activity.progress;
    badge.textContent = activity.label;
    badge.dataset.tone = activity.tone;
    const sessions = data.backend === 'ready' ? (data.sessions || []) : [];
    const live = data.live_transcript || {}, final = data.final_stt || {};
    fields.audio.value.textContent = sessions.length ? [...new Set(sessions.map(s => s.source === 'omi' ? 'CV1' : 'Микрофон iPhone'))].join(' + ') : 'Нет активной записи';
    fields.audio.detail.textContent = sessions.length ? `${formatTime(data.capture.audio_seconds || 0)} получено · ${data.capture.frames_received || 0} кадров` : activity.label;
    fields.live.value.textContent = live.provider || 'Live STT';
    fields.live.detail.textContent = `${states[live.state] || 'Неизвестно'} · отправлено обновлений: ${live.updates || 0}`;
    const diarization = data.diarization || {};
    const diarizationStates = {disabled: 'Выключена', ready: 'Сервис готов', busy: 'Сервис занят',
      pending: 'Включена · ожидаем метки', labeled: 'Метки получены', degraded: 'Ошибка диаризации',
      failed: 'Поток завершился с ошибкой', unavailable: 'Сервис недоступен', unknown: 'Неизвестно'};
    fields.diarization.value.textContent = diarizationStates[diarization.state] || 'Неизвестно';
    fields.diarization.detail.textContent = diarization.state === 'labeled'
      ? `Размечено фрагментов в текущем тексте: ${diarization.labeled_segments || 0}`
      : diarization.state === 'degraded' ? 'Метки сняты. Состояние текста и аудио показано отдельно.'
      : ['ready', 'busy', 'pending'].includes(diarization.state) ? 'Результат текущей записи ещё не подтверждён.'
      : diarization.state === 'disabled' ? 'Метки спикеров в потоке не создаются.'
      : 'Нет подтверждения работы диаризации.';
    fields.final.value.textContent = final.provider || 'Финальный STT';
    fields.final.detail.textContent = `${states[final.state] || 'Недоступен'}${final.jobs?.pending ? ` · в очереди: ${final.jobs.pending}` : ''}${final.jobs?.failed ? ` · ошибок: ${final.jobs.failed}` : ''}`;
    const draft = JSON.stringify(sessions.map(({preview_id, source, text, text_truncated}) => ({preview_id, source, text, text_truncated})));
    if (draft !== lastDraft || data.backend !== previous?.backend) {
      lastDraft = draft;
      const fragment = document.createDocumentFragment();
      if (!sessions.length) fragment.append(element('p', 'live-empty', data.backend === 'ready'
        ? 'Сейчас записи нет. После остановки окончательный текст появится в списке слева.'
        : 'Нет связи с сервером. Live-текст недоступен; повторяем проверку.'));
      for (const session of sessions) {
        const block = element('div', 'live-session');
        if (sessions.length > 1) block.append(element('p', 'eyebrow', session.source === 'omi' ? 'CV1' : 'МИКРОФОН IPHONE'));
        block.append(element('p', session.text ? 'live-text' : 'live-empty', session.text || 'Звук принимается. Ожидаем распознанную речь…'));
        if (session.text_truncated) block.append(element('p', 'monitor-note', 'Показана последняя часть длинного черновика. Полный финальный текст будет доступен после остановки.'));
        fragment.append(block);
      }
      text.replaceChildren(fragment);
    }
    const serialized = JSON.stringify(data.events || []);
    if (serialized !== lastEvents) {
      lastEvents = serialized;
      events.replaceChildren(...(data.events || []).map(event => {
        const row = element('li');
        const stamp = new Date(event.observed_at).toLocaleTimeString('ru-RU');
        row.append(element('time', '', stamp), element('span', '', event.message));
        return row;
      }));
      summary.textContent = `События · ${(data.events || []).length}`;
    }
    freshness.textContent = data.observed_at ? `Проверено в ${new Date(data.observed_at).toLocaleTimeString('ru-RU')} · обновление каждую секунду` : 'Аудиотека не отвечает. Повторяем проверку…';
    previous = data;
  }

  async function poll() {
    if (paused || request) return;
    clearTimeout(timer);
    if (!document.hidden) {
      const controller = new AbortController(), started = generation;
      request = controller;
      try {
        const response = await fetch('/api/runtime', {cache: 'no-store',
          signal: AbortSignal.any([controller.signal, AbortSignal.timeout(6000)])});
        if (!response.ok) throw new Error('Runtime unavailable');
        const data = await response.json();
        if (!paused && started === generation) render(data);
      } catch {
        if (!paused && started === generation) {
          render({backend: 'unavailable', capture: {state: 'unknown'}, sessions: [],
            live_transcript: {state: 'unavailable'}, final_stt: {state: 'unavailable'}, events: previous?.events || []});
        }
      } finally {
        request = null;
      }
    }
    if (!paused) timer = setTimeout(poll, 1000);
  }
  addEventListener('pagehide', () => {
    paused = true;
    generation++;
    clearTimeout(timer);
    request?.abort();
  });
  addEventListener('pageshow', () => {
    if (!paused) return;
    paused = false;
    poll();
  });
  poll();
}
