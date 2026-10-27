import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/env/dev_env.dart';
import 'package:omi/env/env.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/pages/settings/local_runtime_status_card.dart';
import 'package:omi/services/auth/local_mac_session.dart';
import 'package:omi/services/local_runtime_status.dart';
import 'package:omi/services/capture/local_audio_outbox.dart';
import 'package:omi/utils/offline_network_policy.dart';

Map<String, dynamic> payload({String capture = 'received', String live = 'disabled', num seconds = 12.8}) => {
  'backend': 'ready',
  'capture': {'state': capture, 'audio_seconds': seconds, 'frames_received': 640},
  'live_transcript': {'state': live, 'updates': live == 'streaming' ? 2 : 0},
};

class DraftNotifyingSession extends LocalMacSession {
  DraftNotifyingSession() : super(probe: (_, __) async => {'uid': 'alice'});

  Future<void> saveDraftAndNotify() async {
    await saveSettings('https://synthetic-draft.ngrok.app', 'draft-only');
    notifyListeners();
  }
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUpAll(() {
    Env.init(DevEnv());
    Env.setRuntimeModeForTesting(OmiRuntimeMode.offline);
  });
  tearDownAll(() => Env.setRuntimeModeForTesting(null));
  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    FlutterSecureStorage.setMockInitialValues({});
    await SharedPreferencesUtil.init();
  });
  tearDown(() {
    Env.localTunnelConfigured = false;
    OfflineNetworkPolicy.resetForTesting();
  });

  Future<LocalMacSession> paired() async {
    final session = LocalMacSession(probe: (_, __) async => {'uid': 'alice'});
    await session.connect('https://synthetic.ngrok.app', List.filled(43, 's').join());
    await session.saveServer(name: 'faeon', address: session.address, key: session.accessKey!);
    return session;
  }

  Future<void> showCard(
    WidgetTester tester,
    LocalMacSession session,
    Future<LocalRuntimeStatus> Function() fetcher, {
    bool active = true,
    bool expanded = true,
    GlobalKey<NavigatorState>? navigatorKey,
  }) async {
    await tester.pumpWidget(
      MaterialApp(
        navigatorKey: navigatorKey,
        localizationsDelegates: const [
          AppLocalizations.delegate,
          GlobalMaterialLocalizations.delegate,
          GlobalWidgetsLocalizations.delegate,
          GlobalCupertinoLocalizations.delegate,
        ],
        supportedLocales: AppLocalizations.supportedLocales,
        home: Scaffold(
          body: LocalRuntimeStatusCard(session: session, fetcher: fetcher, active: active),
        ),
      ),
    );
    await tester.pumpAndSettle();
    if (expanded && find.byKey(const ValueKey('local-runtime-details')).evaluate().isEmpty) {
      await tester.tap(find.byKey(const ValueKey('local-runtime-toggle')));
      await tester.pumpAndSettle();
    }
  }

  String value(WidgetTester tester, String name) =>
      tester.widget<Text>(find.byKey(ValueKey('local-runtime-$name'))).data!;

  testWidgets('pending phone storage and storage failures stay distinct from server receipt', (tester) async {
    final outbox = LocalAudioOutbox(directory: Directory('/synthetic-outbox-view'), upload: (_, __) async => {});
    outbox.pendingParts = 2;
    outbox.pendingBytes = 2048;
    LocalAudioOutbox.shared = outbox;
    try {
      await showCard(tester, await paired(), () async => LocalRuntimeStatus.fromJson(payload()));
      expect(tester.widget<Text>(find.byKey(const ValueKey('local-runtime-summary'))).data, contains('Pending: 2'));
      expect(tester.widget<Text>(find.byKey(const ValueKey('local-runtime-outbox'))).data, 'Pending: 2 · 2 KiB');
      outbox.reportCaptureIssue('storage');
      await tester.pump();
      expect(tester.widget<Text>(find.byKey(const ValueKey('local-runtime-outbox'))).data, contains('Pending: 2'));
      expect(tester.widget<Text>(find.byKey(const ValueKey('local-runtime-summary'))).data, contains('Pending: 2'));
      await tester.pumpWidget(const SizedBox.shrink());
    } finally {
      LocalAudioOutbox.shared = null;
      outbox.dispose();
    }
  });

  test('parses independent live state and finite cumulative audio seconds', () {
    final status = LocalRuntimeStatus.fromJson(payload());
    expect(status.liveTranscriptState, LocalLiveTranscriptState.disabled);
    expect(status.audioSeconds, 12.8);
    expect(status.framesReceived, 640);
    expect(status.diarizationState, LocalDiarizationState.unknown);
  });

  testWidgets('offline header reports recording on the phone and keeps its server name when expanded', (tester) async {
    final session = await paired();
    final outbox = LocalAudioOutbox(directory: Directory('/synthetic-outbox-status'), upload: (_, __) async => {});
    final recording = outbox.begin(source: 'phone', codec: 'pcm16', address: session.address, key: session.accessKey!);
    outbox.pendingParts = 2;
    outbox.pendingBytes = 2048;
    outbox.deliveryFailed = true;
    LocalAudioOutbox.shared = outbox;
    try {
      await showCard(tester, session, () async => throw LocalRuntimeStatusUnavailable(), expanded: false);
      final summary = value(tester, 'summary');
      expect(summary, 'faeon · Recording · Phone · Offline · Pending: 2');
      expect(summary, isNot(contains('Error')));
      expect(summary, isNot(contains('Local Mac')));
      await tester.tap(find.byKey(const ValueKey('local-runtime-toggle')));
      await tester.pumpAndSettle();
      expect(
        value(tester, 'summary'),
        summary,
        reason: 'Opening details cannot replace the real state with generic Error',
      );
      expect(value(tester, 'connection'), 'Not Connected');
      expect(value(tester, 'outbox'), 'Pending: 2 · 2 KiB');
      outbox.reportCaptureIssue('storage');
      await tester.pump();
      expect(value(tester, 'summary'), contains('Sync failed'));
      expect(value(tester, 'summary'), contains('Pending: 2'));
      await tester.pumpWidget(const SizedBox());
    } finally {
      await recording.stop();
      LocalAudioOutbox.shared = null;
      outbox.dispose();
    }
  });

  testWidgets('active header name ignores saved drafts and a different key on the same address', (tester) async {
    final session = await paired();
    final otherKey = List.filled(43, 'r').join();
    final draft = await session.saveServer(name: 'Unconnected draft', address: session.address, key: otherKey);
    await session.saveSettings(draft.address, draft.key, name: draft.name, serverId: draft.id);
    await showCard(tester, session, () async => LocalRuntimeStatus.fromJson(payload()), expanded: false);
    expect(value(tester, 'summary'), 'faeon · Connected');
    expect(value(tester, 'summary'), isNot(contains('Unconnected draft')));
    await tester.pumpWidget(const SizedBox());
  });

  testWidgets('diarization readiness and actual labels are independent of live text', (tester) async {
    final session = await paired();
    for (final (state, expected, count) in [
      ('disabled', 'Off', 0),
      ('ready', 'Model Ready', 0),
      ('pending', 'Waiting for data...', 0),
      ('labeled', '3 processed', 3),
      ('degraded', 'Cannot identify speakers.', 0),
      ('unknown', 'Unknown', 0),
    ]) {
      await showCard(
        tester,
        session,
        () async => LocalRuntimeStatus.fromJson({
          ...payload(live: 'streaming'),
          'diarization': {'state': state, 'labeled_segments': count},
        }),
      );
      expect(value(tester, 'diarization'), expected);
      expect(value(tester, 'transcript'), 'Transcript received: 2');
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox());
    }
  });

  test('invalid label evidence cannot claim working diarization', () {
    for (final count in [-1, 0, '3']) {
      expect(
        () => LocalRuntimeStatus.fromJson({
          ...payload(),
          'diarization': {'state': 'labeled', 'labeled_segments': count},
        }),
        throwsFormatException,
      );
    }
  });

  test('unknown and malformed payloads never become a healthy status', () {
    final malformed = <Object?>[
      null,
      {},
      {...payload(), 'backend': 'unknown'},
      payload(capture: 'recording'),
      payload(live: 'enabled'),
      payload(seconds: -1),
      payload(seconds: double.infinity),
      {
        ...payload(),
        'capture': {'state': 'received', 'audio_seconds': '12', 'frames_received': 1},
      },
      {
        ...payload(),
        'capture': {'state': 'received', 'audio_seconds': 12, 'frames_received': 1.5},
      },
      {
        ...payload(),
        'live_transcript': {'state': 'streaming', 'updates': -1},
      },
    ];
    for (final data in malformed) {
      expect(() => LocalRuntimeStatus.fromJson(data), throwsFormatException);
    }
  });

  test('client refuses an unpaired session without making a request', () async {
    final client = LocalRuntimeStatusClient(session: LocalMacSession());
    await expectLater(client.fetch(), throwsA(isA<LocalMacUnauthorized>()));
  });

  testWidgets('backend authentication does not imply live transcription is enabled', (tester) async {
    await showCard(tester, await paired(), () async => LocalRuntimeStatus.fromJson(payload()));
    expect(value(tester, 'connection'), 'Connected · API Key Auth');
    expect(value(tester, 'transcript'), 'Off');
    expect(value(tester, 'audio'), 'Total: 12 seconds');
    expect(find.textContaining('receiving'), findsNothing);
    await tester.pumpWidget(const SizedBox());
  });

  test('provider stages parse leniently and never fail the status', () {
    expect(LocalRuntimeStatus.fromJson(payload()).stages, isEmpty);
    final status = LocalRuntimeStatus.fromJson({
      ...payload(),
      'stages': {
        'checked_at': 5,
        'stale': true,
        'stt': {'status': 'ready', 'kind': 'openai-compatible', 'name': 'GPU whisper'},
        'diarization': {'status': 'unavailable', 'kind': 'mycelia', 'name': 'GPU pyannote'},
        'live': {'status': 'disabled'},
        'summary': {'status': 'surprising'},
        'bogus': {'status': 'ready'},
      },
    });
    expect(status.stages.keys, ['stt', 'diarization', 'live', 'summary']);
    expect(
      status.stages['stt'],
      const LocalStageStatus(state: LocalStageState.ready, name: 'GPU whisper', kind: 'openai-compatible'),
    );
    expect(status.stages['diarization']!.state, LocalStageState.unavailable);
    expect(status.stages['live'], const LocalStageStatus(state: LocalStageState.disabled));
    expect(status.stages['summary']!.state, LocalStageState.unknown);
    expect(status.stagesStale, isTrue);
    expect(LocalRuntimeStatus.fromJson({...payload(), 'stages': 'garbage'}).stages, isEmpty);
  });

  testWidgets('expanded card lists server-side stages with provider names', (tester) async {
    await showCard(
      tester,
      await paired(),
      () async => LocalRuntimeStatus.fromJson({
        ...payload(),
        'diarization': {'state': 'labeled', 'labeled_segments': 3},
        'stages': {
          'checked_at': 5,
          'stale': false,
          'stt': {'status': 'ready', 'kind': 'openai-compatible', 'name': 'GPU whisper'},
          'diarization': {'status': 'unavailable', 'kind': 'mycelia', 'name': 'GPU pyannote'},
          'live': {'status': 'ready', 'kind': 'external', 'name': 'GPU WLK'},
          'summary': {'status': 'disabled'},
        },
      }),
    );
    expect(value(tester, 'stage-stt'), 'GPU whisper · Model Ready');
    expect(value(tester, 'stage-diarization'), 'GPU pyannote · Server unavailable');
    expect(value(tester, 'stage-summary'), 'Off');
    expect(value(tester, 'diarization'), '3 processed');
    expect(find.byKey(const ValueKey('local-runtime-stage-live')), findsNothing);
    await tester.pumpWidget(const SizedBox());
  });

  testWidgets('stage rows are absent when the server does not report them', (tester) async {
    await showCard(tester, await paired(), () async => LocalRuntimeStatus.fromJson(payload()));
    expect(find.byKey(const ValueKey('local-runtime-stage-stt')), findsNothing);
    expect(find.byKey(const ValueKey('local-runtime-stage-diarization')), findsNothing);
    await tester.pumpWidget(const SizedBox());
  });

  testWidgets('failed refresh clears all stale healthy and cumulative values', (tester) async {
    var calls = 0;
    await showCard(tester, await paired(), () async {
      if (++calls == 1) return LocalRuntimeStatus.fromJson(payload(live: 'streaming'));
      throw LocalRuntimeStatusUnavailable();
    });
    expect(value(tester, 'transcript'), 'Transcript received: 2');
    await tester.tap(find.byKey(const ValueKey('local-runtime-refresh')));
    await tester.pumpAndSettle();
    expect(value(tester, 'connection'), 'Not Connected');
    expect(value(tester, 'audio'), 'Unknown');
    expect(value(tester, 'transcript'), 'Unknown');
    expect(value(tester, 'diarization'), 'Unknown');
    await tester.pumpWidget(const SizedBox());
  });

  testWidgets('collapsed source and connection share one row at phone width', (tester) async {
    final session = await paired();
    for (final source in [ConversationSource.omi, ConversationSource.phone]) {
      await tester.pumpWidget(
        MaterialApp(
          locale: const Locale('ru'),
          theme: ThemeData(platform: TargetPlatform.iOS),
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          home: Scaffold(
            body: SizedBox(
              width: 320,
              child: LocalRuntimeStatusCard(
                session: session,
                source: source,
                fetcher: () async => LocalRuntimeStatus.fromJson(payload()),
              ),
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();
      final sourceLabel = find.byKey(const Key('recording_source_label'));
      final summary = find.byKey(const Key('local-runtime-summary'));
      expect(tester.getCenter(sourceLabel).dy, closeTo(tester.getCenter(summary).dy, 1));
      expect(tester.getSize(find.byKey(const Key('local-runtime-status'))).height, lessThanOrEqualTo(52));
      await tester.tap(find.byKey(const Key('local-runtime-toggle')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('local-runtime-details')), findsOneWidget);
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox());
    }
  });

  testWidgets('compact pill expands and keeps its chosen state across polling', (tester) async {
    var calls = 0;
    await showCard(tester, await paired(), () async {
      calls++;
      return LocalRuntimeStatus.fromJson(payload());
    }, expanded: false);
    expect(value(tester, 'summary'), 'faeon · Connected');
    expect(find.byKey(const ValueKey('local-runtime-details')), findsNothing);
    expect(tester.getSize(find.byKey(const ValueKey('local-runtime-status'))).height, lessThanOrEqualTo(52));
    await tester.tap(find.byKey(const ValueKey('local-runtime-toggle')));
    await tester.pumpAndSettle();
    expect(value(tester, 'audio'), 'Total: 12 seconds');
    await tester.pump(const Duration(seconds: 5));
    await tester.pump();
    expect(calls, 2);
    expect(find.byKey(const ValueKey('local-runtime-details')), findsOneWidget);
    await tester.tap(find.byKey(const ValueKey('local-runtime-toggle')));
    await tester.pumpAndSettle();
    await tester.pump(const Duration(seconds: 5));
    await tester.pump();
    expect(calls, 3);
    expect(find.byKey(const ValueKey('local-runtime-details')), findsNothing);
    await tester.pumpWidget(const SizedBox());
  });

  testWidgets('pending refresh keeps confirmed values but a later failure clears them', (tester) async {
    final refresh = Completer<LocalRuntimeStatus>();
    var calls = 0;
    await showCard(tester, await paired(), () async {
      if (++calls == 1) return LocalRuntimeStatus.fromJson(payload(live: 'streaming'));
      return refresh.future;
    });
    await tester.tap(find.byKey(const ValueKey('local-runtime-refresh')));
    await tester.pumpAndSettle();
    expect(value(tester, 'summary'), 'faeon · Connected');
    expect(value(tester, 'connection'), 'Connected · API Key Auth');
    expect(value(tester, 'audio'), 'Total: 12 seconds');
    expect(value(tester, 'transcript'), 'Transcript received: 2');
    expect(find.text('Loading...'), findsNothing);
    // This legacy response has no diarization evidence, even while ASR is healthy.
    expect(value(tester, 'diarization'), 'Unknown');
    expect(find.byKey(const ValueKey('local-runtime-details')), findsOneWidget);
    refresh.completeError(LocalRuntimeStatusUnavailable());
    await tester.pumpAndSettle();
    expect(value(tester, 'summary'), 'faeon · Not Connected');
    expect(value(tester, 'audio'), 'Unknown');
    expect(value(tester, 'transcript'), 'Unknown');
    expect(value(tester, 'diarization'), 'Unknown');
    await tester.tap(find.byKey(const ValueKey('local-runtime-toggle')));
    await tester.pumpAndSettle();
    expect(value(tester, 'summary'), 'faeon · Not Connected');
    expect(tester.getSize(find.byKey(const Key('local-runtime-status'))).height, lessThanOrEqualTo(52));
    await tester.pumpWidget(const SizedBox());
  });

  testWidgets('draft notifications do not refresh or clear authenticated status', (tester) async {
    final session = DraftNotifyingSession();
    await session.connect('https://synthetic.ngrok.app', List.filled(43, 's').join());
    var calls = 0;
    await showCard(tester, session, () async {
      calls++;
      return LocalRuntimeStatus.fromJson(payload());
    });
    await session.saveDraftAndNotify();
    await tester.pumpAndSettle();
    expect(calls, 1);
    expect(value(tester, 'audio'), 'Total: 12 seconds');
    expect(value(tester, 'connection'), 'Connected · API Key Auth');
    expect(find.byKey(const ValueKey('local-runtime-details')), findsOneWidget);
    await tester.pumpWidget(const SizedBox());
  });

  testWidgets('changing pairing clears confirmed values before the new request completes', (tester) async {
    final session = await paired();
    final next = Completer<LocalRuntimeStatus>();
    var calls = 0;
    await showCard(tester, session, () async {
      if (++calls == 1) return LocalRuntimeStatus.fromJson(payload());
      return next.future;
    });
    await session.connect('https://second-synthetic.ngrok.app', List.filled(43, 't').join());
    await tester.pumpAndSettle();
    expect(value(tester, 'audio'), 'Unknown');
    expect(value(tester, 'transcript'), 'Unknown');
    expect(value(tester, 'diarization'), 'Unknown');
    expect(value(tester, 'connection'), 'Loading...');
    next.complete(LocalRuntimeStatus.fromJson(payload(seconds: 3)));
    await tester.pumpAndSettle();
    expect(value(tester, 'audio'), 'Total: 3 seconds');
    await tester.pumpWidget(const SizedBox());
  });

  testWidgets('rejected status shows the key error without changing pairing', (tester) async {
    final session = await paired();
    await showCard(tester, session, () async => throw LocalMacUnauthorized());
    expect(value(tester, 'connection'), 'The access key was rejected. Check the key on your Mac.');
    expect(session.isSignedIn, isTrue);
    expect(value(tester, 'transcript'), 'Unknown');
    expect(value(tester, 'diarization'), 'Unknown');
    await tester.pumpWidget(const SizedBox());
  });

  testWidgets('no status requests before a successful pairing', (tester) async {
    var calls = 0;
    await showCard(tester, LocalMacSession(), () async {
      calls++;
      return LocalRuntimeStatus.fromJson(payload());
    });
    await tester.pump(const Duration(seconds: 20));
    expect(calls, 0);
    expect(value(tester, 'connection'), 'Not Connected');
    await tester.pumpWidget(const SizedBox());
  });

  testWidgets('periodic polling stops when disposed', (tester) async {
    var calls = 0;
    await showCard(tester, await paired(), () async {
      calls++;
      return LocalRuntimeStatus.fromJson(payload());
    });
    expect(calls, 1);
    await tester.pump(const Duration(seconds: 5));
    await tester.pump();
    expect(calls, 2);
    await tester.pumpWidget(const SizedBox());
    await tester.pump(const Duration(seconds: 20));
    expect(calls, 2);
    expect(tester.takeException(), isNull);
  });

  testWidgets('hidden tab and background app do not poll', (tester) async {
    var calls = 0;
    final session = await paired();
    Future<LocalRuntimeStatus> fetch() async {
      calls++;
      return LocalRuntimeStatus.fromJson(payload());
    }

    await showCard(tester, session, fetch, active: false);
    await tester.pump(const Duration(seconds: 20));
    expect(calls, 0);
    await showCard(tester, session, fetch);
    expect(calls, 1);
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.inactive);
    await tester.pump();
    expect(value(tester, 'audio'), 'Total: 12 seconds');
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.paused);
    await tester.pump(const Duration(seconds: 20));
    expect(calls, 1);
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
    await tester.pumpAndSettle();
    expect(calls, 2);
    await showCard(tester, session, fetch, active: false);
    await tester.pump(const Duration(seconds: 20));
    expect(calls, 2);
    await tester.pumpWidget(const SizedBox());
  });

  testWidgets('covered route stops polling and resumes after pop', (tester) async {
    var calls = 0;
    final navigator = GlobalKey<NavigatorState>();
    await showCard(tester, await paired(), () async {
      calls++;
      return LocalRuntimeStatus.fromJson(payload());
    }, navigatorKey: navigator);
    expect(calls, 1);
    unawaited(navigator.currentState!.push(MaterialPageRoute<void>(builder: (_) => const Scaffold())));
    await tester.pumpAndSettle();
    await tester.pump(const Duration(seconds: 20));
    expect(calls, 1);
    navigator.currentState!.pop();
    await tester.pumpAndSettle();
    expect(calls, 2);
    await tester.pumpWidget(const SizedBox());
  });

  testWidgets('late result from the previous pairing cannot replace the current status', (tester) async {
    final session = await paired();
    final oldRequest = Completer<LocalRuntimeStatus>();
    var calls = 0;
    await showCard(tester, session, () async {
      if (++calls == 1) return oldRequest.future;
      return LocalRuntimeStatus.fromJson(payload(live: 'disabled', seconds: 3));
    });
    await session.connect('https://second-synthetic.ngrok.app', List.filled(43, 't').join());
    await tester.pumpAndSettle();
    expect(value(tester, 'audio'), 'Total: 3 seconds');
    oldRequest.complete(LocalRuntimeStatus.fromJson(payload(live: 'streaming', seconds: 99)));
    await tester.pumpAndSettle();
    expect(value(tester, 'audio'), 'Total: 3 seconds');
    expect(value(tester, 'transcript'), 'Off');
    await tester.pumpWidget(const SizedBox());
  });
}
