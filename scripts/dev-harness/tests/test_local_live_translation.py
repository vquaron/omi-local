import asyncio
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dev_harness import local_live_translation as translation


def snapshot(*texts, buffer='draft'):
    return {'lines': [{'text': text, 'start': index, 'end': index + 1} for index, text in enumerate(texts)],
            'buffer_transcription': buffer}


def profile(**settings):
    return {'id': 'synthetic-translation', 'name': 'Synthetic', 'stage': 'translation',
            'kind': 'openai-compatible', 'settings': {'base_url': 'https://llm.example/v1', 'model': 'm', **settings}}


class Backend:
    def __init__(self, *, fail=False):
        self.calls = []
        self.fail = fail

    def __call__(self, settings, key, language, texts):
        self.calls.append((key, language, list(texts)))
        if self.fail:
            raise ValueError('synthetic failure')
        return [f'{language}:{text}' for text in texts]


class Sink:
    def __init__(self):
        self.messages = []

    async def __call__(self, raw):
        self.messages.append(json.loads(raw))


class ObservableWake(asyncio.Event):
    """Signal when the real worker has drained its queue, including to_thread."""

    def __init__(self):
        super().__init__()
        self.idle = asyncio.Event()

    def set(self):
        self.idle.clear()
        super().set()

    async def wait(self):
        if not self.is_set():
            self.idle.set()
        result = await super().wait()
        self.idle.clear()
        return result


def make_translator(*args, **kwargs):
    translator = translation.LiveTranslator(*args, **kwargs)
    translator.wake = ObservableWake()
    return translator


async def settle(translator):
    # Event-loop yields do not guarantee that an OS worker thread has finished.
    # Observe the next empty-queue wait instead, without replacing to_thread.
    await asyncio.gather(*translator.status_tasks)
    if translator.worker is not None:
        await asyncio.wait_for(translator.wake.idle.wait(), 5)


def run(coroutine):
    return asyncio.run(coroutine)


def test_passthrough_when_language_is_off():
    async def scenario():
        backend, sink = Backend(), Sink()
        translator = make_translator(profile(), 'k', send=sink, request=backend)
        message = snapshot('hello there.', 'second line.')
        assert translator.annotate(message) == message
        await settle(translator)
        assert backend.calls == [] and sink.messages == []
        await translator.close()
    run(scenario())


def test_enabling_and_changing_language_translates_existing_text_without_more_speech():
    async def scenario():
        backend, sink = Backend(), Sink()
        translator = make_translator(profile(), 'k', send=sink, request=backend)
        original = snapshot('Привет мир.')
        translator.annotate(original)
        for language in ('es', 'en', 'es'):
            translator.set_language(language)
            await settle(translator)
            assert sink.messages[-1]['lines'][0]['translations'] == [
                {'lang': language, 'text': f'{language}:Привет мир.'}]
        assert len(backend.calls) == 2  # Switching back uses the cached result.
        translator.set_language(None)
        await settle(translator)
        assert sink.messages[-1] == original
        await translator.close()
    run(scenario())


def test_repeated_snapshots_do_not_postpone_stable_text_translation():
    async def scenario():
        backend, sink = Backend(), Sink()
        translator = make_translator(profile(), 'k', send=sink, request=backend)
        translator.set_language('es')
        translator.annotate(snapshot('stable text'))
        timer = translator.settle_handle
        translator.annotate(snapshot('stable text', buffer='changed draft'))
        assert translator.settle_handle is timer and not timer.cancelled()
        translator.annotate(snapshot('changed text'))
        assert timer.cancelled()
        timer = translator.settle_handle
        # Drive the real deadline callback without wall-clock sleeps.
        timer.cancel()
        translator._settled('changed text')
        await settle(translator)
        assert backend.calls == [('k', 'es', ['changed text'])]
        assert sink.messages[-1]['lines'][0]['translations'][0]['text'] == 'es:changed text'
        await translator.close()
    run(scenario())


def test_translates_completed_lines_and_reemits_snapshot():
    async def scenario():
        backend, sink = Backend(), Sink()
        translator = make_translator(profile(), 'k', send=sink, request=backend)
        translator.set_language('en')
        first = translator.annotate(snapshot('Привет мир.', 'Как дела'))
        assert first['lines'][0]['translations'] == [] and first['lines'][1]['translations'] == []
        await settle(translator)
        # Only the punctuated line and settled lines are translated; the growing last line waits.
        assert backend.calls == [('k', 'en', ['Привет мир.'])]
        assert sink.messages[-1]['lines'][0]['translations'] == [{'lang': 'en', 'text': 'en:Привет мир.'}]
        assert sink.messages[-1]['lines'][1]['translations'] == []
        assert {'type': 'translation_status', 'status': 'ready'} in sink.messages
        # Cached translation is attached synchronously on the next snapshot without a new request.
        again = translator.annotate(snapshot('Привет мир.', 'Как дела?'))
        assert again['lines'][0]['translations'] == [{'lang': 'en', 'text': 'en:Привет мир.'}]
        await settle(translator)
        assert backend.calls[-1] == ('k', 'en', ['Как дела?'])
        assert sink.messages[-1]['lines'][1]['translations'] == [{'lang': 'en', 'text': 'en:Как дела?'}]
        await translator.close()
    run(scenario())


def test_unpunctuated_last_line_is_translated_after_it_settles(monkeypatch):
    async def scenario():
        backend, sink = Backend(), Sink()
        translator = make_translator(profile(), 'k', send=sink, request=backend)
        loop = asyncio.get_running_loop()
        original_call_later = loop.call_later
        scheduled = []

        def call_later(delay, callback, *args, context=None):
            if callback == translator._settled:
                handle = asyncio.Handle(callback, args, loop)
                scheduled.append((delay, lambda: callback(*args), handle))
                return handle
            return original_call_later(delay, callback, *args, context=context)

        monkeypatch.setattr(loop, 'call_later', call_later)
        translator.set_language('de')
        translator.annotate(snapshot('erste zeile'))
        await settle(translator)
        assert backend.calls == []
        assert len(scheduled) == 1 and scheduled[0][0] == translation.SETTLE_SECONDS
        assert not scheduled[0][2].cancelled()
        scheduled[0][1]()
        await settle(translator)
        assert backend.calls == [('k', 'de', ['erste zeile'])]
        assert sink.messages[-1]['lines'][0]['translations'] == [{'lang': 'de', 'text': 'de:erste zeile'}]
        await translator.close()
    run(scenario())


def test_language_change_retranslates_and_keeps_only_target_language():
    async def scenario():
        backend, sink = Backend(), Sink()
        translator = make_translator(profile(), 'k', send=sink, request=backend)
        translator.set_language('en')
        translator.annotate(snapshot('Bonjour.', 'Salut.'))
        await settle(translator)
        translator.set_language('de')
        message = translator.annotate(snapshot('Bonjour.', 'Salut.'))
        assert all(line['translations'] == [] for line in message['lines'])
        await settle(translator)
        assert backend.calls[-1] == ('k', 'de', ['Bonjour.', 'Salut.'])
        assert [line['translations'] for line in sink.messages[-1]['lines']] == [
            [{'lang': 'de', 'text': 'de:Bonjour.'}], [{'lang': 'de', 'text': 'de:Salut.'}]]
        translator.set_language(None)
        assert translator.annotate(snapshot('Bonjour.')) == snapshot('Bonjour.')
        await translator.close()
    run(scenario())


def test_failures_are_negative_cached_and_reported_once():
    async def scenario():
        backend, sink = Backend(fail=True), Sink()
        translator = make_translator(profile(), 'k', send=sink, request=backend)
        translator.set_language('en')
        translator.annotate(snapshot('One.', 'Two.'))
        await settle(translator)
        translator.annotate(snapshot('One.', 'Two.', 'Three.'))
        await settle(translator)
        assert [call[2] for call in backend.calls] == [['One.', 'Two.'], ['Three.']]
        assert sink.messages.count({'type': 'translation_status', 'status': 'failed'}) == 1
        assert not any('lines' in message for message in sink.messages)
        await translator.close()
    run(scenario())


def test_invalid_provider_answer_counts_as_failure():
    async def scenario():
        sink = Sink()
        translator = make_translator(profile(), 'k', send=sink,
                                                request=lambda *_: ['only one'])
        translator.set_language('en')
        translator.annotate(snapshot('One.', 'Two.'))
        await settle(translator)
        assert sink.messages == [{'type': 'translation_status', 'status': 'failed'}]
        await translator.close()
    run(scenario())


def test_unconfigured_provider_reports_once_and_never_requests():
    async def scenario():
        sink = Sink()
        translator = make_translator(None, '', send=sink, request=lambda *_: pytest.fail('no provider'))
        translator.set_language('en')
        message = translator.annotate(snapshot('One.'))
        assert message == snapshot('One.')
        translator.set_language('de')
        await settle(translator)
        assert [m for m in sink.messages if m.get('type') == 'translation_status'] == [
            {'type': 'translation_status', 'status': 'unconfigured'}]
        await translator.close()
    run(scenario())


def test_control_language_validation():
    assert translation.normalize_language('en') == 'en'
    assert translation.normalize_language(' RU ') == 'ru'
    assert translation.normalize_language('yue') == 'yue'
    for value in ('', None, 'e', 'engl', 'en-US', 'e1', 42):
        assert translation.normalize_language(value) is None
    assert translation.normalize_languages(' ES ', 'RU') == ('es', 'ru')
    assert translation.normalize_languages('es') == ('es', None)
    for other in ('es', 'en-US', 42, [], {}):
        assert translation.normalize_languages('es', other) == (None, None)


def test_bidirectional_translation_keeps_each_original_and_opposite_language():
    async def scenario():
        sink = Sink()
        calls = []
        def request(settings, key, language, texts, *, other_language):
            calls.append((language, other_language, texts))
            return [{'source_lang': 'ru', 'lang': 'es', 'text': ' Hola. '},
                    {'source_lang': 'es', 'lang': 'ru', 'text': 'Добрый день.'}]
        translator = make_translator(profile(), 'k', send=sink, request=request)
        translator.set_language('es', 'ru')
        original = snapshot('Привет.', 'Buenos días.')
        translator.annotate(original)
        await settle(translator)
        assert calls == [('es', 'ru', ['Привет.', 'Buenos días.'])]
        lines = sink.messages[-1]['lines']
        assert [line['text'] for line in lines] == ['Привет.', 'Buenos días.']
        assert [line['translations'] for line in lines] == [
            [{'lang': 'es', 'text': 'Hola.'}], [{'lang': 'ru', 'text': 'Добрый день.'}]]
        translator.annotate(original)
        await settle(translator)
        assert len(calls) == 1
        await translator.close()
    run(scenario())


@pytest.mark.parametrize('answer', [
    ['Hola.'],
    [{'source_lang': 'es', 'lang': 'es', 'text': 'Hola.'}],
    [{'source_lang': 'fr', 'lang': 'es', 'text': 'Hola.'}],
    [{'source_lang': 'es', 'lang': 'ru', 'text': ''}],
])
def test_invalid_pair_answer_is_rejected_without_attaching_a_translation(answer):
    async def scenario():
        sink = Sink()
        translator = make_translator(profile(), 'k', send=sink, request=lambda *args, **kwargs: answer)
        translator.set_language('es', 'ru')
        original = snapshot('Hola.')
        translator.annotate(original)
        await settle(translator)
        assert sink.messages == [{'type': 'translation_status', 'status': 'failed'}]
        assert translator.annotate(original)['lines'][0]['translations'] == []
        await translator.close()
    run(scenario())


def test_inflight_answer_cannot_replace_new_pair_and_cache_includes_both_languages():
    async def scenario():
        import threading
        sink = Sink()
        started = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        calls = []
        def request(settings, key, language, texts, *, other_language):
            calls.append(other_language)
            if len(calls) == 1:
                loop.call_soon_threadsafe(started.set)
                assert release.wait(5)
            return [{'source_lang': 'es', 'lang': other_language, 'text': f'{other_language}:Hola.'}]
        translator = make_translator(profile(), 'k', send=sink, request=request)
        translator.set_language('es', 'ru')
        original = snapshot('Hola.')
        translator.annotate(original)
        try:
            await asyncio.wait_for(started.wait(), 5)
            translator.set_language('es', 'en')
        finally:
            release.set()
        await settle(translator)
        emitted = [m['lines'][0]['translations'] for m in sink.messages if 'lines' in m]
        assert emitted[-1] == [{'lang': 'en', 'text': 'en:Hola.'}]
        assert all(not row or row[0]['lang'] == 'en' for row in emitted)
        translator.set_language('es', 'ru')
        await settle(translator)
        assert sink.messages[-1]['lines'][0]['translations'] == [{'lang': 'ru', 'text': 'ru:Hola.'}]
        assert calls == ['ru', 'en']
        await translator.close()
    run(scenario())


@pytest.mark.parametrize('other_language', [None, 'es'])
def test_http_request_shape(monkeypatch, other_language):
    import httpx
    captured = {}
    answer = ([{'source_lang': 'es', 'lang': 'en', 'text': 'Hello.'},
               {'source_lang': 'en', 'lang': 'es', 'text': 'Mundo.'}] if other_language else ['Hello.', 'World.'])

    def respond(request):
        captured['url'] = str(request.url)
        captured['body'] = json.loads(request.read())
        assert request.headers['Accept-Encoding'] == 'identity'
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop',
                                                     'message': {'content': '```json\n' + json.dumps(answer) + '\n```'}}]})

    factory = httpx.Client
    def client(**kwargs):
        captured['client'] = kwargs
        return factory(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(translation.httpx, 'Client', client)
    result = translation.request_translations(profile(reasoning='off')['settings'], 'secret', 'en',
                                              ['Hola.', 'World.'], other_language=other_language)
    assert result == answer
    assert captured['url'] == 'https://llm.example/v1/chat/completions'
    assert captured['client']['headers'] == {'Authorization': 'Bearer secret'}
    assert captured['body']['temperature'] == 0 and captured['body']['model'] == 'm'
    assert captured['body']['chat_template_kwargs'] == {'enable_thinking': False}
    assert json.loads(captured['body']['messages'][1]['content']) == ['Hola.', 'World.']
    assert 'en' in captured['body']['messages'][0]['content']

    if other_language:
        assert 'es' in captured['body']['messages'][0]['content']
        assert 'source_lang' in captured['body']['messages'][0]['content']
