<img src="web-local/assets/omiloc.png" alt="omiloc logo" width="96">

# omiloc

**A personal audio library on your Mac, controlled from your iPhone.**

Record audio from an Omi CV1, browse transcripts, and jump from a transcript
segment to its audio. The iPhone app sends audio through Tailscale or an authenticated ngrok
tunnel; recordings stay on your Mac. Processing uses local engines or explicitly
selected servers.

[Mac quick start](#quick-start) · [Docker quick start](#docker-on-cpu-or-nvidia-gpu) · [Development](#development) · [Documentation](#documentation) · [MIT license](LICENSE)

> **Status: local MVP.** Clean-machine installation and recording reliability
> during connection loss are not yet fully verified.

## Features

- **CV1 recording controls:** start and finish sessions with the device button;
  mute audio without ending the recording.
- **Audio library:** browse recordings, search by date, source, or transcript
  prefix, adjust playback speed, and seek to transcript timestamps.
- **Local transcription:** prepare local models for final transcripts and an
  experimental live preview.
- **Provider settings:** select independent Live STT, final transcription,
  diarization, and summary providers in the library. See [Providers](docs/PROVIDERS.md).
- **Mac-local storage:** open audio and transcript folders in Finder through the
  library's **Folders** section, currently labeled **Папки**.
- **Flutter development workflow:** run the iPhone app with hot reload using the
  included Debug launcher.

## Requirements

The table below describes the native Mac setup. The [Docker setup](#docker-on-cpu-or-nvidia-gpu)
requires Docker Engine/Desktop and Compose; Python, Java, Node, and Redis run
inside the containers. Installing the iPhone app still requires a Mac with Xcode.

| Component | Requirement |
| --- | --- |
| Mac | Apple Silicon, macOS 14 or later |
| Xcode | Swift 6.0 or later; an iOS SDK compatible with your iPhone |
| iPhone | The local Omi app, installed separately |
| Recording device | Omi CV1 |
| Transport | Tailscale on the server and iPhone; optional ngrok |
| iOS development | Flutter 3.47.4, CocoaPods, and an Apple account for signing |
| Initial setup | Internet access for dependencies and models; at least 4 GiB of free space for model preparation |

The Mac must remain awake while receiving recordings. A free Apple Personal Team
can be used for local signing; see [iPhone setup](docs/LOCAL_SETUP.md) for
provisioning and Developer Mode requirements.

## Quick start

### 1. Start the Mac services

```bash
git clone https://github.com/vquaron/omi-local.git omiloc
cd omiloc
./omiloc bootstrap
./omiloc up
```

You can also open `start.command` in Finder. The launcher runs the same
`bootstrap`, `up`, and `open` commands:

1. Installs missing Mac dependencies and checks prerequisites.
2. Prepares and verifies WhisperKit for final transcription and Parakeet for live
   preview. The first setup may take several minutes.
3. Discovers the connected Tailscale address and prepares the app key for your
   iPhone; existing ngrok settings remain supported.
4. Starts the services and opens the audio library.

Subsequent launches reuse saved settings and prepared models. Existing engine
choices and explicitly disabled transcription settings are preserved. After
startup finishes, the launcher exits and you can close its terminal window.

See [Getting started](docs/START.md) for the complete installation flow and
[Tailscale setup](docs/TAILSCALE.md) for private `.env` configuration.

### 2. Install and pair the iPhone app

`start.command` prepares the Mac services; install the local iPhone app separately
by following the [iPhone setup guide](docs/LOCAL_SETUP.md).

In the app, open **Local Mac** (**Локальный Mac**), enter the Tailscale IP or ngrok HTTPS address and app
key from your private `.env` or launcher, and check the connection. A bare Tailscale IP uses port 20000; Docker uses IP:21000. The app key is separate from
the ngrok authtoken; the authtoken stays on the Mac.

### 3. Record and browse

Connect your CV1 in the iPhone app to start recording automatically. Recording
resumes after a Bluetooth reconnect unless you explicitly stopped or muted it.
The CV1 button and app controls still start/stop recording. The server saves
five-minute audio parts for transcription while the input continues. Saved parts
remain visible in Conversations while queued, processing, or failed.

After setup, open the library from any directory in Terminal:

```bash
omiloc
```

The [audio library](http://127.0.0.1:20001/) is accessible only on your Mac. Select
a recording to play it, or click a transcript segment to seek to that moment.
New recordings and completed transcripts appear automatically.

The **Now** section, currently labeled **Сейчас**, shows live text, the audio
source, transcription status, and events. Select a recording in the left-hand
list to open its completed transcript and player. See
[Live preview](docs/LIVE_PREVIEW.md) for monitoring and log details.

Use `./start.command` whenever you want to receive new recordings. Use `omiloc`
to browse existing recordings. Do not open `web-local/index.html` directly.

## Docker on CPU or NVIDIA GPU

Install and start Docker Engine or Docker Desktop with Compose **2.32 or later**.
From a new checkout of `main`:

```bash
git clone --branch main https://github.com/vquaron/omi-local.git omiloc
cd omiloc
./omiloc --runtime docker bootstrap --device cpu --transport local
./omiloc --runtime docker up
```

If you already have this repository, run the Docker bootstrap and startup commands
from its root. Open the
[audio library](http://127.0.0.1:21001/). The first launch builds the images and
caches the default `faster-whisper-small` model; later launches reuse them.
`up` waits for readiness and leaves the services running in the background.

To launch directly with Docker Compose, without the CLI, run these commands
from the repository root (CPU on Mac or Linux):

```bash
docker compose -f compose.yaml build app stt download
docker compose -f compose.yaml run --rm --no-deps download
docker compose -f compose.yaml up -d --wait
```

The download step prepares the model before STT starts. For direct GPU startup,
hot reload, logs, and shutdown commands, see
[Using Docker Compose directly](docs/DOCKER.md#using-docker-compose-directly).

| Task | Command |
| --- | --- |
| Prepare or rebuild after updating the code | `./omiloc --runtime docker bootstrap` |
| Develop with Python reload and automatic web refresh | `./omiloc --runtime docker dev` |
| Check container status | `./omiloc --runtime docker status` |
| Follow service logs | `./omiloc --runtime docker logs -f` |
| Stop and keep recordings, settings, and models | `./omiloc --runtime docker down` |

Run `dev` in your own terminal and finish active recordings before backend edits.
When changing between `up` and `dev`, stop the stack with `./omiloc --runtime docker down` first.

Docker uses CPU on Mac. For Linux/WSL2 with a configured NVIDIA GPU runtime:

```bash
./omiloc --runtime docker bootstrap --device cuda --gpu 0 --transport tailscale
./omiloc --runtime docker up
```

Docker bootstrap requires an explicit `cpu` or `cuda` choice. CUDA execution
requires the NVIDIA prerequisites described in the Docker guide and is verified
inside the selected container; there is no silent CPU fallback.
WhisperKit/Core ML uses the [native Mac stack](#quick-start).

The library starts without ngrok. To receive recordings over Tailscale, use
`./omiloc --runtime docker bootstrap --transport tailscale` and pair to **IP:21000**. See
[Docker pairing](docs/DOCKER.md#iphone-and-tailscale); ngrok remains optional. Docker has its own
app key and data volumes; native Mac recordings and settings are not imported.
For upgrades, GPU prerequisites, model selection, and troubleshooting, see the
[Docker guide](docs/DOCKER.md).

## Development

### iPhone launcher

```bash
./iphone.command          # choose Debug, Profile, or status
./iphone.command dev      # Omi Local Dev with hot reload
./iphone.command prod     # Omi Local Profile, launched from the app icon
./iphone.command status   # inspect the active session or build
```

`prod` is an alias for the local `profile` mode. Debug installs **Omi Local Dev**
with a `.dev` bundle suffix, alongside **Omi Local**. Each app has its own settings
and Keychain; pair the Dev app separately. Install them one at a time, ending the
previous Flutter session first. The launcher checks for existing Omi builds and
Flutter sessions across worktrees and refuses a duplicate or an unobservable launch.
Start Mac services separately with `./start.command`.

### Run Debug on an iPhone

Complete the [initial iOS setup](docs/LOCAL_SETUP.md), connect and unlock your
iPhone, enable Developer Mode, and open a terminal in the project directory.
Before a new iOS build, run:

```bash
make test-offline
make test-transport-app
```

Then start the services and app:

```bash
./start.command
./dev-iphone.command
```

If the Mac services are already running, only the second command is needed.
The Debug launcher uses the existing signing and connection settings. It checks
an existing build for reuse or builds again when its inputs change. If prompted,
allow local network access on the iPhone for the debugger.

Wait for **Flutter run key commands** and keep that terminal session open.
After saving Dart changes, press a key without Enter:

| Key | Action |
| --- | --- |
| `r` | Hot reload changes, preserving app state |
| `R` | Hot restart Dart and reset app state |
| `v` | Open Flutter DevTools |
| `h` | Show available commands |
| `q` | End the debug session and stop the app |

Saving a file alone does not apply changes. Swift, native-plugin, and build-setting
changes require restarting the build workflow. Update dependencies and generated
files as described in the iOS guide before rebuilding when needed.

Launch Debug through the script. Use a Profile build for everyday launches from
the app icon. To check Debug prerequisites without building or installing:

```bash
./dev-iphone.command --check
```

Run only one Flutter session for the app at a time. For interactive hot reload,
start the command in your own terminal after any previous session has ended.
See [Development](docs/DEVELOPMENT.md) for build verification and additional tests.

## Known limitations

- This standalone snapshot focuses on the local iPhone/CV1 workflow. Upstream
  cloud features and deployments are outside its scope.
- Storage is local; selected remote processing providers and ngrok audio transport
  require a network connection.
- Live preview is experimental. Final transcription runs after recording ends.
- The iPhone microphone is available through the app's **+** control. Switching
  inputs finishes the previous recording while preserving the Bluetooth connection.
- Autonomous recording without a Mac connection and later phone upload are not
  part of the supported local workflow.
- Recording durability across connection loss, long-session reliability, and
  clean-machine setup remain unverified.

See [Current limitations](docs/TEMPORARY_DISABLED_FEATURES.md) for details.

## Troubleshooting

Run these commands from the project directory:

| Problem | First step |
| --- | --- |
| Docker services are unavailable | `./omiloc --runtime docker status`, then `./omiloc --runtime docker logs` |
| Mac services or transcription are unavailable | `./omiloc doctor`, then `./omiloc logs` |
| iPhone tools, signing, or device readiness fail | `./iphone.command --check` |
| `omiloc` is not found | Run `./omiloc --install`, then open a new terminal |
| Setup was interrupted | Run `./start.command` again to resume preparation |
| The phone cannot connect | Follow [Connection diagnostics](docs/CONNECTION_DIAGNOSTICS.md) |
| You need to stop the Mac services | `./omiloc down` |

Setup output is saved in `.local/install.log`. Keep recordings, logs, app keys,
ngrok credentials, and signing files private.

## Documentation

The guides below are in English. Some interface labels and the synthetic Russian
speech fixture retain their original text.

| Guide | Topics |
| --- | --- |
| [Docker](docs/DOCKER.md) | CPU/GPU runtime, development reload, and container storage |
| [Getting started](docs/START.md) | Installation, daily use, and recovery |
| [iPhone setup](docs/LOCAL_SETUP.md) | Tools, signing, and app installation |
| [Tailscale](docs/TAILSCALE.md) | Direct IP pairing, native/Docker ports, and private configuration |
| [Ngrok](docs/NGROK.md) | Optional HTTPS tunnel and pairing |
| [Connection diagnostics](docs/CONNECTION_DIAGNOSTICS.md) | Investigating phone-to-Mac connectivity |
| [Transcription](docs/LOCAL_STT.md) | Preparing and running local engines |
| [Providers](docs/PROVIDERS.md) | Live STT, final transcription, diarization, and summaries |
| [Live preview](docs/LIVE_PREVIEW.md) | Experimental live text and diagnostics |
| [Development](docs/DEVELOPMENT.md) | Tests, builds, hot reload, and verification |
| [Current limitations](docs/TEMPORARY_DISABLED_FEATURES.md) | Supported features and verification limits |

## Contributing

Read [AGENTS.md](AGENTS.md) and the relevant component guide before changing code.
Use the standalone repository's commands, add regression tests for bug fixes,
and describe your verification in the pull request. Report reproducible problems
through [GitHub Issues](https://github.com/vquaron/omi-local/issues), with secrets
and personal recording content removed.

## License and acknowledgements

Licensed under the [MIT License](LICENSE). Based on
[Omi by Based Hardware](https://github.com/BasedHardware/omi).
See [UPSTREAM.md](UPSTREAM.md) for the snapshot's origin and retained notices.
