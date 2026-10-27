import 'package:flutter/material.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/pages/conversation_detail/widgets/local_transcript_stages.dart';

void main() {
  test('local transcript stages parse from external data and survive a round trip', () {
    final data = ConversationExternalData.fromJson({
      'text': '',
      'local_recording': {'status': 'pending', 'duration_seconds': 65},
      'local_transcript': {
        'version': 1,
        'profile': {'engine': 'openai-compatible'},
        'stages': {
          'stt': {'status': 'ready', 'provider': 'GPU whisper'},
          'diarization': {'status': 'ready', 'provider': 'GPU pyannote', 'speakers': 2},
          'summary': {'status': 'disabled'},
        },
      },
    });
    final stages = data.localTranscriptStages!;
    expect(stages.diarization!.status, 'ready');
    expect(stages.diarization!.provider, 'GPU pyannote');
    expect(stages.diarization!.speakers, 2);
    expect(stages.stt!.provider, 'GPU whisper');
    expect(data.toJson()['local_transcript'], isA<Map>());
    expect(ConversationExternalData.fromJson(data.toJson()).localRecording,
        {'status': 'pending', 'duration_seconds': 65});
    expect(ConversationExternalData.fromJson(data.toJson()).localTranscriptStages!.diarization!.speakers, 2);
  });

  test('legacy external data has no stages and malformed blocks are ignored', () {
    expect(ConversationExternalData.fromJson({'text': 'x'}).localTranscriptStages, isNull);
    expect(ConversationExternalData.fromJson({'text': 'x', 'local_transcript': 'junk'}).localTranscriptStages, isNull);
    final odd = ConversationExternalData.fromJson({
      'text': '',
      'local_transcript': {'stages': {'diarization': {'status': 42, 'speakers': 'two', 'provider': ''}}},
    });
    expect(odd.localTranscriptStages!.diarization!.status, 'unknown');
    expect(odd.localTranscriptStages!.diarization!.speakers, isNull);
    expect(odd.localTranscriptStages!.diarization!.provider, isNull);
  });

  Future<void> pump(WidgetTester tester, Map<String, dynamic> stages) async {
    final data = ConversationExternalData.fromJson({'text': '', 'local_transcript': {'stages': stages}});
    await tester.pumpWidget(MaterialApp(
      localizationsDelegates: const [
        AppLocalizations.delegate,
        GlobalMaterialLocalizations.delegate,
        GlobalWidgetsLocalizations.delegate,
        GlobalCupertinoLocalizations.delegate,
      ],
      supportedLocales: AppLocalizations.supportedLocales,
      home: Scaffold(body: LocalTranscriptStagesLine(stages: data.localTranscriptStages!)),
    ));
    await tester.pumpAndSettle();
  }

  String line(WidgetTester tester) => tester.widget<Text>(find.byKey(const ValueKey('local-transcript-diarization'))).data!;

  testWidgets('diarization line names the provider and speaker count', (tester) async {
    await pump(tester, {'diarization': {'status': 'ready', 'provider': 'GPU pyannote', 'speakers': 2}});
    expect(line(tester), 'Diarization: GPU pyannote · 2 speakers');
  });

  testWidgets('diarization line reports disabled and failed stages', (tester) async {
    await pump(tester, {'diarization': {'status': 'disabled'}});
    expect(line(tester), 'Diarization: Diarization not performed');
    await pump(tester, {'diarization': {'status': 'failed', 'provider': 'GPU pyannote'}});
    expect(line(tester), 'Diarization: GPU pyannote · Diarization failed');
  });

  testWidgets('nothing is drawn when the recording carries no diarization stage', (tester) async {
    await pump(tester, {'stt': {'status': 'ready', 'provider': 'GPU whisper'}});
    expect(find.byKey(const ValueKey('local-transcript-diarization')), findsNothing);
  });
}
