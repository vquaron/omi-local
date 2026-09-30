import 'dart:io';

import 'package:flutter/foundation.dart';

import 'package:omi/env/env.dart';

final class OfflineNetworkBlocked implements IOException {
  const OfflineNetworkBlocked(this.message);

  final String message;

  @override
  String toString() => 'OfflineNetworkBlocked: $message';
}

/// Process-wide network boundary for the Personal Team offline runtime.
///
/// The mobile app may talk only to the explicitly paired backend and Firebase
/// Auth emulator authorities. Merely being on a private network is not enough:
/// an arbitrary neighbouring host must not become an accidental fallback.
final class OfflineNetworkPolicy {
  OfflineNetworkPolicy._({required this.offline, required Set<_AllowedAuthority> authorities})
      : _authorities = Set.unmodifiable(authorities);

  factory OfflineNetworkPolicy.standard() => OfflineNetworkPolicy._(offline: false, authorities: const {});

  factory OfflineNetworkPolicy.tunnel(Uri uri) {
    final api = Env.parseLocalTunnelUrl(uri.toString());
    return OfflineNetworkPolicy._(offline: true, authorities: {
      _AllowedAuthority(api.host, api.port, secure: api.scheme == 'https', webSocketAllowed: true),
    });
  }

  factory OfflineNetworkPolicy.offline({
    required Uri apiBaseUri,
    required String authEmulatorHost,
    required int authEmulatorPort,
  }) {
    if (!Env.isPrivateOrLoopbackHost(apiBaseUri.host)) {
      throw StateError('Offline API authority must use a loopback/private host.');
    }
    if (!Env.isPrivateOrLoopbackHost(authEmulatorHost)) {
      throw StateError('Offline Auth emulator authority must use a loopback/private host.');
    }
    final apiPort = apiBaseUri.hasPort ? apiBaseUri.port : (apiBaseUri.scheme == 'https' ? 443 : 80);
    final apiSecure = apiBaseUri.scheme == 'https';
    return OfflineNetworkPolicy._(
      offline: true,
      authorities: {
        _AllowedAuthority(apiBaseUri.host, apiPort, secure: apiSecure, webSocketAllowed: true),
        _AllowedAuthority(authEmulatorHost, authEmulatorPort, secure: false, webSocketAllowed: false),
      },
    );
  }

  static OfflineNetworkPolicy _current = OfflineNetworkPolicy.standard();

  static OfflineNetworkPolicy get current => _current;

  static void installFromEnv() {
    if (!Env.isOfflineRuntime) {
      _current = OfflineNetworkPolicy.standard();
      HttpOverrides.global = null;
      return;
    }
    final api = Uri.parse(Env.apiBaseUrl!);
    final policy = Env.usesLocalTunnel
        ? OfflineNetworkPolicy.tunnel(api)
        : OfflineNetworkPolicy.offline(
            apiBaseUri: api,
            authEmulatorHost: Env.firebaseAuthEmulatorHost,
            authEmulatorPort: Env.firebaseAuthEmulatorPort,
          );
    _current = policy;
    HttpOverrides.global = OfflineHttpOverrides(policy, followCurrent: true);
  }

  final bool offline;
  final Set<_AllowedAuthority> _authorities;

  bool allows(Uri uri) {
    if (!offline) return true;
    final scheme = uri.scheme.toLowerCase();
    if (!const {'http', 'https', 'ws', 'wss'}.contains(scheme) || uri.host.isEmpty || uri.userInfo.isNotEmpty) {
      return false;
    }
    final secure = scheme == 'https' || scheme == 'wss';
    final webSocket = scheme == 'ws' || scheme == 'wss';
    final port = uri.hasPort ? uri.port : (secure ? 443 : 80);
    return _authorities.any(
      (authority) =>
          authority.host == uri.host.toLowerCase() &&
          authority.port == port &&
          authority.secure == secure &&
          (!webSocket || authority.webSocketAllowed),
    );
  }

  void requireAllowed(Uri uri) {
    if (allows(uri)) return;
    throw OfflineNetworkBlocked('outbound ${_safeAuthority(uri)} is not permitted in offline runtime');
  }

  /// External applications, browser views and custom URL schemes are outside
  /// the local runtime boundary even when opening them would not use Dart IO.
  bool get allowsExternalLaunch => !offline;

  @visibleForTesting
  static void installForTesting(OfflineNetworkPolicy policy) {
    _current = policy;
    HttpOverrides.global = policy.offline ? OfflineHttpOverrides(policy) : null;
  }

  @visibleForTesting
  static void resetForTesting() {
    _current = OfflineNetworkPolicy.standard();
    HttpOverrides.global = null;
  }

  static String _safeAuthority(Uri uri) {
    final host = uri.host.isEmpty ? '<invalid>' : uri.host;
    final port = uri.hasPort ? ':${uri.port}' : '';
    return '${uri.scheme}://$host$port';
  }
}

final class _AllowedAuthority {
  _AllowedAuthority(String host, this.port, {required this.secure, required this.webSocketAllowed})
      : host = host.toLowerCase();

  final String host;
  final int port;
  final bool secure;
  final bool webSocketAllowed;

  @override
  bool operator ==(Object other) =>
      other is _AllowedAuthority &&
      host == other.host &&
      port == other.port &&
      secure == other.secure &&
      webSocketAllowed == other.webSocketAllowed;

  @override
  int get hashCode => Object.hash(host, port, secure, webSocketAllowed);
}

final class OfflineHttpOverrides extends HttpOverrides {
  OfflineHttpOverrides(this.policy, {this.followCurrent = false});

  final OfflineNetworkPolicy policy;
  final bool followCurrent;

  @override
  HttpClient createHttpClient(SecurityContext? context) {
    final client = super.createHttpClient(context);
    client.findProxy = (uri) => findProxyFromEnvironment(uri, null);
    return client;
  }

  @override
  String findProxyFromEnvironment(Uri url, Map<String, String>? environment) {
    (followCurrent ? OfflineNetworkPolicy.current : policy).requireAllowed(url);
    return 'DIRECT';
  }
}
