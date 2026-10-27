import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/message_event.dart';
import 'package:omi/backend/schema/transcript_segment.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/widgets/transcript.dart';

void main() {
  setUpAll(() async {
    TestWidgetsFlutterBinding.ensureInitialized();
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
  });

  /// Helper to reset SharedPreferences with optional cached people
  Future<void> setupSharedPreferences({List<Map<String, dynamic>>? cachedPeople}) async {
    final values = <String, Object>{};
    if (cachedPeople != null) {
      values['cachedPeople'] = cachedPeople.map((p) => jsonEncode(p)).toList();
    }
    SharedPreferences.setMockInitialValues(values);
    await SharedPreferencesUtil.init();
  }

  TranscriptSegment segmentFor(String id, int speakerId) {
    // Note: speakerId is extracted from speaker string by TranscriptSegment constructor
    return TranscriptSegment(
      id: id,
      text: 'Hello world',
      speaker: 'SPEAKER_0$speakerId',
      isUser: false,
      personId: null,
      start: 0.0,
      end: 1.0,
      translations: [],
    );
  }

  testWidgets('both translation directions appear below their unchanged originals', (tester) async {
    final segment = TranscriptSegment.fromJson({
      'id': 'local-preview-synthetic-line-0',
      'text': 'Добрый день.',
      'speaker': 'SPEAKER_00',
      'is_user': false,
      'start': 0.0,
      'end': 1.0,
      'translations': [
        {'lang': 'es', 'text': 'Buenos días.'}
      ],
    });
    final reply = TranscriptSegment.fromJson({
      'id': 'local-preview-synthetic-line-1',
      'text': '¿Cómo estás?',
      'speaker': 'SPEAKER_01',
      'is_user': false,
      'start': 2.0,
      'end': 3.0,
      'translations': [
        {'lang': 'ru', 'text': 'Как дела?'}
      ],
    });
    await tester.pumpWidget(MaterialApp(
      localizationsDelegates: const [
        AppLocalizations.delegate,
        GlobalMaterialLocalizations.delegate,
        GlobalWidgetsLocalizations.delegate,
        GlobalCupertinoLocalizations.delegate,
      ],
      supportedLocales: AppLocalizations.supportedLocales,
      home: Scaffold(body: TranscriptWidget(segments: [segment, reply], isConversationDetail: false)),
    ));
    await tester.pumpAndSettle();
    final original = find.byWidgetPredicate(
      (widget) => widget is RichText && widget.text.toPlainText() == 'Добрый день.',
    );
    final translated = find.text('Buenos días.');
    expect(original, findsOneWidget);
    expect(translated, findsOneWidget);
    expect(tester.getTopLeft(translated).dy, greaterThan(tester.getBottomLeft(original).dy));
    final replyOriginal = find.byWidgetPredicate(
      (widget) => widget is RichText && widget.text.toPlainText() == '¿Cómo estás?',
    );
    expect(replyOriginal, findsOneWidget);
    expect(find.text('Как дела?'), findsOneWidget);
    expect(tester.getTopLeft(find.text('Как дела?')).dy, greaterThan(tester.getBottomLeft(replyOriginal).dy));
  });

  group('Speaker label display', () {
    testWidgets('shows person name when personId is set and in cache', (tester) async {
      final now = DateTime.now();
      await setupSharedPreferences(
        cachedPeople: [
          {
            'id': 'person-123',
            'name': 'Alice',
            'created_at': now.toUtc().toIso8601String(),
            'updated_at': now.toUtc().toIso8601String(),
          },
        ],
      );

      final segment = TranscriptSegment(
        id: 'seg1',
        text: 'Hello world',
        speaker: 'SPEAKER_01',
        isUser: false,
        personId: 'person-123',
        start: 0.0,
        end: 1.0,
        translations: [],
      );

      await tester.pumpWidget(
        MaterialApp(
          localizationsDelegates: const [
            AppLocalizations.delegate,
            GlobalMaterialLocalizations.delegate,
            GlobalWidgetsLocalizations.delegate,
            GlobalCupertinoLocalizations.delegate,
          ],
          supportedLocales: AppLocalizations.supportedLocales,
          home: Scaffold(body: TranscriptWidget(segments: [segment], isConversationDetail: false)),
        ),
      );
      await tester.pumpAndSettle();

      // Should show person name
      expect(find.text('Alice'), findsOneWidget);
      expect(find.text('Speaker 2'), findsNothing);
    });

    testWidgets('shows Speaker X when no person is assigned', (tester) async {
      final segment = segmentFor('seg2', 0);

      await tester.pumpWidget(
        MaterialApp(
          localizationsDelegates: const [
            AppLocalizations.delegate,
            GlobalMaterialLocalizations.delegate,
            GlobalWidgetsLocalizations.delegate,
            GlobalCupertinoLocalizations.delegate,
          ],
          supportedLocales: AppLocalizations.supportedLocales,
          home: Scaffold(body: TranscriptWidget(segments: [segment], isConversationDetail: false)),
        ),
      );
      await tester.pumpAndSettle();

      // Should show Speaker X fallback
      expect(find.text('Speaker 1'), findsOneWidget);
    });

    testWidgets('local timed speakers keep Omi cards, provider and time ranges', (tester) async {
      final first = segmentFor('first', 0)..sttProvider = 'WhisperLiveKit';
      final second = segmentFor('second', 1)
        ..start = 2
        ..end = 4
        ..text = 'Second voice'
        ..sttProvider = 'WhisperLiveKit';
      await tester.pumpWidget(MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: Scaffold(body: TranscriptWidget(segments: [first, second], bottomMargin: 0)),
      ));
      await tester.pumpAndSettle();
      expect(find.text('Speaker 1'), findsOneWidget);
      expect(find.text('Speaker 2'), findsOneWidget);
      expect(find.text('WhisperLiveKit'), findsNWidgets(2));
      expect(find.text('00:00:02 - 00:00:04'), findsOneWidget);
      final colors = tester.widgetList<CircleAvatar>(find.byType(CircleAvatar)).map((w) => w.backgroundColor).toSet();
      expect(colors.length, 2);
    });

    testWidgets('unknown draft is neutral and does not invent a speaker or phrase timing', (tester) async {
      final draft = segmentFor('draft', 0)
        ..speaker = null
        ..speakerId = -1
        ..isDraft = true
        ..sttProvider = 'Local STT';
      await tester.pumpWidget(MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: Scaffold(body: TranscriptWidget(segments: [draft], bottomMargin: 0)),
      ));
      await tester.pumpAndSettle();
      expect(find.text('Unknown'), findsOneWidget);
      expect(find.text('Speaker 1'), findsNothing);
      expect(find.text('00:00:00 - 00:00:01'), findsNothing);
      expect(find.text('Local STT'), findsOneWidget);
      expect(tester.takeException(), isNull);
    });

    testWidgets('Tag button is removed from UI', (tester) async {
      final segment = segmentFor('seg3', 1);
      final suggestion = SpeakerLabelSuggestionEvent(
        speakerId: 1,
        personId: 'person-456',
        personName: 'Bob',
        segmentId: 'seg3',
      );

      await tester.pumpWidget(
        MaterialApp(
          localizationsDelegates: const [
            AppLocalizations.delegate,
            GlobalMaterialLocalizations.delegate,
            GlobalWidgetsLocalizations.delegate,
            GlobalCupertinoLocalizations.delegate,
          ],
          supportedLocales: AppLocalizations.supportedLocales,
          home: Scaffold(
            body: TranscriptWidget(segments: [segment], isConversationDetail: true, suggestions: {'seg3': suggestion}),
          ),
        ),
      );
      await tester.pumpAndSettle();

      // Tag button should no longer exist
      expect(find.text('Tag'), findsNothing);
    });
  });

  test('a transcript scroll-state store resets on a fresh page but reuses a live session', () {
    final store = TranscriptScrollStateStore();
    final first = store.forSession('live-session');
    first.update(offset: 120, isAtBottom: false);

    expect(store.forSession('live-session'), same(first));
    expect(store.forSession('live-session').isAtBottom, isFalse);

    final nextSession = store.forSession('next-session');
    expect(nextSession, isNot(same(first)));
    expect(nextSession.hasPosition, isFalse);

    final freshPageStore = TranscriptScrollStateStore();
    expect(freshPageStore.forSession('live-session').hasPosition, isFalse);
  });

  group('Live transcript scrolling', () {
    late ScrollController controller;
    late TranscriptScrollState scrollState;
    late List<TranscriptSegment> segments;
    late int contentVersion;
    late String layoutIdentity;
    late List<Widget> leadingItems;
    late List<String> leadingItemIds;

    Future<void> pumpTranscript(WidgetTester tester, {bool settle = true}) async {
      await tester.pumpWidget(
        MaterialApp(
          localizationsDelegates: const [
            AppLocalizations.delegate,
            GlobalMaterialLocalizations.delegate,
            GlobalWidgetsLocalizations.delegate,
            GlobalCupertinoLocalizations.delegate,
          ],
          supportedLocales: AppLocalizations.supportedLocales,
          home: Scaffold(
            body: SizedBox(
              height: 320,
              child: TranscriptWidget(
                key: const ValueKey('live-transcript-test-session'),
                segments: segments,
                bottomMargin: 0,
                followLatest: true,
                scrollState: scrollState,
                jumpToLatestButtonBottom: 84,
                onScrollControllerReady: (value) => controller = value,
                contentVersion: contentVersion,
                layoutIdentity: layoutIdentity,
                leadingItems: leadingItems,
                leadingItemIds: leadingItemIds,
              ),
            ),
          ),
        ),
      );
      if (settle) await tester.pumpAndSettle();
    }

    setUp(() {
      scrollState = TranscriptScrollState();
      contentVersion = 0;
      layoutIdentity = 'transcript';
      leadingItems = [];
      leadingItemIds = [];
      segments = List.generate(
        30,
        (index) => TranscriptSegment(
          id: 'live-$index',
          text: 'Transcript segment $index with enough words to make this a long conversation.',
          speaker: 'SPEAKER_0${index % 2}',
          isUser: false,
          personId: null,
          start: index.toDouble(),
          end: index + 1.0,
          translations: const [],
        ),
      );
    });

    testWidgets('a fresh long transcript opens at the latest segment', (tester) async {
      await pumpTranscript(tester);

      expect(controller.offset, closeTo(controller.position.maxScrollExtent, 0.1));
      expect(scrollState.isAtBottom, isTrue);
      expect(find.byKey(const ValueKey('transcript_jump_to_latest')), findsNothing);
    });

    testWidgets('a fresh transcript load starts at the latest segment', (tester) async {
      await pumpTranscript(tester);
      await tester.drag(find.byType(ListView), const Offset(0, 260));
      await tester.pumpAndSettle();

      expect(scrollState.isAtBottom, isFalse);
      expect(find.byKey(const ValueKey('transcript_jump_to_latest')), findsOneWidget);

      await tester.pumpWidget(const SizedBox.shrink());
      scrollState = TranscriptScrollState();
      await pumpTranscript(tester);

      expect(controller.offset, closeTo(controller.position.maxScrollExtent, 0.1));
      expect(scrollState.isAtBottom, isTrue);
      expect(find.byKey(const ValueKey('transcript_jump_to_latest')), findsNothing);
    });

    testWidgets('the jump control animates to the latest segment and hides', (tester) async {
      await pumpTranscript(tester);
      await tester.drag(find.byType(ListView), const Offset(0, 260));
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const ValueKey('transcript_jump_to_latest')));
      await tester.pumpAndSettle();

      expect(controller.offset, closeTo(controller.position.maxScrollExtent, 0.1));
      expect(scrollState.isAtBottom, isTrue);
      expect(find.byKey(const ValueKey('transcript_jump_to_latest')), findsNothing);
    });

    testWidgets('manual scrolling up stays put while live transcript grows', (tester) async {
      await pumpTranscript(tester);
      await tester.drag(find.byType(ListView), const Offset(0, 260));
      await tester.pumpAndSettle();

      final preservedOffset = controller.offset;
      expect(scrollState.isAtBottom, isFalse);

      segments = [
        ...segments,
        TranscriptSegment(
          id: 'live-new',
          text: List.filled(80, 'new live words').join(' '),
          speaker: 'SPEAKER_00',
          isUser: false,
          personId: null,
          start: segments.length.toDouble(),
          end: segments.length + 1.0,
          translations: const [],
        ),
      ];
      contentVersion++;
      await pumpTranscript(tester);

      expect(controller.offset, closeTo(preservedOffset, 0.1));
      expect(scrollState.isAtBottom, isFalse);
      expect(find.byKey(const ValueKey('transcript_jump_to_latest')), findsOneWidget);
    });

    testWidgets('manual scrolling up stays put when live content arrives mid-gesture', (tester) async {
      await pumpTranscript(tester);
      final gestureStart = tester.getCenter(find.byType(ListView));
      final gesture = await tester.startGesture(gestureStart);
      for (var step = 0; step < 5; step++) {
        await gesture.moveBy(const Offset(0, 52));
        await tester.pump(const Duration(milliseconds: 10));
      }
      await tester.pump();

      final preservedOffset = controller.offset;
      segments = [
        ...segments,
        TranscriptSegment(
          id: 'live-mid-gesture',
          text: List.filled(80, 'new live words').join(' '),
          speaker: 'SPEAKER_00',
          isUser: false,
          personId: null,
          start: segments.length.toDouble(),
          end: segments.length + 1.0,
          translations: const [],
        ),
      ];
      contentVersion++;
      await pumpTranscript(tester, settle: false);
      await tester.pump(const Duration(milliseconds: 16));

      expect(controller.offset, closeTo(preservedOffset, 0.1));
      expect(scrollState.isAtBottom, isFalse);
      expect(find.byKey(const ValueKey('transcript_jump_to_latest')), findsOneWidget);

      await gesture.up();
      await tester.pumpAndSettle();
    });

    testWidgets('a drag that starts at the live edge survives snapshots arriving mid-gesture', (tester) async {
      await pumpTranscript(tester);
      expect(scrollState.isAtBottom, isTrue);
      final gesture = await tester.startGesture(tester.getCenter(find.byType(ListView)));
      // The first pixels of a drag away from the bottom are still "at bottom"
      // for the follow heuristic; a live snapshot must not snap back here.
      await gesture.moveBy(const Offset(0, 30)); // consumes the touch slop
      await tester.pump(const Duration(milliseconds: 16));
      await gesture.moveBy(const Offset(0, 12));
      await tester.pump(const Duration(milliseconds: 16));
      final draggedOffset = controller.offset;
      expect(controller.position.maxScrollExtent - draggedOffset, closeTo(12, 1));

      // WhisperLiveKit replaces the whole hypothesis several times per second.
      for (var revision = 1; revision <= 6; revision++) {
        segments = [
          ...segments.take(29),
          TranscriptSegment(
            id: 'live-draft',
            text: List.filled(10 + revision * 7, 'draft').join(' '),
            speaker: 'SPEAKER_00',
            isUser: false,
            personId: null,
            start: 29,
            end: 30,
            translations: const [],
          ),
        ];
        contentVersion++;
        await pumpTranscript(tester, settle: false);
        await tester.pump(const Duration(milliseconds: 16));
        final before = controller.offset;
        await gesture.moveBy(const Offset(0, 30));
        await tester.pump(const Duration(milliseconds: 16));
        // The finger must still own the list: nothing may cancel the drag.
        expect(before - controller.offset, closeTo(30, 1), reason: 'drag cancelled at revision $revision');
      }
      await gesture.up();
      await tester.pumpAndSettle();
      expect(scrollState.isAtBottom, isFalse);
      expect(find.byKey(const ValueKey('transcript_jump_to_latest')), findsOneWidget);

      await tester.tap(find.byKey(const ValueKey('transcript_jump_to_latest')));
      await tester.pumpAndSettle();
      expect(controller.offset, closeTo(controller.position.maxScrollExtent, 0.5));
      expect(scrollState.isAtBottom, isTrue);
    });

    testWidgets('letting go near the live edge resumes following', (tester) async {
      await pumpTranscript(tester);
      final gesture = await tester.startGesture(tester.getCenter(find.byType(ListView)));
      await gesture.moveBy(const Offset(0, 30));
      await tester.pump(const Duration(milliseconds: 16));
      await gesture.moveBy(const Offset(0, 12));
      await tester.pump(const Duration(milliseconds: 16));
      await gesture.up();
      await tester.pumpAndSettle();
      expect(scrollState.isAtBottom, isTrue);

      segments = [
        ...segments,
        TranscriptSegment(
          id: 'live-after-release',
          text: List.filled(60, 'more live words').join(' '),
          speaker: 'SPEAKER_00',
          isUser: false,
          personId: null,
          start: 30,
          end: 31,
          translations: const [],
        ),
      ];
      contentVersion++;
      await pumpTranscript(tester);
      expect(controller.offset, closeTo(controller.position.maxScrollExtent, 0.5));
      expect(find.byKey(const ValueKey('transcript_jump_to_latest')), findsNothing);
    });

    testWidgets('same-ID transcript growth keeps following the live edge', (tester) async {
      await pumpTranscript(tester);

      final last = segments.last;
      segments = [
        ...segments.take(segments.length - 1),
        TranscriptSegment(
          id: last.id,
          text: List.filled(80, 'revised transcript text').join(' '),
          speaker: last.speaker,
          isUser: last.isUser,
          personId: last.personId,
          start: last.start,
          end: last.end,
          translations: const [],
        ),
      ];
      contentVersion++;
      await pumpTranscript(tester);

      expect(controller.offset, closeTo(controller.position.maxScrollExtent, 0.1));
      expect(scrollState.isAtBottom, isTrue);
      expect(find.byKey(const ValueKey('transcript_jump_to_latest')), findsNothing);
    });

    testWidgets('growth inside an existing photo group keeps following the live edge', (tester) async {
      layoutIdentity = 'photo-timeline';
      leadingItemIds = ['photo-group-1'];
      leadingItems = const [SizedBox(height: 80)];
      await pumpTranscript(tester);

      leadingItems = const [SizedBox(height: 280)];
      contentVersion++;
      await pumpTranscript(tester);

      expect(controller.offset, closeTo(controller.position.maxScrollExtent, 0.1));
      expect(scrollState.isAtBottom, isTrue);
    });

    testWidgets('overlapping live updates coalesce into one settled bottom follow', (tester) async {
      await pumpTranscript(tester);

      for (var update = 0; update < 3; update++) {
        segments = [
          ...segments,
          TranscriptSegment(
            id: 'overlap-$update',
            text: List.filled(12, 'new live words').join(' '),
            speaker: 'SPEAKER_00',
            isUser: false,
            personId: null,
            start: segments.length.toDouble(),
            end: segments.length + 1.0,
            translations: const [],
          ),
        ];
        contentVersion++;
        await pumpTranscript(tester, settle: false);
        await tester.pump(const Duration(milliseconds: 40));
      }
      await tester.pumpAndSettle();

      expect(controller.offset, closeTo(controller.position.maxScrollExtent, 0.1));
      expect(scrollState.isAtBottom, isTrue);
    });

    testWidgets('layout changes restore the same transcript anchor instead of a raw offset', (tester) async {
      await pumpTranscript(tester);
      await tester.drag(find.byType(ListView), const Offset(0, 260));
      await tester.pumpAndSettle();

      final anchorId = scrollState.anchorSegmentId;
      expect(anchorId, isNotNull);
      final anchorFinder = find.byKey(ValueKey('transcript-segment-$anchorId'));
      final anchorTop = tester.getTopLeft(anchorFinder).dy - tester.getTopLeft(find.byType(ListView)).dy;
      expect(scrollState.anchorViewportOffset, closeTo(anchorTop, 5));

      layoutIdentity = 'photo-timeline';
      leadingItemIds = ['new-photo-group'];
      leadingItems = const [SizedBox(height: 240)];
      contentVersion++;
      await pumpTranscript(tester);

      expect(scrollState.anchorSegmentId, anchorId);
      final restoredTop = tester.getTopLeft(anchorFinder).dy - tester.getTopLeft(find.byType(ListView)).dy;
      // Sliver layout may round the restored edge by the four-point separator,
      // but it must keep the same segment at the same reading position.
      expect(restoredTop, closeTo(anchorTop, 5));
      expect(scrollState.isAtBottom, isFalse);
    });

    testWidgets('deleting the first segment preserves a later reading anchor', (tester) async {
      await pumpTranscript(tester);
      await tester.drag(find.byType(ListView), const Offset(0, 260));
      await tester.pumpAndSettle();

      final anchorId = scrollState.anchorSegmentId;
      expect(anchorId, isNotNull);
      final anchorFinder = find.byKey(ValueKey('transcript-segment-$anchorId'));
      final anchorTop = tester.getTopLeft(anchorFinder).dy - tester.getTopLeft(find.byType(ListView)).dy;
      expect(scrollState.anchorViewportOffset, closeTo(anchorTop, 5));

      segments = segments.skip(1).toList();
      contentVersion++;
      await pumpTranscript(tester);

      expect(scrollState.anchorSegmentId, anchorId);
      final restoredTop = tester.getTopLeft(anchorFinder).dy - tester.getTopLeft(find.byType(ListView)).dy;
      // Sliver layout may round the restored edge by the four-point separator,
      // but it must keep the same segment at the same reading position.
      expect(restoredTop, closeTo(anchorTop, 5));
      expect(scrollState.isAtBottom, isFalse);
    });

    testWidgets('live controls reserve only the standard 120 point footer', (tester) async {
      await pumpTranscript(tester);

      final footer = tester.widget<SizedBox>(find.byKey(const ValueKey('transcript_bottom_spacing')));
      expect(footer.height, 120);
    });
  });
}
