# Docker: Mac CPU and NVIDIA GPU

## Single-command setup

Run the CLI from the repository root. The Docker adapter is self-contained and
does not require Python, Node, Java or Redis on the host.

```bash
# CPU or Mac Docker Desktop
./omiloc --runtime docker bootstrap --device cpu --transport local
./omiloc --runtime docker up

# Linux server with one NVIDIA GPU and Tailscale
./omiloc --runtime docker bootstrap \
  --device cuda --gpu 0 --transport tailscale
./omiloc --runtime docker up
./omiloc --runtime docker doctor --inference
```

Bootstrap builds the images, prepares the model volume, validates the selected
GPU (CUDA mode), and writes `.env.docker`. It never starts the long-running
stack. `up` uses the prepared images and cached model with `--no-build` and
`--pull never`; if preparation is missing, it exits with the bootstrap command.
The same saved selection is used by `status`, `logs`, `doctor`, `pair`, and
`down`. `dev` starts Compose Watch in the foreground.

## `.env.docker`

`.env.docker` is a private, ignored file created by bootstrap. It is read as
literal `NAME=value` data, never sourced as shell code, and must be mode `0600`.
It is independent from the native `.env`; native ngrok tokens and app keys are
never imported into Docker.

Example:

```dotenv
OMI_DOCKER_DEVICE=cuda
OMI_DOCKER_GPU_ID=0
OMI_DOCKER_TRANSPORT=tailscale
STT_MODEL=Systran/faster-whisper-small
STT_REVISION=8f3f6b0d8d8b2b6d1a0d4c6e9b7a2f1c0d5e6f7a
STT_THREADS=4
```

Supported fields are `OMI_DOCKER_DEVICE` (`cpu` or `cuda`),
`OMI_DOCKER_GPU_ID` (numeric index or `GPU-...` UUID),
`OMI_DOCKER_TRANSPORT` (`local`, `tailscale`, or `ngrok`), `STT_MODEL`,
`STT_REVISION`, and `STT_THREADS`. Use the CLI bootstrap flags to create or
change it; do not edit it while services are running. The model revision written
by bootstrap is an immutable snapshot revision, so startup does not silently
resolve a moving `main` branch.

## Services outside the container

The Docker command can build and run the application, Firebase emulator, Redis,
web library, and STT worker. These host services still require separate setup:

1. Docker Engine/Desktop with Compose 2.32 or later.
2. For CUDA: a Linux NVIDIA driver and the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html), then a configured Docker runtime. Verify with `docker info` and a CUDA container before bootstrap.
3. For phone access through Tailscale: install and sign in to Tailscale on the
   Docker host and iPhone; allow TCP `21000` in the tailnet policy. Bootstrap
   validates the host address but does not install or authenticate Tailscale.
4. For remote administration: SSH access to the Docker host. Run all `omiloc`
   commands in the checkout on that host. A local library tunnel is separate:

```bash
ssh -N -L 21002:127.0.0.1:21001 USER@SERVER
```

Then open `http://127.0.0.1:21002`. The authenticated iPhone API remains on
the Tailscale address at port `21000`.

The iPhone app itself is always outside Docker: install it with Xcode using
`./iphone.command`. Docker cannot perform Apple signing, Bluetooth access, or
physical CV1 pairing.

## First launch

Install and start Docker Desktop on Mac or Docker Engine with Compose on Linux.
Check Docker availability from your own Terminal:

```bash
docker info
docker compose version
```

For a new checkout of the main branch:

```bash
git clone --branch main https://github.com/vquaron/omi-local.git omiloc
cd omiloc
./omiloc --runtime docker bootstrap --device cpu --transport local
./omiloc --runtime docker up
```

If you already have the repository, run the same bootstrap command from its root.
The first bootstrap needs internet access for images, dependencies, and the model.
It leaves the services stopped; `up` starts them in the background.

Open the **[audio library](http://127.0.0.1:21001/)**. Backend:
`http://127.0.0.1:21000`. Browsing the interface does not require ngrok;
for new iPhone recordings, use [Tailscale](#iphone-through-tailscale) or the
optional [ngrok tunnel](#iphone-and-ngrok).

| Action | Command from the project root |
| --- | --- |
| Prepare or rebuild after changing the runtime | `./omiloc --runtime docker bootstrap` |
| Receive iPhone audio through Tailscale | `./omiloc --runtime docker up` |
| Develop with automatic reload | `./omiloc --runtime docker dev` |
| Check container status | `./omiloc --runtime docker status` |
| Follow logs | `./omiloc --runtime docker logs -f` |
| Stop while preserving recordings, settings, and models | `./omiloc --runtime docker down` |

`up` uses CPU on Mac; CUDA selection is described below. Ports and data are
separate from native `start.command`. You do not need Python, Java, Node, or Redis
installed on the host. iPhone installation still uses Xcode on Mac; containers
do not build or install the iOS app.

## Using Docker Compose directly

These commands perform the same build, model preparation, and startup steps as
the CLI. Run them from the repository root. The stack has several services,
shared networking, and persistent volumes, so use Compose for direct Docker startup.

### CPU on Mac or Linux

```bash
docker compose -f compose.yaml build app stt download
docker compose -f compose.yaml run --rm --no-deps download
docker compose -f compose.yaml up -d --wait
```

The `download` service prepares the model cache and exits. Do not skip it on a
fresh installation or after selecting a new model/revision: the STT service
loads cached files only. Open the [audio library](http://127.0.0.1:21001/).

### NVIDIA GPU on Linux or WSL2

After installing the [GPU prerequisites](#requirements-and-gpu):

```bash
docker compose -f compose.yaml -f compose.gpu.yaml build app stt download
docker compose -f compose.yaml -f compose.gpu.yaml run --rm --no-deps download
docker compose -f compose.yaml -f compose.gpu.yaml up -d --wait
```

Direct Compose commands select CPU or GPU through the file list;
The CLI stores the selected device in `.env.docker`; keep that file with the
checkout and use the same CLI for later commands.
Keep the same file list for later commands. Export custom `STT_MODEL`,
`STT_REVISION`, or `STT_THREADS` values before running the sequence so they apply
to every step.

### Hot reload

First build and prepare the model with the CPU or GPU sequence above. If the
stack is already running, finish recording and processing, then stop it with
the matching `down` command below. Start Compose Watch in your terminal:

```bash
# CPU
docker compose -f compose.yaml -f compose.dev.yaml up --watch

# NVIDIA GPU (alternative to the CPU command)
docker compose -f compose.yaml -f compose.gpu.yaml -f compose.dev.yaml up --watch
```

Use one of these commands. `Ctrl+C` ends dev mode. Stop the stack before switching
between CPU/GPU or development/normal mode; then run the desired startup sequence.

### Status, logs, and shutdown

For the CPU stack:

```bash
docker compose -f compose.yaml --profile tunnel ps
docker compose -f compose.yaml logs --tail 80 -f
docker compose -f compose.yaml --profile tunnel down
```

For the GPU stack:

```bash
docker compose -f compose.yaml -f compose.gpu.yaml --profile tunnel ps
docker compose -f compose.yaml -f compose.gpu.yaml logs --tail 80 -f
docker compose -f compose.yaml -f compose.gpu.yaml --profile tunnel down
```

These `down` commands also stop the optional tunnel and preserve data/model
volumes. For updates, stop the stack, run `git pull --ff-only` from a clean
checkout of main, and repeat the matching build/download/start sequence.
Restart the tunnel afterward if configured.

For [iPhone pairing](#iphone-and-ngrok), the direct equivalents are:

```bash
docker compose -f compose.yaml exec app python docker/runtime.py configure
# After configuring, stop and start the stack, then start its tunnel:
docker compose -f compose.yaml --profile tunnel up -d tunnel
```

On GPU, add `-f compose.gpu.yaml` after `-f compose.yaml` to both commands.
Run `configure` interactively while `app` is running.

## Requirements and GPU

Use Docker Engine with Compose or Docker Desktop; `dev` requires Compose **2.32+**.
CI installs Compose **2.40.3** before validating configuration so it does not depend
on the runner's older version.
Linux containers on Mac use CPU, including on Apple Silicon. These containers do
not provide access to Metal, Core ML, or the Neural Engine. For WhisperKit on Mac,
use [native startup](START.md) and [engine setup](LOCAL_STT.md).

An RTX 4090 host needs Linux x86_64, an NVIDIA driver, and the
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html).
On Windows, use Docker Desktop with WSL2 and NVIDIA GPU support, and run commands
in WSL2. Do not install NVIDIA drivers on a Mac.

```bash
./omiloc --runtime docker bootstrap --device cuda --gpu 0
./omiloc --runtime docker up
./omiloc --runtime docker dev
```

The default `auto` mode selects CUDA if the Docker server reports the `nvidia`
runtime; otherwise it selects CPU. Set `OMI_DOCKER_DEVICE=cpu` to select CPU explicitly.
On WSL2, if the GPU is available but the runtime is not listed, select `cuda`
explicitly. The selected device is printed before startup.
A CUDA error stops STT; there is no silent CPU fallback. Check the actual device:

```bash
docker compose exec stt python3 -c "import json,urllib.request; print(json.load(urllib.request.urlopen('http://127.0.0.1:10301/health')))"
```

CPU uses int8 and 4 threads; CUDA uses float16, one GPU, and one job at a time.
The CUDA profile uses CUDA 12.3.2 + cuDNN 9, following the
[faster-whisper requirements](https://github.com/SYSTRAN/faster-whisper#gpu).
GPU availability and a health response do not prove successful transcription:
check a completed recording. No physical 4090 was available on this Mac for testing.

## Model and processing

The default is `Systran/faster-whisper-small`, a portable starting profile for
CPU and GPU. The server returns timestamped segments and words through the
existing OpenAI-compatible adapter. A separate preparation command downloads
the model; processing uses only downloaded files. STT listens on loopback inside
the container network and is not published on the host.

For a fresh installation, you can select another CTranslate2 model:

```bash
./omiloc --runtime docker bootstrap --device cuda --gpu 0 \
  --model mobiuslabsgmbh/faster-whisper-large-v3-turbo
./omiloc --runtime docker up
```

Use the same `STT_MODEL`, `STT_REVISION`, and `OMI_DOCKER_DEVICE` values in all later
commands for this stack; exporting them in your Terminal is convenient.
`STT_REVISION` selects the Hugging Face revision and defaults to `main`; specify a
commit hash for reproducible model selection. `STT_THREADS` sets the CPU thread count.
After initial setup, changing `STT_MODEL` does not overwrite settings or old jobs:
select the new model in the library's STT card and click **Use**.
When changing the revision under the same model name, update the card's
`runtime_revision`. Older jobs keep their profile and require the corresponding server model.

Automatic processing is enabled for new completed recordings. Recordings already
present when processing is first enabled are excluded. Configure Live STT,
diarization, and summarization separately under [Providers](PROVIDERS.md).
The built-in container provides final WAV transcription with one speaker,
`SPEAKER_00`. Apple engines are unavailable inside Linux; use compatible servers
for additional stages.

## Development and updates

`./omiloc --runtime docker dev` runs in an open Terminal:

- Compose Watch synchronizes the Python backend; Uvicorn reloads changes.
- HTML/JS/CSS come from the current `web-local`. The library refreshes the page or
  styles through its existing mechanism, preserving protection for unsaved edits.
- Harness changes restart the app container; STT-code changes restart only STT.
- Lockfile, STT dependency, and Dockerfile changes rebuild the corresponding image.

Python restarts interrupt active WebSocket connections. Finish recording before
changing backend/harness code. Settings, WAVs, the queue, and results remain in
the volume. Models load into memory again after STT restarts.
For Compose/nginx changes, stop dev mode and start it again.
`Ctrl+C` ends dev mode; `./omiloc --runtime docker down` stops the entire stack.

Normal `up` uses the code inside the image. When switching between `dev` and
normal mode, run `./omiloc --runtime docker down` first, then the desired startup command.
See [how Compose Watch works](https://docs.docker.com/compose/how-tos/file-watch/).

### Updating from main

Finish recording and wait for processing to complete. From a clean checkout of main:

```bash
./omiloc --runtime docker down
git pull --ff-only
./omiloc --runtime docker up
./omiloc --runtime docker status
```

The saved named volumes are attached again; recordings, keys, and models are
preserved. For Tailscale, select it in `.env.docker` with
`./omiloc --runtime docker bootstrap --transport tailscale` before startup.
If ngrok is configured, run `./omiloc --runtime docker tunnel` after startup.
Updating the Docker environment alone does not require a new iPhone build; the
phone must already have an app version that supports Tailscale addresses.
Do not use `down -v`, delete volumes, or perform Docker cleanup for routine updates.

## iPhone through Tailscale

Connect the Docker host and iPhone to the same Tailscale network. Its access rules
must allow the iPhone to reach TCP port **21000** on the host. Run from a local
Docker context on that host. With Docker Desktop on Windows, the CLI must inspect
the Windows host's Tailscale address, not a separate WSL2 guest VPN address;
that configuration remains unverified.

```bash
./omiloc --runtime docker bootstrap --transport tailscale
./omiloc --runtime docker up
```

The launcher detects the host's Tailscale IPv4 and verifies that Tailscale is
connected before building or starting services. No ngrok account, token, domain,
Tailscale Serve, or extra Python installation on the host is needed. Mac users
may use the Tailscale app's bundled CLI; elsewhere `tailscale` must be on PATH.
To use a particular installed CLI, set `OMI_TAILSCALE_CLI` to its executable path.
For development, use `./omiloc --runtime docker dev` after selecting Tailscale.

In the iPhone app's **Local Mac** screen, enter **the host Tailscale IPv4 followed
by `:21000`** and the Docker app key, then check the connection. The port is required:
entering only an IP defaults to the native Mac port `20000`. Retrieve the Docker
key locally in your own Terminal; do not copy it into chat or logs:

```bash
docker compose exec app cat /data/connection.env
```

The app uses HTTP/WS inside the encrypted Tailscale network. The library remains
at [localhost:21001](http://127.0.0.1:21001/); only the authenticated backend ingress
is additionally published on the Tailscale IPv4. Firebase, Redis, STT, and internal
backend sockets stay on container loopback. A binding failure stops startup;
the launcher never substitutes `0.0.0.0` or the LAN address.

You can also choose the transport through exported configuration:

```bash
export OMI_LOCAL_TRANSPORT=tailscale
# Optional: set OMI_TAILSCALE_IP to this host's actual Tailscale IPv4.
# If omitted, the launcher discovers and validates it on each startup.
./omiloc --runtime docker up
```

These are Docker host settings; the native `.env` is not imported into the Docker
volume. New Tailscale volumes require only a generated app key. Existing keys,
ngrok configuration, provider settings, and recordings are preserved. To switch
an existing running stack, finish recording/processing, run `./omiloc --runtime docker down`,
then start the selected transport. Stop/status/logs continue to work even when
Tailscale is disconnected. Tailscale startup also stops this Compose project's
optional ngrok tunnel if it was left running. Switching back to ngrok is explicit: unset the exported
transport (or set it to `ngrok`), then follow the ngrok section below.

For direct Compose use, verify that `tailscale status` reports a connected host,
then set its own IPv4 and include the override in every build/start command:

```bash
export OMI_TAILSCALE_IP="$(tailscale ip -4)"
docker compose -f compose.yaml -f compose.tailscale.yaml build app stt download
docker compose -f compose.yaml -f compose.tailscale.yaml run --rm --no-deps download
docker compose -f compose.yaml -f compose.tailscale.yaml up -d --wait
```

Use `./omiloc --runtime docker bootstrap --transport tailscale` for automatic
address and connection validation.
The same override combines with `compose.gpu.yaml` and `compose.dev.yaml`.
This does not connect Omi Bluetooth directly to the host: audio still comes from
CV1 → iPhone → Tailscale → Docker host. Physical phone/CV1 recording and access
from another tailnet device still need verification on the target devices.

## iPhone and ngrok

Start `./omiloc --runtime docker up` first: `configure` runs inside an already running container.
To receive iPhone audio, configure a separate domain/tunnel in your own interactive Terminal:

```bash
./omiloc --runtime docker configure   # domain and hidden token prompt; preserve the app key
./omiloc --runtime docker down
./omiloc --runtime docker up
./omiloc --runtime docker tunnel
```

Settings are saved in the private volume. `configure` does not change a running
tunnel. The connection key is in `connection.env` inside the volume; view it
locally in your own Terminal, and do not forward it to chat or logs:

```bash
docker compose exec app cat /data/connection.env
```

On the iPhone, enter the selected HTTPS domain and `OMI_LOCAL_APP_KEY` as described
in [connection setup](NGROK.md). Docker generates a separate key; it does not
import native Mac settings automatically. Do not run two tunnels on one domain.
Pairing the iPhone with Docker does not transfer existing Mac recordings.
The tunnel targets only the paired backend, not the library or Firebase.

## Data, network, and diagnostics

Compose uses two persistent named volumes: `omiloc-docker_state` and
`omiloc-docker_models`. The first stores pairing, providers, WAVs, transcripts,
the queue, private logs, and Firebase exports. The second stores models.
`down` preserves both; **do not use `down -v` for routine updates**.
For backups, stop the stack first, then copy both volumes while preserving owner
UID 10001 and permissions. Keep a stable Compose project name and the same
container paths when moving to another host. Mac `.local` directories, `.env`,
Keychain data, signing material, and real recordings are not copied into images.

`app` runs the existing harness, Firebase, and Redis in one process environment.
This preserves ownership checks and Firebase's correct export-on-exit behavior.
`stt`, `ingress`, and the optional `tunnel` share Nginx's network namespace,
which survives app and STT reloads. The internal backend, Firebase, Redis, and STT
listen on 127.0.0.1. Nginx normally publishes two ports on host 127.0.0.1.
The Tailscale override adds only the backend ingress on the verified host VPN IPv4;
the unauthenticated library stays loopback-only and ingress rejects local preview.
The backend's outbound policy is unchanged; remote providers are accessed through
the harness and its Live relay. This does not establish physical no-egress for the whole system.

The Finder button is unavailable in containers; data resides in named volumes.
For a remote library, use SSH forwarding with the same port 21001 and open
`http://127.0.0.1:21001`; Host/Origin are intentionally not rewritten.
Do not expose the unauthenticated UI to the LAN or public internet.

On errors, start with `./omiloc --runtime docker status` and
`./omiloc --runtime docker logs`.
Detailed harness logs are private and stored at `/data/harness/docker/logs/`
inside `app`. Checks:

```bash
make test-docker PYTHON=/path/to/python3.11
docker compose -f compose.yaml -f compose.gpu.yaml -f compose.dev.yaml config --quiet
```

`make test-docker` checks the API, model-error failure behavior, settings
preservation, transport switching, and host-address validation without a GPU or
model downloads. With the Compose CLI installed, it also renders the combined
Tailscale/GPU/dev configuration and checks all published ports. It runs in the existing
`transport-unit` CI job. Backend versions come from `pylock.runtime.toml`;
ARM64 uses native wheels of the same versions, so x86_64 artifact hashes are not
presented as an ARM64 lock.

Recorded verification on September 11, 2026, on this Mac using Linux ARM64 in
Docker Desktop: CPU image builds, timestamped synthetic-speech STT, and the path
Opus → WebSocket → WAV → automatic STT → import → browser transcript display.
Python reload was checked by changing an actual HTTP response; CSS reload was
checked by changing the served file. The recording and transcript survived
recreating `app`, and a Firebase export was found.
`make test-transport-unit` and `make test-offline` passed, with 3 offline skips.
NVIDIA/4090, WSL2, a new ngrok tunnel, and physical iPhone/CV1 use in Docker mode
remain unverified. Their configuration is prepared; hardware success is not claimed.

Native Mac Parakeet Live is not included in the container: live preview is disabled
by default. Saved settings and explicitly selected Live providers are preserved.

On September 14, 2026, a separate Docker Desktop CPU stack was tested through the
host's own Tailscale IPv4. Authenticated HTTP and an Opus WebSocket stream produced
an exact 2-second, mono 16 kHz WAV with zero decode errors; the recording appeared
in the localhost library and survived an app restart with its pairing key.
Incorrect keys and external preview requests were rejected, and the library was
unreachable through the Tailscale address. Graceful shutdown exported Firebase.
No models were downloaded; STT was disabled for this transport check. This proves
host binding and the synthetic audio path, not connectivity from a physical iPhone
or another tailnet peer. Existing running services and data were left unchanged.

### Recording without live text

The default Docker STT service transcribes completed WAVs after Stop. Live STT is
disabled unless a live provider is configured. Disabled live processing is not a
recording error. Open the library on port 21001, select a recording, and use the
player at the bottom; recordings with no detected speech still have playable audio.
