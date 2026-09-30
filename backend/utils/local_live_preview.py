"""Optional RAM-only WLK preview. Finished-WAV transcription remains separate."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import uuid
from urllib.parse import urlsplit

import websockets
from websockets.exceptions import ConnectionClosed

from utils.async_tasks import create_named_task, drain_tasks
from utils.observability.fallback import record_fallback

logger = logging.getLogger(__name__)


def preview_url() -> str | None:
    value = os.getenv('OMI_LOCAL_LIVE_PREVIEW_URL', '').strip()
    if not value:
        return None
    url = urlsplit(value)
    if (url.scheme != 'ws' or url.hostname != '127.0.0.1' or not url.port
            or url.path != '/asr' or url.query or url.fragment or url.username or url.password):
        raise ValueError('Live preview requires ws://127.0.0.1:<port>/asr')
    return value


class PreviewSegment:
    """Project authoritative provider snapshots without inventing speaker labels.

    WLK's full mode supplies committed lines plus distinct pending buffers.
    Revisions replace the complete local preview, including retracted rows.
    """

    def __init__(self):
        self.id = f'local-preview-{uuid.uuid4()}'
        self.text = ''
        self.segments = []
        self.revision = 0
        self.diarization = False
        self.configured_diarization = False
        self.provider = 'custom-live'
        self.last_snapshot = None

    def configure(self, message: dict) -> None:
        # WLK emits a placeholder speaker even with diarization disabled. Only
        # an explicit capability authorizes interpreting provider labels.
        self.diarization = self.configured_diarization = message.get('diarization') is True
        provider = message.get('stt_provider')
        if isinstance(provider, str) and re.fullmatch(r'[A-Za-z0-9._-]{1,64}', provider):
            self.provider = provider

    @staticmethod
    def _time(value, duration: float) -> float:
        if isinstance(value, str) and re.fullmatch(r'\d+:\d{2}:\d{2}(?:\.\d+)?', value):
            hours, minutes, seconds = value.split(':')
            if int(minutes) >= 60 or float(seconds) >= 60:
                raise ValueError('Invalid preview timestamp')
            value = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        if (isinstance(value, bool) or not isinstance(value, (float, int))
                or not math.isfinite(value) or value < 0 or value > duration + 0.1):
            raise ValueError('Invalid preview timestamp')
        return min(float(value), duration)

    def _speaker(self, value):
        if not self.diarization:
            return None, -1
        if isinstance(value, str) and re.fullmatch(r'SPEAKER_\d+', value):
            value = int(value.split('_')[1])
        if type(value) is int and 0 <= value <= 9999:
            return f'SPEAKER_{value:02d}', value
        return None, -1

    def _row(self, key, text, start, end, *, speaker=None, translations=None, draft=False):
        label, number = self._speaker(speaker) if not draft else (None, -1)
        return {'id': f'{self.id}-{key}', 'text': text, 'start': start, 'end': end,
                'speaker': label, 'speaker_id': number, 'is_user': False,
                'stt_provider': self.provider, 'is_draft': draft,
                'translations': translations or []}

    @staticmethod
    def _translations(line):
        result = []
        for translation in line.get('translations') or []:
            if (isinstance(translation, dict) and isinstance(translation.get('lang'), str)
                    and isinstance(translation.get('text'), str)
                    and translation['lang'].strip() and translation['text'].strip()):
                result.append({'lang': translation['lang'].strip(), 'text': translation['text'].strip()})
        return result

    def update(self, snapshot: dict, audio_seconds: float) -> dict | None:
        if 'lines' not in snapshot or 'buffer_transcription' not in snapshot:
            return None
        lines = snapshot['lines']
        if not isinstance(lines, list) or any(not isinstance(line, dict) for line in lines):
            raise ValueError('Invalid preview lines')
        lines = [line for line in lines if line.get('speaker') != -2 and line.get('text')]
        if any(not isinstance(line['text'], str) for line in lines):
            raise ValueError('Invalid preview text')
        pending = [snapshot.get(name, '') for name in ('buffer_diarization', 'buffer_transcription')]
        if any(not isinstance(text, str) for text in pending):
            raise ValueError('Invalid preview buffer')
        segments = []
        if any(line.get('start') is None or line.get('end') is None for line in lines):
            # Legacy Parakeet exposes only an accumulated hypothesis. Its full
            # audio span is useful for ordering, but is not a phrase timestamp.
            pending = [line['text'] for line in lines] + pending
        else:
            for index, line in enumerate(lines):
                start = self._time(line['start'], audio_seconds)
                end = self._time(line['end'], audio_seconds)
                if end < start or (segments and start < segments[-1]['start']):
                    raise ValueError('Unordered preview timestamps')
                if line['text'].strip():
                    segments.append(self._row(f'line-{index}', line['text'].strip(), start, end,
                                              speaker=line.get('speaker'), translations=self._translations(line)))
        draft = ' '.join(text.strip() for text in pending if text.strip())
        if draft:
            segments.append(self._row('draft', draft, segments[-1]['end'] if segments else 0.0,
                                      audio_seconds, draft=True))
        self.last_snapshot = snapshot
        if segments == self.segments:
            return None
        self.segments = segments
        self.text = ' '.join(segment['text'] for segment in segments)
        self.revision += 1
        return {'type': 'local_transcript_snapshot', 'preview_id': self.id,
                'revision': self.revision, 'segments': segments}


class LocalLivePreview:
    # Transport limits, not ASR quality/latency acceptance criteria. If a local
    # socket stalls for a whole trial recording, stop preview and preserve WAV.
    MAX_PENDING_BYTES = 60 * 32000
    MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
    FINISH_TIMEOUT = 60.0
    STATUS_TIMEOUT = 1.0

    def __init__(self, url, send_segments, *, unavailable_reason='disabled'):
        self.url = url
        self.send_segments = send_segments
        self.queue = asyncio.Queue()
        self.pending_bytes = 0
        self.audio_bytes = 0
        self.segment = PreviewSegment()
        self.accepting = True
        self.deliver = True
        self.failed = False
        self.disabled = False
        self.updates = 0
        self.eof_ack = False
        self.diarization_capability = None
        self.unavailable_reason = unavailable_reason
        self.failure_status_task = None
        self.task = create_named_task(self._run(), name='local-preview-session')

    @property
    def diarization_status(self):
        """Current session evidence; ordinary ASR placeholders never count."""
        state, labeled = 'unknown', 0
        if self.disabled or self.diarization_capability is False:
            state = 'disabled'
        elif self.failed:
            state = 'failed'
        elif self.diarization_capability is True:
            if not self.segment.diarization:
                state = 'degraded'
            else:
                labeled = sum(row['speaker'] is not None for row in self.segment.segments)
                state = 'labeled' if labeled else 'pending'
        return {'state': state, 'labeled_segments': labeled}

    @classmethod
    def from_environment(cls, send_segments):
        try:
            return cls(preview_url(), send_segments)
        except ValueError:
            # A preview configuration error must never disable WAV capture.
            return cls(None, send_segments, unavailable_reason='invalid_configuration')

    async def _send_status(self, status, reason=None):
        if not self.deliver:
            return
        payload = {'type': 'service_status', 'status': status, 'provider': 'local_live_preview'}
        if status == 'stt_failed':
            payload.update(outcome='unavailable', reason=reason, retryable=False)
        try:
            await asyncio.wait_for(self.send_segments(payload), self.STATUS_TIMEOUT)
        except Exception:
            # Status delivery cannot interrupt audio or expose adapter errors.
            pass

    def _fail(self, reason, *, status_reason='unavailable'):
        if not self.failed:
            self.failed = True
            record_fallback(component='stt_selection', from_mode='local_live_preview',
                            to_mode='offline_capture', reason=reason, outcome='degraded')
            self.failure_status_task = create_named_task(
                self._send_status('stt_failed', status_reason), name='local-preview-status')

    def feed(self, pcm: bytes) -> None:
        if not self.accepting or self.task.done():
            return
        if self.pending_bytes + len(pcm) > self.MAX_PENDING_BYTES:
            self._fail('capacity_full', status_reason='buffer_full')
            self.accepting = False
            self.task.cancel()
            return
        self.audio_bytes += len(pcm)
        self.pending_bytes += len(pcm)
        self.queue.put_nowait(pcm)

    async def _send(self, socket):
        while True:
            pcm = await self.queue.get()
            if pcm is None:
                await socket.send(b'')  # WLK's explicit end-of-input contract.
                return
            self.pending_bytes -= len(pcm)
            await socket.send(pcm)

    async def _receive(self, socket):
        async for raw in socket:
            message = json.loads(raw)
            if message.get('type') == 'ready_to_stop':
                self.eof_ack = True
                return
            if message.get('type') == 'error' or message.get('status') == 'error':
                raise RuntimeError('local_preview_error')
            status = (message.get('status') if message.get('type') == 'diarization_status'
                      else message.get('diarization_status'))
            if status in {'degraded', 'ready'}:
                enabled = self.segment.configured_diarization and status == 'ready'
                if enabled != self.segment.diarization:
                    self.segment.diarization = enabled
                    record_fallback(component='stt_selection',
                                    from_mode='local_live_preview' if enabled else 'local_live_diarization',
                                    to_mode='local_live_diarization' if enabled else 'local_live_preview',
                                    reason='local_heal' if enabled else 'other',
                                    outcome='recovered' if enabled else 'degraded')
                if message.get('type') == 'diarization_status':
                    # Immediately clear previous inferred labels, even before
                    # the ASR produces another update. Audio/text keep flowing.
                    message = self.segment.last_snapshot or {}
            segments = self.segment.update(message, self.audio_bytes / 32000)
            if segments is not None and self.deliver:
                try:
                    await self.send_segments(segments)
                    self.updates += 1
                    if self.updates == 1:
                        logger.warning('Local preview: first_segment_sent')
                except Exception:
                    # Phone Stop closes /v4/listen. Still consume /asr to EOF.
                    self.deliver = False
        if not self.eof_ack:
            raise RuntimeError('local_preview_closed_without_eof')

    async def _run(self):
        sender = None
        try:
            if not self.url:
                if self.unavailable_reason == 'disabled':
                    self.disabled = True
                else:
                    self._fail('config_incomplete', status_reason=self.unavailable_reason)
                return
            async with websockets.connect(self.url, open_timeout=3, close_timeout=2,
                                          max_size=self.MAX_SNAPSHOT_BYTES) as socket:
                # The loopback relay may first negotiate TLS and the selected
                # remote WLK handshake; only that relay can contact the provider.
                handshake_timeout = 12 if os.getenv('OMI_LOCAL_LIVE_PROVIDER_RELAY') == '1' else 3
                config = json.loads(await asyncio.wait_for(socket.recv(), handshake_timeout))
                if config.get('type') != 'config' or config.get('useAudioWorklet') is not True:
                    raise RuntimeError('local_preview_pcm_contract')
                if config.get('enabled') is False:
                    self.disabled = True
                    return
                self.diarization_capability = config.get('diarization') if type(config.get('diarization')) is bool else None
                self.segment.configure(config)
                if self.failed:
                    return
                await self._send_status('ready')
                sender = create_named_task(self._send(socket), name='local-preview-send')
                receiver = create_named_task(self._receive(socket), name='local-preview-receive')
                try:
                    done, _ = await asyncio.wait([sender, receiver], return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                    if receiver in done and not sender.done():
                        raise RuntimeError('local_preview_early_eof')
                    await receiver
                finally:
                    await drain_tasks([sender, receiver], timeout=2, label='local_preview', cancel=True)
        except asyncio.CancelledError:
            raise
        except ConnectionClosed as error:
            code = error.rcvd.code if error.rcvd is not None else None
            # 1013 also covers a recovering or unavailable native worker.
            # Forward only our fixed contract values, never raw close reasons.
            status_reason = {
                'live_preview_busy': 'busy',
                'final_transcription_busy': 'busy',
                'live_preview_recovering': 'recovering',
                'live_preview_unavailable': 'unavailable',
            }.get(error.rcvd.reason, 'unavailable') if code == 1013 else 'unavailable'
            self._fail('capacity_full' if status_reason == 'busy' else 'other', status_reason=status_reason)
        except (ValueError, TypeError, RuntimeError):
            self._fail('other', status_reason='protocol_error')
        except Exception:
            self._fail('other')
        finally:
            self.accepting = False
            self.segment.text = ''
            self.segment.segments = []
            self.segment.last_snapshot = None
            while not self.queue.empty():
                self.queue.get_nowait()
            self.pending_bytes = 0

    def end_input(self) -> None:
        self.deliver = False
        if self.accepting:
            self.accepting = False
            self.queue.put_nowait(None)

    async def finish(self) -> None:
        self.end_input()
        try:
            await asyncio.wait_for(asyncio.shield(self.task), self.FINISH_TIMEOUT)
        except asyncio.TimeoutError:
            self._fail('timeout')
            await drain_tasks([self.task], timeout=2, label='local_preview', cancel=True)
        except asyncio.CancelledError:
            await drain_tasks([self.task], timeout=2, label='local_preview', cancel=True)
            if asyncio.current_task().cancelling():
                raise
        finally:
            if self.failure_status_task is not None:
                await drain_tasks([self.failure_status_task], timeout=self.STATUS_TIMEOUT + 0.1,
                                  label='local_preview_status', cancel=False)
            logger.warning('Local preview ended updates=%d eof_ack=%s failed=%s',
                        self.updates, self.eof_ack, self.failed)
