# Starting omiloc

Omi CV1 connects to the iPhone over Bluetooth. The iPhone sends audio to one
paired server; that server stores recordings and runs the selected processing
providers. Install the [iPhone app](LOCAL_SETUP.md) separately on a Mac with Xcode.

## Installation

Run commands from the checkout on the server. `omiloc` defaults to native on
macOS and Docker on Linux; use `--runtime` to select explicitly.

### Native Apple Silicon Mac

You need macOS 14+, Xcode with Swift 6+, and Tailscale installed and connected.
The Mac must stay awake while receiving audio. Initial preparation needs
internet access and at least 4 GiB free for managed models.

```bash
git clone https://github.com/vquaron/omi-local.git omiloc
cd omiloc
./omiloc bootstrap
./omiloc up
./omiloc open
```

`bootstrap` installs missing dependencies, prepares the configured models and
creates private pairing settings in `.env`. Existing provider choices, keys and
explicitly disabled stages are preserved. It does not start the stack.
`up` starts the prepared services without installing packages or downloading models.
Open `start.command` in Finder to run `bootstrap → up → open` together.

If preparation fails, follow its diagnostic, fix the cause and rerun
`./omiloc bootstrap`. Setup output is in private `.local/install.log`.
A clean-machine repeat installation remains unverified.

### Docker server

Install Docker Engine/Desktop and Compose 2.32+. For a Linux NVIDIA server,
prepare its driver, NVIDIA Container Toolkit and Tailscale first:

```bash
./omiloc --runtime docker bootstrap --device cuda --gpu 0 --transport tailscale
./omiloc --runtime docker up
./omiloc --runtime docker doctor --inference
```

Use `--device cpu` on Docker Desktop for Mac or a CPU host. Docker keeps its own
private `.env.docker`, named volumes and app key. For an existing remote STT
provider, use the [external provider setup](DOCKER.md#external-stt-server-instead-of-the-built-in-container).
See [Docker](DOCKER.md) for prerequisites and remote library access.

## Pair the iPhone

1. Install the [local iPhone app](LOCAL_SETUP.md). Use the Profile build for
   everyday launches from its icon.
2. Connect the iPhone and server to the same Tailscale network. In your own
   interactive terminal run `./omiloc pair`, or `./omiloc --runtime docker pair`.
   Pairing output contains a private app key; do not share it or copy it into logs.
3. Open **Local Mac** in the app, enter the shown address and app key, then tap
   **Check and connect**. Native uses port **20000**; Docker uses **21000**.
   The app key belongs to that runtime and is different from an ngrok authtoken.

Use [Tailscale](TAILSCALE.md) for address, permissions and saved-server details.
An [authenticated ngrok tunnel](NGROK.md) is optional; no ngrok account is needed
for Tailscale. Changing the server address does not require an iPhone rebuild.

## Record and browse

Connect the CV1 in the paired app. Local Omi starts automatically unless you
previously stopped or muted it. The CV1 button and app controls start or finish
a session; mute temporarily silences audio without finishing it.
If the phone microphone is selected, Bluetooth reconnect does not take it over.

The updated app retains received packets locally, seals parts at five minutes
or Stop, and retries uploads until the paired server acknowledges them. The
Local Mac card distinguishes phone pending audio from server receipt. A first
capture after app restart requires a successful authenticated server capability
check. See [delivery and recovery](RECORDING_DURABILITY.md) for limits; this does
not establish continuous BLE recording while iOS suspends or terminates the app.

Check received-audio counters on the phone, then finish a short recording and
open its WAV and transcript in the library. A connected device or green health
check alone does not confirm that audio reached storage.
See [connection diagnostics](CONNECTION_DIAGNOSTICS.md).

Run `./omiloc open` to browse. The native library is
[localhost:20001](http://127.0.0.1:20001/); Docker uses
[localhost:21001](http://127.0.0.1:21001/). The installed native `omiloc` command
also works from other directories. Do not open `index.html` directly.

Select a recording to play it or seek to transcript timestamps. Saved parts
appear while queued, processing or failed. Live text and final transcription
are separate stages; select their providers in [Settings](PROVIDERS.md).
On iPhone, use **Local Mac → Transcription · Diarization** to select prepared
local or remote profiles. `./omiloc providers` exposes the same registry through
profile JSON; add `--runtime docker` for the Docker instance.
For final-ASR migration, see [WhisperKit](WHISPERKIT_MIGRATION.md).

**Delete recording** removes audio, transcript and the linked conversation
after confirmation. Wait for processing to finish first. A shared transcript
remains until its last audio copy is deleted. Pull down Conversations on the
iPhone to refresh afterward.

## Daily commands and diagnostics

| Task | Native command; add `--runtime docker` after `omiloc` for Docker |
| --- | --- |
| Start the prepared server | `./omiloc up` |
| Observe services | `./omiloc status` |
| Check prerequisites, configured assets and runtime readiness | `./omiloc doctor` |
| Also transcribe synthetic speech | `./omiloc doctor --inference` |
| Follow private service logs | `./omiloc logs -f` |
| Stop while preserving data, models and keys | `./omiloc down` |

`doctor` fails when the runtime is stopped or unavailable. Its readiness checks
do not prove iPhone authentication, CV1 delivery or recording durability.
`--inference` tests synthetic final transcription without importing a recording;
it does not exercise live preview or a physical phone.

After a code/dependency update, finish recording and processing, run `down`,
`bootstrap`, then `up`. A server-only update needs no iPhone rebuild.
For iPhone prerequisites without building, use `./iphone.command --check`.
See [current limitations](TEMPORARY_DISABLED_FEATURES.md) before relying on
unattended recording or recovery after connection loss.
