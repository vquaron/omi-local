# Migrating final transcription from WhisperX to WhisperKit

WhisperKit is already integrated. New native Mac installations select it through
`local_transcription.DEFAULT_ENGINE`; startup preserves an existing engine and
an explicit disabled setting. Check the selected provider before migrating:
`openai-compatible` can already be backed by the owned Argmax/WhisperKit server.
Installing the managed CLI is optional for that configuration.

## Pipeline and ownership

```text
iPhone/CV1 -> local receiver -> completed PCM16 mono 16 kHz WAV
  -> final transcription adapter -> optional independent diarization
  -> optional summary -> validated Conversation import

live PCM -> WLK-compatible /asr -> draft text (separate from final transcription)
```

The harness owns final processing in `scripts/dev-harness/dev_harness/`.
`local_stt.py` validates WAVs, selects adapters and records result provenance;
`local_stt_watch.py` queues completed recordings; `local_pipeline.py` manages
stage results. `backend/utils/local_transcript.py` projects the result into the
existing Conversation schema. An ASR switch needs no iPhone rebuild.

| Concern | WhisperX | WhisperKit |
| --- | --- | --- |
| Preparation | `scripts/install-local-stt.sh` | `scripts/install-local-whisperkit.sh` |
| Installer | `stt_install.py`, `local_whisperx.py` | `whisperkit_install.py`, `local_whisperkit.py` |
| Pins | `requirements-whisperx-macos.{in,txt}`, `whisperx-models.json`, `whisperx-local.patch` | `whisperkit-models.json`, upstream `Package.resolved` |
| Runtime | Python 3.12 lock, WhisperX 3.8.6, faster-whisper 1.2.1, CTranslate2 4.8.2, Torch 2.8.0, TorchCodec 0.7.0 | Swift CLI, WhisperKit 1.1.0, Core ML, CPU + Neural Engine |
| Managed files | `.local/stt/` | `.local/whisperkit/` |
| ASR model | large-v3-turbo | `openai_whisper-large-v3-v20240930_626MB` |
| Alignment | WhisperX alignment model | WhisperKit word timing; no WhisperX align call |
| Speakers | Optional embedded Pyannote | Independent diarization stage |

The WhisperKit source is pinned to
`1e2a163736dfa5a198e637ae44c114e1c6d5cc2d`; model files and tokenizer assets
have pinned revisions, sizes and SHA-256 checksums. Swift Argument Parser 1.7.0
is locked by the upstream package. The installer builds release
`whisperkit-cli` with two jobs and automatic dependency resolution disabled.
It verifies synthetic speech and word timestamps with network access denied
before writing readiness. Installation does not activate the engine.

## Installation and activation

Run commands from the repository root. The managed installer needs Apple Silicon,
macOS 14+, Xcode with Swift 5.10+, at least 4 GiB free space and network access
for initial preparation. It uses the repository's backend Python, not a global
`pip install whisperkit` package.

```bash
# For a fresh Mac checkout: prepare first, then start the native runtime.
./omiloc bootstrap
./omiloc up

# For an existing prepared backend: prepare the managed final CLI only.
bash scripts/install-local-whisperkit.sh

# File/receipt verification only; this does not perform new inference.
bash scripts/install-local-whisperkit.sh --check
```

For an existing installation, finish the current recording and final job before
changing providers. In the library Settings select the prepared WhisperKit card
for **Transcription**, check it, save it and use it. Preserve the independently
selected **Diarization**, **Live STT** and **Summarization** cards.

If `providers.json` exists, it is authoritative: changing `stt-engine.json`
alone does not switch the active provider. Before registry migration, the legacy
procedure is documented in [Local STT](LOCAL_STT.md). When editing that legacy
profile, replace engine-specific fields but retain the intended `speaker_*`
settings. Use `engine: whisperkit`, the Core ML model above, `language: auto`,
and `diarization_model: none`; remove obsolete WhisperX `python`,
`library_path`, `assets_path`, `device` and `runtime_revision` overrides so the
WhisperKit loader supplies its runtime values. Do not copy a whole single-speaker
example over a profile that intentionally enables independent speakers.

WhisperX's embedded diarization cannot carry over as an embedded WhisperKit
setting. Select independent Pyannote first, or explicitly disable diarization.
Existing queued jobs retain their engine/model settings; runtime provenance is
refreshed by the worker. A changed engine does not reprocess the archive.

## API and timestamp contract

The managed adapter runs `whisperkit-cli transcribe` with local model/tokenizer
paths, `--word-timestamps`, `--skip-special-tokens`, one inference worker and
JSON reports under a network-denied sandbox. It omits `--language` for `auto`.
It does not require an HTTP server.

The alternative owned Argmax service uses `run_argmax.py` and
`local_stt_services.py`, binds loopback and receives requests through
`local_openai_stt.py`. The client checks `GET /v1/models` and submits multipart
WAVs to `POST /v1/audio/transcriptions` with `response_format=verbose_json`,
word/segment timestamp granularities and the server's exact model ID. An HTTP
model ID need not equal the Core ML directory name. Redirects and environment
proxies are disabled. Plain text responses cannot satisfy the contract.

Both adapters produce this internal shape (times are seconds from WAV start):

```json
{
  "language": "ru",
  "segments": [{
    "start": 0.2, "end": 0.8, "text": "Привет",
    "speaker": "SPEAKER_00",
    "words": [{"start": 0.2, "end": 0.8, "word": " Привет", "speaker": "SPEAKER_00"}]
  }]
}
```

CLI `probability` maps to optional internal word `score`. HTTP responses may
omit word confidence; the adapter does not invent it. Keep segment/word times
numeric, finite and ordered. The CLI adapter handles padded hypotheses and
uses the pinned chunk `seek` offsets in 16 kHz samples to bound real audio.
Do not convert seconds to milliseconds or add chunk offsets a second time.
No speech is a successful empty outcome, not an error or a fabricated segment.

Independent Pyannote consumes the same WAV and reconciles speaker turns by word
overlap, splitting phrases at speaker changes. With no timed words it assigns
whole segments; unmatched spans remain unknown. `SPEAKER_00` before diarization
does not identify a person. Pyannote and its model cache remain separate from
WhisperKit; removing Torch/Pyannote globally would break this stage and can also
break an existing WLK live environment.

## Live preview is a separate migration

WhisperX, WhisperKit and WhisperLiveKit are different components. The app's live
transport expects WLK's WebSocket protocol: configuration/useAudioWorklet,
PCM16 frames, `lines`/`buffer_transcription`, an empty binary end-of-stream frame,
and `ready_to_stop`. New managed native live preparation uses Parakeet/FluidAudio;
existing explicitly configured WLK servers remain supported.

WhisperKit's microphone CLI or HTTP server-sent events do not implement this
WebSocket contract. Replacing live ASR with WhisperKit would require a persistent
model-owning adapter, PCM buffering and resampling validation, stable committed
versus revisable text, timestamp offsets, cancellation, backpressure and final
drain behavior. Keep the existing live provider until those behaviors are
implemented and tested. Switching final transcription alone is supported now.

## Verification and rollback

Use the existing synthetic `scripts/dev-harness/fixtures/speech-check.wav`, not
private recordings, for initial runtime verification. Installation exercises
Russian keyword recognition and word timings offline. Test an English fixture
with automatic language detection, silence, audio over 30 seconds, clipped final
words and speaker changes before declaring quality/long-session acceptance.

```bash
PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python -m pytest -q -p no:cacheprovider \
  scripts/dev-harness/tests/test_local_stt.py \
  scripts/dev-harness/tests/test_stt_install.py \
  scripts/dev-harness/tests/test_local_transcription.py \
  scripts/dev-harness/tests/test_local_providers.py \
  scripts/dev-harness/tests/test_local_stt_watch.py \
  scripts/dev-harness/tests/test_local_provider_health.py

(cd backend && PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q \
  -p no:cacheprovider tests/unit/test_local_transcript.py)

bash scripts/local-mac.sh transcription-status
```

One macOS installer test binds a temporary loopback socket to verify the child
sandbox's network denial. An outer sandbox denying that bind makes this check
indeterminate; rerun with local socket access rather than weakening the test.
Unit tests and `--check` do not establish live service availability, first-run
installation, real inference quality or physical iPhone reliability.

Rollback selects the previously prepared provider for new jobs. Keep its model
files and the existing queue/results. Do not delete WhisperX support merely
because it is no longer the default: legacy jobs and transcript history still
use it. The retained `fal_whisperx` collections/enums and comparison UI are
historical data contracts, not an active dependency of WhisperKit inference.

Native `bootstrap` owns `local_transcription.prepare()` and
`configure_defaults()`; `up` starts the prepared runtime without downloads.
No second bootstrap path or global dependency replacement is needed. Docker
retains its separate Linux CPU/CUDA ASR runtime; Core ML runs in the native Mac stack.
