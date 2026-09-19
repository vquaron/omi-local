#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fixture_dir="$(mktemp -d "${TMPDIR:-/tmp}/omi mobile wrapper.XXXXXX")"
trap 'find "$fixture_dir" -depth -delete' EXIT
mkdir -p "$fixture_dir/scripts" "$fixture_dir/ios/Runner" "$fixture_dir/ios/Flutter"
cp "$ROOT_DIR/setup.sh" "$fixture_dir/setup.sh"
cp "$ROOT_DIR/pubspec.yaml" "$fixture_dir/pubspec.yaml"
cp "$ROOT_DIR/scripts/validate_mobile_build_config.sh" "$fixture_dir/scripts/"
# Native generation/build/install are outside this portable wrapper contract.
for script in apply_personal_ios_plugin_overlay generate_ios_dev_info_plist; do
  printf '#!/usr/bin/env bash\nprintf "native-preparation\\n" >>flutter.log\n' >"$fixture_dir/scripts/$script.sh"
done

(
  cd "$fixture_dir"
  source ./setup.sh >/dev/null
  flutter() { echo 'Flutter 3.47.4'; }
  xcodebuild() { echo 'Xcode 26.6'; }
  xcrun() { echo "$fixture_dir"; }
  pod() { printf '%s' "$fixture_pod_version"; return "$fixture_pod_exit"; }
  for fixture_pod_version in '' 1.15.0; do
    fixture_pod_exit=0
    if check_ios_prerequisites >prerequisites.out 2>&1; then
      echo 'FAIL: unavailable or outdated CocoaPods passed preflight' >&2
      exit 1
    fi
    grep 'CocoaPods' prerequisites.out >/dev/null
  done
  fixture_pod_version=1.16.2
  fixture_pod_exit=1
  if check_ios_prerequisites >prerequisites.out 2>&1; then
    echo 'FAIL: failed CocoaPods invocation passed preflight' >&2
    exit 1
  fi
  fixture_pod_exit=0
  check_ios_prerequisites
)

(
  cd "$fixture_dir"
  unset OMI_DEV_HOST OMI_LOCAL_API_BASE_URL OMI_IOS_DEVICE_ID OMI_PERSONAL_BUNDLE_ID OMI_LOCAL_TEST_USER
  source ./setup.sh >/dev/null
  log_file="$fixture_dir/flutter.log"
  : >"$log_file"
  hostname() { printf 'test-mac\n'; }
  record() { printf '%s\n' "$*" >>"$log_file"; }
  flutter() {
    if [[ "$*" == 'devices --machine' ]]; then
      printf '%s\n' "$fixture_devices_json"
    else
      record flutter "$@"
    fi
  }
  pod() { record pod "$@"; }
  dart() { record dart "$@"; }
  check_ios_prerequisites() { :; }
  check_ios_signing() { :; }
  check_ios_phone() { OMI_SELECTED_IOS_DEVICE=$(select_ios_device); }
  prepare_personal_ios_build_dir() { record build-directory; }
  setup_firebase() { record firebase-config; }
  generate_ios_custom_config() { record ios-config "$@"; }

  export OMI_APPLE_TEAM_ID=ABCDEFGHIJ OMI_RUNTIME_MODE=offline
  # A simulator must not steal the physical destination, regardless of model.
  for model in 'iPhone SE (3rd generation)' 'iPhone 13' 'iPhone 17 Pro' 'Future iPhone'; do
    fixture_devices_json=$(jq -cn --arg name "$model" '[
      {id:"SIMULATOR",name:"iPhone simulator",targetPlatform:"ios",emulator:true},
      {id:"TEST-PHONE",name:$name,targetPlatform:"ios",emulator:false,isSupported:true}
    ]')
    run_build_ios dev
  done
  [[ "$(grep -c '^flutter run --profile --flavor dev -d TEST-PHONE ' "$log_file")" == 4 ]]
  grep -F 'API_BASE_URL=http://127.0.0.1:8000/' .dev.env >/dev/null
  grep -Fx 'APP_BUNDLE_IDENTIFIER=com.omi.local.testmac' ios/Flutter/PersonalTeam.xcconfig >/dev/null
  [[ "$(grep '^flutter run ' "$log_file" | tr ' ' '\n' | grep -c '^--dart-define=OMI_APP_PROFILE=local_dev$')" == 4 ]]
  [[ "$(grep '^flutter run ' "$log_file" | tr ' ' '\n' | grep -c '^--dart-define=OMI_RUNTIME_MODE=offline$')" == 4 ]]

  expect_rejected() {
    local before
    before=$(shasum -a 256 "$log_file" .dev.env ios/Flutter/PersonalTeam.xcconfig)
    if run_build_ios "${1:-dev}" >rejected.out 2>rejected.err; then
      echo 'FAIL: wrapper accepted an unavailable device or invalid configuration' >&2
      exit 1
    fi
    [[ "$(shasum -a 256 "$log_file" .dev.env ios/Flutter/PersonalTeam.xcconfig)" == "$before" ]]
  }
  fixture_devices_json='[{"id":"SIMULATOR","targetPlatform":"ios","emulator":true}]'
  expect_rejected
  fixture_devices_json='[{"id":"MAC","targetPlatform":"darwin","emulator":false}]'
  expect_rejected
  fixture_devices_json='[{"id":"OLD-PHONE","targetPlatform":"ios","emulator":false,"isSupported":false}]'
  expect_rejected
  fixture_devices_json='[]'
  expect_rejected
  fixture_devices_json='unreadable'
  expect_rejected

  fixture_devices_json='[{"id":"PHONE-A","targetPlatform":"ios","emulator":false},{"id":"PHONE-B","targetPlatform":"ios","emulator":false}]'
  expect_rejected </dev/null
  OMI_IOS_DEVICE_ID=MISSING expect_rejected
  OMI_IOS_DEVICE_ID=PHONE-B OMI_APPLE_TEAM_ID=KLMNOPQRST OMI_PERSONAL_BUNDLE_ID=com.example.omiloc run_build_ios dev
  grep -F 'flutter run --profile --flavor dev -d PHONE-B ' "$log_file" >/dev/null
  grep -Fx 'OMI_APPLE_TEAM_ID=KLMNOPQRST' ios/Flutter/PersonalTeam.xcconfig >/dev/null
  grep -Fx 'APP_BUNDLE_IDENTIFIER=com.example.omiloc' ios/Flutter/PersonalTeam.xcconfig >/dev/null
  OMI_IOS_DEVICE_ID=PHONE-B OMI_APPLE_TEAM_ID= expect_rejected
  OMI_IOS_DEVICE_ID=PHONE-B OMI_PERSONAL_BUNDLE_ID=com.omi.local. expect_rejected
  OMI_IOS_DEVICE_ID=PHONE-B expect_rejected prod
  OMI_IOS_DEVICE_ID=PHONE-B OMI_RUNTIME_MODE=standard expect_rejected

  # LAN remains an explicit alternative; ngrok pairing does not require it.
  LOCAL_DEV_HOST=192.168.2.10 LOCAL_API_BASE_URL=http://192.168.2.10:8000/ OMI_IOS_DEVICE_ID=PHONE-B run_build_ios dev
  grep -Fx 'API_BASE_URL=http://192.168.2.10:8000/' .dev.env >/dev/null
  hostname() { printf 'Компьютер\n'; }
  [[ "$(personal_bundle_id)" =~ ^com\.omi\.local\.[a-f0-9]{12}$ ]]
)
echo 'PASS: physical iPhone selection, independent signing, default pairing and fail-before-build checks'
