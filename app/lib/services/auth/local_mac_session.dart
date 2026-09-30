import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:uuid/uuid.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/env/env.dart';
import 'package:omi/utils/offline_network_policy.dart';

class LocalMacUnauthorized implements Exception {}

typedef LocalProfileProbe = Future<Map<String, dynamic>> Function(Uri base, String key);

class LocalMacServer {
  const LocalMacServer({required this.id, required this.name, required this.address, required this.key});

  final String id;
  final String name;
  final String address;
  final String key;

  factory LocalMacServer.fromJson(Map<String, dynamic> value) => LocalMacServer(
        id: value['id'] as String,
        name: value['name'] as String,
        address: value['url'] as String,
        key: value['key'] as String,
      );

  Map<String, String> toJson() => {'id': id, 'name': name, 'url': address, 'key': key};
}

class LocalTranscriptionReadiness {
  final String live;
  final String finalTranscript;

  const LocalTranscriptionReadiness({this.live = 'unknown', this.finalTranscript = 'unknown'});

  bool get isReady => live == 'ready' && finalTranscript == 'ready';

  factory LocalTranscriptionReadiness.fromProfile(Map<String, dynamic> profile) {
    final value = profile['local_transcription'];
    String stage(String name) {
      final entry = value is Map ? value[name] : null;
      final status = entry is Map ? entry['status'] : null;
      return const {'ready', 'disabled', 'unavailable', 'busy'}.contains(status) ? status as String : 'unknown';
    }

    return LocalTranscriptionReadiness(live: stage('live'), finalTranscript: stage('final'));
  }
}

/// A local owner session, independent of Firebase's cached user and token timer.
class LocalMacSession extends ChangeNotifier {
  LocalMacSession({FlutterSecureStorage? storage, LocalProfileProbe? probe})
      : _storage = storage ??
            const FlutterSecureStorage(
              iOptions: IOSOptions(accessibility: KeychainAccessibility.first_unlock),
            ),
        _probe = probe ?? probeProfile;

  static final instance = LocalMacSession();
  static const storageKey = 'omi.localMacPairing.v1';
  static const settingsKey = 'omi.localMacSettings.v1';
  static const serversKey = 'omi.localMacServers.v1';
  final FlutterSecureStorage _storage;
  final LocalProfileProbe _probe;
  Uri? _base;
  String? _key;
  bool _rejected = false;
  bool _publishing = false;
  LocalTranscriptionReadiness readiness = const LocalTranscriptionReadiness();

  String get address => _base?.toString() ?? '';
  bool get isSignedIn => _key != null && !_rejected;
  String? get accessKey => isSignedIn ? _key : null;

  static String normalizeKey(String key) => key.replaceAll(RegExp(r'\s'), '');

  /// Explicit installer handoff only: no embedded credentials and no network activation.
  Future<void> importLaunchSettings() async {
    if (!Env.isOfflineRuntime || defaultTargetPlatform != TargetPlatform.iOS) return;
    final saved =
        await const MethodChannel('com.omi/environment').invokeMapMethod<String, String>('takeLocalMacSettings');
    if (saved == null) return;
    final url = Env.parseLocalTunnelUrl(saved['url'] ?? '');
    final key = normalizeKey(saved['key'] ?? '');
    if (!RegExp(r'^[A-Za-z0-9_-]{43}$').hasMatch(key)) throw LocalMacUnauthorized();
    await saveSettings(url.toString(), key);
  }

  /// Editing settings never activates a server or changes the authenticated origin.
  Future<({String address, String key, String name, String? serverId})> readSettings() async {
    final raw = await _storage.read(key: settingsKey);
    if (raw != null) {
      final saved = jsonDecode(raw) as Map<String, dynamic>;
      return (
        address: saved['url'] as String,
        key: saved['key'] as String,
        name: saved['name'] as String? ?? '',
        serverId: saved['serverId'] as String?,
      );
    }
    return (address: address, key: _key ?? '', name: '', serverId: null);
  }

  Future<void> saveSettings(String address, String key, {String name = '', String? serverId}) async {
    if (!Env.isOfflineRuntime) throw StateError('Local Mac requires offline runtime');
    await _storage.write(
        key: settingsKey,
        value:
            jsonEncode({'url': address.trim(), 'key': normalizeKey(key), 'name': name.trim(), 'serverId': serverId}));
  }

  /// Saved entries are credentials, not sessions. Migration never authenticates.
  Future<List<LocalMacServer>> readServers() async {
    if (!Env.isOfflineRuntime) throw StateError('Local Mac requires offline runtime');
    final raw = await _storage.read(key: serversKey);
    if (raw != null) {
      return (jsonDecode(raw) as List).map((value) => LocalMacServer.fromJson(value as Map<String, dynamic>)).toList();
    }
    final servers = <LocalMacServer>[];
    for (final legacyKey in [settingsKey, storageKey]) {
      final legacy = await _storage.read(key: legacyKey);
      if (legacy == null) continue;
      final saved = jsonDecode(legacy) as Map<String, dynamic>;
      final address = saved['url'] as String? ?? '';
      final key = saved['key'] as String? ?? '';
      if (address.isEmpty || key.isEmpty) continue;
      String canonical(String address) {
        try {
          return Env.parseLocalTunnelUrl(address).toString();
        } on FormatException {
          return address.trim(); // Preserve unfinished drafts for editing.
        }
      }

      if (servers.any((server) => canonical(server.address) == canonical(address) && server.key == key)) continue;
      servers.add(LocalMacServer(id: const Uuid().v4(), name: address, address: address, key: key));
    }
    await _writeServers(servers);
    return servers;
  }

  Future<void> _writeServers(List<LocalMacServer> servers) =>
      _storage.write(key: serversKey, value: jsonEncode(servers.map((server) => server.toJson()).toList()));

  Future<LocalMacServer> saveServer(
      {String? id, required String name, required String address, required String key}) async {
    final servers = await readServers();
    address = address.trim();
    if (address.isEmpty) throw const FormatException('Server address is required');
    final index = id == null ? -1 : servers.indexWhere((server) => server.id == id);
    if (id != null && index < 0) throw StateError('Saved server no longer exists');
    final server = LocalMacServer(
        id: id ?? const Uuid().v4(),
        name: name.trim().isEmpty ? address : name.trim(),
        address: address,
        key: normalizeKey(key));
    if (index < 0) {
      servers.add(server);
    } else {
      servers[index] = server;
    }
    await _writeServers(servers);
    return server;
  }

  Future<void> deleteServer(String id) async {
    final servers = await readServers();
    servers.removeWhere((server) => server.id == id);
    await _writeServers(servers);
  }

  Future<void> restore() async {
    if (!Env.isOfflineRuntime) return;
    final raw = await _storage.read(key: storageKey);
    if (raw == null) return;
    try {
      final saved = jsonDecode(raw) as Map<String, dynamic>;
      _base = Env.parseLocalTunnelUrl(saved['url'] as String);
      final key = saved['key'] as String?;
      _key = key != null && RegExp(r'^[A-Za-z0-9_-]{43}$').hasMatch(key) ? key : null;
      _rejected = saved['rejected'] == true;
      _activate();
    } on FormatException {
      _key = null;
    } on TypeError {
      _key = null;
    }
  }

  void _activate() {
    Env.localTunnelConfigured = true;
    Env.overrideApiBaseUrl(_base!.toString());
    if (isSignedIn) SharedPreferencesUtil().uid = 'alice';
  }

  Future<void> connect(String address, String key) async {
    if (!Env.isOfflineRuntime) throw StateError('Local Mac requires offline runtime');
    key = normalizeKey(key);
    final base = Env.parseLocalTunnelUrl(address);
    if (!RegExp(r'^[A-Za-z0-9_-]{43}$').hasMatch(key)) throw LocalMacUnauthorized();
    final profile = await _probe(base, key);
    if (profile['uid'] != 'alice') throw LocalMacUnauthorized();
    // Persist one atomic Keychain record before publishing a new session.
    _publishing = true;
    try {
      await _storage.write(key: storageKey, value: jsonEncode({'url': base.toString(), 'key': key}));
    } finally {
      _publishing = false;
    }
    _base = base;
    _key = key;
    _rejected = false;
    readiness = LocalTranscriptionReadiness.fromProfile(profile);
    _activate();
    OfflineNetworkPolicy.installFromEnv();
    notifyListeners();
  }

  bool permits(Uri uri) =>
      _base != null &&
      (_base!.scheme == 'https'
          ? uri.scheme == 'https' || uri.scheme == 'wss'
          : uri.scheme == 'http' || uri.scheme == 'ws') &&
      uri.userInfo.isEmpty &&
      uri.host == _base!.host &&
      (uri.hasPort ? uri.port : (_base!.scheme == 'https' ? 443 : 80)) == _base!.port;

  String authorizationFor(Uri uri) {
    if (!permits(uri) || !isSignedIn) throw LocalMacUnauthorized();
    return 'Bearer $_key';
  }

  Future<void> rejectKey() async {
    if (_rejected) return;
    _rejected = true;
    notifyListeners();
    await _storage.write(key: storageKey, value: jsonEncode({'url': address, 'key': _key, 'rejected': true}));
  }

  Future<void> rejectRequest(Uri uri, String? authorization) async {
    // A late response from the previous address/key cannot expire a new pairing.
    if (_publishing || !permits(uri) || authorization != 'Bearer $_key') return;
    await rejectKey();
  }

  Future<void> signOut() async {
    _key = null;
    _rejected = true;
    notifyListeners();
    await _storage.write(key: storageKey, value: jsonEncode({'url': address, 'rejected': true}));
    await _storage.delete(key: settingsKey);
    await _storage.delete(key: serversKey);
  }

  static Future<Map<String, dynamic>> probeProfile(Uri base, String key) =>
      HttpOverrides.runWithHttpOverrides(() async {
        final client = HttpClient()..connectionTimeout = const Duration(seconds: 8);
        try {
          final request = await client.getUrl(base.resolve('v1/users/profile')).timeout(const Duration(seconds: 8));
          request.followRedirects = false;
          request.headers.set(HttpHeaders.authorizationHeader, 'Bearer $key');
          final response = await request.close().timeout(const Duration(seconds: 8));
          if (response.statusCode == 401 || response.statusCode == 403) throw LocalMacUnauthorized();
          if (response.statusCode != 200) throw const HttpException('Local Mac unavailable');
          final bytes = <int>[];
          await for (final chunk in response.timeout(const Duration(seconds: 8))) {
            bytes.addAll(chunk);
            if (bytes.length > 262144) throw const FormatException('Invalid local profile');
          }
          return jsonDecode(utf8.decode(bytes)) as Map<String, dynamic>;
        } finally {
          client.close(force: true);
        }
      }, OfflineHttpOverrides(OfflineNetworkPolicy.tunnel(base)));
}
