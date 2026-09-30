import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/structured.dart';
import 'package:omi/l10n/app_localizations.dart';

import 'package:omi/pages/conversations/widgets/processing_capture.dart';

void main() {
  testWidgets('every saved recording has a dated card while transcription is pending or failed', (tester) async {
    final pending = ServerConversation(
      id: 'pending',
      createdAt: DateTime(2026, 9, 17, 12, 15),
      structured: Structured('Local recording', ''),
      status: ConversationStatus.processing,
      source: ConversationSource.omi,
      externalIntegration:
          ConversationExternalData(text: '', localRecording: {'status': 'pending', 'duration_seconds': 300}),
    );
    final failed = ServerConversation(
      id: 'failed',
      createdAt: DateTime(2026, 9, 17, 12, 20),
      structured: Structured('Local recording', ''),
      status: ConversationStatus.processing,
      source: ConversationSource.phone,
      externalIntegration:
          ConversationExternalData(text: '', localRecording: {'status': 'failed', 'duration_seconds': 65}),
    );
    await tester.pumpWidget(MaterialApp(
      localizationsDelegates: AppLocalizations.localizationsDelegates,
      supportedLocales: AppLocalizations.supportedLocales,
      locale: const Locale('en'),
      home: Scaffold(
          body: CustomScrollView(slivers: [
        getProcessingConversationsWidget([pending, failed])
      ])),
    ));
    expect(find.text('Omi · Saved'), findsOneWidget);
    expect(find.text('Microphone · Saved'), findsOneWidget);
    expect(find.textContaining('5:00\nPending'), findsOneWidget);
    expect(find.textContaining('1:05\nTranscription failed'), findsOneWidget);
    expect(find.textContaining('12:15'), findsOneWidget);
    expect(ConversationExternalData.fromJson(pending.externalIntegration!.toJson()).localRecording,
        pending.externalIntegration!.localRecording);
    await tester.tap(find.text('Omi · Saved'));
    await tester.pump();
    expect(find.byType(ProcessingConversationWidget), findsNWidgets(2));
  });

  group('RecordingStatusIndicator', () {
    testWidgets('uses FadeTransition for blinking animation', (tester) async {
      await tester.pumpWidget(
        const MaterialApp(
          home: Scaffold(body: Center(child: RecordingStatusIndicator())),
        ),
      );

      final indicator = find.byType(RecordingStatusIndicator);
      expect(indicator, findsOneWidget);

      // Should use FadeTransition within the indicator (efficient opacity animation)
      final fadeTransitions = find.descendant(of: indicator, matching: find.byType(FadeTransition));
      expect(fadeTransitions, findsOneWidget);

      // Should have red recording icon
      final icon = find.descendant(of: indicator, matching: find.byIcon(Icons.fiber_manual_record));
      expect(icon, findsOneWidget);
    });

    testWidgets('animation cycles at 1000ms duration', (tester) async {
      await tester.pumpWidget(
        const MaterialApp(
          home: Scaffold(body: Center(child: RecordingStatusIndicator())),
        ),
      );

      expect(find.byType(RecordingStatusIndicator), findsOneWidget);

      // Advance through animation cycle
      await tester.pump(const Duration(milliseconds: 500));
      expect(find.byType(RecordingStatusIndicator), findsOneWidget);

      await tester.pump(const Duration(milliseconds: 500));
      expect(find.byType(RecordingStatusIndicator), findsOneWidget);
    });

    testWidgets('disposes animation controller on unmount', (tester) async {
      await tester.pumpWidget(
        const MaterialApp(
          home: Scaffold(body: Center(child: RecordingStatusIndicator())),
        ),
      );

      await tester.pumpWidget(const MaterialApp(home: Scaffold(body: SizedBox.shrink())));

      // Should dispose without errors
      expect(find.byType(RecordingStatusIndicator), findsNothing);
    });

    testWidgets('displays red color for recording state', (tester) async {
      await tester.pumpWidget(
        const MaterialApp(
          home: Scaffold(body: Center(child: RecordingStatusIndicator())),
        ),
      );

      final indicator = find.byType(RecordingStatusIndicator);
      final iconFinder = find.descendant(of: indicator, matching: find.byIcon(Icons.fiber_manual_record));
      final iconWidget = tester.widget<Icon>(iconFinder);
      expect(iconWidget.color, Colors.red);
    });
  });

  group('PausedStatusIndicator', () {
    testWidgets('uses FadeTransition for blinking animation', (tester) async {
      await tester.pumpWidget(
        const MaterialApp(
          home: Scaffold(body: Center(child: PausedStatusIndicator())),
        ),
      );

      final indicator = find.byType(PausedStatusIndicator);
      expect(indicator, findsOneWidget);

      // Should use FadeTransition within the indicator
      final fadeTransitions = find.descendant(of: indicator, matching: find.byType(FadeTransition));
      expect(fadeTransitions, findsOneWidget);

      // Should have paused icon
      final icon = find.descendant(of: indicator, matching: find.byIcon(Icons.fiber_manual_record));
      expect(icon, findsOneWidget);
    });

    testWidgets('displays orange color for paused state', (tester) async {
      await tester.pumpWidget(
        const MaterialApp(
          home: Scaffold(body: Center(child: PausedStatusIndicator())),
        ),
      );

      final indicator = find.byType(PausedStatusIndicator);
      final iconFinder = find.descendant(of: indicator, matching: find.byIcon(Icons.fiber_manual_record));
      final iconWidget = tester.widget<Icon>(iconFinder);
      expect(iconWidget.color, Colors.orange);
    });
  });
}
