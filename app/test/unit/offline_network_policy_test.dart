import 'package:flutter_test/flutter_test.dart';

import 'package:omi/services/connectivity_service.dart';
import 'package:omi/utils/offline_network_policy.dart';
import 'package:omi/utils/safe_url_launcher.dart' as safe_launcher;
import 'package:connectivity_plus/connectivity_plus.dart';

void main() {
  final policy = OfflineNetworkPolicy.offline(
    apiBaseUri: Uri.parse('http://192.168.40.8:8000/'),
    authEmulatorHost: '192.168.40.8',
    authEmulatorPort: 9099,
  );

  tearDown(OfflineNetworkPolicy.resetForTesting);

  test('tunnel policy allows only the selected HTTPS and WSS endpoint', () {
    final tunnel = OfflineNetworkPolicy.tunnel(Uri.parse('https://synthetic.ngrok.app/'));
    expect(tunnel.allows(Uri.parse('https://synthetic.ngrok.app/v1/health')), isTrue);
    expect(tunnel.allows(Uri.parse('wss://synthetic.ngrok.app/v4/listen')), isTrue);
    expect(tunnel.allows(Uri.parse('https://synthetic.ngrok.app:0/v4/listen')), isFalse);
    expect(tunnel.allows(Uri.parse('https://synthetic.ngrok.app.evil.test/')), isFalse);
    expect(tunnel.allows(Uri.parse('http://127.0.0.1:9099/')), isFalse);
    expect(tunnel.allows(Uri.parse('http://synthetic.ngrok.app/')), isFalse);
    expect(ConnectivityService.hasTunnelTransport([ConnectivityResult.mobile]), isTrue);
    expect(ConnectivityService.hasTunnelTransport([ConnectivityResult.none]), isFalse);
  });

  test('Tailscale policy confines HTTP and WS to the paired address and port', () {
    final tunnel = OfflineNetworkPolicy.tunnel(Uri.parse('http://100.64.0.1:21000/'));
    expect(tunnel.allows(Uri.parse('http://100.64.0.1:21000/v1/health')), isTrue);
    expect(tunnel.allows(Uri.parse('ws://100.64.0.1:21000/v4/listen')), isTrue);
    for (final address in [
      'http://100.64.0.2:21000/',
      'http://100.64.0.1:20000/',
      'http://100.64.0.1/',
      'http://100.64.0.1:0/',
      'https://100.64.0.1:21000/',
      'wss://100.64.0.1:21000/',
      'http://100.64.0.1:9099/',
      'http://127.0.0.1:9099/',
      'http://key@100.64.0.1:21000/',
    ]) {
      expect(tunnel.allows(Uri.parse(address)), isFalse, reason: address);
    }
  });

  test('offline policy permits only the configured API and Auth authorities', () {
    expect(policy.allows(Uri.parse('http://192.168.40.8:8000/v1/health')), isTrue);
    expect(policy.allows(Uri.parse('ws://192.168.40.8:8000/v4/listen')), isTrue);
    expect(policy.allows(Uri.parse('http://192.168.40.8:9099/identitytoolkit.googleapis.com/v1/accounts')), isTrue);

    for (final uri in [
      'https://one.one.one.one',
      'https://api.omi.me/v1/health',
      'https://api.openai.com',
      'wss://api.deepgram.com/v1/listen',
      'wss://generativelanguage.googleapis.com',
      'https://fal.ai',
      'https://api.github.com',
      'https://maps.googleapis.com',
      'http://192.168.40.9:8000',
      'http://192.168.40.8:8001',
    ]) {
      expect(policy.allows(Uri.parse(uri)), isFalse, reason: uri);
    }
  });

  test('offline HTTP override blocks before selecting a proxy', () {
    final overrides = OfflineHttpOverrides(policy);
    expect(overrides.findProxyFromEnvironment(Uri.parse('http://192.168.40.8:8000/v1/health'), null), 'DIRECT');
    expect(
      () => overrides.findProxyFromEnvironment(Uri.parse('https://api.openai.com/v1/models'), null),
      throwsA(isA<OfflineNetworkBlocked>()),
    );
  });

  test('policy-aware launcher never invokes a platform launcher offline', () async {
    OfflineNetworkPolicy.installForTesting(policy);
    expect(await safe_launcher.launchUrl(Uri.parse('https://github.com/BasedHardware/omi')), isFalse);
  });

  test('standard runtime policy preserves ordinary networking', () {
    final standard = OfflineNetworkPolicy.standard();
    expect(standard.allows(Uri.parse('https://api.omi.me/v1/health')), isTrue);
    expect(standard.allowsExternalLaunch, isTrue);
  });

  test('offline connectivity accepts local transports but not cellular-only', () {
    expect(ConnectivityService.hasLocalNetworkTransport([ConnectivityResult.wifi]), isTrue);
    expect(ConnectivityService.hasLocalNetworkTransport([ConnectivityResult.ethernet]), isTrue);
    expect(ConnectivityService.hasLocalNetworkTransport([ConnectivityResult.vpn]), isTrue);
    expect(ConnectivityService.hasLocalNetworkTransport([ConnectivityResult.other]), isFalse);
    expect(ConnectivityService.hasLocalNetworkTransport([ConnectivityResult.mobile]), isFalse);
    expect(ConnectivityService.hasLocalNetworkTransport([ConnectivityResult.none]), isFalse);
  });
}
