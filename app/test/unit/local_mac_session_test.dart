import 'dart:io';
import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/env/env.dart';
import 'package:omi/env/dev_env.dart';
import 'package:omi/services/auth/local_mac_session.dart';
import 'package:omi/services/auth_service.dart';
import 'package:omi/services/auth/auth_token_result.dart';
import 'package:omi/utils/offline_network_policy.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  const url = 'https://synthetic.ngrok.app/';
  final key = List.filled(43, 's').join();
  setUpAll(() {
    Env.init(DevEnv());
    Env.setRuntimeModeForTesting(OmiRuntimeMode.offline);
  });
  tearDownAll(() => Env.setRuntimeModeForTesting(null));

  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    FlutterSecureStorage.setMockInitialValues({});
    await SharedPreferencesUtil.init();
    Env.localTunnelConfigured = false;
    Env.clearApiBaseUrlOverrideForTesting();
  });
  tearDown(() {
    Env.localTunnelConfigured = false;
    Env.clearApiBaseUrlOverrideForTesting();
    OfflineNetworkPolicy.resetForTesting();
  });

  test('saved servers migrate pairing and draft once without activating either', () async {
    final session = LocalMacSession(probe: (_, __) async => {'uid': 'alice'});
    await session.connect(url, key);
    await session.saveSettings('100.64.0.1:21000', 'unfinished');
    final servers = await session.readServers();
    expect(servers.map((entry) => entry.address), ['100.64.0.1:21000', url]);
    expect(session.address, url);
    expect(session.accessKey, key);
    for (final server in servers) {
      await session.deleteServer(server.id);
    }
    expect(await LocalMacSession().readServers(), isEmpty); // No legacy resurrection.
    expect(session.address, url); // Removing a saved entry does not disconnect.
    expect(session.accessKey, key);
  });

  test('saved server edits retain identity and exact credentials across restart and rejection', () async {
    var probes = 0;
    final session = LocalMacSession(probe: (base, submitted) async {
      probes++;
      if (base.host != 'synthetic.ngrok.app') throw LocalMacUnauthorized();
      return {'uid': 'alice'};
    });
    await session.readServers(); // Fresh catalog.
    await session.connect(url, key);
    final server = await session.saveServer(name: 'Docker', address: '100.64.0.1:21000', key: key);
    final replacement = List.filled(43, 'r').join();
    await session.saveServer(id: server.id, name: 'Home', address: '100.64.0.2:21000', key: replacement);
    final restored = LocalMacSession();
    final saved = (await restored.readServers()).single;
    expect(saved.id, server.id);
    expect(saved.name, 'Home');
    expect(saved.address, '100.64.0.2:21000');
    expect(saved.key, replacement);
    await session.saveSettings(saved.address, saved.key, name: saved.name, serverId: saved.id);
    expect((await restored.readSettings()).serverId, saved.id);
    expect(probes, 1);
    await expectLater(session.connect(saved.address, saved.key), throwsA(isA<LocalMacUnauthorized>()));
    expect(session.address, url);
    expect(session.authorizationFor(Uri.parse(url)), 'Bearer $key');
    expect(() => session.authorizationFor(Uri.parse('http://100.64.0.2:21000/')), throwsA(isA<LocalMacUnauthorized>()));
    await session.signOut();
    expect(await const FlutterSecureStorage().read(key: LocalMacSession.serversKey), isNull);
  });

  test('corrupt saved servers are reported without overwriting credentials', () async {
    const storage = FlutterSecureStorage();
    await storage.write(key: LocalMacSession.serversKey, value: 'invalid-json');
    final session = LocalMacSession();
    await expectLater(session.saveServer(name: 'Mac', address: url, key: key), throwsFormatException);
    expect(await storage.read(key: LocalMacSession.serversKey), 'invalid-json');
    expect(session.isSignedIn, isFalse);
  });

  test('first pairing and restart do not need a Firebase session', () async {
    final session = LocalMacSession(probe: (_, __) async => {'uid': 'alice'});
    await session.connect(url, key);
    expect(session.isSignedIn, isTrue);
    expect(SharedPreferencesUtil().uid, 'alice');
    final restored = LocalMacSession();
    await restored.restore();
    expect(restored.isSignedIn, isTrue);
    expect(restored.authorizationFor(Uri.parse('wss://synthetic.ngrok.app/v4/listen')), 'Bearer $key');
    expect(restored.address, url);
    expect(Env.usesLocalTunnel, isTrue);
    expect(session.readiness.isReady, isFalse);
    expect(session.readiness.live, 'unknown');
  });

  test('Tailscale input supports native defaults and explicit Docker ports', () {
    for (final entry in {
      ' 100.64.0.1 ': 'http://100.64.0.1:20000/',
      '100.127.255.255/': 'http://100.127.255.255:20000/',
      '100.64.0.1:21000': 'http://100.64.0.1:21000/',
      'http://100.64.0.1:21000/': 'http://100.64.0.1:21000/',
      '100.64.0.1:80': 'http://100.64.0.1/',
      'http://100.64.0.1': 'http://100.64.0.1/',
      '100.64.0.1:65535': 'http://100.64.0.1:65535/',
    }.entries) {
      final parsed = Env.parseLocalTunnelUrl(entry.key);
      expect(parsed.toString(), entry.value);
      expect(Env.parseLocalTunnelUrl(parsed.toString()), parsed, reason: 'Keychain round trip');
    }
  });

  test('pairing rejects malformed IPs, unsafe HTTP hosts and invalid authorities', () {
    for (final address in [
      '100.63.255.255',
      '100.128.0.1',
      '100.064.0.1',
      '100.64.0.256',
      'http://127.0.0.1:20000',
      'http://192.168.1.2:20000',
      'http://example.test',
      'http://[::1]:20000',
      'http://100.64.0.1.evil.test:20000',
      '100.64.0.1:0',
      '100.64.0.1:65536',
      '100.64.0.1:-1',
      'http://key@100.64.0.1:20000',
      'http://100.64.0.1:20000/path',
      'http://100.64.0.1:20000?key=value',
      'http://100.64.0.1:20000#fragment',
      'https://100.64.0.1',
      'ws://100.64.0.1:20000',
      '//100.64.0.1:20000',
    ]) {
      expect(() => Env.parseLocalTunnelUrl(address), throwsFormatException, reason: address);
    }
  });

  test('Tailscale pairing, restart and credentials retain exactly the selected HTTP/WS authority', () async {
    final session = LocalMacSession(probe: (base, submitted) async {
      expect(base.toString(), 'http://100.64.0.1:21000/');
      expect(submitted, key);
      return {'uid': 'alice'};
    });
    await session.connect('100.64.0.1:21000', key);
    final restored = LocalMacSession();
    await restored.restore();
    expect(restored.address, 'http://100.64.0.1:21000/');
    expect(restored.authorizationFor(Uri.parse('http://100.64.0.1:21000/v1/local/status')), 'Bearer $key');
    expect(restored.authorizationFor(Uri.parse('ws://100.64.0.1:21000/v4/listen')), 'Bearer $key');
    for (final other in [
      'http://100.64.0.2:21000/',
      'http://100.64.0.1:20000/',
      'http://100.64.0.1/',
      'https://100.64.0.1:21000/',
      'wss://100.64.0.1:21000/',
      'ws://100.64.0.1/',
      'http://key@100.64.0.1:21000/',
      'http://100.64.0.1:0/',
    ]) {
      expect(() => restored.authorizationFor(Uri.parse(other)), throwsA(isA<LocalMacUnauthorized>()));
    }
    expect(OfflineNetworkPolicy.current.allows(Uri.parse('http://127.0.0.1:9099')), isFalse);
    await restored.saveSettings('100.64.0.2:21000', key);
    expect(restored.address, 'http://100.64.0.1:21000/');
    expect(restored.permits(Uri.parse('http://100.64.0.2:21000/')), isFalse);
  });

  test('Tailscale rejected key never replaces a working HTTPS pairing', () async {
    var reject = false;
    final session = LocalMacSession(probe: (_, __) async {
      if (reject) throw LocalMacUnauthorized();
      return {'uid': 'alice'};
    });
    await session.connect(url, key);
    reject = true;
    await expectLater(session.connect('100.64.0.1', key), throwsA(isA<LocalMacUnauthorized>()));
    expect(session.address, url);
    expect(session.isSignedIn, isTrue);
  });

  test('pairing admits audio while reporting separate live and final readiness', () async {
    final session = LocalMacSession(
        probe: (_, __) async => {
              'uid': 'alice',
              'local_transcription': {
                'live': {'status': 'busy'},
                'final': {'status': 'ready'}
              },
            });
    await session.connect(url, key);
    expect(session.isSignedIn, isTrue);
    expect(session.readiness.live, 'busy');
    expect(session.readiness.finalTranscript, 'ready');
    expect(session.readiness.isReady, isFalse);
    final restored = LocalMacSession();
    await restored.restore();
    expect(restored.readiness.finalTranscript, 'unknown');
    expect(
        LocalTranscriptionReadiness.fromProfile({
          'local_transcription': {'live': 1}
        }).live,
        'unknown');
  });

  test('saved settings survive restart without authenticating or switching the active server', () async {
    final session = LocalMacSession(probe: (_, __) async => {'uid': 'alice'});
    await session.connect(url, key);
    await session.saveSettings(' https://another.ngrok.app ', ' unfinished-key ');
    final restored = LocalMacSession();
    await restored.restore();
    expect(await restored.readSettings(),
        (address: 'https://another.ngrok.app', key: 'unfinished-key', name: '', serverId: null));
    expect(restored.address, url);
    expect(restored.authorizationFor(Uri.parse(url)), 'Bearer $key');
    expect(restored.permits(Uri.parse('https://another.ngrok.app')), isFalse);
  });

  test('legacy pairing pre-fills settings and sign-out erases the saved key', () async {
    final session = LocalMacSession(probe: (_, __) async => {'uid': 'alice'});
    await session.connect(url, key);
    expect(await session.readSettings(), (address: url, key: key, name: '', serverId: null));
    await session.saveSettings(url, key);
    await session.signOut();
    expect((await session.readSettings()).key, isEmpty);
    expect(await const FlutterSecureStorage().read(key: LocalMacSession.settingsKey), isNull);
  });

  test('explicit native launch handoff persists settings without changing authenticated origin', () async {
    debugDefaultTargetPlatformOverride = TargetPlatform.iOS;
    const channel = MethodChannel('com.omi/environment');
    Map<String, String>? handoff = {'url': url, 'key': key};
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(channel, (call) async {
      expect(call.method, 'takeLocalMacSettings');
      final value = handoff;
      handoff = null;
      return value;
    });
    addTearDown(() {
      debugDefaultTargetPlatformOverride = null;
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(channel, null);
    });
    final session = LocalMacSession();
    await session.importLaunchSettings();
    expect(await LocalMacSession().readSettings(), (address: url, key: key, name: '', serverId: null));
    expect(session.isSignedIn, isFalse);
    await session.saveSettings('https://changed.ngrok.app', key);
    await session.importLaunchSettings();
    expect((await session.readSettings()).address, 'https://changed.ngrok.app');
  });

  test('whitespace from pasted app keys is normalized before verification and storage', () async {
    final session = LocalMacSession(probe: (_, submitted) async {
      expect(submitted, key);
      return {'uid': 'alice'};
    });
    final pasted = ' ${key.substring(0, 20)}\n${key.substring(20)} ';
    await session.saveSettings(url, pasted);
    await session.connect(url, pasted);
    expect((await session.readSettings()).key, key);
    expect(session.accessKey, key);
  });

  test('credentials are restricted to one secure origin', () async {
    final session = LocalMacSession(probe: (_, __) async => {'uid': 'alice'});
    await session.connect(url, key);
    for (final other in [
      'https://synthetic.ngrok.app.evil.test/',
      'http://synthetic.ngrok.app/',
      'https://synthetic.ngrok.app:8000/',
      'https://key@synthetic.ngrok.app/'
    ]) {
      expect(() => session.authorizationFor(Uri.parse(other)), throwsA(isA<LocalMacUnauthorized>()));
    }
    expect(OfflineNetworkPolicy.current.allows(Uri.parse('http://127.0.0.1:9099/')), isFalse);
  });

  test('network failure keeps previous pairing; rejected key stays rejected after restart', () async {
    var available = true;
    final session = LocalMacSession(probe: (_, __) async {
      if (!available) throw const SocketException('unavailable');
      return {'uid': 'alice'};
    });
    await session.connect(url, key);
    available = false;
    await expectLater(session.connect('https://another.ngrok.app/', key), throwsA(isA<SocketException>()));
    expect(session.address, url);
    expect(session.isSignedIn, isTrue);
    await session.rejectKey();
    final restored = LocalMacSession();
    await restored.restore();
    expect(restored.address, url);
    expect(restored.isSignedIn, isFalse);
  });

  test('wrong key or owner never replaces the paired session', () async {
    final session = LocalMacSession(probe: (_, __) async => {'uid': 'bob'});
    await expectLater(session.connect(url, key), throwsA(isA<LocalMacUnauthorized>()));
    await expectLater(session.connect(url, 'wrong'), throwsA(isA<LocalMacUnauthorized>()));
    expect(session.isSignedIn, isFalse);
    expect(await const FlutterSecureStorage().read(key: LocalMacSession.storageKey), isNull);
  });

  test('late rejection from the previous address or key preserves new pairing', () async {
    final session = LocalMacSession(probe: (_, __) async => {'uid': 'alice'});
    await session.connect(url, key);
    final replacement = List.filled(43, 'r').join();
    await session.connect('https://another.ngrok.app/', replacement);
    await session.rejectRequest(Uri.parse(url), 'Bearer $key');
    await session.rejectRequest(Uri.parse(session.address), 'Bearer $key');
    expect(session.isSignedIn, isTrue);
    await session.rejectRequest(Uri.parse(session.address), 'Bearer $replacement');
    expect(session.isSignedIn, isFalse);
  });

  test('late Firebase expiry cannot clear the local owner', () async {
    final session = LocalMacSession(probe: (_, __) async => {'uid': 'alice'});
    await session.connect(url, key);
    await AuthService.instance.expireSession(
      const AuthSessionExpiredEvent(reason: AuthSessionExpirationReason.missingToken),
    );
    expect(session.isSignedIn, isTrue);
    expect(SharedPreferencesUtil().uid, 'alice');
  });
}
