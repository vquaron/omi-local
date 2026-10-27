import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/env/env.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/pages/settings/local_processing_page.dart';
import 'package:omi/services/local_processing.dart';

import '../unit/local_processing_test.dart' show ProcessingTestSession;

Map<String, dynamic> registry({int revision = 1, String live = 'native'}) {
  final profiles = <Map<String, dynamic>>[
    {'id': 'native', 'stage': 'live', 'kind': 'whisperlivekit', 'name': 'Mac Live', 'settings': {}, 'can_edit': false},
    {
      'id': 'remote',
      'stage': 'live',
      'kind': 'external',
      'name': 'Remote Live',
      'has_key': true,
      'settings': {'url': 'wss://remote.example/asr'},
      'can_edit': true
    },
    {'id': 'stt', 'stage': 'stt', 'kind': 'whisperkit', 'name': 'Mac Final', 'settings': {}, 'can_edit': false},
  ];
  return {
    'revision': revision,
    'profiles': profiles,
    'effective': {
      'live': live.isEmpty ? null : profiles.firstWhere((p) => p['id'] == live),
      'stt': profiles[2],
      'diarization': null,
    }
  };
}

void main() {
  setUp(() => Env.setRuntimeModeForTesting(OmiRuntimeMode.offline));
  tearDown(() => Env.setRuntimeModeForTesting(null));

  Future<void> open(WidgetTester tester, ProcessingTestSession session, LocalProcessingTransport transport) async {
    await tester.pumpWidget(MaterialApp(
      theme: ThemeData.dark(),
      localizationsDelegates: AppLocalizations.localizationsDelegates,
      supportedLocales: AppLocalizations.supportedLocales,
      home:
          LocalProcessingPage(session: session, client: LocalProcessingClient(session: session, transport: transport)),
    ));
    await tester.pumpAndSettle();
  }

  Future<void> tap(WidgetTester tester, String key) async {
    final finder = find.byKey(ValueKey(key));
    await tester.scrollUntilVisible(finder, 160, scrollable: find.byType(Scrollable).last);
    await tester.pumpAndSettle();
    expect(finder.hitTestable(), findsOneWidget);
    await tester.tap(finder);
    await tester.pumpAndSettle();
  }

  testWidgets('explicit live selection uses registry revision and keeps finished STT independent', (tester) async {
    final session = ProcessingTestSession();
    final mutations = <Map<String, dynamic>>[];
    await open(tester, session, (method, uri, _, body, __) async {
      if (method == 'GET') return registry();
      mutations.add(body!);
      expect(uri.path, '/v1/local/processing/activate');
      return registry(revision: 2, live: 'remote');
    });
    await tap(tester, 'processing-live-selection-1');
    await tester.tap(find.text('Remote Live').last);
    await tester.pumpAndSettle();
    expect(mutations, isEmpty);
    await tap(tester, 'processing-live-apply');
    expect(mutations, [
      {'revision': 1, 'stage': 'live', 'id': 'remote'}
    ]);
    expect(find.text('Active: Remote Live'), findsOneWidget);
    expect(find.text('Active: Mac Final'), findsOneWidget);
    expect(find.byKey(const ValueKey('processing-stt-edit')), findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets('live can be disabled while finished STT has no disabled choice', (tester) async {
    Map<String, dynamic>? applied;
    await open(tester, ProcessingTestSession(), (method, _, __, body, ___) async {
      if (method == 'GET') return registry();
      applied = body;
      return registry(revision: 2, live: '');
    });
    final stt =
        tester.widget<DropdownButtonFormField<String>>(find.byKey(const ValueKey('processing-stt-selection-1')));
    expect(stt.initialValue, 'stt');
    await tap(tester, 'processing-live-selection-1');
    await tester.tap(find.text('Off').last);
    await tester.pumpAndSettle();
    await tap(tester, 'processing-live-apply');
    expect(applied, {'revision': 1, 'stage': 'live', 'id': null});
    expect(find.text('Active: Mac Final'), findsOneWidget);
    await tap(tester, 'processing-stt-selection-2');
    expect(find.text('Off').hitTestable(), findsNothing);
    await tester.tap(find.text('Mac Final').last);
    await tester.pumpAndSettle();
  });

  testWidgets('unknown write outcome requires refresh before another action and is not retried', (tester) async {
    var attempts = 0;
    await open(tester, ProcessingTestSession(), (method, _, __, body, ___) async {
      if (method == 'GET') return registry();
      attempts++;
      throw const LocalProcessingException(LocalProcessingFailure.unavailable);
    });
    await tap(tester, 'processing-live-apply');
    expect(attempts, 1);
    expect(tester.widget<FilledButton>(find.byKey(const ValueKey('processing-live-apply'))).onPressed, isNull);
    await tap(tester, 'processing-refresh');
    expect(tester.widget<FilledButton>(find.byKey(const ValueKey('processing-live-apply'))).onPressed, isNotNull);
  });

  testWidgets('legacy server has a clear unsupported message and no settings writes', (tester) async {
    await open(tester, ProcessingTestSession(), (_, __, ___, ____, _____) async {
      throw const LocalProcessingException(LocalProcessingFailure.unsupported);
    });
    expect(find.text('Update the paired server to edit these settings.'), findsOneWidget);
    expect(find.byKey(const ValueKey('processing-live-apply')), findsNothing);
  });

  testWidgets('editing remote profile keeps blank saved key and saving does not activate', (tester) async {
    final requests = <String>[];
    Map<String, dynamic>? saved;
    await open(tester, ProcessingTestSession(), (method, uri, _, body, __) async {
      requests.add(uri.path);
      if (uri.path.endsWith('/save')) saved = body;
      return registry(live: 'remote');
    });
    await tap(tester, 'processing-live-edit');
    final key = tester.widget<TextFormField>(find.byKey(const ValueKey('processing-editor-key')));
    expect(key.controller!.text, isEmpty);
    await tap(tester, 'processing-editor-save');
    expect(saved!['revision'], 1);
    expect(saved!.containsKey('api_key'), isFalse);
    expect(saved!.containsKey('clear_key'), isFalse);
    expect(requests.where((p) => p.endsWith('/activate')), isEmpty);
    expect(find.byKey(const ValueKey('processing-editor-save')), findsNothing);
  });

  testWidgets('new remote STT supports model discovery and keeps credentials off GET requests', (tester) async {
    Map<String, dynamic>? saved;
    await open(tester, ProcessingTestSession(), (method, uri, auth, body, _) async {
      if (method == 'GET') {
        expect(body, isNull);
        return registry();
      }
      if (uri.path.endsWith('/models')) {
        expect((body!['profile'] as Map)['kind'], 'openai-compatible');
        expect(body['api_key'], 'synthetic-provider-key');
        return {
          'state': 'available',
          'models': ['test-model']
        };
      }
      if (uri.path.endsWith('/save')) saved = body;
      return registry(revision: 2);
    });
    await tap(tester, 'processing-stt-add');
    await tester.enterText(find.byKey(const ValueKey('processing-editor-name')), 'Remote STT');
    await tester.enterText(find.byKey(const ValueKey('processing-editor-url')), 'https://stt.example/v1');
    await tester.ensureVisible(find.byKey(const ValueKey('processing-editor-key')));
    await tester.enterText(find.byKey(const ValueKey('processing-editor-key')), 'synthetic-provider-key');
    await tap(tester, 'processing-editor-models');
    final model = find.widgetWithText(ActionChip, 'test-model');
    await tester.ensureVisible(model);
    await tester.tap(model);
    await tester.pumpAndSettle();
    await tap(tester, 'processing-editor-save');
    expect(saved!['api_key'], 'synthetic-provider-key');
    expect((saved!['profile'] as Map)['settings'], {
      'provider_url': 'https://stt.example/v1',
      'model': 'test-model',
      'language': 'auto',
      'diarization_model': 'none',
    });
    expect(find.byKey(const ValueKey('processing-editor-key')), findsNothing);
  });

  testWidgets('saved active profile changes stay pending until confirmed, including a key-only change', (tester) async {
    var data = registry(live: 'remote');
    var activated = 0;
    await open(tester, ProcessingTestSession(), (method, uri, _, body, __) async {
      if (uri.path.endsWith('/save')) {
        expect(body!['api_key'], 'replacement-key');
        final revision = (data['revision'] as int) + 1;
        final saved = {
          ...body['profile'] as Map<String, dynamic>,
          'configuration_revision': revision,
          'can_edit': true
        };
        data = {
          ...data,
          'revision': revision,
          'profiles': [
            for (final profile in data['profiles'] as List) profile['id'] == 'remote' ? saved : profile,
          ],
        };
      } else if (uri.path.endsWith('/activate')) {
        activated++;
        data = {
          ...data,
          'revision': (data['revision'] as int) + 1,
          'effective': {
            ...data['effective'] as Map<String, dynamic>,
            'live': (data['profiles'] as List).firstWhere((profile) => profile['id'] == 'remote'),
          },
        };
      }
      return data;
    });
    for (final changeEndpoint in [false, true]) {
      await tap(tester, 'processing-live-edit');
      await tester.enterText(find.byKey(const ValueKey('processing-editor-key')), 'replacement-key');
      if (changeEndpoint) {
        await tester.enterText(find.byKey(const ValueKey('processing-editor-url')), 'wss://next.example/asr');
      }
      await tap(tester, 'processing-editor-save');
      expect(find.text('Active: Remote Live'), findsOneWidget);
      expect(find.byKey(const ValueKey('processing-live-pending')), findsOneWidget);
      expect(activated, changeEndpoint ? 1 : 0);
      if (changeEndpoint) {
        expect(find.text('Active: wss://remote.example/asr'), findsOneWidget);
        expect(find.text('wss://next.example/asr'), findsOneWidget);
      } else {
        expect(find.byKey(const ValueKey('processing-live-effective-endpoint')), findsNothing);
      }
      await tap(tester, 'processing-live-apply');
      expect(find.byKey(const ValueKey('processing-live-pending')), findsNothing);
      expect(find.byKey(const ValueKey('processing-live-effective-endpoint')), findsNothing);
    }
    expect(activated, 2);
  });

  testWidgets('pairing change closes a credential draft and ignores its pending probe', (tester) async {
    final session = ProcessingTestSession();
    final pending = Completer<Map<String, dynamic>>();
    await open(tester, session, (method, uri, __, ___, ____) async {
      if (uri.path.endsWith('/probe')) return pending.future;
      return registry(live: 'remote');
    });
    await tap(tester, 'processing-live-edit');
    await tester.enterText(find.byKey(const ValueKey('processing-editor-key')), 'draft-key');
    final probe = find.byKey(const ValueKey('processing-editor-probe'));
    await tester.scrollUntilVisible(probe, 160, scrollable: find.byType(Scrollable).last);
    await tester.pumpAndSettle();
    await tester.tap(probe);
    await tester.pump();
    session.change('https://other.example/');
    session.change('https://third.example/');
    pending.complete({'state': 'ready'});
    await tester.pumpAndSettle();
    expect(find.byKey(const ValueKey('processing-editor-key')), findsNothing);
    expect(find.byType(LocalProcessingPage), findsOneWidget);
    await tester.scrollUntilVisible(find.text('https://third.example/'), -160,
        scrollable: find.byType(Scrollable).last);
    await tester.pumpAndSettle();
    expect(find.text('https://third.example/'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('pairing change discards an old response', (tester) async {
    final session = ProcessingTestSession();
    final pending = Completer<Map<String, dynamic>>();
    var reads = 0;
    await open(tester, session, (_, uri, __, ___, ____) async {
      reads++;
      if (reads == 2) return pending.future;
      return registry(live: uri.host == 'other.example' ? 'remote' : 'native');
    });
    await tester.tap(find.byKey(const ValueKey('processing-refresh')));
    await tester.pump();
    session.change('https://other.example/');
    await tester.pumpAndSettle();
    expect(find.text('Active: Remote Live'), findsOneWidget);
    pending.complete(registry());
    await tester.pumpAndSettle();
    expect(find.text('Active: Remote Live'), findsOneWidget);
    expect(find.text('Active: Mac Live'), findsNothing);
  });
}
