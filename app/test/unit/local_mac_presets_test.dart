import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/env/dev_env.dart';
import 'package:omi/env/env.dart';
import 'package:omi/services/auth/local_mac_session.dart';
import 'package:omi/utils/offline_network_policy.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  const storage = FlutterSecureStorage();
  final key = List.filled(43, 's').join();
  const address = 'https://home.example/';
  Map<String, String> preset({String id = 'home', String name = 'Home', String? url, String? accessKey}) => {
    'id': id,
    'name': name,
    'url': url ?? address,
    'key': accessKey ?? key,
  };
  String payload(List<Map<String, String>> servers) =>
      base64Encode(utf8.encode(jsonEncode({'version': 1, 'servers': servers})));

  setUpAll(() => Env.init(DevEnv()));
  setUp(() async {
    Env.setRuntimeModeForTesting(OmiRuntimeMode.offline);
    SharedPreferences.setMockInitialValues({});
    FlutterSecureStorage.setMockInitialValues({});
    await SharedPreferencesUtil.init();
  });
  tearDown(() {
    Env.setRuntimeModeForTesting(null);
    Env.localTunnelConfigured = false;
    Env.clearApiBaseUrlOverrideForTesting();
    OfflineNetworkPolicy.resetForTesting();
  });

  test('presets prefill saved servers without connecting and survive edits deletions and sign-out', () async {
    var probes = 0;
    final session = LocalMacSession(
      probe: (_, __) async {
        probes++;
        return {'uid': 'alice'};
      },
    );
    final encoded = payload([preset(), preset(id: 'travel', name: 'Travel', url: '100.64.0.2:21000')]);
    await session.importServerPresets(encoded);
    expect(probes, 0);
    expect(session.isSignedIn, isFalse);
    expect((await session.readSettings()).serverId, 'home');
    expect((await session.readServers()).last.address, 'http://100.64.0.2:21000/');
    await session.saveServer(id: 'home', name: 'My manual name', address: 'https://edited.example/', key: key);
    await session.saveSettings('unfinished', 'manual draft');
    await session.deleteServer('travel');
    final before = await storage.read(key: LocalMacSession.serversKey);
    await LocalMacSession().importServerPresets(encoded);
    expect(await storage.read(key: LocalMacSession.serversKey), before);
    expect((await session.readServers()).single.name, 'My manual name');
    expect((await session.readSettings()).address, 'unfinished');
    await session.signOut();
    await LocalMacSession().importServerPresets(encoded);
    expect(await session.readServers(), isEmpty);
    expect(await storage.read(key: LocalMacSession.serversKey), isNot(contains(key)));
    expect(probes, 0);
  });

  test('same-address manual entry wins over a differently named preset and its replacement key', () async {
    final session = LocalMacSession();
    final manual = await session.saveServer(name: 'Manual Docker', address: '100.64.0.1:21000', key: key);
    await session.importServerPresets(
      payload([preset(url: 'http://100.64.0.1:21000/', accessKey: List.filled(43, 'r').join())]),
    );
    final saved = (await session.readServers()).single;
    expect(saved.id, manual.id);
    expect(saved.name, 'Manual Docker');
    expect(saved.key, key);
  });

  test('preset name length counts Unicode code points like the host parser', () async {
    final session = LocalMacSession();
    final allowed = List.filled(80, '🌲').join();
    await session.importServerPresets(payload([preset(name: allowed)]));
    expect((await session.readServers()).single.name, allowed);
    await expectLater(
      session.importServerPresets(payload([preset(id: 'too_long', name: '$allowed🌲')])),
      throwsFormatException,
    );
    expect((await session.readServers()).single.name, allowed);
  });

  test('an invalid later preset cannot partly update the existing catalog or active pairing', () async {
    final session = LocalMacSession(probe: (_, __) async => {'uid': 'alice'});
    await session.connect(address, key);
    await session.saveServer(name: 'Existing', address: address, key: key);
    final before = await storage.read(key: LocalMacSession.serversKey);
    for (final invalid in [
      preset(id: 'other', url: 'http://public.example/'),
      preset(id: 'other', accessKey: 'invalid'),
      preset(id: 'home'),
      preset(id: 'other', name: 'bad\nname'),
    ]) {
      await expectLater(session.importServerPresets(payload([preset(), invalid])), throwsFormatException);
      expect(await storage.read(key: LocalMacSession.serversKey), before);
      expect(session.address, address);
      expect(session.accessKey, key);
    }
  });

  test('parser failures cannot expose a private preset payload in startup diagnostics', () async {
    final session = LocalMacSession();
    final invalid = base64Encode(utf8.encode('{"key":"$key", invalid JSON}'));
    try {
      await session.importServerPresets(invalid);
      fail('Invalid payload must be rejected');
    } on FormatException catch (error) {
      expect(error.toString(), isNot(contains(key)));
      expect(error.source, isNull);
    }
    expect(await storage.read(key: LocalMacSession.serversKey), isNull);
  });

  test('catalog writes serialize manual saves with preset import without losing either entry', () async {
    final session = LocalMacSession();
    await Future.wait([
      session.importServerPresets(payload([preset()])),
      session.saveServer(name: 'Manual', address: '100.64.0.1:21000', key: key),
    ]);
    expect((await session.readServers()).map((server) => server.name).toSet(), {'Home', 'Manual'});
  });

  test('probe succeeds before stopping and unchanged checks never stop the active recording', () async {
    final order = <String>[];
    final session = LocalMacSession(
      probe: (_, __) async {
        order.add('probe');
        return {'uid': 'alice'};
      },
    );
    await session.connect(
      address,
      key,
      beforeSwitch: () async {
        order.add('stop');
      },
    );
    await session.connect(
      address,
      key,
      beforeSwitch: () async {
        order.add('unexpected stop');
      },
    );
    expect(order, ['probe', 'stop', 'probe']);
    expect(session.address, address);
  });

  test('failed preflight preserves active credentials and never stops recording', () async {
    var reject = false;
    var stops = 0;
    final session = LocalMacSession(
      probe: (_, __) async {
        if (reject) throw LocalMacUnauthorized();
        return {'uid': 'alice'};
      },
    );
    await session.connect(address, key);
    reject = true;
    await expectLater(
      session.connect(
        'https://other.example/',
        key,
        beforeSwitch: () async {
          stops++;
        },
      ),
      throwsA(isA<LocalMacUnauthorized>()),
    );
    expect(stops, 0);
    expect(session.address, address);
    expect(session.accessKey, key);
  });

  test('a late preflight cannot reconnect after sign-out', () async {
    final pending = Completer<Map<String, dynamic>>();
    final session = LocalMacSession(probe: (_, __) => pending.future);
    var stops = 0;
    final connecting = expectLater(
      session.connect(
        address,
        key,
        beforeSwitch: () async {
          stops++;
        },
      ),
      throwsA(isA<LocalMacPairingChanged>()),
    );
    await session.signOut();
    pending.complete({'uid': 'alice'});
    await connecting;
    expect(stops, 0);
    expect(session.isSignedIn, isFalse);
    expect(jsonDecode((await storage.read(key: LocalMacSession.storageKey))!)['rejected'], isTrue);
  });

  test(
    'restored legacy pairing adopts its existing cache without a network request or deleting the original',
    () async {
      final preferences = SharedPreferencesUtil();
      const original = ['{"synthetic":"existing paired cache"}'];
      await preferences.saveStringList('cachedConversations', original);
      await storage.write(key: LocalMacSession.storageKey, value: jsonEncode({'url': address, 'key': key}));
      var probes = 0;
      final session = LocalMacSession(
        probe: (_, __) async {
          probes++;
          return {'uid': 'alice'};
        },
      );
      await session.restore();
      final namespace = preferences.getString('localConversationCacheNamespace');
      expect(preferences.getStringList('localConversations.$namespace'), original);
      expect(preferences.getStringList('cachedConversations'), original);
      expect(probes, 0);
      expect(session.isSignedIn, isTrue);
    },
  );

  test('server cache namespaces preserve each previous projection without deleting the legacy cache', () async {
    final preferences = SharedPreferencesUtil();
    const original = ['{"synthetic":"legacy cache preserved"}'];
    await preferences.saveStringList('cachedConversations', original);
    final session = LocalMacSession(probe: (_, __) async => {'uid': 'alice'});
    await session.connect(address, key);
    final first = preferences.getString('localConversationCacheNamespace');
    await preferences.saveStringList('localConversations.$first', ['first server projection']);
    await session.connect('https://second.example/', key);
    final second = preferences.getString('localConversationCacheNamespace');
    expect(second, isNot(first));
    expect(preferences.getStringList('localConversations.$first'), ['first server projection']);
    expect(preferences.getStringList('localConversations.$second'), isEmpty);
    expect(preferences.getStringList('cachedConversations'), original);
    await session.connect(address, key);
    expect(preferences.getString('localConversationCacheNamespace'), first);
    expect(preferences.getStringList('localConversations.$first'), ['first server projection']);
  });
}
