# Starting omiloc

## One command interface

Use the same CLI for the native Mac runtime and the Docker runtime. The runtime
defaults to native on macOS and Docker on Linux; pass `--runtime` when the
default is not appropriate.

```bash
./omiloc --help

# Apple Silicon Mac
./omiloc bootstrap
./omiloc up
./omiloc open

# Linux/NVIDIA server, run from the checkout on that server
./omiloc --runtime docker bootstrap --device cuda --gpu 0 --transport tailscale
./omiloc --runtime docker up
./omiloc --runtime docker doctor --inference
./omiloc --runtime docker pair
```

`bootstrap` installs or builds dependencies and prepares models. It leaves
services stopped. `up` only starts an already prepared runtime; it does not
install packages, rebuild images or download models. Use `down`, `status`,
`logs`, and `doctor` for lifecycle operations. Running `./omiloc` with no
command opens the existing library.

`start.command` is a Finder convenience wrapper for the native sequence
`bootstrap → up → open`. It is not a second lifecycle implementation.

The iPhone app is separate because it requires Xcode and Apple signing:

```bash
./iphone.command --help
./iphone.command debug
```

New native setups use [Tailscale](TAILSCALE.md). Existing ngrok settings remain
supported; the ngrok-specific steps below apply when that transport is selected.

For containers on Mac CPU or Linux/WSL2 NVIDIA, see [Docker](DOCKER.md).

You need an Apple Silicon Mac, macOS 14 or later, Xcode with Swift 6.0 or later,
and an [ngrok account](https://dashboard.ngrok.com).
Recording requires a CV1 and the [iPhone app](LOCAL_SETUP.md).
The first installation needs internet access.

## Installation

Get the project and start setup in Terminal:

```bash
git clone https://github.com/vquaron/omi-local.git omiloc
cd omiloc
./omiloc bootstrap
./omiloc up
./omiloc open
```

If macOS offers to install developer tools for Git, complete that installation
and repeat the command. If you already have the project, start with `./omiloc bootstrap`.

## First launch

For setup without prompts, use a private [`.env`](NGROK.md#noninteractive-setup-env).
`bash scripts/local-mac.sh init-env` creates it without printing secrets.

1. Run `./omiloc bootstrap` in Terminal from the project directory. You may also
   double-click `start.command`, which runs bootstrap, startup and open in order.
2. The script installs and checks missing dependencies. If a check fails, it
   explains what to fix; Enter retries, and `q` exits setup.
   It then prepares WhisperKit for final transcripts and Parakeet for live text:
   it compiles workers, downloads pinned models, and checks them on synthetic speech.
   You need at least 4 GiB of free space; initial preparation may take several minutes.
   If model preparation stops, fix the reported cause and run `./omiloc bootstrap` again.
   Then enter the HTTPS address and authtoken from your ngrok dashboard.
   The authtoken input is hidden; press Enter if it is already configured.
   You can also supply both values in a [private `.env`](NGROK.md#supplying-the-address-and-token-through-env).
3. In the Omi iPhone app, open **Local Mac**, enter the **domain and key shown in
   the boxed output**, and check the connection.

The app key is different from the ngrok authtoken and is shown once: save it.
This applies to the interactive wizard; with `.env`, the key is stored in that file.
Subsequent launches reuse the saved settings. The final boxed output shows the
`omiloc` command and library address; you can close Terminal after startup.
Repeated startup preserves the running library, tunnel, and local STT services.
Install the iPhone app separately. A fresh setup enables both transcription paths;
existing engine choices and explicit disabled settings are preserved.
See [final transcription](LOCAL_STT.md) and [live preview](LIVE_PREVIEW.md).

## Usage

Run `./omiloc up` to receive recordings. Connect the CV1 in the app, press the CV1 button once
to start recording, and press it again to finish. Mute temporarily silences audio
without finishing the file. The Mac must remain running and awake.

To browse your archive, run **`omiloc`** from any directory in Terminal.
It opens the [audio library](http://127.0.0.1:20001/), accessible only on this Mac.
Do not open `index.html` directly.

Select a recording, start the player, or click a phrase to seek to its timestamp.
You can change playback speed and search by date, source, or transcript prefix.
New recordings and completed transcripts appear automatically.

**Delete recording** removes the audio, transcript, and linked conversation after
confirmation. Start the services with `start.command` and wait for transcription
to finish before deleting. For duplicate audio, the shared transcript remains
until the last copy is deleted. Then return to the iPhone conversation list and
pull down to refresh. If the last deleted recording remains, update the app.

## Troubleshooting

Mac setup output is saved in `.local/install.log`.
Run the following commands from the project directory.

- `omiloc` is not found: run `./omiloc --install`, then open a new Terminal window.
- `start.command` does not open: run `./omiloc bootstrap`, then `./omiloc up`.
- Setup was interrupted: run `./omiloc bootstrap` again.
  To repair dependencies manually: `bash scripts/install-local-mac.sh`.
- Check native prerequisites: `./omiloc doctor`.
- Check synthetic transcription: `./omiloc doctor --inference`.
- Check tools, signing, and the phone: `./iphone.command --check`.
- Stop services while preserving data: `./omiloc down`.
- Replace a lost key: after stopping, run `bash scripts/local-mac.sh rotate-key`,
  run `./omiloc bootstrap`, and enter the new key on the iPhone.
- The phone cannot connect: see [connection setup](NGROK.md).
- The phone connects but text is unavailable: check `./omiloc status` and
  `./omiloc logs`. Models run on the native Mac runtime.
- An update reports that the backend uses earlier settings: finish recording and
  processing, run `./omiloc down`, then `./omiloc bootstrap` and `./omiloc up`.

After restarting the Mac, run `./omiloc up` to record or `./omiloc open` to listen.
See [current limitations](TEMPORARY_DISABLED_FEATURES.md).
