#!/usr/bin/env bash
set -euo pipefail

SOURCE_PLIST="${1:-ios/Runner/Info.plist}"
OUTPUT_PLIST="${2:-ios/Runner/Info-Dev.plist}"
MODE="${3:-standard}"

if [[ ! -f "$SOURCE_PLIST" ]]; then
  echo "Missing source plist: $SOURCE_PLIST" >&2
  exit 1
fi

mkdir -p "$(dirname "$OUTPUT_PLIST")"
cp "$SOURCE_PLIST" "$OUTPUT_PLIST"
/usr/libexec/PlistBuddy -c 'Add :NSAppTransportSecurity dict' "$OUTPUT_PLIST"
/usr/libexec/PlistBuddy -c 'Add :NSAppTransportSecurity:NSAllowsLocalNetworking bool true' "$OUTPUT_PLIST"

if [[ "$MODE" == 'personal' ]]; then
  # iOS 17+ supports IPv4 CIDR exceptions; the paired-origin policy restricts
  # requests further to the selected host and port inside this encrypted VPN.
  # https://developer.apple.com/documentation/bundleresources/information-property-list/nsapptransportsecurity/nsexceptiondomains
  /usr/libexec/PlistBuddy -c 'Add :NSAppTransportSecurity:NSExceptionDomains dict' "$OUTPUT_PLIST"
  /usr/libexec/PlistBuddy -c 'Add :NSAppTransportSecurity:NSExceptionDomains:100.64.0.0/10 dict' "$OUTPUT_PLIST"
  /usr/libexec/PlistBuddy -c 'Add :NSAppTransportSecurity:NSExceptionDomains:100.64.0.0/10:NSExceptionAllowsInsecureHTTPLoads bool true' "$OUTPUT_PLIST"
  delete_key() {
    /usr/libexec/PlistBuddy -c "Delete :$1" "$OUTPUT_PLIST" >/dev/null 2>&1 || true
  }

  for key in \
    BGTaskSchedulerPermittedIdentifiers \
    CFBundleURLTypes \
    LSApplicationQueriesSchemes \
    MWDAT \
    NSHealthShareUsageDescription \
    NSHealthUpdateUsageDescription \
    NSLocationAlwaysAndWhenInUseUsageDescription \
    NSLocationUsageDescription \
    NSLocationWhenInUseUsageDescription \
    UISupportedExternalAccessoryProtocols \
    WKCompanionAppBundleIdentifier; do
    delete_key "$key"
  done

  delete_key UIBackgroundModes
  /usr/libexec/PlistBuddy -c 'Add :UIBackgroundModes array' "$OUTPUT_PLIST"
  /usr/libexec/PlistBuddy -c 'Add :UIBackgroundModes:0 string audio' "$OUTPUT_PLIST"
  /usr/libexec/PlistBuddy -c 'Add :UIBackgroundModes:1 string bluetooth-central' "$OUTPUT_PLIST"
  /usr/libexec/PlistBuddy -c 'Add :NSLocalNetworkUsageDescription string Omi Local connects only to the offline services running on your Mac.' "$OUTPUT_PLIST"
  /usr/libexec/PlistBuddy -c 'Add :FirebaseMessagingAutoInitEnabled bool false' "$OUTPUT_PLIST"
  /usr/libexec/PlistBuddy -c 'Add :FirebaseAppDelegateProxyEnabled bool false' "$OUTPUT_PLIST"
  /usr/libexec/PlistBuddy -c 'Add :FirebaseCrashlyticsCollectionEnabled bool false' "$OUTPUT_PLIST"
elif [[ "$MODE" != 'standard' ]]; then
  echo "Unsupported Info.plist mode: $MODE" >&2
  exit 1
fi
plutil -lint "$OUTPUT_PLIST" >/dev/null
