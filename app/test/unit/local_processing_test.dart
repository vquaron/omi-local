import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:omi/env/env.dart';
import 'package:omi/services/auth/local_mac_session.dart';
import 'package:omi/services/local_processing.dart';

class ProcessingTestSession extends LocalMacSession {
  String host = 'https://paired.example/';
  String key = 'synthetic-pairing-key';
  @override
  String get address => host;
  @override
  String? get accessKey => key;
  @override
  bool get isSignedIn => true;
  @override
  String authorizationFor(Uri uri) {
    if (uri.origin != Uri.parse(host).origin) throw LocalMacUnauthorized();
    return 'Bearer $key';
  }

  void change(String address) {
    host = address;
    notifyListeners();
  }
}

void main() {
  setUp(() => Env.setRuntimeModeForTesting(OmiRuntimeMode.offline));
  tearDown(() => Env.setRuntimeModeForTesting(null));

  test('HTTP writes are bounded UTF-8 JSON with Content-Length and redirects disabled', () async {
    final transport = _TestHttpClient();
    final client = LocalProcessingClient(session: ProcessingTestSession(), httpClientFactory: () => transport);
    final body = {
      'revision': 1,
      'profile': {'name': 'Локальный сервер'},
      'api_key': 'synthetic-key'
    };
    await client.request('/save', body: body);
    expect(transport.request.contentLength, transport.request.bytes.length);
    expect(jsonDecode(utf8.decode(transport.request.bytes)), body);
    expect(transport.request.followRedirects, isFalse);
    expect(transport.opened!.host, 'paired.example');
    expect(transport.closed, isTrue);
  });

  test('HTTP adapter surfaces an old-server 404 and never follows a redirect', () async {
    for (final status in [404, 302, 403]) {
      final transport = _TestHttpClient();
      transport.request.responseStatus = status;
      final client = LocalProcessingClient(session: ProcessingTestSession(), httpClientFactory: () => transport);
      await expectLater(
          client.request('', method: 'GET'),
          throwsA(isA<LocalProcessingException>().having(
              (error) => error.failure,
              'failure',
              switch (status) {
                404 => LocalProcessingFailure.unsupported,
                403 => LocalProcessingFailure.invalid,
                _ => LocalProcessingFailure.unavailable
              })));
      expect(transport.request.followRedirects, isFalse);
      expect(transport.calls, 1);
    }
  });

  test('processing edits use only the paired origin and never retry failed writes', () async {
    final session = ProcessingTestSession();
    var calls = 0;
    final client = LocalProcessingClient(
        session: session,
        transport: (method, uri, auth, body, timeout) async {
          calls++;
          expect(uri.toString(), 'https://paired.example/v1/local/processing/save');
          expect(auth, 'Bearer synthetic-pairing-key');
          expect(method, 'POST');
          expect(body!['api_key'], 'synthetic-provider-key');
          throw const LocalProcessingException(LocalProcessingFailure.unavailable);
        });
    await expectLater(
        client.request('/save', body: {
          'api_key': 'synthetic-provider-key',
          'profile': {
            'settings': {'provider_url': 'https://remote.example/v1'}
          }
        }),
        throwsA(isA<LocalProcessingException>()));
    expect(calls, 1);
    await expectLater(client.request('//other.example/'), throwsA(isA<LocalProcessingException>()));
    expect(calls, 1);
  });

  test('rejects stale response even if pairing changes away and back', () async {
    final session = ProcessingTestSession();
    final pending = Completer<Map<String, dynamic>>();
    final client = LocalProcessingClient(session: session, transport: (_, __, ___, ____, _____) => pending.future);
    final request = client.request('', method: 'GET');
    session.change('https://other.example/');
    session.change('https://paired.example/');
    pending.complete({});
    await expectLater(
        request,
        throwsA(isA<LocalProcessingException>()
            .having((error) => error.failure, 'failure', LocalProcessingFailure.pairingChanged)));
  });

  test('blank key is omitted, explicit clear wins, and status metadata is never posted', () {
    final profile = {
      'id': 'live',
      'name': ' Test ',
      'stage': 'live',
      'kind': 'external',
      'settings': {'url': 'wss://remote.example/asr'},
      'can_edit': true,
      'has_key': true
    };
    final blank = LocalProcessingClient.draftPayload(profile, apiKey: ' ');
    expect(blank.containsKey('api_key'), isFalse);
    expect(blank.containsKey('clear_key'), isFalse);
    expect((blank['profile'] as Map).containsKey('has_key'), isFalse);
    expect((blank['profile'] as Map)['name'], 'Test');
    final clear = LocalProcessingClient.draftPayload(profile, apiKey: 'new-key', clearKey: true);
    expect(clear['clear_key'], isTrue);
    expect(clear.containsKey('api_key'), isFalse);
    expect(LocalProcessingClient.draftPayload(profile, apiKey: ' new-key ')['api_key'], 'new-key');
  });
}

class _TestHeaders implements HttpHeaders {
  final values = <String, Object>{};
  @override
  set contentType(ContentType? value) => values[HttpHeaders.contentTypeHeader] = value.toString();
  @override
  void set(String name, Object value, {bool preserveHeaderCase = false}) => values[name] = value;
  @override
  dynamic noSuchMethod(Invocation invocation) => super.noSuchMethod(invocation);
}

class _TestRequest implements HttpClientRequest {
  @override
  final headers = _TestHeaders();
  @override
  int contentLength = -1;
  @override
  bool followRedirects = true;
  final bytes = <int>[];
  int responseStatus = 200;
  @override
  void add(List<int> data) => bytes.addAll(data);
  @override
  Future<HttpClientResponse> close() async => _TestResponse(responseStatus);
  @override
  dynamic noSuchMethod(Invocation invocation) => super.noSuchMethod(invocation);
}

class _TestResponse extends Stream<List<int>> implements HttpClientResponse {
  _TestResponse(this.statusCode);
  @override
  final int statusCode;
  @override
  int get contentLength => 2;
  @override
  StreamSubscription<List<int>> listen(void Function(List<int>)? onData,
          {Function? onError, void Function()? onDone, bool? cancelOnError}) =>
      Stream.value(utf8.encode('{}')).listen(onData, onError: onError, onDone: onDone, cancelOnError: cancelOnError);
  @override
  dynamic noSuchMethod(Invocation invocation) => super.noSuchMethod(invocation);
}

class _TestHttpClient implements HttpClient {
  final request = _TestRequest();
  @override
  Duration? connectionTimeout;
  Uri? opened;
  bool closed = false;
  int calls = 0;
  @override
  Future<HttpClientRequest> openUrl(String method, Uri url) async {
    calls++;
    opened = url;
    return request;
  }

  @override
  void close({bool force = false}) => closed = force;
  @override
  dynamic noSuchMethod(Invocation invocation) => super.noSuchMethod(invocation);
}
