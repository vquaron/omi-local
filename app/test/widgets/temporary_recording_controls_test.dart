import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:provider/provider.dart';
import 'package:font_awesome_flutter/font_awesome_flutter.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/bt_device/bt_device.dart';
import 'package:omi/backend/schema/phone_call.dart';
import 'package:omi/env/env.dart';
import 'package:omi/backend/schema/message_event.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/widgets/recording_source_label.dart';
import 'package:omi/services/capture/local_capture_phase.dart';
import 'package:omi/services/wals/wal.dart';
import 'package:omi/pages/conversation_capturing/page.dart';
import 'package:omi/providers/device_provider.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/pages/conversations/widgets/temporary_recording_controls.dart';
import 'package:omi/pages/conversations/widgets/processing_capture.dart';
import 'package:omi/pages/home/widgets/battery_info_widget.dart';
import 'package:omi/providers/capture_provider.dart';
import 'package:omi/providers/phone_call_provider.dart';
import 'package:omi/services/capture/conversation_location_capture.dart';
import 'package:omi/utils/enums.dart';

class _Capture extends CaptureProvider {
  _Capture()
      : super(
          inProgressConversationLoader: () async {},
          conversationLocationCapture: ConversationLocationCapture(isLocationServiceEnabled: () async => false),
        );
  final calls = <String>[];
  bool deviceConnected = true;
  bool muted = false;
  bool failStart = false;
  Completer<void>? startGate;
  OmiButtonEvent? buttonEvent;
  LocalOmiButtonAction? buttonAction;
  @override
  LocalOmiButtonAction? get localOmiButtonFeedback => buttonAction;
  @override
  OmiButtonEvent? get lastOmiButtonEvent => buttonEvent;
  @override
  List<Wal> get unsyncedSessionWals => [];
  @override
  int get inFlightAudioSeconds => 0;

  @override
  bool get havingRecordingDevice => deviceConnected;
  @override
  bool get isPaused => muted;
  @override
  Future<void> streamDeviceRecording({BtDevice? device, bool userInitiated = false}) async {
    calls.add('start:$userInitiated');
    await startGate?.future;
    if (failStart) throw StateError('Synthetic start failure');
    muted = false;
    updateRecordingState(RecordingState.deviceRecord);
  }

  @override
  Future<void> stopStreamDeviceRecording({bool cleanDevice = false}) async {
    calls.add('stop:$cleanDevice');
    updateRecordingState(RecordingState.stop);
  }

  @override
  Future<void> pauseDeviceRecording() async {
    calls.add('mute');
    muted = true;
    updateRecordingState(RecordingState.pause);
  }

  @override
  Future<void> resumeDeviceRecording() async {
    calls.add('unmute');
    muted = false;
    updateRecordingState(RecordingState.deviceRecord);
  }

  @override
  Future<void> streamRecording() async {
    calls.add('phone:start');
    updateRecordingState(RecordingState.initialising);
    await startGate?.future;
    if (failStart) {
      updateRecordingState(RecordingState.stop);
      throw StateError('Synthetic phone permission failure');
    }
    updateRecordingState(RecordingState.record);
  }

  @override
  Future<void> forceProcessingCurrentConversation() async {
    calls.add('phone:finalize');
  }

  @override
  Future<void> stopStreamRecording({String reason = 'user_stopped'}) async {
    calls.add('phone:stop');
    updateRecordingState(RecordingState.stop);
  }
}

class _IdleCalls extends ChangeNotifier implements PhoneCallProvider {
  @override
  PhoneCallState get callState => PhoneCallState.idle;
  @override
  dynamic noSuchMethod(Invocation invocation) => super.noSuchMethod(invocation);
}

class _IdleDevice extends ChangeNotifier implements DeviceProvider {
  @override
  BtDevice? get connectedDevice => null;
  @override
  dynamic noSuchMethod(Invocation invocation) => super.noSuchMethod(invocation);
}

Widget _app(Widget child) => MaterialApp(
      locale: const Locale('ru'),
      localizationsDelegates: AppLocalizations.localizationsDelegates,
      supportedLocales: AppLocalizations.supportedLocales,
      home: Scaffold(body: SizedBox(width: 320, child: child)),
    );

void main() {
  setUp(() async {
    SharedPreferences.setMockInitialValues({'batchModeEnabled': false});
    await SharedPreferencesUtil.init();
    Env.setRuntimeModeForTesting(OmiRuntimeMode.offline);
  });
  tearDown(() => Env.setRuntimeModeForTesting(null));

  testWidgets('live screen reports adapter failure and recovers only on preview readiness', (tester) async {
    final capture = _Capture();
    final device = _IdleDevice();
    addTearDown(capture.dispose);
    addTearDown(device.dispose);
    capture.updateRecordingState(RecordingState.deviceRecord);
    capture.onMessageEventReceived(MessageServiceStatusEvent(
      status: 'stt_failed',
      provider: 'local_live_preview',
      reason: 'busy',
      retryable: false,
    ));
    await tester.pumpWidget(MultiProvider(
      providers: [
        ChangeNotifierProvider<CaptureProvider>.value(value: capture),
        ChangeNotifierProvider<DeviceProvider>.value(value: device),
      ],
      child: const MaterialApp(
        locale: Locale('en'),
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: ConversationCapturingPage(),
      ),
    ));
    await tester.pump();
    expect(find.text('Transcription unavailable'), findsOneWidget);
    expect(find.text('Listening'), findsNothing);
    capture.onMessageEventReceived(MessageServiceStatusEvent(status: 'ready', provider: 'offline_capture'));
    await tester.pump();
    expect(find.text('Transcription unavailable'), findsOneWidget);
    capture.onMessageEventReceived(MessageServiceStatusEvent(status: 'ready', provider: 'local_live_preview'));
    await tester.pump();
    expect(find.text('Transcription unavailable'), findsNothing);
    expect(find.text('Listening'), findsNothing);
    expect(find.text('Waiting for data...'), findsOneWidget);
    expect(tester.takeException(), isNull);
    await tester.pumpWidget(const SizedBox.shrink());
  });

  testWidgets('connected idle Omi never says Listening; button and capture state stay separate', (tester) async {
    final capture = _Capture();
    final calls = _IdleCalls();
    addTearDown(capture.dispose);
    addTearDown(calls.dispose);
    await tester.pumpWidget(_app(MultiProvider(
      providers: [
        ChangeNotifierProvider<CaptureProvider>.value(value: capture),
        ChangeNotifierProvider<PhoneCallProvider>.value(value: calls),
      ],
      child: const ConversationCaptureWidget(),
    )));
    await tester.pump();
    expect(find.text('Запись не идёт'), findsOneWidget);
    expect(find.text('Listening'), findsNothing);
    for (final event in [OmiButtonEvent.pressed, OmiButtonEvent.released, OmiButtonEvent.singleTap]) {
      capture.buttonEvent = event;
      capture.notifyListeners();
      await tester.pump();
      expect(find.byKey(const Key('local_omi_button_feedback')), findsNothing);
      expect(find.byKey(const Key('local_omi_button_receipt')), findsOneWidget);
      expect(find.text('Запись не идёт'), findsOneWidget);
    }
    capture.buttonAction = LocalOmiButtonAction.started;
    capture.notifyListeners();
    await tester.pump();
    final actionL10n = AppLocalizations.of(tester.element(find.byKey(const Key('local_omi_button_feedback'))));
    expect(find.text('Omi · ${actionL10n.recordingStartedSuccessfully}'), findsOneWidget);
    capture.buttonAction = null;
    capture.notifyListeners();
    await tester.pump();
    expect(find.byKey(const Key('local_omi_button_feedback')), findsNothing);
    capture.updateRecordingState(RecordingState.deviceRecord);
    await tester.pump();
    final label = tester.widget<Text>(find.byKey(const Key('local_capture_state'))).data;
    final l10n = AppLocalizations.of(tester.element(find.byKey(const Key('local_capture_state'))));
    expect(label, l10n.waitingForData);
    expect(find.text(l10n.listening), findsNothing);
    capture.muted = true;
    capture.updateRecordingState(RecordingState.pause);
    await tester.pump();
    expect(tester.widget<Text>(find.byKey(const Key('local_capture_state'))).data, l10n.paused);
    capture.updateRecordingState(RecordingState.stop);
    await tester.pump();
    expect(find.text('Запись не идёт'), findsOneWidget);
    expect(tester.takeException(), isNull);
    await tester.pumpWidget(const SizedBox.shrink());
  });

  testWidgets('transcript page uses the same idle and paused states instead of unconditional Listening',
      (tester) async {
    final capture = _Capture();
    final device = _IdleDevice();
    addTearDown(capture.dispose);
    addTearDown(device.dispose);
    await tester.pumpWidget(_app(MultiProvider(
      providers: [
        ChangeNotifierProvider<CaptureProvider>.value(value: capture),
        ChangeNotifierProvider<DeviceProvider>.value(value: device),
      ],
      child: const ConversationCapturingPage(),
    )));
    await tester.pump();
    expect(tester.widget<Text>(find.byKey(const Key('local_capture_page_state'))).data, 'Запись не идёт');
    capture.buttonEvent = OmiButtonEvent.released;
    capture.muted = true;
    capture.updateRecordingState(RecordingState.pause);
    await tester.pump();
    final l10n = AppLocalizations.of(tester.element(find.byKey(const Key('local_capture_page_state'))));
    expect(tester.widget<Text>(find.byKey(const Key('local_capture_page_state'))).data, l10n.paused);
    expect(find.text('Omi · Кнопка отпущена'), findsOneWidget);
    expect(find.byKey(const Key('local_omi_button_feedback')), findsNothing);
    expect(find.text(l10n.listening), findsNothing);
    expect(tester.takeException(), isNull);
    await tester.pumpWidget(const SizedBox.shrink());
  });

  testWidgets('source label follows the current input and clears when capture stops', (tester) async {
    Widget sourceApp(ConversationSource? source, {String language = 'ru', double scale = 1}) => MaterialApp(
          locale: Locale(language),
          theme: ThemeData(platform: TargetPlatform.iOS),
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          home: Scaffold(
            body: MediaQuery(
              data: MediaQueryData(textScaler: TextScaler.linear(scale)),
              child: SizedBox(width: 240, child: RecordingSourceLabel(source: source)),
            ),
          ),
        );

    await tester.pumpWidget(sourceApp(ConversationSource.omi));
    await tester.pumpAndSettle();
    expect(find.text('Omi'), findsOneWidget);

    await tester.pumpWidget(sourceApp(ConversationSource.phone, scale: 2));
    await tester.pumpAndSettle();
    expect(find.text('Микрофон · iPhone'), findsOneWidget);
    expect(find.text('Omi'), findsNothing);
    expect(tester.takeException(), isNull);

    await tester.pumpWidget(sourceApp(ConversationSource.phone, language: 'en'));
    await tester.pumpAndSettle();
    expect(find.text('Microphone · iPhone'), findsOneWidget);

    for (final source in [null, ConversationSource.desktop]) {
      await tester.pumpWidget(sourceApp(source));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('recording_source_label')), findsNothing);
    }
  });

  testWidgets('start, mute, unmute and stop use existing functions; idle mute is disabled', (tester) async {
    final capture = _Capture();
    addTearDown(capture.dispose);
    await tester.pumpWidget(_app(ListenableBuilder(
      listenable: capture,
      builder: (_, __) => TemporaryRecordingControls(provider: capture),
    )));
    final start = find.byKey(const Key('temporary_start_recording'));
    final stop = find.byKey(const Key('temporary_stop_recording'));
    final mute = find.byKey(const Key('temporary_recording_mute'));
    bool enabled(Finder finder) => tester.widget<IconButton>(finder).onPressed != null;
    expect(enabled(start), isTrue);
    expect(enabled(stop), isFalse);
    expect(enabled(mute), isFalse);
    expect(tester.getCenter(start).dx, lessThan(tester.getCenter(stop).dx));
    expect(tester.getCenter(stop).dx, lessThan(tester.getCenter(mute).dx));
    await tester.tap(start);
    await tester.pumpAndSettle();
    expect(enabled(start), isFalse);
    expect(enabled(stop), isTrue);
    expect(enabled(mute), isTrue);
    await tester.tap(mute);
    await tester.pumpAndSettle();
    expect(capture.isPaused, isTrue);
    await tester.tap(mute);
    await tester.pumpAndSettle();
    expect(capture.isPaused, isFalse);
    await tester.tap(mute);
    await tester.pumpAndSettle();
    await tester.tap(stop);
    await tester.pumpAndSettle();
    expect(enabled(start), isTrue);
    expect(enabled(stop), isFalse);
    expect(enabled(mute), isFalse);
    await tester.tap(start);
    await tester.pumpAndSettle();
    expect(capture.isPaused, isFalse);
    expect(capture.calls, ['start:true', 'mute', 'unmute', 'mute', 'stop:false', 'start:true']);
    expect(tester.takeException(), isNull);
  });

  testWidgets('pending start blocks duplicate actions and failed start closes the session', (tester) async {
    final capture = _Capture()..startGate = Completer<void>();
    addTearDown(capture.dispose);
    await tester.pumpWidget(_app(TemporaryRecordingControls(provider: capture)));
    await tester.tap(find.byKey(const Key('temporary_start_recording')));
    await tester.pump();
    expect(tester.widgetList<IconButton>(find.byType(IconButton)).every((button) => button.onPressed == null), isTrue);
    capture.failStart = true;
    capture.startGate!.complete();
    await tester.pumpAndSettle();
    expect(capture.calls, ['start:true', 'stop:false']);
    expect(find.byType(SnackBar), findsOneWidget);
    expect(tester.widget<IconButton>(find.byKey(const Key('temporary_recording_mute'))).onPressed, isNull);
  });

  testWidgets('home microphone failure shows feedback without opening the capture page', (tester) async {
    final capture = _Capture()..failStart = true;
    addTearDown(capture.dispose);
    await tester.pumpWidget(_app(ChangeNotifierProvider<CaptureProvider>.value(
      value: capture,
      child: const HomeRecordButton(),
    )));
    await tester.tap(find.byType(HomeRecordButton));
    await tester.pumpAndSettle();
    expect(capture.calls, ['phone:start']);
    expect(find.byType(SnackBar), findsOneWidget);
    expect(find.byType(ConversationCapturingPage), findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets('home button stops an interrupted phone session while Omi stays connected', (tester) async {
    final capture = _Capture()..updateRecordingState(RecordingState.interrupted);
    addTearDown(capture.dispose);
    await tester.pumpWidget(_app(ChangeNotifierProvider<CaptureProvider>.value(
      value: capture,
      child: const HomeRecordButton(),
    )));
    await tester.tap(find.byIcon(Icons.stop_rounded));
    await tester.pumpAndSettle();
    expect(capture.calls.first, 'phone:stop');
    expect(capture.calls, isNot(contains('phone:start')));
    expect(capture.recordingState, RecordingState.stop);
  });

  testWidgets('original home plus opens record choices in local mode', (tester) async {
    final capture = _Capture()..deviceConnected = false;
    addTearDown(capture.dispose);
    await tester.pumpWidget(_app(ChangeNotifierProvider<CaptureProvider>.value(
      value: capture,
      child: const HomeRecordButton(),
    )));
    await tester.pumpAndSettle();
    final plus = find.byIcon(Icons.add);
    expect(plus, findsOneWidget);
    expect(find.byKey(const Key('local_phone_recording')), findsNothing);
    expect(find.byKey(const Key('temporary_phone_recording_disabled')), findsNothing);
    await tester.longPress(plus);
    await tester.pumpAndSettle();
    expect(find.byType(RecordOptionsSheet), findsOneWidget);
    expect(capture.calls, isEmpty);
    expect(tester.takeException(), isNull);
  });

  testWidgets('original home button shows busy and stops an active phone recording', (tester) async {
    final capture = _Capture()
      ..deviceConnected = false
      ..updateRecordingState(RecordingState.initialising);
    addTearDown(capture.dispose);
    await tester.pumpWidget(_app(ChangeNotifierProvider<CaptureProvider>.value(
      value: capture,
      child: const HomeRecordButton(),
    )));
    await tester.pump();
    expect(find.byType(CircularProgressIndicator), findsOneWidget);
    await tester.tap(find.byType(HomeRecordButton));
    await tester.pump();
    expect(capture.calls, isEmpty);
    capture.updateRecordingState(RecordingState.record);
    await tester.pumpAndSettle();
    await tester.tap(find.byIcon(Icons.stop_rounded));
    await tester.pumpAndSettle();
    expect(capture.calls, ['phone:stop', 'phone:finalize']);
    expect(find.byIcon(Icons.add), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('original Omi card uses mute and resume without temporary home controls', (tester) async {
    final capture = _Capture()..updateRecordingState(RecordingState.deviceRecord);
    final calls = _IdleCalls();
    addTearDown(capture.dispose);
    addTearDown(calls.dispose);
    await tester.pumpWidget(_app(MultiProvider(
      providers: [
        ChangeNotifierProvider<CaptureProvider>.value(value: capture),
        ChangeNotifierProvider<PhoneCallProvider>.value(value: calls),
      ],
      child: const ConversationCaptureWidget(),
    )));
    await tester.pumpAndSettle();
    expect(find.byType(TemporaryRecordingControls), findsNothing);
    expect(find.byKey(const Key('local_live_transcript_open')), findsNothing);
    Finder icon(FaIconData data) => find.byWidgetPredicate((widget) => widget is FaIcon && widget.icon == data.data);
    await tester.tap(icon(FontAwesomeIcons.microphone));
    await tester.pumpAndSettle();
    expect(capture.calls, ['mute']);
    expect(capture.isPaused, isTrue);
    await tester.tap(icon(FontAwesomeIcons.microphoneSlash));
    await tester.pumpAndSettle();
    expect(capture.calls, ['mute', 'unmute']);
    expect(capture.isPaused, isFalse);
    expect(tester.takeException(), isNull);
    await tester.pumpWidget(const SizedBox.shrink());
  });
}
