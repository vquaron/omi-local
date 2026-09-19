# Development

For containers on Mac CPU or Linux/WSL2 NVIDIA, see [Docker](DOCKER.md).

[Daily startup](START.md) · [iPhone builds](LOCAL_SETUP.md).
Run commands from the repository root. Do not commit real recordings, `.local/`,
keys, certificates, or build artifacts.

## Project and checks

`app/` contains the app, `backend/` receives and stores audio,
`scripts/dev-harness/` manages local services, and `web-local/` contains the library and logo.

- `make test-library`: library and startup checks.
- `make test-transport-unit`: isolated transport, live-adapter, and local-service
  checks using the small dependency set used in CI.
- `make test-offline`: local backend and services with full backend dependencies;
  includes importing the actual application and checking the offline route allowlist.
- `make test-transport-app`: app tests and analyzer; uses Flutter 3.47.4, matching CI.

The canonical Flutter version is `environment.flutter` in `app/pubspec.yaml`
(currently 3.47.4); CI and the iPhone preflight read it directly. From `app/`, run
`flutter pub get --enforce-lockfile` to verify the SDK and locked packages agree.
When upgrading Flutter, update that version and the lock together and rerun the app checks.

To reproduce transport-unit CI, use a separate Python 3.11 environment:
`python -m pip install -r .github/requirements-transport.txt`, then
`make test-transport-unit PYTHON="$(command -v python)"`.
The dependencies include NumPy for PCM conversion in the diarization test;
torch and models are mocked there. The authentication test uses the real router
and key verification while isolating unrelated services; `test-offline` separately
checks a full backend import. Secret scanning covers files and all available
history. A verified synthetic fixture may be exempted only by exact value plus
exact path. `python3 scripts/test-secret-scan.py /path/to/gitleaks` checks that
boundary with real Gitleaks 8.24.2 and runs in the same CI job.

Run the last two suites before an iOS build. Verify physical recording separately:
start and stop a CV1 recording, switch to the iPhone microphone while CV1 is
connected, stop, and start Omi again. Check both recordings in the library.
The Omi button switches the source back; Bluetooth reconnection and returning to
the home screen do not change the source by themselves.

The installer uses Homebrew. Python 3.11.15 and dependencies are pinned in
`backend/.python-version` and `backend/pylock.macos.toml`; Firebase CLI is pinned
in `package-lock.json`. Prepare Flutter, Xcode, and CocoaPods using the
[iPhone guide](LOCAL_SETUP.md). `start.command --iphone-check` checks tools,
signing, and the phone without building. WhisperKit has
`scripts/install-local-whisperkit.sh`; engine selection is documented in [LOCAL_STT.md](LOCAL_STT.md).
The Java check requires version 21 or later; the iOS check rejects a broken CocoaPods installation.
Setup output is saved in `.local/install.log`, readable only by its owner and
excluded from Git. Homebrew prompts remain visible in Terminal; key input is not
logged. Homebrew paths are detected automatically. To use a separate Firebase
cache, set `FIREBASE_EMULATORS_PATH` before installation and startup.
Installation is complete only after all checks pass and `.local/install.ready`
is written. Changed inputs or interrupted preparation trigger a retry on the next start.

Backend: `127.0.0.1:20000`; library: `127.0.0.1:20001`;
ngrok diagnostics: `127.0.0.1:16040`. Emulators and the library are not exposed
through the tunnel. The backend requires the app key except for `/v1/health`;
its verification hash is stored on the Mac. Deletion is available through the
local library, without a new public route.

## Automatic web library reload

While visible, `http://127.0.0.1:20001/` checks web files for changes every second.
Saving `web-local/style.css` replaces styles without resetting the page or player;
HTML or JavaScript changes reload the page automatically. Recording text and
identifiers are not saved for restoration after reload. During recording deletion,
reload waits for the server response.

The `/api/assets` version check reads only page assets. Python changes require
restarting the corresponding service separately. Web-file reload does not restart
the backend, STT, or iPhone app.

## iPhone builds

Before installation, verify the commit, configuration, executable SHA-256,
signature, and entitlements. The exact path is
`app/build/ios/Profile-dev-iphoneos/Runner.app`; the copy at
`app/build/ios/iphoneos/Runner.app` may belong to another build.
A fresh checkout does not contain a built app.

Reuse an attested artifact when source, dependencies, tools, and valid signing
inputs are unchanged. Changing servers or the ngrok address, or a USB failure,
does not require rebuilding. An expired provisioning profile must be renewed.
Use a clean build only after a relevant signing, native dependency, or toolchain
change, or a confirmed problem with intermediate files.

## Unified iPhone launcher

`./iphone.command` offers Debug, Profile, and status checks.
For direct launch, use `./iphone.command dev` or `./iphone.command profile`
(`prod` means local Profile). `--check` checks readiness for the selected mode;
`debug --build-only` builds without installing.

The two modes install side by side and update independently:

| Command | iPhone app | Identifier |
|---|---|---|
| `./iphone.command prod` | Omi Local — Profile, launched from its icon | Existing local bundle ID |
| `./iphone.command dev` | Omi Local Dev — Debug, hot reload | Same ID with a `.dev` suffix |

To install both, run the commands one at a time and end the first Flutter session
before starting the second (`d` detaches Flutter while leaving the app running).
Here, `prod` means the daily local build; both apps use the offline profile.
Data and Keychain entries for the previous bundle ID remain with Omi Local.
Omi Local Dev has separate settings: pair the Mac and grant permissions on first
launch. Signing uses the same Apple team.

The launcher holds a user-wide lock until its child process exits. It also checks
for older Flutter run/attach/build sessions, Xcode, and Omi installers using
processes and project directories. If a session already exists, the command
reports it and exits without launching. Failed observation is not treated as an
idle system. Do not delete the lock file manually; the lock releases when the process exits.

`status` checks sessions and builds on the Mac. A standalone Profile app on the
phone after Flutter exits is not a Mac session; the next installation updates the
app with the same bundle ID. The guard does not control external Flutter/Xcode
commands started manually after its check. For repeated launches, use
`iphone.command` or `dev-iphone.command`, not a parallel direct `setup.sh`.
The existing `test_ios_setup.py` in `make test-offline` covers these checks.

## Debug on iPhone and hot reload

For a prepared Mac with iOS dependencies installed:

```bash
./start.command
./dev-iphone.command
```

Connect one iPhone, unlock it, confirm trust, and enable Developer Mode.
On the first Debug launch, allow local-network access so Flutter can communicate
with the phone. USB is preferred over Wi-Fi.
Run `make test-offline` and `make test-transport-app` before launching.
Use the [standard iOS setup](LOCAL_SETUP.md) for initial preparation.

`dev-iphone.command` uses a separate local Debug bundle and the existing signing
team; it does not embed `.env` in the app. After readiness checks, it adds the
identifier and display-name settings only for `Debug-dev` to ignored
`PersonalTeam.xcconfig`; the base bundle ID remains unchanged. `--check` writes
nothing. The launcher checks tools and the phone, builds
`Debug-dev / local_dev / offline`, verifies the signature, entitlements,
provisioning profile, and SHA-256, then passes the **exact**
`app/build/ios/Debug-dev-iphoneos/Runner.app` to Flutter for installation and
launch with the debugger. Matching inputs and a fresh signature check allow
artifact reuse; attestation is stored in ignored `.local/ios-debug.json`.
The CocoaPods version comes from `app/ios/Podfile.lock`; the script does not install gems.

Keep this Terminal open. After saving a Dart file:

- `r`: hot reload UI/code while preserving state.
- `R`: hot restart Dart `main`/`initState`, resetting state.
- `v`: open Flutter DevTools in the browser; `h`: show commands.
- `q`: end the Flutter session.

Keys work without Enter after **Flutter run key commands** appears.
Saving alone does not apply changes; press `r`.

Native Swift and build-setting changes require ending the session and running the
script again. For dependency or generator changes, update dependencies and
generated files following the iOS guide before launching Debug: this script
expects a prepared project and does not run `pub get`/`build_runner`.
Changing the server URL/key does not require rebuilding.
Hot reload changes running code after the original bundle was attested;
the SHA-256 is not a fingerprint of post-reload state.

Additional modes:

```bash
./dev-iphone.command --check       # readiness only, without build/install
./dev-iphone.command --build-only  # build and verify, without installing
```

Relaunch Debug through the script, not the app icon: modern iOS requires a debugger
for JIT. Use a normal Profile build for standalone daily launches.
Debug does not automatically enable ASR. An agent-started session may not be
visible in your Terminal; for your own `r/R/q` controls, run the command in your
own Terminal window after the earlier session ends.

[Flutter: iOS debugging](https://docs.flutter.dev/platform-integration/ios/ios-debugging) ·
[Hot reload](https://docs.flutter.dev/tools/hot-reload).

## Transcription models

WhisperKit 1.1.0 is built from Argmax commit
`1e2a163736dfa5a198e637ae44c114e1c6d5cc2d`.
Swift Argument Parser 1.7.0 is pinned by upstream `Package.resolved`.
The [recipe](../scripts/dev-harness/whisperkit-models.json) contains revisions and
SHA-256 values for the archive, Core ML large-v3-v20240930_626MB, tokenizer, and
speech fixture. Runtime files live in `.local/whisperkit/`; `ready.json` is written
after offline inference. The result key includes the model, language, parameters,
binary, and adapter version. The worker retains word timestamps and their
probability as a score; a network sandbox is mandatory.
The installer does not switch the active engine or install Python ML dependencies.

WhisperX 3.8.6/CPU/float32/batch 1 uses a separate Python 3.12.14.
[Dependencies with checksums](../scripts/dev-harness/requirements-whisperx-macos.txt),
[WhisperX patches](../scripts/dev-harness/whisperx-local.patch), and
[model versions](../scripts/dev-harness/whisperx-models.json) are stored in the
repository. The installer places them in `.local/stt/`, checks recognition, and
publishes `ready.json` only after success. To check file completeness:
`bash scripts/install-local-stt.sh --check`.
Changing the environment version changes the transcript cache key while preserving
older results. The queue retains the selected model and records the environment
version actually used on retry.

Parakeet-MLX 0.5.2/GPU/FP32 and pyannote.audio 4.0.7 were also tested.
Parakeet used Python 3.14.6, Apple Silicon, and the
[pinned dependencies](../scripts/dev-harness/requirements-parakeet-mlx-macos.txt).

Example `stt-engine.json` for Parakeet:

```json
{
  "engine": "parakeet-mlx",
  "model": "mlx-community/parakeet-tdt-0.6b-v3",
  "language": "auto",
  "device": "gpu",
  "compute_type": "float32",
  "diarization_model": "none"
}
```

WhisperX and Parakeet support `python` and `library_path`.
FFmpeg 7 is installed automatically for standard WhisperX. Its ASR, alignment,
and NLTK assets are pinned and loaded from local paths.
Manual environments honor `HF_HOME`, `HF_HUB_CACHE`, `TORCH_HOME`, and `NLTK_DATA`;
prepare their models separately, including pyannote for speaker separation.
`start.command --check` checks Mac services and the configured transcription
readiness; the individual model installers' `--check` modes check prepared assets.

Queue and results: `.local/dev-harness/ngrok/services/local-transcripts/`.
Failures receive up to three attempts with delays of 60 and 120 seconds; further
retries are manual. If emulator state is lost, the conversation is restored from
saved JSON. After changing adapter code, wait for processing to finish and
restart it with `auto-transcribe-off` / `auto-transcribe-on`.
