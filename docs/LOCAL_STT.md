# Transcribing recordings

For containers on Mac CPU or Linux/WSL2 NVIDIA, see [Docker](DOCKER.md).

Select engines and servers in the web library's **Settings**.
See [STT, diarization, and summarization providers](PROVIDERS.md).
The manual JSON settings below apply until the first save through the UI;
after migration, the provider registry is the source of settings.

Text is created on the Mac after recording ends. Use timestamps in `omiloc` to
listen to uncertain passages. Changing the engine does not require reinstalling
the iPhone app.

The separate experimental path for draft text during recording is documented in
[Live preview](LIVE_PREVIEW.md); it preserves final WAV processing.

## WhisperKit

The selected profile is **large-v3-turbo**, the Core ML variant
`openai_whisper-large-v3-v20240930_626MB`. The audio encoder and text decoder use
`cpuAndNeuralEngine`.

Earlier experimental speed and quality reports were outdated and removed on
September 9, 2026. This profile's quality and speed are being evaluated again.

On a new Mac, `start.command` prepares WhisperKit, selects this profile, and
enables processing for new recordings. Repeated startup preserves an existing
engine or explicit disabled setting. To prepare WhisperKit separately:

```bash
bash scripts/install-local-whisperkit.sh
```

Requirements: Apple Silicon, macOS 14 or later, Xcode with Swift 5.10 or later,
internet access during installation, and at least 4 GiB free space.
The installer builds WhisperKit 1.1.0 and downloads about 630 MB: compressed
large-v3-turbo and its tokenizer. No Hugging Face account is required.
Files and checksums are pinned in the repository. The first launch may take
several minutes while Core ML prepares the model. Readiness is checked with
synthetic Russian speech, timestamps, and network access denied.

The environment is in `.local/whisperkit/`. If interrupted, repeat the command:
verified downloads and a matching build are reused. Compilation errors are saved
in `.local/whisperkit/build.log`. To check prepared files without building:
`bash scripts/install-local-whisperkit.sh --check`.

The separate installer prepares the engine without changing active processing.
To switch manually from another engine when processing is enabled, wait for the
current recording and transcription to finish, then run:

```bash
bash scripts/local-mac.sh auto-transcribe-off
```

Save this in `.local/dev-harness/ngrok/stt-engine.json`:

```json
{
  "engine": "whisperkit",
  "model": "openai_whisper-large-v3-v20240930_626MB",
  "language": "auto",
  "diarization_model": "none"
}
```

Then enable processing (`start.command` does this for a normal fresh installation):

```bash
bash scripts/local-mac.sh auto-transcribe-on
```

WhisperKit reads prepared local model files; each transcription process is denied
network access. The Core ML profile uses CPU and Neural Engine, one job at a time.
Segments and words receive timestamps and a single speaker label, `SPEAKER_00`.
Hypotheses outside the original audio are excluded. For a word that crosses the
end of the recording, only its interval within the WAV is retained; earlier
speech is preserved. `"language": "auto"` detects the recording's language.
Forcing `"language": "ru"` can produce Russian text even for English speech;
use it only for recordings known to be in Russian.

New recordings are processed automatically. Text appears in the library; on the
iPhone, refresh the list and open **Local recording**. The Mac must remain running
and awake. Submit recordings that existed before processing was first enabled
manually. Old transcripts are preserved; changing the engine does not reprocess
the archive by itself. Jobs already started retain their engine for retries.
If WhisperKit finds no speech, the library displays **No speech detected**.
The WAV remains playable; the empty result is saved without creating a text
record or repeatedly retrying transcription.

| Action | Command from the project directory |
|---|---|
| Check processing | `bash scripts/local-mac.sh transcription-status` |
| Disable while preserving recordings | `bash scripts/local-mac.sh auto-transcribe-off` |
| Process a file or retry after an error | `bash scripts/local-mac.sh transcribe "/path/to/recording.wav"` |

Input must be PCM16 WAV, mono, 16 kHz. Repeating the same file and profile reuses
the saved result. After restarting the Mac, run `start.command` again.

## Other prepared engines

WhisperX and Parakeet remain available for older jobs and explicit selection.
The earlier WhisperX installer is `bash scripts/install-local-stt.sh`; it prepares
large-v3-turbo/ru/CPU/float32 without speaker separation in `.local/stt/`.
Installing WhisperKit does not remove an earlier manual Python environment or its models.

See [WhisperX/Parakeet options and environment requirements](DEVELOPMENT.md#transcription-models).

## Your own local STT server

Completed WAVs support an OpenAI-compatible loopback API. The server must provide
`GET /v1/models` and `POST /v1/audio/transcriptions`, accept a multipart WAV,
and return `verbose_json` with timestamped `segments` and optional `words`.
Plain text without timestamps is insufficient. Redirects and HTTP proxies are
disabled; this configuration does not send recordings to the cloud.

In `.local/dev-harness/ngrok/stt-engine.json`:

```json
{
  "engine": "openai-compatible",
  "provider_url": "http://127.0.0.1:10301/v1",
  "model": "large-v3-v20240930_626MB",
  "language": "auto",
  "diarization_model": "none"
}
```

The model name must match `/v1/models`. After current processing finishes, apply
the profile with `auto-transcribe-off` / `auto-transcribe-on`.
The queue preserves the address and model for each job; old results are not replaced.
When updating a server under the same name, set a new `runtime_revision` to
distinguish results from different versions. Changing final STT does not change the live engine.

To start an already built Argmax automatically, create private
`.local/dev-harness/ngrok/argmax-stt.json` with `enabled: true`, `port: 10301`,
`model`, `binary`, `model_dir`, and `tokenizer_dir`. Paths must point to existing
local files. `local-mac.sh up` starts the stack-owned process, checks the model,
and restricts networking to loopback. Argmax output is suppressed; temporary WAVs
are stored in a private directory and removed on normal server shutdown.

## Speaker separation with any STT engine

Independent local diarization can follow local WhisperKit/WhisperX/Parakeet or a
separate OpenAI-compatible STT server. Add
`speaker_model: "pyannote/speaker-diarization-community-1"` to the existing
`stt-engine.json`, keeping `diarization_model: "none"`.
Explicit selection of `pyannote/speaker-diarization-3.1` from a prepared cache is also supported.

A separate Python with pyannote.audio 4.0.7 is expected at
`.local/diarization/venv/bin/python`; set `speaker_python` for another path.
Download models in advance from official Hugging Face sources after accepting
their terms. Downloads are prohibited during processing.
Settings: `speaker_device: "cpu"`, `speaker_threads: 4`, and `speaker_count: 0`
(automatic speaker count). Select `mps` only after checking speed and results on
your own recording; use a positive `speaker_count` for a known participant count.

Labels identify voices within a recording, not people's names. With word timestamps,
phrases are split at speaker changes; without them, each whole phrase is assigned
by maximum overlap. Unmatched regions remain unknown. Community-1 uses exclusive
speaker turns to align text. If diarization fails, intermediate transcription is
preserved for retry; a successful transcript is not published until both stages
finish. Changing settings creates a separate result and preserves original audio
and transcripts. This processes completed recordings; live preview remains separate.

Prepare the separate environment from the repository root:

```bash
uv venv --python 3.11 .local/diarization/venv
uv pip install --python .local/diarization/venv/bin/python 'pyannote.audio==4.0.7'
```

For a prepared 3.1 cache, the configuration previously tested on Mac is:

```json
{
  "speaker_model": "pyannote/speaker-diarization-3.1",
  "speaker_device": "mps",
  "speaker_threads": 4,
  "speaker_count": 0
}
```

These are additional fields for an existing profile, not a complete replacement.
After current processing finishes, restart only its worker with
`bash scripts/local-mac.sh auto-transcribe-off`, then
`bash scripts/local-mac.sh auto-transcribe-on`.
Older jobs retain their previous mode; use `transcribe` for an older recording.

## Continuous recording and pending audio

The local receiver saves a WAV part after five minutes of decoded audio and
continues receiving into a new part on the same socket. The final partial part
is saved when capture stops. Transcription consumes finished parts independently.
Conversations shows saved parts before a transcript exists, including queued,
processing, failed and no-transcript outcomes. These cards are read-only views
of the local audio library and disappear after successful transcript import.
The foreground list refreshes every five seconds. Physical long-session and
background reliability still require device verification.
