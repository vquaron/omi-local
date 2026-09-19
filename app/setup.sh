#!/bin/bash
#
# Set up the Omi Mobile Project(iOS/Android).
#
# Prerequisites (stable versions, use these or higher):
#
# Common for all developers:
# - Flutter SDK: exact version from environment.flutter in pubspec.yaml
# - Opus Codec: https://opus-codec.org
#
# For iOS Developers:
# - Xcode (v16.4)
# - CocoaPods (v1.16.2)
#
# For Android Developers:
# - Android Studio (Iguana | 2024.3)
# - Android SDK Platform (API 35)
# - JDK (v21)
# - Gradle (v8.10)
# - NDK (28.2.13676358)
#
# Usages:
# - $bash setup.sh ios personal
# - $bash setup.sh android

set -euo pipefail

LOCAL_DEV_HOST="${OMI_DEV_HOST:-127.0.0.1}"
LOCAL_API_BASE_URL="${OMI_LOCAL_API_BASE_URL:-http://${LOCAL_DEV_HOST}:8000/}"
ANDROID_DEV_HOST="${OMI_ANDROID_DEV_HOST:-${OMI_DEV_HOST:-10.0.2.2}}"
ANDROID_LOCAL_API_BASE_URL="${OMI_LOCAL_API_BASE_URL:-http://${ANDROID_DEV_HOST}:8000/}"
BETA_API_BASE_URL="${OMI_BETA_API_BASE_URL:-https://api.omiapi.com/}"

######################################
# Generate device suffix from hostname
######################################
function generate_device_suffix() {
  # Use hostname or a hash of it as suffix
  local host_name suffix
  host_name=$(hostname -s)
  suffix=$(printf '%s' "$host_name" | LC_ALL=C tr '[:upper:]' '[:lower:]' | LC_ALL=C tr -cd '[:alnum:]')
  # A non-Latin computer name must still produce a valid bundle identifier.
  if [[ -z "$suffix" ]]; then
    suffix=$(printf '%s' "$host_name" | shasum -a 256 | awk '{print substr($1, 1, 12)}')
  fi
  echo "$suffix"
}

function personal_bundle_id() {
  local suffix bundle_id
  suffix=$(generate_device_suffix)
  bundle_id="${OMI_PERSONAL_BUNDLE_ID:-com.omi.local.${suffix}}"
  if [[ ! "$bundle_id" =~ ^[A-Za-z0-9][A-Za-z0-9-]*(\.[A-Za-z0-9][A-Za-z0-9-]*)+$ ]]; then
    echo "ERROR: OMI_PERSONAL_BUNDLE_ID must be a reverse-DNS-style bundle identifier." >&2
    return 1
  fi
  case "$bundle_id" in
    com.friend-app-with-wearable.ios12|com.friend-app-with-wearable.ios12.*)
      echo "ERROR: Personal Team builds cannot use the upstream bundle namespace." >&2
      return 1
      ;;
  esac
  echo "$bundle_id"
}

######################################
# Generate custom configs for iOS
######################################
function generate_ios_custom_config() {
  local config_name="${1:-Dev}"
  local callback_scheme="${2:-omi-dev}"
  local personal_mode="${3:-false}"
  if [[ "$personal_mode" == 'true' ]]; then
    local personal_id
    personal_id=$(personal_bundle_id) || return 1
    /usr/libexec/PlistBuddy -c "Set :BUNDLE_ID ${personal_id}" "ios/Config/${config_name}/GoogleService-Info.plist"
    /usr/libexec/PlistBuddy -c "Set :BUNDLE_ID ${personal_id}" ios/Runner/GoogleService-Info.plist
    # Every dev xcconfig includes this file, including a fresh Personal Team
    # checkout. The local overlay has no Google sign-in callback scheme.
    printf '// Generated local configuration.\nGOOGLE_REVERSE_CLIENT_ID=\n' > ios/Flutter/Custom.xcconfig
    return
  fi

  bash scripts/generate_ios_custom_config.sh "ios/Config/${config_name}/GoogleService-Info.plist" ios/Flutter

  if [[ "$config_name" == "Dev" ]]; then
    # Keep ordinary local builds installable beside the App Store build.
    local suffix
    suffix=$(generate_device_suffix)
    # The prebuilt/placeholder GoogleService-Info.plist (setup_firebase) carries the
    # stock unsuffixed BUNDLE_ID. Firebase's native SDK validates that field against
    # the running app's actual bundle identifier at Firebase.initializeApp() and
    # refuses to start if they don't match. Runner/GoogleService-Info.plist is what
    # Xcode actually bundles (project.pbxproj references that path directly, not
    # Config/Dev/); patch both so on-disk copies stay consistent.
    local suffixed_bundle_id="com.friend-app-with-wearable.ios12-${suffix}"
    echo "APP_BUNDLE_IDENTIFIER=${suffixed_bundle_id}" >> ios/Flutter/Custom.xcconfig
    /usr/libexec/PlistBuddy -c "Set :BUNDLE_ID ${suffixed_bundle_id}" "ios/Config/${config_name}/GoogleService-Info.plist"
    /usr/libexec/PlistBuddy -c "Set :BUNDLE_ID ${suffixed_bundle_id}" ios/Runner/GoogleService-Info.plist
  else
    # Beta uses a distinct bundle/callback identity and must be provisioned
    # explicitly by the developer's Apple team.
    echo "APP_BUNDLE_IDENTIFIER=${OMI_MOBILE_BETA_BUNDLE_ID:-com.friend-app-with-wearable.ios12.beta}" >> ios/Flutter/Custom.xcconfig
    echo "AUTH_CALLBACK_SCHEME=${callback_scheme}" >> ios/Flutter/Custom.xcconfig
  fi
}

function write_personal_team_config() {
  local team_id="${OMI_APPLE_TEAM_ID:-}"
  local bundle_id
  if [[ ! "$team_id" =~ ^[A-Z0-9]{10}$ ]]; then
    echo "ERROR: set OMI_APPLE_TEAM_ID to the 10-character Personal Team identifier shown by Xcode." >&2
    return 1
  fi
  bundle_id=$(personal_bundle_id) || return 1
  umask 077
  {
    echo 'OMI_PERSONAL_LOCAL=YES'
    echo 'OMI_RUNTIME_MODE=offline'
    echo "OMI_APPLE_TEAM_ID=${team_id}"
    echo "APP_BUNDLE_IDENTIFIER=${bundle_id}"
    echo 'BUNDLE_NAME=Omi Local'
    echo 'BUNDLE_DISPLAY_NAME=Omi Local'
    echo 'ASSET_PREFIX=personal'
    echo 'SWIFT_ACTIVE_COMPILATION_CONDITIONS=$(inherited) OMI_PERSONAL_LOCAL'
    echo 'GCC_PREPROCESSOR_DEFINITIONS=$(inherited) OMI_PERSONAL_LOCAL=1'
    echo "APP_BUNDLE_IDENTIFIER[config=Debug-dev]=${bundle_id}.dev"
    echo 'BUNDLE_NAME[config=Debug-dev]=Omi Local Dev'
    echo 'BUNDLE_DISPLAY_NAME[config=Debug-dev]=Omi Local Dev'
  } > ios/Flutter/PersonalTeam.xcconfig
}

# macOS File Provider adds provenance xattrs to every file created under a
# managed workspace. codesign rejects those attributes on copied Flutter
# frameworks, so Personal Team builds use one stable, host-local build cache
# outside the workspace. The symlink itself is covered by Flutter's /build/
# ignore rule.
function prepare_personal_ios_build_dir() {
  if [[ "$(uname -s)" != 'Darwin' ]] || ! command -v xattr >/dev/null 2>&1; then
    return
  fi
  if ! xattr -p com.apple.provenance . >/dev/null 2>&1; then
    return
  fi

  local app_root build_key temp_root external_build current_target migrated had_native_build
  migrated=false
  had_native_build=false
  if [[ -d build/ios ]]; then
    had_native_build=true
  fi
  app_root=$(pwd -P)
  build_key=$(printf '%s' "$app_root" | shasum -a 256 | awk '{print substr($1, 1, 12)}')
  temp_root="${TMPDIR:-/private/tmp}"
  external_build="${temp_root%/}/omi-personal-ios-build-${UID}-${build_key}"

  if [[ -L build ]]; then
    current_target=$(readlink build)
    if [[ "$current_target" != "$external_build" ]]; then
      echo "ERROR: build is already a symlink to an unexpected target; refusing to replace it." >&2
      return 1
    fi
    mkdir -p "$external_build"
  elif [[ -e build ]]; then
    if [[ -e "$external_build" ]]; then
      echo "ERROR: both build and the Personal Team external build cache already exist." >&2
      return 1
    fi
    mv build "$external_build"
    ln -s "$external_build" build
    migrated=true
  else
    mkdir -p "$external_build"
    ln -s "$external_build" build
    migrated=true
  fi

  # Safe here: external_build is a generated cache rooted in this user's temp
  # directory and includes a hash of this app checkout.
  # SwiftPM may keep some cached checkout files read-only; those are not signed.
  # Remove attributes from writable build outputs and let Flutter repeat the
  # same cleanup on frameworks as it copies them.
  xattr -cr "$external_build" 2>/dev/null || true

  # Xcode dependency files contain absolute XCFramework paths. Clear them once
  # when moving to the stable cache, otherwise a previous temporary path can
  # survive and fail much later with a misleading missing module.modulemap.
  # flutter clean removes the generated local Swift package before this helper
  # runs. In that state there is no package graph for xcodebuild to clean, and
  # flutter pub get below will recreate it from scratch.
  if [[ "$migrated" == 'true' && "$had_native_build" == 'true' && -d ios/Flutter/ephemeral/Packages/FlutterGeneratedPluginSwiftPackage ]]; then
    OMI_RUNTIME_MODE=offline xcodebuild \
      -workspace ios/Runner.xcworkspace \
      -scheme dev \
      -configuration Debug-dev \
      clean >/dev/null 2>&1 || {
        echo "ERROR: Xcode could not clear stale Personal Team build metadata." >&2
        return 1
      }
  fi
}

######################################
# Setup Firebase with prebuilt configs
######################################
function setup_firebase() {
  mkdir -p android/app/src/dev/ android/app/src/prod/ ios/Config/Dev/ ios/Config/Prod/ ios/Runner/
  cp lib/firebase_options_local.dart lib/firebase_options_dev.dart
  cp lib/firebase_options_local.dart lib/firebase_options_prod.dart
  cp setup/prebuilt/google-services-local.json android/app/src/dev/google-services.json
  cp setup/prebuilt/google-services-local.json android/app/src/prod/google-services.json
  cp setup/prebuilt/GoogleService-Info-Local.plist ios/Config/Dev/GoogleService-Info.plist
  cp setup/prebuilt/GoogleService-Info-Local.plist ios/Config/Prod/GoogleService-Info.plist
  cp setup/prebuilt/GoogleService-Info-Local.plist ios/Runner/GoogleService-Info.plist
}

##########################################
# Setup Firebase with Service Account Json
##########################################
function setup_firebase_with_service_account_ios() {
  dart pub global activate flutterfire_cli
  flutterfire config \
    --platforms=ios \
    --out=lib/firebase_options_prod.dart \
    --ios-bundle-id=${OMI_MOBILE_BETA_BUNDLE_ID:-com.friend-app-with-wearable.ios12.beta} \
    --ios-out=ios/Config/Prod/ \
    --service-account="$FIREBASE_SERVICE_ACCOUNT_KEY" \
    --project="based-hardware" \
    --ios-target="Runner" \
    --yes
}

function setup_firebase_with_service_account_android() {
  dart pub global activate flutterfire_cli
  flutterfire config \
    --platforms=android \
    --out=lib/firebase_options_prod.dart \
    --android-app-id=com.friend.ios \
    --android-out=android/app/src/prod/ \
    --service-account="$FIREBASE_SERVICE_ACCOUNT_KEY" \
    --project="based-hardware" \
    --yes
}

######################################
# Setup provisioning profile
######################################
function setup_provisioning_profile() {
    # Only install fastlane if it doesn't exist
    if ! command -v fastlane &> /dev/null; then
        echo "Installing fastlane..."
        brew install fastlane
    fi

    MATCH_PASSWORD=omi fastlane match development --readonly \
        --app_identifier com.friend-app-with-wearable.ios12.development \
        --git_url "git@github.com:BasedHardware/omi-community-certs.git"
}


#################
# Set up App .env
#################
function setup_app_env() {
  local profile="${1:-local_dev}"
  local configured_api_base_url="${2:-}"
  local env_file='.dev.env'
  local api_base_url="${configured_api_base_url:-$LOCAL_API_BASE_URL}"
  if [[ "$profile" == "mobile_beta" ]]; then
    env_file='.env'
    api_base_url="$BETA_API_BASE_URL"
  fi

  # Keep developer-owned settings and comments intact. These are the only keys
  # owned by setup, so update them in place and replace the file atomically.
  local temp_file source_file="/dev/null"
  if [[ -f "$env_file" ]]; then source_file="$env_file"; fi
  temp_file=$(mktemp "${env_file}.tmp.XXXXXX")
  if ! awk \
    -v api_base_url="$api_base_url" \
    'BEGIN { api_written = 0; web_auth_written = 0; custom_token_written = 0 }
     /^[[:space:]]*API_BASE_URL[[:space:]]*=/ {
       if (!api_written) { print "API_BASE_URL=" api_base_url; api_written = 1 }
       next
     }
     /^[[:space:]]*USE_WEB_AUTH[[:space:]]*=/ {
       if (!web_auth_written) { print "USE_WEB_AUTH=true"; web_auth_written = 1 }
       next
     }
     /^[[:space:]]*USE_AUTH_CUSTOM_TOKEN[[:space:]]*=/ {
       if (!custom_token_written) { print "USE_AUTH_CUSTOM_TOKEN=true"; custom_token_written = 1 }
       next
     }
     { print }
     END {
       if (!api_written) print "API_BASE_URL=" api_base_url
       if (!web_auth_written) print "USE_WEB_AUTH=true"
       if (!custom_token_written) print "USE_AUTH_CUSTOM_TOKEN=true"
     }' \
    "$source_file" 2>/dev/null >"$temp_file"; then
    rm -f "$temp_file"
    return 1
  fi
  if ! mv "$temp_file" "$env_file"; then
    rm -f "$temp_file"
    return 1
  fi
}

function validate_flutter_profile_arg() {
  local expected_profile="$1"
  shift
  local arg requested_profile profile_arg_count=0
  for arg in "$@"; do
    if [[ "$arg" == --dart-define=OMI_APP_PROFILE=* ]]; then
      profile_arg_count=$((profile_arg_count + 1))
      requested_profile="${arg#--dart-define=OMI_APP_PROFILE=}"
      if [[ "$requested_profile" != "$expected_profile" ]]; then
        echo "ERROR: this flavor requires OMI_APP_PROFILE=$expected_profile, got '$requested_profile'." >&2
        return 1
      fi
    fi
  done
  if [[ "$profile_arg_count" -gt 1 ]]; then
    echo "ERROR: pass OMI_APP_PROFILE only once; the wrapper supplies the required value." >&2
    return 1
  fi
}

function prepare_mobile_build_env() {
  local flavor="$1"
  local configured_api_base_url="${2:-}"
  local runtime_mode="${3:-standard}"
  local profile api_base_url
  case "$flavor" in
    dev)
      profile='local_dev'
      api_base_url="$LOCAL_API_BASE_URL"
      ;;
    prod)
      profile='mobile_beta'
      api_base_url="$BETA_API_BASE_URL"
      ;;
    *)
      echo "ERROR: unsupported mobile flavor '$flavor' (expected dev or prod)." >&2
      return 1
      ;;
  esac
  if [[ "$flavor" == 'dev' && -n "$configured_api_base_url" ]]; then
    api_base_url="$configured_api_base_url"
  fi
  setup_app_env "$profile" "$api_base_url" || return 1
  scripts/validate_mobile_build_config.sh \
    --flavor "$flavor" \
    --profile "$profile" \
    --runtime-mode "$runtime_mode" || return 1
}

# #######################
# Set up Android Keystore
# #######################
function setup_keystore_android() {
  cp setup/prebuilt/key.properties android/
}

# #####
# Build
# #####
function run_build_android() {
  local flavor="${1:-dev}"
  local profile='local_dev'
  local api_base_url="$ANDROID_LOCAL_API_BASE_URL"
  local emulator_host="$ANDROID_DEV_HOST"
  case "$flavor" in
    dev) ;;
    prod)
      profile='mobile_beta'
      api_base_url="$BETA_API_BASE_URL"
      emulator_host=''
      ;;
    *)
      echo "ERROR: unsupported mobile flavor '$flavor' (expected dev or prod)." >&2
      return 1
      ;;
  esac
  prepare_mobile_build_env "$flavor" "$api_base_url"
  local flutter_args=(
    --flavor "$flavor"
    "--dart-define=OMI_APP_PROFILE=$profile"
    "--dart-define=OMI_API_BASE_URL=$api_base_url"
  )
  if [[ -n "$emulator_host" ]]; then
    flutter_args+=("--dart-define=OMI_FIREBASE_AUTH_EMULATOR_HOST=$emulator_host")
  fi
  flutter pub get \
    && dart run build_runner build \
    && flutter run "${flutter_args[@]}"
}

# #####################################
# iOS prerequisite and device selection
# #####################################

# True if $1 (a dotted version, e.g. "16.4") is >= $2. Relies on `sort -V`,
# which Apple's /usr/bin/sort on macOS supports (verified; this is iOS-only
# tooling, so a non-macOS sort is not a concern).
function _version_at_least() {
  local have="$1" want="$2"
  [ "$(printf '%s\n%s\n' "$want" "$have" | sort -V | head -n1)" = "$want" ]
}

# Shared by Profile setup and the prepared Debug launcher.
function check_flutter_version() {
  local version required_flutter
  required_flutter=$(awk '
    /^environment:/ { in_environment=1; next }
    in_environment && /^[^[:space:]#]/ { exit }
    in_environment && $1 == "flutter:" { gsub(/"/, "", $2); print $2; exit }
  ' "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/pubspec.yaml") || return 1
  if ! [[ "$required_flutter" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo 'В pubspec.yaml должна быть указана точная версия environment.flutter.' >&2
    return 1
  fi
  if ! version=$(flutter --version 2>/dev/null) || ! [[ "$version" =~ Flutter[[:space:]]+([^[:space:]]+) ]] || [[ "${BASH_REMATCH[1]}" != "$required_flutter" ]]; then
    echo "Нужен Flutter $required_flutter из app/pubspec.yaml в PATH. Установка: https://docs.flutter.dev/install" >&2
    echo 'Выберите эту версию SDK; после изменения PATH откройте новый Terminal и повторите проверку.' >&2
    return 1
  fi
}

# Named checks with remedies, matching the harness's own pattern
# (scripts/dev-harness's `Cannot start; missing prerequisites:` block) instead
# of letting a missing/outdated tool surface as a confusing downstream failure
# several minutes into a build.
function check_ios_prerequisites() {
  local version sdk
  if ! version=$(xcodebuild -version 2>/dev/null) || ! [[ "$version" =~ Xcode[[:space:]]+([0-9.]+) ]] || ! _version_at_least "${BASH_REMATCH[1]}" 16.4; then
    echo 'Нужен рабочий Xcode 16.4 или новее. Установите его из App Store и откройте.' >&2
    echo 'Примите лицензию; в Settings → Locations → Command Line Tools выберите Xcode.' >&2
    return 1
  fi
  if ! xcodebuild -checkFirstLaunchStatus >/dev/null 2>&1; then
    echo 'Завершите первый запуск Xcode: откройте его, примите лицензию и дождитесь компонентов.' >&2
    return 1
  fi
  if ! sdk=$(xcrun --sdk iphoneos --show-sdk-path 2>/dev/null) || [[ ! -d "$sdk" ]]; then
    echo 'Не найден iOS SDK. В Xcode → Settings → Components установите поддержку iOS.' >&2
    return 1
  fi
  check_flutter_version || return 1
  if ! version=$(pod --version 2>/dev/null) || ! [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || ! _version_at_least "$version" 1.16.2; then
    echo 'Нужен рабочий CocoaPods 1.16.2 или новее: brew install cocoapods (обновление: brew upgrade cocoapods).' >&2
    echo 'Если Homebrew ещё не установлен, сначала запустите start.command из корня проекта.' >&2
    return 1
  fi
  if ! command -v jq >/dev/null 2>&1; then
    echo 'Не найден jq: brew install jq. Homebrew подготавливается через start.command.' >&2
    return 1
  fi
}

# A retry is a fresh observation, never permission to skip a failed check.
function ios_retry() {
  local reply
  while ! "$@"; do
    if [[ ! -t 0 || ! -t 1 ]]; then
      echo 'Проверка остановлена. Исправьте причину и запустите команду снова в Terminal.' >&2
      return 1
    fi
    read -r -p 'После исправления нажмите Enter; q — выйти: ' reply || return 1
    [[ "$reply" != q && "$reply" != Q ]] || return 1
    # Tools installed in another terminal may be newly available on PATH.
    hash -r
  done
}

function check_ios_signing() {
  local identities certificates line pem='' details fingerprint team
  # security returns only valid signing identities (certificate + private key).
  # Keep identities and subjects in memory; never send them to the terminal/log.
  if ! identities=$(security find-identity -v -p codesigning 2>/dev/null); then
    echo 'Не удалось проверить Связку ключей. Разблокируйте её и повторите проверку из Terminal.' >&2
    return 1
  fi
  certificates=$(security find-certificate -a -p -c 'Apple Development' 2>/dev/null) || certificates=''
  while IFS= read -r line; do
    pem+="$line"$'\n'
    if [[ "$line" == '-----END CERTIFICATE-----' ]]; then
      details=$(printf '%s' "$pem" | openssl x509 -noout -fingerprint -sha1 -subject -nameopt sep_multiline 2>/dev/null) || details=''
      fingerprint=$(printf '%s\n' "$details" | awk -F= '/Fingerprint=/ {gsub(":", "", $2); print toupper($2)}')
      team=$(printf '%s\n' "$details" | sed -nE 's/^[[:space:]]*OU[[:space:]]*=[[:space:]]*([A-Z0-9]{10})[[:space:]]*$/\1/p')
      if [[ -n "$fingerprint" && "$team" == "${OMI_APPLE_TEAM_ID:-}" && "$identities" == *"$fingerprint"* ]]; then
        return 0
      fi
      pem=''
    fi
  done <<< "$certificates"
  echo 'Не подтверждён действующий Apple Development с закрытым ключом для выбранной Team.' >&2
  echo 'Разблокируйте Связку ключей. В Xcode → Settings → Accounts добавьте Apple Account.' >&2
  echo 'Выберите Team → Manage Certificates → + → Apple Development; существующий рабочий сертификат оставьте.' >&2
  echo 'Проверьте Team ID по полю Organizational Unit (OU) сертификата. Подробнее: docs/LOCAL_SETUP.md.' >&2
  return 1
}

function prepare_ios_signing() {
  local reply
  while :; do
    if ! [[ "${OMI_APPLE_TEAM_ID:-}" =~ ^[A-Z0-9]{10}$ ]]; then
      echo 'Подпись: Xcode → Settings → Accounts → ваша Team → Manage Certificates → Apple Development.' >&2
      echo 'Team ID: Связка ключей → Мои сертификаты → Apple Development → Organizational Unit (OU).' >&2
      if [[ ! -t 0 || ! -t 1 ]]; then
        echo 'Задайте OMI_APPLE_TEAM_ID или повторите запуск в Terminal; ввод Team ID будет скрыт.' >&2
        return 1
      fi
      read -r -s -p 'Team ID (10 символов; q — выйти): ' OMI_APPLE_TEAM_ID || return 1
      printf '\n' >&2
      [[ "$OMI_APPLE_TEAM_ID" != q && "$OMI_APPLE_TEAM_ID" != Q ]] || return 1
      continue
    fi
    if check_ios_signing; then
      export OMI_APPLE_TEAM_ID
      return 0
    fi
    [[ -t 0 && -t 1 ]] || return 1
    read -r -p 'Enter — проверить снова; t — другой Team ID; q — выйти: ' reply || return 1
    case "$reply" in q|Q) return 1 ;; t|T) unset OMI_APPLE_TEAM_ID ;; esac
  done
}

# Select a connected physical iOS device. Model names and device identifiers
# belong to this Mac's discovery result, never to a repository allowlist.
function select_ios_device() {
  local devices_json
  devices_json=$(flutter devices --machine 2>/dev/null) || {
    echo "❌ Could not query connected devices (flutter devices --machine failed)." >&2
    return 1
  }

  local ios_devices
  ios_devices=$(echo "$devices_json" | jq -ce '[.[] | select(.targetPlatform == "ios" and .emulator == false and .isSupported != false)]') || {
    echo "ERROR: Could not read the connected iOS device list." >&2
    return 1
  }
  local count
  count=$(echo "$ios_devices" | jq 'length')

  if [[ -n "${OMI_IOS_DEVICE_ID:-}" ]]; then
    local requested_device
    requested_device=$(echo "$ios_devices" | jq -r --arg id "$OMI_IOS_DEVICE_ID" '.[] | select(.id == $id) | .id')
    if [[ -z "$requested_device" ]]; then
      echo "❌ OMI_IOS_DEVICE_ID does not identify a currently available physical iOS device." >&2
      return 1
    fi
    echo "$requested_device"
    return 0
  fi

  if [[ "$count" -eq 0 ]]; then
    echo "❌ No supported physical iPhone found." >&2
    echo "   Connect and unlock the iPhone, trust this Mac and enable Developer Mode, then retry." >&2
    return 1
  fi

  if [[ "$count" -eq 1 ]]; then
    echo "$ios_devices" | jq -r '.[0].id'
    return 0
  fi

  echo "⚠️  Multiple iOS destinations found. Choose one:" >&2
  local i=0 line
  while IFS= read -r line; do
    i=$((i + 1))
    echo "   $i) $line" >&2
  done < <(echo "$ios_devices" | jq -r '.[] | "\(.sdk // "iOS") · \(.connectionInterface // "connected")"')

  # Never block on read without a TTY, or a non-interactive run (CI, nested
  # automation) hangs indefinitely instead of failing with a usable message.
  if [[ ! -t 0 ]]; then
    echo "   ❌ No terminal available to choose a device." >&2
    echo "      Disconnect the extras or set OMI_IOS_DEVICE_ID, then retry." >&2
    return 1
  fi

  local choice
  read -rp "   Enter number [1-${count}]: " choice
  if [[ "$choice" =~ ^[0-9]+$ ]] && [ "$choice" -ge 1 ] && [ "$choice" -le "$count" ]; then
    echo "$ios_devices" | jq -r ".[$((choice - 1))].id"
    return 0
  fi
  echo "❌ Invalid selection." >&2
  return 1
}

# #########
# Build iOS
# #########
function check_ios_phone() {
  local device_id devices device major details locked
  device_id=$(select_ios_device) || return 1
  # xcdevice includes old iOS releases that predate CoreDevice/Developer Mode.
  if ! devices=$(xcrun xcdevice list --timeout 10 2>/dev/null) ||
     ! device=$(printf '%s' "$devices" | jq -ce --arg id "$device_id" '.[] | select(.identifier == $id and .simulator == false and .platform == "com.apple.platform.iphoneos" and .available == true and .error == null)'); then
    echo 'Xcode не подтвердил готовность телефона. Подключите и разблокируйте iPhone, подтвердите доверие Mac.' >&2
    echo 'Откройте Xcode → Window → Devices and Simulators и дождитесь готовности. Xcode должен поддерживать iOS телефона.' >&2
    return 1
  fi
  major=$(printf '%s' "$device" | jq -r '.operatingSystemVersion // ""' | cut -d. -f1)
  if ! [[ "$major" =~ ^[0-9]+$ ]]; then
    echo 'Не удалось определить версию iOS. Проверьте телефон в Xcode → Window → Devices and Simulators.' >&2
    return 1
  fi
  if (( major >= 17 )); then
    # Stream JSON into memory, never a file containing device/account identifiers.
    if ! details=$(xcrun devicectl device info details --device "$device_id" --timeout 15 --json-output /dev/stdout --quiet 2>/dev/null) ||
       ! printf '%s' "$details" | jq -e '.result.connectionProperties.pairingState == "paired" and .result.deviceProperties.developerModeStatus == "enabled" and .result.deviceProperties.ddiServicesAvailable == true' >/dev/null 2>&1; then
      echo 'Готовность iPhone не подтверждена. Разблокируйте его, подтвердите доверие и дождитесь подготовки в Xcode.' >&2
      echo 'На iPhone: Настройки → Конфиденциальность и безопасность → Режим разработчика; перезагрузите и подтвердите включение.' >&2
      return 1
    fi
    if ! locked=$(xcrun devicectl device info lockState --device "$device_id" --timeout 15 --json-output /dev/stdout --quiet 2>/dev/null) ||
       ! printf '%s' "$locked" | jq -e '.result.unlockedSinceBoot == true and .result.passcodeRequired == false' >/dev/null 2>&1; then
      echo 'Не удалось подтвердить разблокировку iPhone. Разблокируйте экран и повторите проверку.' >&2
      return 1
    fi
  fi
  OMI_SELECTED_IOS_DEVICE="$device_id"
}

function prepare_ios_installation() {
  echo 'Проверяем Xcode, iOS SDK, Flutter и CocoaPods…' >&2
  ios_retry check_ios_prerequisites || return 1
  echo 'Инструменты готовы. Проверяем подпись…' >&2
  prepare_ios_signing || return 1
  echo 'Сертификат с закрытым ключом найден. Проверяем iPhone…' >&2
  ios_retry check_ios_phone || return 1
  echo 'Подготовка к установке на iPhone проверена.' >&2
}

function run_build_ios() {
  local flavor="${1:-dev}"
  shift || true
  if [[ "$flavor" != 'dev' ]]; then
    echo "ERROR: the Personal Team overlay forbids production/mobile-beta iOS builds." >&2
    return 1
  fi
  if [[ "${OMI_RUNTIME_MODE:-}" != 'offline' ]]; then
    echo "ERROR: the Personal Team overlay requires OMI_RUNTIME_MODE=offline; use 'bash setup.sh ios personal'." >&2
    return 1
  fi
  local profile='local_dev'
  local api_base_url="$LOCAL_API_BASE_URL"
  case "$flavor" in
    dev) ;;
    *)
      echo "ERROR: unsupported mobile flavor '$flavor' (expected dev or prod)." >&2
      return 1
      ;;
  esac
  validate_flutter_profile_arg "$profile" "$@" || return 1
  local flutter_args=(
    "--dart-define=OMI_APP_PROFILE=$profile"
    "--dart-define=OMI_RUNTIME_MODE=offline"
    "--dart-define=OMI_LOCAL_TEST_USER=${OMI_LOCAL_TEST_USER:-alice}"
  )
  local arg
  for arg in "$@"; do
    # The wrapper owns this invariant and injects exactly one profile define.
    if [[ "$arg" == --dart-define=OMI_APP_PROFILE=* ]]; then
      continue
    fi
    if [[ "$arg" == --dart-define=OMI_RUNTIME_MODE=* ]]; then
      if [[ "$arg" != '--dart-define=OMI_RUNTIME_MODE=offline' ]]; then
        echo "ERROR: the Personal Team overlay requires OMI_RUNTIME_MODE=offline." >&2
        return 1
      fi
      continue
    fi
    if [[ "$arg" == --dart-define=OMI_LOCAL_TEST_USER=* ]]; then
      echo "ERROR: set OMI_LOCAL_TEST_USER in the environment; the wrapper owns its Dart define." >&2
      return 1
    fi
    flutter_args+=("$arg")
  done
  prepare_ios_installation || return 1
  local device_id="$OMI_SELECTED_IOS_DEVICE"
  personal_bundle_id >/dev/null || return 1
  write_personal_team_config \
    && prepare_mobile_build_env "$flavor" "$api_base_url" offline \
    && setup_firebase \
    && bash scripts/generate_ios_dev_info_plist.sh ios/Runner/Info.plist ios/Runner/Info-Dev.plist personal \
    && generate_ios_custom_config Dev omi-dev true || return 1
  prepare_personal_ios_build_dir || return 1
  flutter pub get \
    && bash scripts/apply_personal_ios_plugin_overlay.sh \
    && pushd ios && pod install --repo-update && popd \
    && dart run build_runner build \
    && flutter run --profile --flavor "$flavor" -d "$device_id" "${flutter_args[@]}" 2>&1 \
      | sed -u "s/${OMI_APPLE_TEAM_ID}/<redacted-team>/g"
}


if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
case "${1:-}" in
  ios)
    cd "$(dirname "${BASH_SOURCE[0]}")"
    source ../scripts/macos-runtime.sh
    omi_require_apple_silicon "$PWD/setup.sh" "$@"
    omi_macos_path
    if [[ "${2:-}" == "personal" && "${3:-}" == --check && -z "${4:-}" ]]; then
      prepare_ios_installation
      echo 'Проверка завершена. Сборка и установка не запускались.'
    elif [[ "${2:-}" == "personal" && -z "${3:-}" ]]; then
      OMI_RUNTIME_MODE=offline run_build_ios dev \
          --dart-define=OMI_API_BASE_URL="$LOCAL_API_BASE_URL" \
          --dart-define=OMI_FIREBASE_AUTH_EMULATOR_HOST="$LOCAL_DEV_HOST"
    else
      echo "ERROR: this branch only supports 'bash setup.sh ios personal'." >&2
      exit 1
    fi
    ;;
  android)
    if [[ "${2:-}" == "beta" ]]; then
      if [[ -z "${FIREBASE_SERVICE_ACCOUNT_KEY:-}" ]]; then
        echo "android beta requires FIREBASE_SERVICE_ACCOUNT_KEY so the production Firebase app config can be generated." >&2
        exit 1
      fi
      prepare_mobile_build_env prod \
        && setup_keystore_android \
        && setup_firebase \
        && setup_firebase_with_service_account_android \
        && run_build_android prod
    else
      prepare_mobile_build_env dev \
        && setup_keystore_android \
        && setup_firebase \
        && run_build_android dev
    fi
    ;;
  *)
    echo "Unexpected platform '${1}'" >&2
    exit 1
    ;;
esac
fi
