"""Timed OpenAI-compatible STT; remote routes require an explicit provider selection."""
from __future__ import annotations

import io
import math
from urllib.parse import urlsplit
import wave

import httpx


class ProviderError(ValueError):
    """Fixed diagnostics only: HTTP bodies may contain private audio/text."""


def validate_url(value):
    try:
        url = urlsplit(value)
        if (url.scheme != 'http' or url.hostname != '127.0.0.1' or not url.port
                or url.path.rstrip('/') != '/v1' or url.query or url.fragment
                or url.username or url.password):
            raise ValueError()
    except (ValueError, TypeError):
        raise ProviderError('STT provider requires http://127.0.0.1:<port>/v1') from None
    return value.rstrip('/')


def fields(model, language):
    data = {'model': model, 'response_format': 'verbose_json', 'timestamp_granularities[]': ['segment', 'word'],
            'temperature': '0'}
    if language != 'auto':
        data['language'] = language
    return data


def normalize(raw, duration):
    """Keep actual provider timestamps; never invent word confidence."""
    def interval(item):
        start, end = item['start'], item['end']
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in (start, end)):
            raise ValueError()
        if start < 0 or end < start:
            raise ValueError()
        return start, min(end, duration)

    try:
        if not isinstance(raw['segments'], list) or not isinstance(raw.get('language', ''), str):
            raise ValueError()
        all_words = raw.get('words') or []
        if not isinstance(all_words, list):
            raise ValueError()
        segments = []
        previous = 0
        for item in raw['segments']:
            start, end = interval(item)
            if start >= duration:
                continue
            if start < previous or not isinstance(item['text'], str):
                raise ValueError()
            previous = start
            text = item['text'].strip()
            words = []
            source_words = item.get('words') if item.get('words') is not None else all_words
            for word in source_words:
                ws, we = interval(word)
                if ws >= duration or we <= start or ws >= end:
                    continue
                if not isinstance(word['word'], str):
                    raise ValueError()
                words.append({'word': word['word'], 'start': ws, 'end': we, 'speaker': 'SPEAKER_00'})
            if words:
                text = ''.join(w['word'] if w['word'].startswith(' ') else ' ' + w['word'] for w in words).strip()
            if text and end > start:
                segments.append({'text': text, 'start': start, 'end': end, 'speaker': 'SPEAKER_00', 'words': words})
        result = {'language': raw.get('language', ''), 'segments': segments}
        if not segments:
            if raw.get('text', '').strip() and not raw['segments']:
                raise ValueError()  # Text-only providers cannot satisfy the timed contract.
            result['outcome'] = 'no_speech'
        return result
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ProviderError('STT provider returned an invalid timed transcript') from None


def check(url, model, *, timeout=5, selected=False, key=''):
    try:
        if selected:
            from . import local_provider_http as transport
            endpoint, session = transport.validate_url(url), transport.client(key=key, timeout=timeout)
        else:
            endpoint, session = validate_url(url), httpx.Client(trust_env=False, follow_redirects=False, timeout=timeout)
        with session as client:
            response = transport.metadata_get(client, endpoint + '/models') if selected else client.get(endpoint + '/models')
            if selected and response.status_code in {404, 405} and isinstance(model, str) and model.strip():
                return  # Explicit manual model; inference remains the capability check.
            if not selected:
                response.raise_for_status()
            catalogue = (transport.model_ids(transport.json_response(response)) if selected else
                         {item['id'] for item in response.json()['data']})
            if model not in catalogue:
                raise ProviderError('Configured STT model is not served by this endpoint')
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        raise ProviderError('STT endpoint/model preflight failed') from None


def transcribe(url, model, language, audio, duration, *, selected=False, key=''):
    try:
        from . import local_provider_http as transport
        if selected:
            endpoint, session = transport.validate_url(url), transport.client(key=key, timeout=3600)
        else:
            endpoint, session = validate_url(url), httpx.Client(trust_env=False, follow_redirects=False, timeout=3600)
        with session as client:
            with audio.open('rb') as source:
                raw = transport.post_json(client, endpoint + '/audio/transcriptions',
                                          limit=32 * 1024 * 1024, deadline_seconds=3600,
                                          data=fields(model, language),
                                          files={'file': ('audio.wav', source, 'audio/wav')})
            return normalize(raw, duration)
    except (httpx.HTTPError, OSError, ValueError):
        raise ProviderError('STT request failed; original WAV retained') from None


def pcm_wav(pcm):
    stream = io.BytesIO()
    with wave.open(stream, 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(pcm)
    return stream.getvalue()
