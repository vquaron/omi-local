import 'dart:async';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:web_socket_channel/io.dart';

import 'package:omi/backend/http/shared.dart';
import 'package:omi/services/auth/auth_token_result.dart';
import 'package:omi/services/sockets/pure_socket.dart';
import 'package:omi/utils/offline_network_policy.dart';

void main() {
  tearDown(OfflineNetworkPolicy.resetForTesting);

  for (final scheme in ['ws', 'wss']) {
    test('$scheme uses its standard port before Dart HTTP upgrade', () async {
      final server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
      final headersSeen = Completer<String?>();
      server.listen((request) async {
        headersSeen.complete(request.headers.value('authorization'));
        final webSocket = await WebSocketTransformer.upgrade(request);
        await webSocket.close();
      });
      addTearDown(() => server.close(force: true));
      final policy = scheme == 'wss'
          ? OfflineNetworkPolicy.tunnel(Uri.parse('https://synthetic.ngrok.app/'))
          : OfflineNetworkPolicy.offline(
              apiBaseUri: Uri.parse('http://127.0.0.1/'),
              authEmulatorHost: '127.0.0.1',
              authEmulatorPort: 9099,
            );
      final host = scheme == 'wss' ? 'synthetic.ngrok.app' : '127.0.0.1';
      OfflineNetworkPolicy.installForTesting(policy);
      final localClient = HttpClient()..findProxy = (_) => 'DIRECT';
      addTearDown(() => localClient.close(force: true));
      final socket = PureSocket(
        '$scheme://$host/v4/listen?source=phone',
        headersProvider: () async => {'Authorization': 'Bearer synthetic-key'},
        channelConnector: (uri, headers) {
          // Exercise the same URI conversion as dart:io WebSocket.connect.
          final upgradeUri = Uri(
            scheme: scheme == 'wss' ? 'https' : 'http',
            host: uri.host,
            port: uri.port,
            path: uri.path,
            query: uri.query,
          );
          expect(uri.port, scheme == 'wss' ? 443 : 80);
          policy.requireAllowed(upgradeUri);
          expect(uri.queryParameters['source'], 'phone');
          // Only the injected test connector routes the upgrade to a local
          // ephemeral server. The production policy remains restricted.
          return IOWebSocketChannel.connect(
            'ws://127.0.0.1:${server.port}/v4/listen',
            headers: headers,
            customClient: localClient,
          );
        },
      );
      expect(await socket.connect(), isTrue);
      expect(await headersSeen.future, 'Bearer synthetic-key');
      await socket.disconnect();
    });
  }

  test('explicit disallowed ports and other origins never reach the connector', () async {
    OfflineNetworkPolicy.installForTesting(
      OfflineNetworkPolicy.tunnel(Uri.parse('https://synthetic.ngrok.app/')),
    );
    for (final url in [
      'wss://synthetic.ngrok.app:444/v4/listen',
      'wss://other.invalid/v4/listen',
    ]) {
      final socket = PureSocket(
        url,
        headersProvider: () async => throw StateError('Headers must not be requested'),
        channelConnector: (_, __) => throw StateError('Must not connect'),
      );
      expect(await socket.connect(), isFalse);
    }
  });

  test('Tailscale WebSocket upgrade preserves the paired Docker port and authorization', () async {
    final server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    final headersSeen = Completer<String?>();
    server.listen((request) async {
      headersSeen.complete(request.headers.value('authorization'));
      final webSocket = await WebSocketTransformer.upgrade(request);
      await webSocket.close();
    });
    addTearDown(() => server.close(force: true));
    final policy = OfflineNetworkPolicy.tunnel(Uri.parse('http://100.64.0.1:21000/'));
    OfflineNetworkPolicy.installForTesting(policy);
    final localClient = HttpClient()..findProxy = (_) => 'DIRECT';
    addTearDown(() => localClient.close(force: true));
    final socket = PureSocket(
      'ws://100.64.0.1:21000/v4/listen?source=phone',
      headersProvider: () async => {'Authorization': 'Bearer synthetic-key'},
      channelConnector: (uri, headers) {
        expect(uri.port, 21000);
        policy.requireAllowed(uri.replace(scheme: 'http'));
        expect(uri.queryParameters['source'], 'phone');
        return IOWebSocketChannel.connect(
          'ws://127.0.0.1:${server.port}/v4/listen',
          headers: headers,
          customClient: localClient,
        );
      },
    );
    expect(await socket.connect(), isTrue);
    expect(await headersSeen.future, 'Bearer synthetic-key');
    await socket.disconnect();
    for (final address in ['ws://100.64.0.1:20000/', 'ws://100.64.0.2:21000/', 'wss://100.64.0.1:21000/']) {
      final blocked = PureSocket(
        address,
        headersProvider: () async => throw StateError('Must not request credentials'),
        channelConnector: (_, __) => throw StateError('Must not connect'),
      );
      expect(await blocked.connect(), isFalse);
    }
  });

  test('unavailable auth blocks a socket before opening the network', () async {
    final socket = PureSocket(
      'wss://example.invalid',
      headersProvider: () async {
        throw AuthTokenUnavailableException(const AuthTokenTransientFailure(failureClass: 'offline'));
      },
    );

    expect(await socket.connect(), isFalse);
    expect(socket.status, PureSocketStatus.notConnected);
  });

  test('merges extra headers with a custom provider', () async {
    final server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    final headersSeen = Completer<Map<String, String>>();
    server.listen((request) async {
      final webSocket = await WebSocketTransformer.upgrade(request);
      headersSeen.complete({
        'extra': request.headers.value('x-extra') ?? '',
        'provider': request.headers.value('x-provider') ?? '',
      });
      await webSocket.close();
    });
    addTearDown(() => server.close(force: true));

    final socket = PureSocket(
      'ws://${InternetAddress.loopbackIPv4.address}:${server.port}',
      headersProvider: () async => {'x-provider': 'provider'},
      extraHeaders: const {'x-extra': 'extra', 'x-provider': 'override'},
    );

    expect(await socket.connect(), isTrue);
    expect(await headersSeen.future, {'extra': 'extra', 'provider': 'override'});
    await socket.disconnect();
  });
}
