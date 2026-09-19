# The iPhone app

For containers on Mac CPU or Linux/WSL2 NVIDIA, see [Docker](DOCKER.md).

Recording requires a separately installed local Omi app.
`start.command` prepares Mac services but does not install the phone app.
After installation, connect it using the [ngrok guide](NGROK.md).

## Preparing your Mac

You need an Apple Silicon Mac, Xcode, Flutter, CocoaPods, and your own Apple Account.
There is no restriction to a particular iPhone model or M-series generation.
The project targets iOS 15.0 or later; Xcode must support the phone's iOS version
and run on your macOS version.

The iOS app uses the [UIScene lifecycle](https://docs.flutter.dev/release/breaking-changes/uiscenedelegate).
Native channels are registered when the Flutter engine is ready; scene callbacks
handle links and Bluetooth background/foreground transitions.

1. Install Xcode from the App Store, open it, accept the license, and wait for the
   iOS components to finish installing. Under **Settings → Locations → Command Line
   Tools**, select the installed Xcode.
2. Install [Flutter for iOS](https://docs.flutter.dev/platform-integration/ios/setup)
   and add it to PATH as documented. Use version 3.47.4 to match the app dependency
   lock and CI checks.
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
   In Xcode, open **Window → Devices and Simulators** and wait for the phone to
   become ready. On the iPhone, enable **Settings → Privacy & Security → Developer
   Mode**, restart the phone, and confirm the setting.

## Installation

[Download the project and prepare Mac services](START.md#installation).
From the project directory:

```bash
cd app
bash setup.sh ios personal
```

The script checks Xcode and the iOS SDK, Flutter, CocoaPods, the certificate with
its private key, and phone availability. The Team ID prompt hides your input.
If something is missing, follow the displayed instructions and press Enter to
retry; `q` exits setup. Installation starts only after all checks pass.

To check without building or installing, run `./start.command --iphone-check`
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

The phone is selected from connected devices. With multiple phones, the script
prompts you to choose; use `OMI_IOS_DEVICE_ID` for a noninteractive selection.
If Xcode reports that the bundle identifier is taken, set your own
`OMI_PERSONAL_BUNDLE_ID`. Enter the domain and key in the app after installation.
Do not transfer old `.local`, `.venv`, `build`, keys, or signing settings.

The current build was physically verified on an iPhone 17 Pro using Xcode 27.0,
Flutter 3.47.4, and CocoaPods 1.17.0. Checks covered cold launch, reopening,
background/foreground return, CV1 start/stop and pause/resume, and final transcripts
with live preview enabled and disabled.
A complete repeat installation on a clean Mac remains unverified.

[Reinstallation and build verification](DEVELOPMENT.md#iphone-builds) ·
[Debug and hot reload](DEVELOPMENT.md#debug-on-iphone-and-hot-reload) ·
[Startup and CV1 recording](START.md).
