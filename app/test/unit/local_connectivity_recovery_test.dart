import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/services.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/env/dev_env.dart';
import 'package:omi/env/env.dart';
import 'package:omi/services/auth/local_mac_session.dart';
import 'package:omi/services/connectivity_service.dart';
import 'package:omi/utils/offline_network_policy.dart';

class _HealthResponse extends Fake implements HttpClientResponse {
  @override
  int get statusCode => 200;
}

class _HealthRequest extends Fake implements HttpClientRequest {
  _HealthRequest(this.response);
  final Future<HttpClientResponse> Function() response;
  @override
  set followRedirects(bool value) => expect(value, isFalse);
  @override
  Future<HttpClientResponse> close() => response();
}

class _HealthClient extends Fake implements HttpClient {
  _HealthClient(this.response);
  final Future<HttpClientResponse> Function() response;
  int requests = 0;
  @override
  set connectionTimeout(Duration? value) {}
  @override
  Future<HttpClientRequest> getUrl(Uri uri) async {
    expect(uri.toString(), 'https://synthetic.ngrok.app/v1/health');
    requests++;
    return _HealthRequest(response);
  }

  @override
  void close({bool force = false}) {}
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  const channel = MethodChannel('dev.fluttercommunity.plus/connectivity');
  var transports = ['wifi'];

  setUpAll(() {
    Env.init(DevEnv());
    Env.setRuntimeModeForTesting(OmiRuntimeMode.offline);
  });
  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    FlutterSecureStorage.setMockInitialValues({
      LocalMacSession.storageKey: jsonEncode({
        'url': 'https://synthetic.ngrok.app/',
        'key': List.filled(43, 's').join(),
      }),
    });
    await SharedPreferencesUtil.init();
    await LocalMacSession.instance.restore();
    transports = ['wifi'];
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(channel, (call) async {
      expect(call.method, 'check');
      return transports;
    });
  });
  tearDown(() {
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(channel, null);
    Env.localTunnelConfigured = false;
    Env.clearApiBaseUrlOverrideForTesting();
    OfflineNetworkPolicy.resetForTesting();
  });

  test('successful Wi-Fi to cellular handoff wakes recovery even without a false reachability edge', () async {
    final changes = <bool>[];
    final service = ConnectivityService();
    final subscription = service.onConnectionChange.listen(changes.add);
    addTearDown(subscription.cancel);
    final client = _HealthClient(() async => _HealthResponse());
    await HttpOverrides.runZoned(() async {
      await service.refreshLocalTunnel();
      await pumpEventQueue();
      changes.clear();
      transports = ['mobile'];
      await service.refreshLocalTunnel(notifyWhenConnected: true);
      await pumpEventQueue();
      expect(changes, [true]);
      await service.refreshLocalTunnel();
      await pumpEventQueue();
      expect(changes, [true], reason: 'Ordinary periodic health must not rotate a part or reconnect a healthy socket');
      transports = ['none'];
      await service.refreshLocalTunnel(notifyWhenConnected: true);
      await pumpEventQueue();
      expect(changes, [true, false]);
      transports = ['mobile'];
      await service.refreshLocalTunnel();
      await pumpEventQueue();
      expect(changes, [true, false, true]);
    }, createHttpClient: (_) => client);
  });

  test('a route change during an existing health probe retains its recovery wakeup', () async {
    final service = ConnectivityService();
    final changes = <bool>[];
    final subscription = service.onConnectionChange.listen(changes.add);
    addTearDown(subscription.cancel);
    final response = Completer<HttpClientResponse>();
    final client = _HealthClient(() => response.future);
    await HttpOverrides.runZoned(() async {
      final probing = service.refreshLocalTunnel();
      await pumpEventQueue();
      expect(client.requests, 1);
      transports = ['mobile'];
      await service.refreshLocalTunnel(notifyWhenConnected: true);
      response.complete(_HealthResponse());
      await probing;
      await pumpEventQueue();
      expect(client.requests, 1);
      expect(changes, [true]);
    }, createHttpClient: (_) => client);
  });
}
