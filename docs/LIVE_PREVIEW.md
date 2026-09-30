# Experimental live preview on Mac

Live STT is also configurable under **Settings → Live STT** in the library.
See [supported providers and applying settings](PROVIDERS.md#live-stt).
After the first UI save, the registry controls the active profile;
the earlier `live-stt.json` remains a copy of the original settings.

## Monitoring in the web library

Open `http://127.0.0.1:20001` on the Mac and select **Now**.
The screen shows the actual audio source, received audio duration, live text,
both STT states, and events. If the frame counter does not change for more than
five seconds, a stream-paused message appears. Server transmission of live text
does not prove that the iPhone displayed it.

**Events** is a bounded observation log held in the library process's memory.
It begins when the monitor is first opened; timestamps indicate observation time.
It contains no words, keys, addresses, or recording identifiers.
After Stop, the draft disappears. Once final STT finishes, the completed transcript
is available in the left-hand list under **Recording**.
Clicking a phrase plays the corresponding audio segment.

Technical logs are in `.local/dev-harness/ngrok/logs/`:
`live-stt.log` contains live STT events and timing;
`stt-worker.log` covers final processing and import;
`backend.log` covers connections, received frames, and sent segments.
Read recognized text in the interface, not in logs.

While the tab is visible, the library gets snapshots from its own `/api/runtime`
every second. Only its server reads the key from private `.env`.
The backend supplies a separate `/v1/local/preview` endpoint with the existing
key check and additional rejection of proxy or remote access. Text stays in memory
until the session ends; `/v1/local/status` still returns text-free diagnostics.
Updates resume automatically when returning to the page. Final-STT checks have a
separate timeout and do not hide available live text.

After updating this feature, stop recording and update the backend and library
processes. Refreshing the page alone does not change server-side Python code.

## Live preview flow

The app receives complete `local_transcript_snapshot` messages over `/v4/listen`:
`preview_id`, an increasing `revision`, and a `segments` array. A new snapshot
atomically replaces that live session's lines, including corrections and deletions.
The first transition from the old single-segment format requires updating both
the iOS app and backend. Later STT changes do not require a new build.
The home screen uses the original Omi interface; tap the recording card to open
text. There is no temporary **Transcript** button.

CV1 sends Opus; the backend decodes it to little-endian PCM16, mono, 16 kHz.
The phone microphone sends PCM16. A copy of the PCM goes over local WebSocket
`/asr`, while the existing handler saves the WAV. Live text remains in memory;
the independently selected final STT engine processes completed WAVs.
Available engines include Core ML Parakeet TDT v3 INT8 through FluidAudio and
WhisperLiveKit/MLX; provider selection is described below.
The following parameters apply to Parakeet. New tokens from each window are
appended to accumulated text, and the app receives the updated complete draft.
Low confidence in a new window does not erase earlier phrases.
This text does not replace the final WhisperKit transcript.

## Preparation and startup

On a new Mac, run `./start.command`. It prepares and starts live transcription
alongside the backend and final transcription. Requirements: Apple Silicon,
macOS 14+, Xcode with Swift 6.0+, and free disk space. Prerequisites for both
models are checked first; a failure stops startup with a reason.
Existing verified files are reused.

For separate preparation: `bash scripts/install-local-live.sh`.
To check prepared files without building or downloading:
`bash scripts/install-local-live.sh --check`.
Files are stored in `.local/parakeet-live/`; the manifest and `Package.resolved`
pin the build and models. Installation verifies real Start/PCM/Stop behavior on
synthetic speech with networking denied. To check running live and final STT:
`./start.command --check`.

The `live-preview` service belongs to the same instance as the backend.
Repeated startup preserves a ready service; `bash scripts/local-mac.sh down`
stops it with the other services. The backend receives its address automatically.
After changing settings for a running backend, finish recording/processing,
run `down`, and run `start.command` again.

## Worker

The installer uses FluidAudio v0.15.6 at a pinned commit, the local
`FluidInference/parakeet-tdt-0.6b-v3-coreml` model, and `ParakeetWorker.swift`.
It uses the INT8 encoder, `cpuAndNeuralEngine`, and automatic language detection
without a hint. Standard `SlidingWindowAsrManager.default` processes windows
sequentially: 11 seconds of new audio, 2 seconds of left context, and 2 seconds
of right context. Batch parameters `parallelChunkConcurrency` and `melChunkContext`
are not used in this path. Models load once from verified local files; each new
session gets separate state. There are no network downloads, and the OS sandbox
policy denies the worker network access. Swap usage is not a stopping criterion.

For a separate manually managed process, from `scripts/`:

```bash
"$BACKEND_PYTHON" -m local_live_preview.serve_parakeet \
  --worker "$PARAKEET_WORKER" \
  --model-dir "$PARAKEET_MODEL_DIR" \
  --inference-lock ../.local/dev-harness/ngrok/services/local-transcripts/.lock \
  --port 18090
```

The variables point to the existing backend Python, compiled worker, and
`parakeet-tdt-0.6b-v3` model directory. Prepared components are reused;
WLK code and its model remain available for an explicit switch back.
The lock file must belong to the same local backend instance that performs final
transcription. Wait for `model_ready`; `http://127.0.0.1:18090/health` contains
only technical metrics.

Managed Parakeet and WhisperLiveKit health report `busy` while their shared
inference slot is occupied, including by final transcription. The iPhone and
Mac status show **busy** in that case; a loaded model alone does not mean a new
live session can start. Audio recording and final transcription remain separate.
If a recording starts while the slot is occupied, live preview is unavailable
for that recording; wait for final processing to finish before starting the next
recording to use Live. The current stream does not retry busy admission.

For that external process, save
`{"enabled":true,"url":"ws://127.0.0.1:18090/asr"}` in
`.local/dev-harness/ngrok/live-preview.json`. Only a loopback `/asr` endpoint is
allowed. The launcher checks readiness but does not install or restart the external
process. Use `{"enabled":true}` for normal managed operation or
`{"enabled":false}` to disable explicitly. Settings apply after Stop/`down`/startup.
The earlier `OMI_LOCAL_LIVE_PREVIEW_URL` variable is retained as an external URL
during initial setup. If using `live-stt.json` below, set `provider: "external"`
for a manual process; that file takes precedence over `live-preview.json`.

## Checking on iPhone

The current recording card and transcript header show the source: **Omi** or
**Microphone · iPhone**. This label belongs to the active recording;
Bluetooth connection alone does not change the source. The label disappears after Stop.

Start recording with a single press of the physical CV1 button, then tap the
recording card in the app. Speak for 30–60 seconds, including a pause.
The first text is expected after about 13 seconds, with updates about every
11 seconds. Check that new phrases are added and earlier ones remain.
Press the CV1 button again, then verify the WAV and final text in the library.

For a separate iPhone-microphone check, keep the Mac connection and disable
Transcribe Later. The original bottom **+** button starts the microphone and
opens the recording screen; a long press opens the menu.
Switching to the phone microphone finishes the Omi recording while preserving
Bluetooth. An explicit Omi start switches the input back; reconnection alone does not.
The normal phone fallback can record locally without a network connection,
but delivering those recordings into this library is not connected yet.

On Stop, the phone closes `/v4/listen` immediately; the backend separately sends
EOF to Parakeet and completes the session. Remaining text after Stop may therefore
never reach the screen. The next session gets a new segment and empty recognition
context. Without a separate live diarizer, speakers in these ASR engines are unknown.
WhisperLiveKit preserves phrase boundaries and preliminary timestamps;
Parakeet sends one combined draft without phrase timestamps. Unconfirmed text is
marked `is_draft`; its timing and speaker are not presented as established results.
Earlier tokens are retained without later revision. This is experimental windowed
output, not word-by-word updates; human quality evaluation is still required.

Only one recording is recognized at a time. If live or final transcription is busy,
preview is unavailable for a new connection, while WAV storage continues.
After `/asr` fails, it is not reconnected within that recording; the app reports
unavailable transcription while the WAV continues to be saved.
The adapter attempts to recover the worker for the next recording; loading, busy,
and permanently unavailable states are distinct. Startup errors produce a safe
`startup_failed` event in the `live-preview` log without audio or text.
Transport-buffer and final-drain limits protect recording from a stuck preview;
they are not recognition-quality criteria. Live logs contain neither audio nor text.

## Selecting a live provider

Select final STT and live STT independently. Save private
`.local/dev-harness/ngrok/live-stt.json`:

```json
{
  "enabled": true,
  "provider": "external",
  "url": "ws://127.0.0.1:18090/asr"
}
```

This file takes precedence over `live-preview.json`: startup and readiness checks
use the selected provider, without installing another Parakeet instance.
`enabled: false` keeps live disabled. Without `live-stt.json`, the previous
`live-preview.json` settings apply.

Any local server implementing the existing contract is supported: binary PCM16,
mono, 16 kHz; an empty binary frame ends the stream. The server first sends
`{"type":"config","useAudioWorklet":true,"diarization":true,"stt_provider":"my-local-stt"}`,
then complete snapshots such as
`{"lines":[{"text":"confirmed text","speaker":0,"start":0.0,"end":1.5}],"buffer_transcription":"draft"}`,
and finally `{"type":"ready_to_stop"}`. `/health` returns `ready` and `active`.
`diarization: true` is valid only if the server actually distinguishes voices;
without it, or with `false`, numeric placeholder speakers are treated as unknown.
Line times may be seconds or `H:MM:SS.cc`. `speaker: -2` means silence.
`buffer_diarization` and `buffer_transcription` appear as a separate unknown draft;
a snapshot may correct or remove earlier lines. Actual translations can be supplied
as `translations: [{"lang":"en","text":"..."}]`; otherwise no translation is created.
The URL allows only `127.0.0.1` and `/asr`.

Prepare a separate Mac environment after installing the backend:

```bash
uv venv --python backend/.venv/bin/python .local/live-stt/venv
uv pip install --python .local/live-stt/venv/bin/python -r scripts/dev-harness/requirements-live-stt-macos.txt
```

Download the model separately: place `config.json` and `weights.npz` from the
Hugging Face revision below into `.local/live-stt/model/`.
Python and model paths in the configuration must be absolute.

For managed WhisperLiveKit, set `provider: "whisperlivekit"` and add `python`,
`model_dir`, `chunk_seconds: 2`, and `language` (`ru`, `en`, or `auto`).
This uses the existing MLX/LocalAgreement adapter: WhisperLiveKit 0.2.26,
mlx-whisper 0.4.3, MLX 0.32.2, model `mlx-community/whisper-large-v3-turbo-q4`,
revision `660c343bbf4e52ac257f0b7d952e5388e6f93bef`.
The weights' SHA-256 is pinned in `scripts/local_live_preview/serve.py`.
Processing uses overlapping windows. `chunk_seconds` sets the minimum inference
interval (1–10 seconds, default 4), not guaranteed latency.
Start with 2 seconds for short updates. After loading local models, the adapter
denies network connections.

`bash scripts/local-mac.sh apply-stt` starts the configured STT services and applies
live settings to the stack-owned backend. It verifies that recording has finished
before restarting. Normal `up` starts the services automatically.
`enabled: false` disables live for new connections after `apply-stt`.
No iPhone reinstallation is needed. Logs contain only technical counters;
physical CV1/iPhone behavior and speech quality are checked separately from server readiness.

## Speakers during recording

Independent local pyannote can run over managed WhisperLiveKit or a compatible
external `/asr`. Extend the existing `live-stt.json`, preserving its `url` and other fields:

```json
{
  "diarization": {
    "enabled": true,
    "python": "/absolute/path/to/.local/diarization/venv/bin/python",
    "port": 18091,
    "model": "pyannote/speaker-diarization-3.1",
    "device": "mps",
    "threads": 4,
    "interval_seconds": 15
  }
}
```

Install dependencies into the separate prepared environment with
`uv pip install --python .local/diarization/venv/bin/python -r scripts/dev-harness/requirements-live-diarization-macos.txt`.
Prepare models beforehand following [LOCAL_STT.md](LOCAL_STT.md).
Community-1 is also supported when its complete cache is available.
`apply-stt` starts the diarizer on a separate loopback port and routes the backend
through it to the original ASR. The diarizer loads its model once; text continues
arriving independently of speaker detection. `/health` reports readiness and
activity; `live-diarization.log` contains only technical events.

After at least 15 seconds of new audio, speaker boundaries are recalculated over
the accumulated recording. Voices are matched to earlier turns by interval overlap;
visible numbers are assigned when the first actual utterance appears.
Noise clusters before speech do not consume speaker numbers. Only one calculation
runs at a time; latency grows with recording length, without accumulating a queue
of calculations. WhisperLiveKit word timestamps allow phrases to be split between
voices. An external ASR may supply
`words: [{"word":" word", "start":0.0, "end":0.5}]`; their complete text must match
the line's `text`. Without word timestamps, the label applies to the entire phrase.

The iPhone displays standard colored Speaker 1, Speaker 2, and later cards,
the ASR name, and actual phrase boundaries. Earlier cards may be refined.
Until a voice is determined, it shows **Unknown**; unfinished drafts do not receive
invented timestamps. Translations appear only when supplied by the provider.
If diarization fails, speakers become unknown while text and recording continue.
After four hours, diarization for that session is disabled while ASR continues;
temporary PCM is removed when the session closes. This is a resource limit,
not a verified operating duration.

Recorded verification on September 10, 2026: synthetic CV1 Opus sent through ngrok
produced its first live text after 3.6 seconds, nine updates, and a 19.04-second WAV
without decoding errors. Argmax automatically imported the final text (six segments).
This verified the software path; physical CV1 recording after switching providers
and long-session reliability remain unverified.


### Live diarization status

The Mac library's **Сейчас** view and the expanded **Local Mac** status card on
iPhone show live speaker-label status separately from transcription and audio.
The phone row is labeled **Instant speaker labels** (localized in the app).

- **Off**: the selected stream explicitly disables diarization.
- **Service/model ready**: a bounded health check confirms readiness, not inference.
- **Service busy / waiting for data**: the service is occupied or this session has
  negotiated diarization but has not produced usable labels yet.
- **Labels received / N processed**: N segments in this session's current draft
  contain labels from a provider that explicitly negotiated diarization.
- **Cannot identify speakers / diarization error**: the stream degraded or failed.
  Previous labels are not counted as current success; audio and ASR have separate status.
- **Unknown**: the server or provider does not report this capability.

The relay checks only the selected provider's health endpoint, with bounded time
and response size, no redirects, and no speech transmission. Remote credentials
remain in the relay and are not passed to an independent local diarization proxy.
A provider without the health extension can still report capability in the next
recording's handshake. Older phone/server versions remain usable.

These are statuses of the live stream, not the separate **Diarization** provider
for completed recordings. Labels are anonymous within a recording and do not prove
speaker identity or diarization accuracy. A healthy service alone never marks a
recording as successfully diarized.
