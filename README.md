<img src="web-local/assets/omiloc.png" alt="omiloc logo" width="96">

# omiloc

**A personal audio library on your Mac, controlled from your iPhone.**

Record audio from an Omi CV1, browse transcripts, and jump from a transcript
segment to its audio. The iPhone app sends audio through an authenticated ngrok
tunnel; recordings and speech recognition stay on your Mac.

[Quick start](#quick-start) · [Development](#development) · [Documentation](#documentation) · [MIT license](LICENSE)

> **Status: local MVP.** Clean-machine installation and recording reliability
> during connection loss are not yet fully verified.

## Features

- **CV1 recording controls:** start and finish sessions with the device button;
  mute audio without ending the recording.
- **Audio library:** browse recordings, search by date, source, or transcript
  prefix, adjust playback speed, and seek to transcript timestamps.
- **Local transcription:** prepare local models for final transcripts and an
  experimental live preview.
- **Mac-local storage:** open audio and transcript folders in Finder through the
  library's **Folders** section, currently labeled **Папки**.
- **Flutter development workflow:** run the iPhone app with hot reload using the
  included Debug launcher.

## Requirements

| Component | Requirement |
| --- | --- |
| Mac | Apple Silicon, macOS 14 or later |
| Xcode | Swift 6.0 or later; an iOS SDK compatible with your iPhone |
| iPhone | The local Omi app, installed separately |
| Recording device | Omi CV1 |
| Transport | An [ngrok account](https://dashboard.ngrok.com) and internet access |
| iOS development | Flutter 3.44.5 or later, CocoaPods, and an Apple account for signing |
| Initial setup | Internet access for dependencies and models; at least 4 GiB of free space for model preparation |

The Mac must remain awake while receiving recordings. A free Apple Personal Team
can be used for local signing; see [iPhone setup](docs/LOCAL_SETUP.md) for
provisioning and Developer Mode requirements.

## Quick start

### 1. Start the Mac services

```bash
git clone https://github.com/vquaron/omi-local.git omiloc
cd omiloc
./start.command
```

You can also open `start.command` in Finder. The launcher:

1. Installs missing Mac dependencies and checks prerequisites.
2. Prepares and verifies WhisperKit for final transcription and Parakeet for live
   preview. The first setup may take several minutes.
3. Guides you through ngrok configuration and shows the address and app key for
   your iPhone.
4. Starts the services and opens the audio library.

Subsequent launches reuse saved settings and prepared models. Existing engine
choices and explicitly disabled transcription settings are preserved. After
startup finishes, the launcher exits and you can close its terminal window.

See [Getting started](docs/START.md) for the complete installation flow and
[Connection setup](docs/NGROK.md) for private `.env` configuration.

### 2. Install and pair the iPhone app

`start.command` prepares the Mac services; install the local iPhone app separately
by following the [iPhone setup guide](docs/LOCAL_SETUP.md).

In the app, open **Local Mac** (**Локальный Mac**), enter the HTTPS address and app
key shown by the launcher, and check the connection. The app key is separate from
the ngrok authtoken; the authtoken stays on the Mac.

### 3. Record and browse

Connect your CV1 in the iPhone app. Press the CV1 button once to start a recording
and again to finish it. Muting temporarily pauses audio within the same session.

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

## Development

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
- Storage and transcription are local, but audio transport uses ngrok and
  requires a network connection.
- Live preview is experimental. Final transcription runs after recording ends.
- The iPhone microphone is available through the app's **+** control, but the
  current `main` version requires disconnecting Omi before starting it.
- Autonomous recording without a Mac connection and later phone upload are not
  part of the supported local workflow.
- Recording durability across connection loss, long-session reliability, and
  clean-machine setup remain unverified.

See [Current limitations](docs/TEMPORARY_DISABLED_FEATURES.md) for details.

## Troubleshooting

Run these commands from the project directory:

| Problem | First step |
| --- | --- |
| Mac services or transcription are unavailable | `./start.command --check` |
| iPhone tools, signing, or device readiness fail | `./start.command --iphone-check` |
| `omiloc` is not found | Run `./omiloc --install`, then open a new terminal |
| Setup was interrupted | Run `./start.command` again to resume preparation |
| The phone cannot connect | Follow [Connection diagnostics](docs/CONNECTION_DIAGNOSTICS.md) |
| You need to stop the Mac services | `bash scripts/local-mac.sh down` |

Setup output is saved in `.local/install.log`. Keep recordings, logs, app keys,
ngrok credentials, and signing files private.

## Documentation

The detailed guides are currently written in Russian.

| Guide | Topics |
| --- | --- |
| [Getting started](docs/START.md) | Installation, daily use, and recovery |
| [iPhone setup](docs/LOCAL_SETUP.md) | Tools, signing, and app installation |
| [Connection setup](docs/NGROK.md) | ngrok, pairing, and private configuration |
| [Connection diagnostics](docs/CONNECTION_DIAGNOSTICS.md) | Investigating phone-to-Mac connectivity |
| [Transcription](docs/LOCAL_STT.md) | Preparing and running local engines |
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
