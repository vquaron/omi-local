# The iPhone app

For containers on Mac CPU or Linux/WSL2 NVIDIA, see [Docker](DOCKER.md).

Recording requires a separately installed local Omi app.
`start.command` prepares Mac services but does not install the phone app.
After installation, connect it using [Tailscale](TAILSCALE.md) or the optional [ngrok guide](NGROK.md).

## Preparing your Mac

You need an Apple Silicon Mac, Xcode, Flutter, CocoaPods, and your own Apple Account.
There is no restriction to a particular iPhone model or M-series generation.
The deployment target is iOS 15.0; this alone does not establish physical
compatibility on every supported OS. Xcode must support the phone's iOS version
and run on your macOS version. On iOS 17+, the local build adds the narrow
Tailscale IP range exception required by [Apple's ATS policy](https://developer.apple.com/documentation/bundleresources/information-property-list/nsapptransportsecurity/nsallowslocalnetworking).

The iOS app uses the [UIScene lifecycle](https://docs.flutter.dev/release/breaking-changes/uiscenedelegate).
Native channels are registered when the Flutter engine is ready; scene callbacks
handle links and Bluetooth background/foreground transitions.

1. Install Xcode from the App Store, open it, accept the license, and wait for the
   iOS components to finish installing. Under **Settings → Locations → Command Line
   Tools**, select the installed Xcode.
2. The launcher automatically uses an existing SDK at `.local/toolchains/flutter`
   in this checkout; otherwise it uses Flutter from PATH. Install [Flutter for iOS](https://docs.flutter.dev/platform-integration/ios/setup)
   and add it to PATH as documented. Use version 3.47.4 to match the app dependency lock and CI checks.
3. After `start.command` prepares Homebrew, install
   [CocoaPods](https://formulae.brew.sh/formula/cocoapods): `brew install cocoapods`.
   Run `flutter doctor -v`: the Xcode section must have no errors.
   Android tooling is not required for this setup.

## Signing and the phone

A free **Personal Team** is sufficient to install the app on your own iPhone.
Its provisioning profile lasts 7 days, after which you must sign and install the
app again. This is an [Apple limitation](https://developer.apple.com/help/account/basics/about-your-developer-account).

1. In Xcode, open **Settings → Accounts**, add your Apple Account, and select your
   Personal Team.
2. Open **Manage Certificates → + → Apple Development**. Xcode creates the
   certificate and private key on this Mac; no manual CSR is required.
   If you already have a certificate with a working private key, do not create
   another. See [Apple's guide](https://developer.apple.com/documentation/Xcode/sharing-your-teams-signing-certificates).
3. In Keychain Access, open **My Certificates → Apple Development**.
   A private key must appear beneath the certificate. The Team ID is the
   10-character value under **Subject Name → Organizational Unit (OU)** in the
   certificate properties. The number in parentheses in its name may differ;
   [Apple explains the distinction](https://developer.apple.com/forums/thread/811970).
4. Connect the unlocked iPhone with a USB data cable and confirm trust on the Mac.
   In Xcode 27, open **Xcode → Open Developer Tool → Device Hub**
   ([Apple's guide](https://developer.apple.com/documentation/xcode/running-your-app-on-simulated-or-physical-devices)). In older
   versions, use **Window → Devices and Simulators**. Wait for the phone to
   become ready. On the iPhone, enable **Settings → Privacy & Security → Developer
   Mode**, restart the phone, and confirm the setting.

Keep the phone unlocked until device preparation and installation finish.
If Device Hub remains at **Preparing for development…** or installation times
out, reconnect the USB data cable and recheck device readiness. Retain the
attested app; a device-connection failure alone does not require rebuilding it
or clearing build caches.

## Installation

[Download the project and prepare Mac services](START.md#installation).
From the project directory:

```bash
./iphone.command profile
```

To prefill server addresses and app keys, prepare [private presets](#prefill-saved-servers)
before this command. Otherwise enter them under **Local Mac** after installation.

The script checks Xcode and the iOS SDK, Flutter, CocoaPods, the certificate with
its private key, and phone availability. It reuses the Team ID from an existing
private `app/ios/Flutter/PersonalTeam.xcconfig` and verifies its signing identity.
An explicit `OMI_APPLE_TEAM_ID` overrides it. First-time Team ID input is hidden.
If something is missing, follow the displayed instructions and press Enter to
retry; `q` exits setup. Installation starts only after all checks pass.

To check without building or installing, run `./iphone.command --check`
from the repository root. You can fix issues and retry checks in this mode too.
Xcode verifies the Apple Account's access to provisioning during signing;
a certificate alone does not prove that the account session is still valid.

After the checks, the script prepares dependencies, builds, installs, and launches
the app. If macOS requests access to the signing key, allow it using your Mac
password. If the iPhone reports an untrusted developer, open **Settings → General
→ VPN & Device Management** and trust your developer identity.

Verify that the main screen opens and remains responsive for a minute. Close the
app from the app switcher and reopen it from its icon, then leave it in the
background for 30 seconds and return. Both checks should reach the main screen
without a crash or a stuck splash screen.

Keep one paired iPhone connected by USB for automatic installation. A wired
iPhone takes precedence over stale Wi-Fi discovery; multiple wired phones are
rejected. With no USB phone, exactly one paired Wi-Fi candidate is checked live.
`OMI_IOS_DEVICE_ID` may pin the build destination; installation also verifies
that it matches the connected phone.
If Xcode reports that the bundle identifier is taken, set your own
`OMI_PERSONAL_BUNDLE_ID`. Use `./omiloc pair` in your own interactive terminal,
then enter the shown address and app key under **Local Mac** in the app.
Do not transfer old `.local`, `.venv`, `build`, keys, or signing settings.

Previous builds were physically verified on an iPhone 17 Pro using Xcode 27.0,
Flutter 3.47.4, and CocoaPods 1.17.0. Checks covered cold launch, reopening,
background/foreground return, CV1 start/stop and pause/resume, and final transcripts
with live preview enabled and disabled.
Those checks predate the current phone-outbox delivery changes. An updated,
attested iOS build and fresh interruption tests are required for that path.
A complete repeat installation on a clean Mac remains unverified.

## Prefill saved servers

On the Mac that builds and launches the iPhone app, copy the public template:

```bash
cp -n omiloc.servers.env.example .env.servers
chmod 600 .env.servers
```

Edit `.env.servers` locally with each recording server's existing app key.
For example, an entry named `home` uses these three literal fields:

```dotenv
OMI_SERVER_home_NAME=Home Mac
OMI_SERVER_home_URL=100.64.0.1:20000
OMI_SERVER_home_KEY=<existing 43-character app key>
```

Docker normally uses port `21000`; the native Mac receiver uses `20000`.
An HTTPS server URL is also accepted. These are recording-server credentials,
not provider API keys or an ngrok authtoken. Obtain the app key in your own
terminal with `./omiloc pair` on that server; never send it through chat.
Add another three fields with a different stable lowercase ID for each server.

The file supports at most 16 servers and 32 KiB. IDs begin with a lowercase
letter and contain up to 32 lowercase letters, digits, underscores or hyphens.
Names contain 1–80 printable characters. Values may be quoted; they are read
literally, without shell execution or variable expansion. The file must remain
an owner-only regular file with mode `0600`. Invalid entries stop provisioning.

Run `./iphone.command profile` after preparing the file. Debug also imports
presets with `./iphone.command dev`, into its separate Keychain. To choose a
different private file, set `OMI_LOCAL_SERVER_PRESETS_FILE` to its path in your
local shell. The default `.env.servers` is optional; without it, an existing
native `.env` with an app key provides one **Local Mac** preset. A legacy setup
with only a pairing hash continues without automatic import.

Presets arrive as saved entries, without connecting or changing the active
server. Select an entry and tap **Check and connect**. Manual edits, deletions
and active pairing survive updates; repeated imports do not overwrite them.
Edit credentials on the phone to update an existing entry. Deleting a preset or
signing out does not let a later launch silently restore it.

Only a transient launch environment carries the validated catalog. Neither
`.env.servers` nor its keys are bundled into the app, Dart defines, source files
or build-attestation records. The app consumes the handoff once and stores the
entries in Keychain, so ordinary launches from the app icon need no Mac session.
Installing an app without launching it through this flow cannot import presets.

[Reinstallation and build verification](DEVELOPMENT.md#iphone-builds) ·
[Debug and hot reload](DEVELOPMENT.md#debug-on-iphone-and-hot-reload) ·
[Startup and CV1 recording](START.md).
