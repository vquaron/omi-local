#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
fixture_dir="$(mktemp -d "${TMPDIR:-/tmp}/omi-personal-ios.XXXXXX")"
trap 'find "$fixture_dir" -type f -delete; rmdir "$fixture_dir"' EXIT

if grep -Fq 'RunnerPersonal.entitlements' "$ROOT_DIR/ios/Runner.xcodeproj/project.pbxproj"; then
  echo 'FAIL: Personal Team config must not sign an empty custom entitlement blob' >&2
  exit 1
fi

runner_target="$(awk '
  /97C146ED1CF9000F007C117D \/\* Runner \*\// { capture = 1 }
  capture { print }
  capture && /^\t\t};$/ { exit }
' "$ROOT_DIR/ios/Runner.xcodeproj/project.pbxproj")"
[[ "$runner_target" == *'Validate Personal Team Offline Build'* ]]
[[ "$runner_target" != *'Embed Foundation Extensions'* ]]
[[ "$runner_target" != *'Embed Watch Content'* ]]
[[ "$runner_target" != *'468E5B26D379E32080A67B11'* ]]
[[ "$runner_target" != *'42A7BA3D2E788BD400138969'* ]]
[[ -f "$ROOT_DIR/ios/Runner/Assets.xcassets/personalAppIcon.appiconset/Contents.json" ]]
[[ "$(shasum -a 256 "$ROOT_DIR/ios/Runner/Assets.xcassets/personalAppIcon.appiconset/OmiAppIcon-1024@1x 1.png" | awk '{print $1}')" != "$(shasum -a 256 "$ROOT_DIR/ios/Runner/Assets.xcassets/devAppIcon.appiconset/OmiAppIcon-1024@1x 1.png" | awk '{print $1}')" ]]
grep -Fq -- "-d ios/Flutter/ephemeral/Packages/FlutterGeneratedPluginSwiftPackage" "$ROOT_DIR/setup.sh"

registrant="$ROOT_DIR/ios/Runner/GeneratedPluginRegistrant.m"
grep -Fq "echo 'GCC_PREPROCESSOR_DEFINITIONS=\$(inherited) OMI_PERSONAL_LOCAL=1'" "$ROOT_DIR/setup.sh"
bash "$ROOT_DIR/scripts/apply_personal_ios_plugin_overlay.sh" "$registrant" >/dev/null
grep -q 'OMI_PERSONAL_EXTERNAL_PLUGINS_DISABLED' "$registrant"
for plugin in \
  FLTFirebaseCrashlyticsPlugin \
  FLTFirebaseMessagingPlugin \
  FLTGoogleSignInPlugin \
  IntercomFlutterPlugin \
  PosthogFlutterPlugin \
  SignInWithApplePlugin; do
  ruby -e '
    text = File.read(ARGV.fetch(0))
    plugin = ARGV.fetch(1)
    registration = text.index("[#{plugin} registerWithRegistrar") or abort "unguarded plugin missing: #{plugin}"
    guard = text.rindex("#if !OMI_PERSONAL_LOCAL", registration) or abort "guard missing: #{plugin}"
    close = text.index("#endif", guard) or abort "guard not closed: #{plugin}"
    abort "registration outside guard: #{plugin}" unless registration < close
  ' "$registrant" "$plugin"
done

for config in Debug Profile Release; do
  block="$(awk -v name="$config-dev" '
    $0 ~ "/\\* " name " \\*/ = \\{" { capture = 1 }
    capture { print }
    capture && /^\t\t};$/ { exit }
  ' "$ROOT_DIR/ios/Runner.xcodeproj/project.pbxproj")"
  [[ "$block" != *'CODE_SIGN_ENTITLEMENTS ='* ]]
  [[ "$block" == *'DEVELOPMENT_TEAM = "$(OMI_APPLE_TEAM_ID)";'* ]]
done

cp "$ROOT_DIR/ios/Runner/Info.plist" "$fixture_dir/Info.plist"
bash "$ROOT_DIR/scripts/generate_ios_dev_info_plist.sh" \
  "$fixture_dir/Info.plist" "$fixture_dir/Info-Personal.plist" personal

for key in \
  BGTaskSchedulerPermittedIdentifiers \
  CFBundleURLTypes \
  MWDAT \
  UISupportedExternalAccessoryProtocols \
  WKCompanionAppBundleIdentifier; do
  if /usr/libexec/PlistBuddy -c "Print :$key" "$fixture_dir/Info-Personal.plist" >/dev/null 2>&1; then
    echo "FAIL: Personal Team Info.plist contains forbidden key $key" >&2
    exit 1
  fi
done
[[ "$(/usr/libexec/PlistBuddy -c 'Print :UIBackgroundModes:0' "$fixture_dir/Info-Personal.plist")" == 'audio' ]]
[[ "$(/usr/libexec/PlistBuddy -c 'Print :UIBackgroundModes:1' "$fixture_dir/Info-Personal.plist")" == 'bluetooth-central' ]]
[[ -n "$(/usr/libexec/PlistBuddy -c 'Print :NSLocalNetworkUsageDescription' "$fixture_dir/Info-Personal.plist")" ]]
[[ "$(/usr/libexec/PlistBuddy -c 'Print :NSAppTransportSecurity:NSAllowsLocalNetworking' "$fixture_dir/Info-Personal.plist")" == 'true' ]]
[[ "$(/usr/libexec/PlistBuddy -c 'Print :NSAppTransportSecurity:NSExceptionDomains:100.64.0.0/10:NSExceptionAllowsInsecureHTTPLoads' "$fixture_dir/Info-Personal.plist")" == 'true' ]]
python3 - "$fixture_dir/Info-Personal.plist" <<'PY'
import plistlib
import sys

with open(sys.argv[1], "rb") as source:
    ats = plistlib.load(source)["NSAppTransportSecurity"]
assert ats["NSExceptionDomains"] == {
    "100.64.0.0/10": {"NSExceptionAllowsInsecureHTTPLoads": True}
}
PY
if /usr/libexec/PlistBuddy -c 'Print :NSAppTransportSecurity:NSAllowsArbitraryLoads' "$fixture_dir/Info-Personal.plist" >/dev/null 2>&1; then
  echo 'FAIL: Personal Team Info.plist enables NSAllowsArbitraryLoads' >&2
  exit 1
fi
[[ "$(/usr/libexec/PlistBuddy -c 'Print :FirebaseMessagingAutoInitEnabled' "$fixture_dir/Info-Personal.plist")" == 'false' ]]
[[ "$(/usr/libexec/PlistBuddy -c 'Print :FirebaseAppDelegateProxyEnabled' "$fixture_dir/Info-Personal.plist")" == 'false' ]]
[[ "$(/usr/libexec/PlistBuddy -c 'Print :FirebaseCrashlyticsCollectionEnabled' "$fixture_dir/Info-Personal.plist")" == 'false' ]]

validator="$ROOT_DIR/scripts/validate_personal_ios_xcode_build.sh"
CONFIGURATION=Debug-dev \
OMI_PERSONAL_LOCAL=YES \
OMI_RUNTIME_MODE=offline \
DEVELOPMENT_TEAM=ABCDEFGHIJ \
PRODUCT_BUNDLE_IDENTIFIER=com.example.omi.local \
bash "$validator" >/dev/null

if CONFIGURATION=Release-prod \
  OMI_PERSONAL_LOCAL=YES \
  OMI_RUNTIME_MODE=offline \
  DEVELOPMENT_TEAM=ABCDEFGHIJ \
  PRODUCT_BUNDLE_IDENTIFIER=com.example.omi.local \
  bash "$validator" 2>"$fixture_dir/prod.err"; then
  echo 'FAIL: Xcode policy accepted a production configuration' >&2
  exit 1
fi
grep -F 'only the dev flavor is available' "$fixture_dir/prod.err" >/dev/null

if CONFIGURATION=Debug-dev \
  OMI_PERSONAL_LOCAL=YES \
  OMI_RUNTIME_MODE=offline \
  DEVELOPMENT_TEAM=ABCDEFGHIJ \
  PRODUCT_BUNDLE_IDENTIFIER=com.friend-app-with-wearable.ios12.development \
  bash "$validator" 2>"$fixture_dir/bundle.err"; then
  echo 'FAIL: Xcode policy accepted the upstream bundle namespace' >&2
  exit 1
fi
grep -F 'upstream bundle namespace is forbidden' "$fixture_dir/bundle.err" >/dev/null

if bash "$ROOT_DIR/setup.sh" ios >"$fixture_dir/setup.out" 2>"$fixture_dir/setup.err"; then
  echo "FAIL: setup accepted iOS without the explicit personal mode" >&2
  exit 1
fi
grep -F "only supports 'bash setup.sh ios personal'" "$fixture_dir/setup.err" >/dev/null

echo 'Personal Team iOS overlay has no custom entitlement blob, minimal targets/background modes, and fail-closed build gates'
