import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:crypto/crypto.dart';
import 'package:uuid/uuid.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/env/env.dart';
import 'package:omi/utils/offline_network_policy.dart';

class LocalMacUnauthorized implements Exception {}

class LocalMacInvalidKey extends LocalMacUnauthorized {}

class LocalMacInvalidAddress implements Exception {}

class LocalMacPairingChanged implements Exception {}

class LocalAudioDeliveryUnsupported implements Exception {}

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

class _LocalServerCatalog {
  _LocalServerCatalog(this.servers, this.seenPresets);
  final List<LocalMacServer> servers;
  final Set<String> seenPresets;

  factory _LocalServerCatalog.decode(String raw) {
    final decoded = jsonDecode(raw);
    if (decoded is List) {
      return _LocalServerCatalog(
        decoded.map((value) => LocalMacServer.fromJson(value as Map<String, dynamic>)).toList(),
        {},
      );
    }
    if (decoded is! Map<String, dynamic> ||
        decoded['version'] != 2 ||
        decoded['servers'] is! List ||
        decoded['seen_presets'] is! List) {
      throw const FormatException('Invalid saved server catalog');
    }
    return _LocalServerCatalog(
      (decoded['servers'] as List).map((value) => LocalMacServer.fromJson(value as Map<String, dynamic>)).toList(),
      (decoded['seen_presets'] as List).cast<String>().toSet(),
    );
  }

  String encode() => jsonEncode({
    'version': 2,
    'servers': servers.map((server) => server.toJson()).toList(),
    'seen_presets': seenPresets.toList(),
  });
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
    : _storage =
          storage ??
          const FlutterSecureStorage(iOptions: IOSOptions(accessibility: KeychainAccessibility.first_unlock)),
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
  Future<void> _serverEdits = Future<void>.value();
  Future<void> _connectionEdits = Future<void>.value();
  int _connectionAttempt = 0;
  int _audioDeliveryVersion = 0;
  bool get supportsAudioDelivery => isSignedIn && _audioDeliveryVersion == 2;
  LocalTranscriptionReadiness readiness = const LocalTranscriptionReadiness();

  String get address => _base?.toString() ?? '';
  bool get isSignedIn => _key != null && !_rejected && !_publishing;
  String? get accessKey => isSignedIn ? _key : null;

  static String normalizeKey(String key) => key.replaceAll(RegExp(r'\s'), '');

  /// Explicit installer handoff only: no embedded credentials and no network activation.
  Future<void> importLaunchSettings() async {
    if (!Env.isOfflineRuntime || defaultTargetPlatform != TargetPlatform.iOS) return;
    final saved = await const MethodChannel(
      'com.omi/environment',
    ).invokeMapMethod<String, String>('takeLocalMacSettings');
    if (saved == null) return;
    final encoded = saved['server_presets_b64'];
    if (encoded == null) throw const FormatException('Invalid local server presets');
    await importServerPresets(encoded);
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
    if (address.isEmpty) {
      final servers = await readServers();
      if (servers.isNotEmpty) {
        final first = servers.first;
        return (address: first.address, key: first.key, name: first.name, serverId: first.id);
      }
    }
    return (address: address, key: _key ?? '', name: '', serverId: null);
  }

  Future<void> saveSettings(String address, String key, {String name = '', String? serverId}) async {
    if (!Env.isOfflineRuntime) throw StateError('Local Mac requires offline runtime');
    await _storage.write(
      key: settingsKey,
      value: jsonEncode({'url': address.trim(), 'key': normalizeKey(key), 'name': name.trim(), 'serverId': serverId}),
    );
  }

  Future<T> _editServers<T>(Future<T> Function() operation) {
    final result = _serverEdits.then((_) => operation());
    _serverEdits = result.then<void>((_) {}, onError: (Object _, StackTrace __) {});
    return result;
  }

  /// Saved entries are credentials, not sessions. Migration never authenticates.
  Future<_LocalServerCatalog> _loadServerCatalog() async {
    if (!Env.isOfflineRuntime) throw StateError('Local Mac requires offline runtime');
    final raw = await _storage.read(key: serversKey);
    if (raw != null) {
      return _LocalServerCatalog.decode(raw);
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
    return _LocalServerCatalog(servers, {});
  }

  Future<void> _writeServerCatalog(_LocalServerCatalog catalog) =>
      _storage.write(key: serversKey, value: catalog.encode());

  Future<List<LocalMacServer>> readServers() => _editServers(() async {
    final catalog = await _loadServerCatalog();
    if (await _storage.read(key: serversKey) == null) await _writeServerCatalog(catalog);
    return List<LocalMacServer>.unmodifiable(catalog.servers);
  });

  /// One private installer payload, validated completely before a single atomic
  /// Keychain write. Seen IDs protect manual edits and deleted presets forever.
  Future<void> importServerPresets(String encoded) async {
    if (!Env.isOfflineRuntime) throw StateError('Local Mac requires offline runtime');
    try {
      await _importServerPresets(encoded);
    } on FormatException {
      // Parser exceptions retain their source; never let a private payload
      // enter startup diagnostics, including malformed JSON or Base64 input.
      throw const FormatException('Invalid local server presets');
    }
  }

  Future<void> _importServerPresets(String encoded) async {
    if (encoded.length > 44 * 1024) throw const FormatException('Invalid local server presets');
    final bytes = base64Decode(encoded);
    if (bytes.length > 32 * 1024) throw const FormatException('Invalid local server presets');
    final document = jsonDecode(utf8.decode(bytes));
    if (document is! Map<String, dynamic> ||
        document['version'] != 1 ||
        document.keys.toSet().difference({'version', 'servers'}).isNotEmpty ||
        document['servers'] is! List) {
      throw const FormatException('Invalid local server presets');
    }
    final values = document['servers'] as List;
    if (values.isEmpty || values.length > 16) throw const FormatException('Invalid local server presets');
    final ids = <String>{};
    final presets = <LocalMacServer>[];
    for (final value in values) {
      if (value is! Map<String, dynamic> ||
          value.keys.toSet().difference({'id', 'name', 'url', 'key'}).isNotEmpty ||
          value['id'] is! String ||
          value['name'] is! String ||
          value['url'] is! String ||
          value['key'] is! String) {
        throw const FormatException('Invalid local server presets');
      }
      final id = value['id'] as String;
      final name = (value['name'] as String).trim();
      final key = normalizeKey(value['key'] as String);
      if (!RegExp(r'^[a-z][a-z0-9_-]{0,31}$').hasMatch(id) ||
          !ids.add(id) ||
          name.isEmpty ||
          name.runes.length > 80 ||
          name.runes.any((rune) => rune < 32 || rune == 127) ||
          !RegExp(r'^[A-Za-z0-9_-]{43}$').hasMatch(key)) {
        throw const FormatException('Invalid local server presets');
      }
      presets.add(
        LocalMacServer(
          id: id,
          name: name,
          address: Env.parseLocalTunnelUrl(value['url'] as String).toString(),
          key: key,
        ),
      );
    }
    await _editServers(() async {
      final catalog = await _loadServerCatalog();
      var changed = false;
      for (final preset in presets) {
        if (catalog.seenPresets.contains(preset.id)) continue;
        if (!catalog.servers.any(
          (server) => server.id == preset.id || _canonicalSavedAddress(server.address) == preset.address,
        )) {
          catalog.servers.add(preset);
        }
        catalog.seenPresets.add(preset.id);
        changed = true;
      }
      if (changed) await _writeServerCatalog(catalog);
    });
  }

  static String _canonicalSavedAddress(String address) {
    try {
      return Env.parseLocalTunnelUrl(address).toString();
    } on FormatException {
      return address.trim();
    }
  }

  Future<LocalMacServer> saveServer({String? id, required String name, required String address, required String key}) =>
      _editServers(() async {
        final catalog = await _loadServerCatalog();
        final servers = catalog.servers;
        address = address.trim();
        if (address.isEmpty) throw const FormatException('Server address is required');
        final index = id == null ? -1 : servers.indexWhere((server) => server.id == id);
        if (id != null && index < 0) throw StateError('Saved server no longer exists');
        final server = LocalMacServer(
          id: id ?? const Uuid().v4(),
          name: name.trim().isEmpty ? address : name.trim(),
          address: address,
          key: normalizeKey(key),
        );
        if (index < 0) {
          servers.add(server);
        } else {
          servers[index] = server;
        }
        await _writeServerCatalog(catalog);
        return server;
      });

  Future<void> deleteServer(String id) => _editServers(() async {
    final catalog = await _loadServerCatalog();
    catalog.servers.removeWhere((server) => server.id == id);
    catalog.seenPresets.add(id);
    await _writeServerCatalog(catalog);
  });

  Future<void> restore() async {
    _audioDeliveryVersion = 0;
    if (!Env.isOfflineRuntime) return;
    final raw = await _storage.read(key: storageKey);
    if (raw == null) return;
    try {
      final saved = jsonDecode(raw) as Map<String, dynamic>;
      _base = Env.parseLocalTunnelUrl(saved['url'] as String);
      final key = saved['key'] as String?;
      _key = key != null && RegExp(r'^[A-Za-z0-9_-]{43}$').hasMatch(key) ? key : null;
      _rejected = saved['rejected'] == true;
      if (isSignedIn) await _selectConversationCache(adoptLegacy: true);
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

  Future<void> _selectConversationCache({Uri? base, String? key, bool adoptLegacy = false}) =>
      SharedPreferencesUtil().selectLocalConversationCache(
        sha256.convert(utf8.encode(jsonEncode([(base ?? _base!).toString(), key ?? _key!]))).toString(),
        adoptLegacy: adoptLegacy,
      );

  Future<void> _editConnection(Future<void> Function() operation) {
    final result = _connectionEdits.then((_) => operation());
    _connectionEdits = result.onError((Object _, StackTrace __) {});
    return result;
  }

  Future<void> connect(String address, String key, {Future<void> Function()? beforeSwitch}) async {
    if (!Env.isOfflineRuntime) throw StateError('Local Mac requires offline runtime');
    final attempt = ++_connectionAttempt;
    key = normalizeKey(key);
    final Uri base;
    try {
      base = Env.parseLocalTunnelUrl(address);
    } on FormatException {
      throw LocalMacInvalidAddress();
    }
    if (!RegExp(r'^[A-Za-z0-9_-]{43}$').hasMatch(key)) throw LocalMacInvalidKey();
    final profile = await _probe(base, key);
    if (profile['uid'] != 'alice') throw LocalMacUnauthorized();
    await _editConnection(() async {
      if (attempt != _connectionAttempt) throw LocalMacPairingChanged();
      final changing = !isSignedIn || _base != base || _key != key;
      if (changing && beforeSwitch != null) await beforeSwitch();
      if (attempt != _connectionAttempt) throw LocalMacPairingChanged();
      // Persist one atomic Keychain record before publishing a new session.
      _publishing = true;
      try {
        await _storage.write(key: storageKey, value: jsonEncode({'url': base.toString(), 'key': key}));
        await _selectConversationCache(base: base, key: key);
        _base = base;
        _key = key;
        _audioDeliveryVersion = profile['local_audio_delivery_version'] == 2 ? 2 : 0;
        _rejected = false;
        readiness = LocalTranscriptionReadiness.fromProfile(profile);
      } finally {
        _publishing = false;
      }
      _activate();
      OfflineNetworkPolicy.installFromEnv();
      notifyListeners();
    });
  }

  /// Existing pairings refresh capability once before protected capture. A
  /// failed probe keeps the saved owner/key and all pending audio unchanged.
  Future<void> ensureAudioDelivery() async {
    if (supportsAudioDelivery) return;
    if (!isSignedIn || _base == null) throw LocalMacUnauthorized();
    final base = _base!;
    final key = _key!;
    final profile = await _probe(base, key);
    if (!isSignedIn || _base != base || _key != key) throw LocalMacUnauthorized();
    if (profile['uid'] != 'alice' || profile['local_audio_delivery_version'] != 2) {
      throw LocalAudioDeliveryUnsupported();
    }
    _audioDeliveryVersion = 2;
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
    ++_connectionAttempt;
    await _editConnection(() async {
      if (_rejected) return;
      _rejected = true;
      notifyListeners();
      await _storage.write(key: storageKey, value: jsonEncode({'url': address, 'key': _key, 'rejected': true}));
    });
  }

  Future<void> rejectRequest(Uri uri, String? authorization) async {
    // A late response from the previous address/key cannot expire a new pairing.
    if (_publishing || !permits(uri) || authorization != 'Bearer $_key') return;
    await rejectKey();
  }

  Future<void> signOut() async {
    ++_connectionAttempt;
    await _editConnection(() async {
      _audioDeliveryVersion = 0;
      _key = null;
      _rejected = true;
      notifyListeners();
      await _storage.write(key: storageKey, value: jsonEncode({'url': address, 'rejected': true}));
      await _storage.delete(key: settingsKey);
      await _editServers(() async {
        final catalog = await _loadServerCatalog();
        catalog.seenPresets.addAll(catalog.servers.map((server) => server.id));
        catalog.servers.clear();
        await _writeServerCatalog(catalog);
      });
    });
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
