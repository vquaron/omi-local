import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:omi/env/env.dart';
import 'package:omi/services/auth/local_mac_session.dart';
import 'package:omi/utils/offline_network_policy.dart';

enum LocalProcessingFailure { unavailable, unauthorized, unsupported, conflict, invalid, pairingChanged }

class LocalProcessingException implements Exception {
  const LocalProcessingException(this.failure);
  final LocalProcessingFailure failure;
}

/// Shared with the Mac library: drafts and effective snapshots are deliberately distinct.
class LocalProcessingSettings {
  LocalProcessingSettings.fromJson(Map<String, dynamic> data)
      : revision = data['revision'] as int,
        profiles = (data['profiles'] as List).map((item) => Map<String, dynamic>.from(item as Map)).toList(),
        effective = Map<String, dynamic>.from(data['effective'] as Map);

  final int revision;
  final List<Map<String, dynamic>> profiles;
  final Map<String, dynamic> effective;

  Map<String, dynamic>? active(String stage) => effective[stage] as Map<String, dynamic>?;
}

typedef LocalProcessingTransport = Future<Map<String, dynamic>> Function(
  String method,
  Uri uri,
  String authorization,
  Map<String, dynamic>? body,
  Duration timeout,
);

/// One request, one captured pairing, no redirects and no automatic mutation retries.
class LocalProcessingClient {
  LocalProcessingClient(
      {LocalMacSession? session, LocalProcessingTransport? transport, HttpClient Function()? httpClientFactory})
      : session = session ?? LocalMacSession.instance,
        _transport = transport,
        _httpClientFactory = httpClientFactory ?? HttpClient.new;

  final LocalMacSession session;
  final LocalProcessingTransport? _transport;
  final HttpClient Function() _httpClientFactory;

  Future<Map<String, dynamic>> request(String path, {String method = 'POST', Map<String, dynamic>? body}) async {
    if (!Env.isOfflineRuntime || !session.isSignedIn) {
      throw const LocalProcessingException(LocalProcessingFailure.unauthorized);
    }
    // Paths are selected by this client, never taken from provider URLs.
    if (!RegExp(r'^(/(save|activate|probe|models)|/profiles/[A-Za-z0-9_-]{1,64})?$').hasMatch(path)) {
      throw const LocalProcessingException(LocalProcessingFailure.invalid);
    }
    final address = session.address;
    final base = Env.parseLocalTunnelUrl(address);
    final uri = base.resolve('v1/local/processing$path');
    final authorization = session.authorizationFor(uri);
    var pairingChanged = false;
    void observePairing() {
      if (!session.isSignedIn || session.address != address || session.authorizationFor(uri) != authorization) {
        pairingChanged = true;
      }
    }

    session.addListener(observePairing);
    final timeout = Duration(
      seconds: path == '/activate'
          ? 720
          : (path == '/probe' || path == '/models')
              ? 130
              : 15,
    );
    try {
      final result = await (_transport ?? _http)(method, uri, authorization, body, timeout).timeout(timeout);
      observePairing();
      if (pairingChanged) throw const LocalProcessingException(LocalProcessingFailure.pairingChanged);
      return result;
    } finally {
      session.removeListener(observePairing);
    }
  }

  Future<LocalProcessingSettings> fetch() async => LocalProcessingSettings.fromJson(await request('', method: 'GET'));

  static Map<String, dynamic> draftPayload(Map<String, dynamic> profile, {String apiKey = '', bool clearKey = false}) =>
      {
        'profile': {
          if (profile['id'] != null) 'id': profile['id'],
          'name': (profile['name'] as String).trim(),
          'stage': profile['stage'],
          'kind': profile['kind'],
          'settings': Map<String, dynamic>.from(profile['settings'] as Map),
        },
        if (clearKey) 'clear_key': true else if (apiKey.trim().isNotEmpty) 'api_key': apiKey.trim(),
      };

  Future<Map<String, dynamic>> _http(
    String method,
    Uri uri,
    String authorization,
    Map<String, dynamic>? body,
    Duration timeout,
  ) async {
    return HttpOverrides.runWithHttpOverrides(() async {
      final client = _httpClientFactory()..connectionTimeout = const Duration(seconds: 15);
      try {
        return await (() async {
          final request = await client.openUrl(method, uri);
          request.followRedirects = false;
          request.headers.set(HttpHeaders.authorizationHeader, authorization);
          request.headers.contentType = ContentType.json;
          if (body != null) {
            final payload = utf8.encode(jsonEncode(body));
            if (payload.length > 64 * 1024) {
              throw const LocalProcessingException(LocalProcessingFailure.invalid);
            }
            // The paired bridge deliberately rejects chunked mutation requests.
            request.contentLength = payload.length;
            request.add(payload);
          }
          final response = await request.close();
          const maximum = 512 * 1024;
          if (response.contentLength > maximum) {
            throw const LocalProcessingException(LocalProcessingFailure.unavailable);
          }
          final bytes = <int>[];
          await for (final chunk in response) {
            if (bytes.length + chunk.length > maximum) {
              throw const LocalProcessingException(LocalProcessingFailure.unavailable);
            }
            bytes.addAll(chunk);
          }
          if (response.statusCode != 200) {
            throw LocalProcessingException(switch (response.statusCode) {
              401 => LocalProcessingFailure.unauthorized,
              404 || 405 || 501 => LocalProcessingFailure.unsupported,
              409 => LocalProcessingFailure.conflict,
              400 || 403 || 422 => LocalProcessingFailure.invalid,
              _ => LocalProcessingFailure.unavailable,
            });
          }
          return jsonDecode(utf8.decode(bytes)) as Map<String, dynamic>;
        })()
            .timeout(timeout);
      } finally {
        client.close(force: true);
      }
    }, OfflineHttpOverrides(OfflineNetworkPolicy.tunnel(uri.resolve('/'))));
  }
}
