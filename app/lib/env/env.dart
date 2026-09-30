import 'package:flutter/foundation.dart';

import 'package:omi/flavors.dart';

import 'environment_profile.dart';

enum OmiRuntimeMode {
  standard,
  offline;

  static OmiRuntimeMode parse(String value) {
    return switch (value) {
      '' || 'standard' => OmiRuntimeMode.standard,
      'offline' => OmiRuntimeMode.offline,
      _ => throw StateError('Unknown OMI_RUNTIME_MODE "$value". Use standard or offline.'),
    };
  }
}

abstract class Env {
  static const productionApiBaseUrl = 'https://api.omi.me/';
  static const _apiBaseUrlFromDefine = String.fromEnvironment('OMI_API_BASE_URL');
  static const _runtimeModeFromDefine = String.fromEnvironment('OMI_RUNTIME_MODE', defaultValue: 'standard');
  static const localTestUser = String.fromEnvironment('OMI_LOCAL_TEST_USER', defaultValue: 'alice');
  static const firebaseAuthEmulatorHost = String.fromEnvironment(
    'OMI_FIREBASE_AUTH_EMULATOR_HOST',
    defaultValue: '127.0.0.1',
  );
  static const _firebaseAuthEmulatorPort = String.fromEnvironment(
    'OMI_FIREBASE_AUTH_EMULATOR_PORT',
    defaultValue: '9099',
  );
  static late final EnvFields _instance;
  static String? _apiBaseUrlOverride;
  static bool isTestFlight = false;
  static bool localTunnelConfigured = false;
  static bool get usesLocalTunnel => isOfflineRuntime && localTunnelConfigured;

  static bool isTailscaleIpv4(String host) {
    final parts = host.split('.');
    if (parts.length != 4) return false;
    final octets = <int>[];
    for (final part in parts) {
      final octet = int.tryParse(part);
      if (octet == null || octet < 0 || octet > 255 || part != octet.toString()) return false;
      octets.add(octet);
    }
    return octets[0] == 100 && octets[1] >= 64 && octets[1] <= 127;
  }

  static Uri parseLocalTunnelUrl(String value) {
    final address = value.trim();
    final tailscale = RegExp(r'^(?:http://)?([0-9.]+)(?::([0-9]+))?/?$').firstMatch(address);
    if (tailscale != null && isTailscaleIpv4(tailscale[1]!)) {
      final port = tailscale[2] == null ? (address.startsWith('http://') ? 80 : 20000) : int.tryParse(tailscale[2]!);
      if (port != null && port > 0 && port <= 65535) {
        return Uri(scheme: 'http', host: tailscale[1], port: port, path: '/');
      }
    }
    final uri = Uri.tryParse(address);
    if (uri == null ||
        uri.scheme != 'https' ||
        uri.host.isEmpty ||
        !uri.host.contains('.') ||
        uri.userInfo.isNotEmpty ||
        uri.port != 443 ||
        uri.hasQuery ||
        uri.hasFragment ||
        (uri.path.isNotEmpty && uri.path != '/') ||
        isPrivateOrLoopbackHost(uri.host)) {
      throw const FormatException('Enter a Tailscale IPv4 address with an optional port, or an HTTPS server address.');
    }
    return Uri(scheme: 'https', host: uri.host.toLowerCase(), path: '/');
  }

  static AppEnvironmentProfile get profile =>
      AppEnvironmentProfile.forFlavor(productionFlavor: F.env == Environment.prod);

  static OmiRuntimeMode? _testRuntimeMode;
  @visibleForTesting
  static void setRuntimeModeForTesting(OmiRuntimeMode? mode) => _testRuntimeMode = mode;
  static OmiRuntimeMode get runtimeMode => _testRuntimeMode ?? OmiRuntimeMode.parse(_runtimeModeFromDefine);

  static bool get isOfflineRuntime => runtimeMode == OmiRuntimeMode.offline;

  static void init(EnvFields instance) {
    _instance = instance;
  }

  static void overrideApiBaseUrl(String url) {
    _apiBaseUrlOverride = url;
  }

  static void clearApiBaseUrlOverrideForTesting() {
    _apiBaseUrlOverride = null;
  }

  static String? get posthogApiKey => isOfflineRuntime ? null : _instance.posthogApiKey;

  // static String? get apiBaseUrl => 'https://omi-backend.ngrok.app/';
  static String? get apiBaseUrl {
    if (_apiBaseUrlOverride != null) return _apiBaseUrlOverride;
    if (_apiBaseUrlFromDefine.isNotEmpty) return _apiBaseUrlFromDefine;
    final configuredApiBaseUrl = _instance.apiBaseUrl;
    if (configuredApiBaseUrl != null && configuredApiBaseUrl.isNotEmpty) {
      return configuredApiBaseUrl;
    }
    return profile.defaultApiBaseUrl;
  }

  static int get firebaseAuthEmulatorPort => int.tryParse(_firebaseAuthEmulatorPort) ?? 9099;

  static String get authCallbackScheme => profile.authCallbackScheme;

  static String get authRedirectUri => '$authCallbackScheme://auth/callback';

  /// OAuth remains on the production identity plane even when mobile Beta
  /// uses the development serving API for product traffic.
  static String get authApiBaseUrl => authApiBaseUrlForProfile(profile, servingApiBaseUrl: apiBaseUrl);

  static String authApiBaseUrlForProfile(AppEnvironmentProfile configuredProfile, {String? servingApiBaseUrl}) {
    if (configuredProfile == AppEnvironmentProfile.mobileBeta) {
      return productionApiBaseUrl;
    }
    return servingApiBaseUrl ?? configuredProfile.defaultApiBaseUrl;
  }

  static void validateProfilePairing() {
    final productionFlavor = F.env == Environment.prod;
    if (!productionFlavor && profile != AppEnvironmentProfile.localDev) {
      throw StateError('Profile ${profile.name} must be built with the prod flavor.');
    }
    if (productionFlavor && profile == AppEnvironmentProfile.localDev) {
      throw StateError('The prod flavor cannot use the local_dev profile.');
    }
    if (isOfflineRuntime) {
      validateOfflineRuntimePolicy(
        configuredProfile: profile,
        configuredApiBaseUrl: apiBaseUrl,
        authEmulatorHost: firebaseAuthEmulatorHost,
        testUser: localTestUser,
        tunnel: usesLocalTunnel,
      );
    }
  }

  static void validateOfflineRuntimePolicy({
    required AppEnvironmentProfile configuredProfile,
    required String? configuredApiBaseUrl,
    required String authEmulatorHost,
    required String testUser,
    bool tunnel = false,
  }) {
    if (configuredProfile != AppEnvironmentProfile.localDev) {
      throw StateError('Offline runtime requires OMI_APP_PROFILE=local_dev.');
    }
    if (tunnel) {
      parseLocalTunnelUrl(configuredApiBaseUrl ?? '');
      if (testUser != 'alice') throw StateError('Local tunnel requires the synthetic local owner.');
      return;
    }
    final apiUri = Uri.tryParse((configuredApiBaseUrl ?? '').trim());
    if (apiUri == null ||
        (apiUri.scheme != 'http' && apiUri.scheme != 'https') ||
        !isPrivateOrLoopbackHost(apiUri.host)) {
      throw StateError('Offline runtime requires a loopback/private OMI_API_BASE_URL.');
    }
    final normalizedAuthHost = authEmulatorHost.trim().toLowerCase();
    if (!isPrivateOrLoopbackHost(normalizedAuthHost)) {
      throw StateError('Offline runtime requires a loopback/private Firebase Auth emulator host.');
    }
    if (apiUri.host.toLowerCase() != normalizedAuthHost) {
      throw StateError('Offline runtime requires the API and Firebase Auth emulator to use the same host.');
    }
    if (testUser != 'alice') {
      throw StateError('Offline runtime only permits the synthetic local user "alice".');
    }
  }

  static void requireExternalAuthAllowed() {
    requireExternalAuthAllowedFor(runtimeMode);
  }

  static void requireExternalAuthAllowedFor(OmiRuntimeMode mode) {
    if (mode == OmiRuntimeMode.offline) {
      throw StateError('External OAuth is disabled in offline runtime.');
    }
  }

  static void validateFirebaseProject({required String projectId, AppEnvironmentProfile? configuredProfile}) {
    final effectiveProfile = configuredProfile ?? profile;
    if (projectId != effectiveProfile.firebaseProjectId) {
      throw StateError(
        'Mobile profile ${effectiveProfile.name} requires Firebase project ${effectiveProfile.firebaseProjectId}, '
        'but the app was initialized with $projectId.',
      );
    }
  }

  /// Production-family packages have one pinned backend authority. This runs
  /// during startup so a misconfigured signing group fails before networking.
  static void validateStartupRouting({
    required bool productionFamily,
    String? configuredApiBaseUrl,
    AppEnvironmentProfile? configuredProfile,
    bool releaseBuild = kReleaseMode,
  }) {
    final effectiveProfile = configuredProfile ?? (productionFamily ? AppEnvironmentProfile.production : profile);
    final normalized = (configuredApiBaseUrl ?? apiBaseUrl ?? '').trim().replaceFirst(RegExp(r'/+$'), '');
    final expected = effectiveProfile.defaultApiBaseUrl.replaceFirst(RegExp(r'/+$'), '');

    if (effectiveProfile == AppEnvironmentProfile.localDev) {
      if (usesLocalTunnel && !productionFamily) {
        parseLocalTunnelUrl(normalized);
        return;
      }
      if (!_isLocalDevelopmentApi(normalized)) {
        throw StateError(
          'Profile local_dev requires a loopback or private-network API endpoint; '
          'use mobile_beta for https://api.omiapi.com/.',
        );
      }
      return;
    }

    if (effectiveProfile == AppEnvironmentProfile.localProd) {
      if (releaseBuild) {
        throw StateError('Profile local_prod is only available in debug builds.');
      }
      final uri = Uri.tryParse(normalized);
      if (uri == null || uri.host.isEmpty || (uri.scheme != 'http' && uri.scheme != 'https')) {
        throw StateError('Profile local_prod requires a valid http(s) API endpoint.');
      }
      return;
    }

    if (normalized != expected) {
      throw StateError('Profile ${effectiveProfile.name} requires API_BASE_URL=${effectiveProfile.defaultApiBaseUrl}');
    }
  }

  static void requireProductionRouting() => validateStartupRouting(productionFamily: true);

  static bool _isLocalDevelopmentApi(String base) {
    final uri = Uri.tryParse(base);
    if (uri == null || uri.host.isEmpty || (uri.scheme != 'http' && uri.scheme != 'https')) {
      return false;
    }
    final host = uri.host.toLowerCase();
    if (host == 'localhost' || host == 'host.docker.internal' || host == '::1') {
      return true;
    }
    return isPrivateOrLoopbackHost(host);
  }

  static bool isPrivateOrLoopbackHost(String host) {
    final normalized = host.trim().toLowerCase().replaceAll(RegExp(r'^\[|\]$'), '');
    if (normalized == 'localhost' || normalized == '::1') return true;
    if (normalized.contains(':')) {
      return normalized.startsWith('fc') || normalized.startsWith('fd') || RegExp(r'^fe[89ab]').hasMatch(normalized);
    }
    final octets = normalized.split('.').map(int.tryParse).toList();
    if (octets.length != 4 || octets.any((octet) => octet == null || octet < 0 || octet > 255)) {
      return false;
    }
    final first = octets[0]!;
    final second = octets[1]!;
    return first == 10 ||
        (first == 172 && second >= 16 && second <= 31) ||
        (first == 192 && second == 168) ||
        // 100.64.0.0/10 — RFC 6598 shared address space, the range Tailscale
        // assigns. Included because a physical device has no other route to a
        // developer's local harness: the harness binds loopback only by design,
        // so the device cannot use 127.x, and a plain LAN address does not reach
        // it either. Bounded to the real /10 — 100.63.x and 100.128.x are public.
        (first == 100 && second >= 64 && second <= 127) ||
        (first == 127);
  }

  static String? get googleMapsApiKey => _instance.googleMapsApiKey;

  static String? get intercomAppId => isOfflineRuntime ? null : _instance.intercomAppId;

  static String? get intercomIOSApiKey => isOfflineRuntime ? null : _instance.intercomIOSApiKey;

  static String? get intercomAndroidApiKey => isOfflineRuntime ? null : _instance.intercomAndroidApiKey;

  static String? get googleClientId => _instance.googleClientId;

  static String? get googleClientSecret => _instance.googleClientSecret;

  static bool get useWebAuth => _instance.useWebAuth ?? false;

  static bool get useAuthCustomToken => _instance.useAuthCustomToken ?? false;
}

abstract class EnvFields {
  String? get posthogApiKey;

  String? get apiBaseUrl;

  String? get googleMapsApiKey;

  String? get intercomAppId;

  String? get intercomIOSApiKey;

  String? get intercomAndroidApiKey;

  String? get googleClientId;

  String? get googleClientSecret;

  bool? get useWebAuth;

  bool? get useAuthCustomToken;
}
