"""Bound real HTTPX streaming behavior at the provider response boundary."""
import hashlib
import json
from pathlib import Path
import sys

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dev_harness import local_live_translation, local_openai_stt, local_pipeline, local_provider_http as transport


class Body(httpx.SyncByteStream):
    def __init__(self, chunks, before_chunk=None):
        self.chunks = chunks
        self.before_chunk = before_chunk
        self.reads = 0
        self.closed = False

    def __iter__(self):
        for chunk in self.chunks:
            if self.before_chunk:
                self.before_chunk()
            self.reads += 1
            yield chunk

    def close(self):
        self.closed = True


def test_post_stops_reading_at_limit_and_closes_response():
    body = Body([b'x' * 9] * 100)
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=body))) as client:
        with pytest.raises(ValueError, match='слишком большой'):
            transport.post_json(client, 'https://provider.example/v1', limit=16)
    assert body.reads == 2 and body.closed


def test_elapsed_budget_stops_a_trickling_body_without_sleep(monkeypatch):
    now = [0]
    monkeypatch.setattr(transport.time, 'monotonic', lambda: now[0])
    def advance():
        now[0] += 1
    body = Body([b' '] * 100, before_chunk=advance)
    def respond(request):
        assert all(timeout == 2 for timeout in request.extensions['timeout'].values())
        return httpx.Response(200, stream=body)
    with httpx.Client(transport=httpx.MockTransport(respond), timeout=None) as client:
        with pytest.raises(ValueError, match='время ожидания'):
            transport.post_json(client, 'https://provider.example/v1', deadline_seconds=2)
    assert body.reads == 3 and body.closed


@pytest.mark.parametrize('status,headers', [(503, {}), (307, {'Location': 'https://other.example/'}),
                                         (200, {'Content-Encoding': 'gzip'})])
def test_rejected_status_redirect_or_encoding_never_reads_private_body(status, headers):
    body = Body([b'synthetic private transcript'])
    requests = []
    def respond(request):
        requests.append(request)
        assert request.headers['Accept-Encoding'] == 'identity'
        return httpx.Response(status, headers=headers, stream=body)
    with httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=True) as client:
        with pytest.raises(ValueError) as error:
            transport.post_json(client, 'https://provider.example/v1')
    assert len(requests) == 1 and body.reads == 0 and body.closed
    assert 'private' not in str(error.value) and 'provider.example' not in str(error.value)


def test_successful_post_keeps_request_shape_and_private_transport_options(monkeypatch):
    factory = httpx.Client
    seen = []
    def respond(request):
        seen.append(request)
        assert json.loads(request.read()) == {'model': 'synthetic'}
        assert request.headers['Authorization'] == 'Bearer synthetic-key'
        return httpx.Response(200, stream=Body([b'{"result":', b'"ok"}']))
    def client(**kwargs):
        assert kwargs['trust_env'] is False and kwargs['follow_redirects'] is False
        return factory(transport=httpx.MockTransport(respond), **kwargs)
    monkeypatch.setattr(transport.httpx, 'Client', client)
    with transport.client(key='synthetic-key') as session:
        assert transport.post_json(session, 'https://provider.example/v1', json={'model': 'synthetic'}) == {'result': 'ok'}
    assert len(seen) == 1


def test_network_errors_cannot_expose_credentials_or_provider_body():
    def respond(request):
        raise httpx.ReadError('synthetic-key private transcript', request=request)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ValueError) as error:
            transport.post_json(client, 'https://provider.example/v1')
    assert str(error.value) == 'Не удалось получить ответ сервера.'
    assert error.value.__suppress_context__


@pytest.mark.parametrize('stage', ['legacy-stt', 'selected-stt', 'diarization', 'summary', 'translation'])
def test_each_provider_adapter_uses_bounded_reader_and_preserves_audio(tmp_path, monkeypatch, stage):
    audio = tmp_path / 'audio.wav'
    original = b'synthetic audio retained'
    audio.write_bytes(original)
    body = Body([b'x' * 9] * 100)
    factory = httpx.Client
    monkeypatch.setattr(transport.httpx, 'Client', lambda **kwargs:
                        factory(transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=body)), **kwargs))
    bounded_post = transport.post_json
    def small_response(client, url, **kwargs):
        kwargs['limit'] = 16
        return bounded_post(client, url, **kwargs)
    monkeypatch.setattr(transport, 'post_json', small_response)
    settings = {'base_url': 'https://provider.example/v1', 'model': 'synthetic'}
    raw = {'segments': [{'start': 0, 'end': 1, 'text': 'Synthetic speech.'}]}
    manifest = {'audio_sha256': hashlib.sha256(original).hexdigest(), 'duration_seconds': 1}
    with pytest.raises(ValueError) as error:
        if stage.endswith('stt'):
            local_openai_stt.transcribe('http://127.0.0.1:10301/v1', 'synthetic', 'auto', audio, 1,
                                       selected=stage == 'selected-stt', key='synthetic-key')
        elif stage == 'diarization':
            local_pipeline.remote_diarization({'settings': settings}, 'synthetic-key', audio, tmp_path, manifest, raw)
        elif stage == 'summary':
            local_pipeline.summarize({'settings': settings}, 'synthetic-key', raw, tmp_path,
                                     lambda *_: pytest.fail('failed results must not be saved'))
        else:
            local_live_translation.request_translations(settings, 'synthetic-key', 'en', ['Synthetic speech.'])
    assert body.reads == 2 and body.closed
    assert audio.read_bytes() == original
    assert 'synthetic-key' not in str(error.value) and 'provider.example' not in str(error.value)
