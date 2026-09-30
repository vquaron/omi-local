import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:connectivity_plus_platform_interface/connectivity_plus_platform_interface.dart';
import 'package:fake_async/fake_async.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/bt_device/bt_device.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/geolocation.dart';
import 'package:omi/backend/schema/message_event.dart';
import 'package:omi/backend/schema/transcript_segment.dart';
import 'package:omi/env/env.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/app_globals.dart';
import 'package:omi/models/custom_stt_config.dart';
import 'package:omi/models/stt_provider.dart';
import 'package:omi/providers/capture_provider.dart';
import 'package:omi/services/capture/capture_external_actions.dart';
import 'package:omi/services/capture/local_capture_phase.dart';
import 'package:omi/services/capture/conversation_location_capture.dart';
import 'package:omi/services/services.dart';
import 'package:omi/services/sockets/pure_socket.dart';
import 'package:omi/services/sockets/transcription_service.dart';
import 'package:omi/utils/enums.dart';

/// Fake external actions that tracks people-refresh calls.
class MockCaptureExternalActions extends NoopCaptureExternalActions {
  int setPeopleCallCount = 0;
  int fetchSubscriptionCallCount = 0;
  Completer<void>? _setPeopleCompleter;
  bool? outOfCreditsOverride;
  String? topConversationIdOverride;

  @override
  bool? get isOutOfCredits => outOfCreditsOverride;

  @override
  String? get topConversationId => topConversationIdOverride;

  @override
  Future<void> refreshPeople() async {
    setPeopleCallCount++;
    if (_setPeopleCompleter != null) {
      // Simulate async work - wait for completer
      await _setPeopleCompleter!.future;
    }
  }

  /// Set a completer to control when setPeople completes
  void setSetPeopleCompleter(Completer<void> completer) {
    _setPeopleCompleter = completer;
  }

  @override
  Future<void> fetchSubscription() async {
    fetchSubscriptionCallCount++;
  }
}

class _TestConnectivityPlatform extends ConnectivityPlatform {
  @override
  Future<List<ConnectivityResult>> checkConnectivity() async {
    return [ConnectivityResult.none];
  }

  @override
  Stream<List<ConnectivityResult>> get onConnectivityChanged => const Stream.empty();
}

TranscriptSegment _segment(String id, String text) {
  return TranscriptSegment(
    id: id,
    text: text,
    speaker: 'SPEAKER_00',
    isUser: false,
    personId: null,
    start: 0.0,
    end: 1.0,
    translations: [],
  );
}

BtDevice _device({required String id, required DeviceType type, String name = 'TestDevice'}) =>
    BtDevice(id: id, name: name, type: type, rssi: -50);

/// CaptureProvider whose socket opening is held on a per-call gate, so a
/// connection attempt can be kept in flight while another caller tries to
/// start one, and each attempt can be released independently.
class _GatedSocketCaptureProvider extends CaptureProvider {
  final List<Completer<void>> gates = [];
  final List<_IdentifiedSocketService> sockets = [];
  int? lastSubscribedId;
  bool returnSockets = false;

  int get openCalls => gates.length;

  @override
  Future<TranscriptSegmentSocketService?> openConversationSocket({
    required BleAudioCodec codec,
    required int sampleRate,
    required String language,
    required bool force,
    String? source,
    String? clientConversationId,
    CustomSttConfig? customSttConfig,
  }) async {
    final gate = Completer<void>();
    gates.add(gate);
    await gate.future;
    if (!returnSockets) return null;
    final socket = _IdentifiedSocketService(gates.length - 1, (id) => lastSubscribedId = id);
    sockets.add(socket);
    return socket;
  }

  void release(int attempt) => gates[attempt].complete();

  void releaseAll() {
    for (final gate in gates) {
      if (!gate.isCompleted) gate.complete();
    }
  }
}

class _NullSocketCaptureProvider extends CaptureProvider {
  @override
  Future<TranscriptSegmentSocketService?> openConversationSocket({
    required BleAudioCodec codec,
    required int sampleRate,
    required String language,
    required bool force,
    String? source,
    String? clientConversationId,
    CustomSttConfig? customSttConfig,
  }) async =>
      null;
}

class _PhoneProfileCaptured implements Exception {}

class _PhoneStartProbe extends CaptureProvider {
  Uri? requestedUri;

  @override
  Future<void> changeAudioRecordProfile({
    required BleAudioCodec audioCodec,
    int? sampleRate,
    int? channels,
    bool? isPcm,
    String? source,
  }) async {
    final service = TranscriptSegmentSocketService.create(sampleRate!, audioCodec, 'en', source: source);
    requestedUri = Uri.parse((service.socket as PureSocket).url);
    // Stop before opening the native microphone; inspect the real wire URL.
    throw _PhoneProfileCaptured();
  }
}

class _CountingSocketCaptureProvider extends CaptureProvider {
  _CountingSocketCaptureProvider({super.audioCodecLoader, super.microphonePermissionRequester, super.phoneMicRecorder});

  int openCalls = 0;

  @override
  Future<TranscriptSegmentSocketService?> openConversationSocket({
    required BleAudioCodec codec,
    required int sampleRate,
    required String language,
    required bool force,
    String? source,
    String? clientConversationId,
    CustomSttConfig? customSttConfig,
  }) async {
    openCalls++;
    return null;
  }
}

class _BufferedStartProvider extends CaptureProvider {
  _BufferedStartProvider(
      {required super.audioListenerLoader,
      super.audioCodecLoader,
      super.buttonListenerLoader,
      super.microphonePermissionRequester,
      super.phoneMicRecorder})
      : super(inProgressConversationLoader: () async {});

  final gates = <Completer<TranscriptSegmentSocketService?>>[];
  final sockets = <_AudioPacketSocket>[];
  final stopOperations = <Future<dynamic>>[];

  @override
  Future<dynamic> stopStreamDeviceRecording({bool cleanDevice = false}) {
    final stop = super.stopStreamDeviceRecording(cleanDevice: cleanDevice);
    stopOperations.add(stop);
    return stop;
  }

  @override
  Future<TranscriptSegmentSocketService?> openConversationSocket({
    required BleAudioCodec codec,
    required int sampleRate,
    required String language,
    required bool force,
    String? source,
    String? clientConversationId,
    CustomSttConfig? customSttConfig,
  }) {
    final gate = Completer<TranscriptSegmentSocketService?>();
    gates.add(gate);
    return gate.future;
  }

  _AudioPacketSocket connect(int index, {BleAudioCodec codec = BleAudioCodec.opusFS320}) {
    final socket = _AudioPacketSocket();
    sockets.add(socket);
    gates[index].complete(TranscriptSegmentSocketService.withSocket(16000, codec, 'en', socket));
    return socket;
  }
}

class _AudioPacketSocket extends _TrackingSocket {
  final packets = <List<int>>[];
  bool stopped = false;

  @override
  PureSocketStatus get status => stopped ? PureSocketStatus.disconnected : PureSocketStatus.connected;

  @override
  void send(dynamic message) {
    expect(stopped, isFalse, reason: 'Audio must drain before the socket closes');
    packets.add(List<int>.from(message as List<int>));
  }

  @override
  Future<void> stop() async => stopped = true;
}

class _ButtonCaptureProvider extends CaptureProvider {
  _ButtonCaptureProvider({super.buttonListenerLoader});

  final calls = <String>[];
  Completer<void>? startGate;
  bool failStart = false;

  @override
  Future<void> streamDeviceRecording({BtDevice? device, bool userInitiated = false}) async {
    if (!userInitiated) {
      await super.streamDeviceRecording(device: device);
      return;
    }
    calls.add('start');
    await startGate?.future;
    if (failStart) throw StateError('Synthetic capture failure');
    updateRecordingState(RecordingState.deviceRecord);
  }

  @override
  Future<void> stopStreamDeviceRecording({bool cleanDevice = false}) async {
    calls.add('stop');
    await super.stopStreamDeviceRecording(cleanDevice: cleanDevice);
  }
}

class _CountingConversationLocationCapture extends ConversationLocationCapture {
  int calls = 0;
  final List<bool> promptIfDeniedArgs = [];

  @override
  Future<Geolocation?> captureAndUpload({bool promptIfDenied = true}) async {
    calls++;
    promptIfDeniedArgs.add(promptIfDenied);
    return Geolocation(latitude: 1, longitude: 2, time: DateTime.utc(2026));
  }
}

class _HangingConversationLocationCapture extends ConversationLocationCapture {
  int calls = 0;
  final Completer<Geolocation?> _done = Completer<Geolocation?>();

  @override
  Future<Geolocation?> captureAndUpload({bool promptIfDenied = true}) async {
    calls++;
    return _done.future;
  }

  @override
  Future<Geolocation?> capture({bool promptIfDenied = true}) async {
    calls++;
    return _done.future;
  }

  @override
  Future<void> uploadCompatibilitySnapshot(Geolocation geolocation) async {}

  void complete() {
    if (!_done.isCompleted) {
      _done.complete(Geolocation(latitude: 1, longitude: 2, time: DateTime.utc(2026)));
    }
  }
}

class _FakeLiveMicRecorder extends _FakeBatchMicRecorder {
  int starts = 0;
  int stops = 0;
  Function(Uint8List)? receive;
  Function()? stopped;
  @override
  Future<void> start(
      {required Function(Uint8List) onByteReceived,
      Function()? onRecording,
      Function()? onStop,
      Function()? onInitializing,
      Function()? onStalled,
      Function(bool)? onInterruption}) async {
    starts++;
    receive = onByteReceived;
    stopped = onStop;
    onRecording?.call();
  }

  @override
  void stop() {
    stops++;
    stopped?.call();
  }
}

class _FakeBatchMicRecorder implements IMicRecorderService {
  int startBatchCalls = 0;

  @override
  Future<void> start({
    required Function(Uint8List bytes) onByteReceived,
    Function()? onRecording,
    Function()? onStop,
    Function()? onInitializing,
    Function()? onStalled,
    Function(bool began)? onInterruption,
  }) async {}

  @override
  Future<void> startBatch({
    Function()? onStop,
    Function(bool began)? onInterruption,
    Function()? onBatchStalled,
    Function(String code, String message)? onError,
  }) async {
    startBatchCalls++;
  }

  @override
  void stop() {}

  @override
  void probeStallAfterForeground() {}
}

class _IdentifiedSocketService extends TranscriptSegmentSocketService {
  _IdentifiedSocketService(this.id, this.onSubscribed)
      : super.withSocket(16000, BleAudioCodec.pcm16, 'en', _TrackingSocket());

  final int id;
  final void Function(int id) onSubscribed;

  @override
  void subscribe(Object context, ITransctiptSegmentSocketServiceListener listener) {
    onSubscribed(id);
    throw StateError('test socket subscribed');
  }
}

class _TrackingSocket implements IPureSocket {
  @override
  PureSocketStatus get status => PureSocketStatus.connected;

  @override
  Future<bool> connect() async => true;

  @override
  Future<void> disconnect() async {}

  @override
  Future<void> stop() async {}

  @override
  void send(dynamic message) {}

  @override
  void setListener(IPureSocketListener listener) {}

  @override
  void onMessage(dynamic message) {}

  @override
  void onConnected() {}

  @override
  void onClosed() {}

  @override
  void onError(Object err, StackTrace trace) {}
}

/// Minimal EnvFields stub so Env-backed code paths (e.g. native BLE stream
/// config reading Env.apiBaseUrl) don't hit a LateInitializationError.
class _TestEnvFields implements EnvFields {
  @override
  String? get posthogApiKey => null;
  @override
  String? get apiBaseUrl => null;
  @override
  String? get googleMapsApiKey => null;
  @override
  String? get intercomAppId => null;
  @override
  String? get intercomIOSApiKey => null;
  @override
  String? get intercomAndroidApiKey => null;
  @override
  String? get googleClientId => null;
  @override
  String? get googleClientSecret => null;
  @override
  bool? get useWebAuth => false;
  @override
  bool? get useAuthCustomToken => false;
}

void main() {
  setUpAll(() async {
    TestWidgetsFlutterBinding.ensureInitialized();
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(
      const MethodChannel('plugins.flutter.io/path_provider'),
      (MethodCall call) async {
        if (call.method == 'getApplicationDocumentsDirectory') return Directory.systemTemp.path;
        return null;
      },
    );
    ConnectivityPlatform.instance = _TestConnectivityPlatform();
    try {
      Env.init(_TestEnvFields());
    } catch (_) {
      // Env._instance is late final — ignore if already initialized in this isolate.
    }
    try {
      await ServiceManager.init();
    } catch (_) {
      // Ignore if already initialized by another test.
    }
  });

  // ------------------------------------------------------------------ //
  // Existing tests (preserved verbatim from the original file)          //
  // ------------------------------------------------------------------ //

  test('completed button feedback expires even when no further BLE event arrives', () {
    fakeAsync((async) {
      Env.setRuntimeModeForTesting(OmiRuntimeMode.offline);
      final buttons = StreamController<List<int>>.broadcast(sync: true);
      final provider = _ButtonCaptureProvider(
        buttonListenerLoader: (_, callback) async => buttons.stream.listen(callback),
      );
      provider.streamDeviceRecording(device: _device(id: 'synthetic-button', type: DeviceType.omi));
      async.flushMicrotasks();
      buttons.add([1, 0, 0, 0]);
      async.flushMicrotasks();
      expect(provider.localOmiButtonFeedback, LocalOmiButtonAction.started);
      async.elapse(const Duration(seconds: 3));
      buttons.add([5, 0, 0, 0]);
      expect(provider.localOmiButtonFeedback, LocalOmiButtonAction.started);
      async.elapse(const Duration(seconds: 1));
      expect(provider.localOmiButtonFeedback, isNull);
      provider.dispose();
      buttons.close();
      async.flushMicrotasks();
      Env.setRuntimeModeForTesting(null);
    });
  });

  test('button receipt expires while Start waits and result starts its own lifetime', () {
    fakeAsync((async) {
      Env.setRuntimeModeForTesting(OmiRuntimeMode.offline);
      final buttons = StreamController<List<int>>.broadcast(sync: true);
      final provider = _ButtonCaptureProvider(
        buttonListenerLoader: (_, callback) async => buttons.stream.listen(callback),
      )..startGate = Completer<void>();
      provider.streamDeviceRecording(device: _device(id: 'synthetic-button', type: DeviceType.omi));
      async.flushMicrotasks();

      buttons.add([1, 0, 0, 0]);
      expect(provider.lastOmiButtonEvent, OmiButtonEvent.singleTap);
      expect(provider.localCapturePhase, LocalCapturePhase.starting);
      expect(provider.localOmiButtonFeedback, isNull);
      async.elapse(const Duration(seconds: 4));
      expect(provider.lastOmiButtonEvent, isNull);
      expect(provider.localCapturePhase, LocalCapturePhase.starting);
      expect(provider.localOmiButtonFeedback, isNull);

      provider.startGate!.complete();
      async.flushMicrotasks();
      expect(provider.localOmiButtonFeedback, LocalOmiButtonAction.started);
      expect(provider.localCapturePhase, LocalCapturePhase.waitingAudio);
      expect(provider.lastOmiButtonEvent, isNull);
      async.elapse(const Duration(seconds: 3));
      expect(provider.localOmiButtonFeedback, LocalOmiButtonAction.started);
      async.elapse(const Duration(seconds: 1));
      expect(provider.localOmiButtonFeedback, isNull);
      expect(provider.calls, ['start']);

      provider.dispose();
      buttons.close();
      async.flushMicrotasks();
      Env.setRuntimeModeForTesting(null);
    });
  });

  test('button receipt expires and repeated edges extend receipt without starting audio', () {
    fakeAsync((async) {
      Env.setRuntimeModeForTesting(OmiRuntimeMode.offline);
      final buttons = StreamController<List<int>>.broadcast(sync: true);
      final provider = _ButtonCaptureProvider(
        buttonListenerLoader: (_, callback) async => buttons.stream.listen(callback),
      );
      provider.streamDeviceRecording(
        device: _device(id: 'synthetic-button', type: DeviceType.omi),
      );
      async.flushMicrotasks();
      buttons.add([4, 0, 0, 0]);
      expect(provider.lastOmiButtonEvent, OmiButtonEvent.pressed);
      async.elapse(const Duration(seconds: 3));
      buttons.add([4, 0, 0, 0]);
      async.elapse(const Duration(seconds: 3));
      expect(provider.lastOmiButtonEvent, OmiButtonEvent.pressed);
      async.elapse(const Duration(seconds: 1));
      expect(provider.lastOmiButtonEvent, isNull);
      expect(provider.localOmiButtonFeedback, isNull);
      expect(provider.calls, isEmpty);
      provider.dispose();
      buttons.close();
      async.flushMicrotasks();
      Env.setRuntimeModeForTesting(null);
    });
  });

  test('local button edges are visible without starting recording or a voice command', () async {
    Env.setRuntimeModeForTesting(OmiRuntimeMode.offline);
    final buttons = StreamController<List<int>>.broadcast(sync: true);
    final provider =
        _ButtonCaptureProvider(buttonListenerLoader: (_, callback) async => buttons.stream.listen(callback));
    addTearDown(() async {
      provider.dispose();
      await buttons.close();
      Env.setRuntimeModeForTesting(null);
    });
    await provider.streamDeviceRecording(device: _device(id: 'synthetic-button', type: DeviceType.omi));
    for (final event in [OmiButtonEvent.pressed, OmiButtonEvent.longPress, OmiButtonEvent.released]) {
      buttons.add([event.code, 0, 0, 0]);
      expect(provider.lastOmiButtonEvent, event);
      expect(provider.localOmiButtonFeedback, isNull);
      expect(provider.localCapturePhase, LocalCapturePhase.idle);
    }
    buttons.add([99, 0, 0, 0]);
    expect(provider.lastOmiButtonEvent, OmiButtonEvent.released);
    expect(provider.calls, isEmpty);
    provider.startGate = Completer<void>();
    buttons.add([1, 0, 0, 0]);
    expect(provider.localCapturePhase, LocalCapturePhase.starting);
    expect(provider.localOmiButtonFeedback, isNull);
    expect(provider.lastOmiButtonEvent, OmiButtonEvent.singleTap);
    buttons.add([5, 0, 0, 0]);
    expect(provider.calls, ['start']); // Release is not a second toggle.
    provider.startGate!.complete();
    await pumpEventQueue();
    expect(provider.localCapturePhase, LocalCapturePhase.waitingAudio);
    expect(provider.localOmiButtonFeedback, LocalOmiButtonAction.started);
    buttons.add([5, 0, 0, 0]);
    expect(provider.localOmiButtonFeedback, LocalOmiButtonAction.started);
    provider.updateRecordingDevice(null);
    expect(provider.localOmiButtonFeedback, isNull);
    expect(provider.lastOmiButtonEvent, isNull);
  });

  test('local audio status needs payload and expires without stopping capture', () {
    Env.setRuntimeModeForTesting(OmiRuntimeMode.offline);
    addTearDown(() => Env.setRuntimeModeForTesting(null));
    fakeAsync((async) {
      final audio = StreamController<List<int>>.broadcast(sync: true);
      final provider = CaptureProvider(audioListenerLoader: (_, callback) async => audio.stream.listen(callback));
      provider.updateRecordingDevice(_device(id: 'synthetic-audio', type: DeviceType.omi));
      expect(provider.localCapturePhase, LocalCapturePhase.idle);
      provider.updateRecordingState(RecordingState.deviceRecord);
      provider.streamAudioToWs('synthetic-audio', BleAudioCodec.pcm16);
      async.flushMicrotasks();
      expect(provider.localCapturePhase, LocalCapturePhase.waitingAudio);
      audio.add([0, 0, 0]);
      expect(provider.localCapturePhase, LocalCapturePhase.waitingAudio);
      audio.add([0, 0, 0, 1, 2]);
      expect(provider.localCapturePhase, LocalCapturePhase.recording);
      async.elapse(const Duration(seconds: 2));
      audio.add([1, 0, 0, 3, 4]);
      async.elapse(const Duration(seconds: 2));
      expect(provider.localCapturePhase, LocalCapturePhase.recording);
      async.elapse(const Duration(seconds: 1));
      expect(provider.localCapturePhase, LocalCapturePhase.waitingAudio);
      expect(provider.recordingState, RecordingState.deviceRecord);
      audio.add([2, 0, 0, 5, 6]);
      expect(provider.localCapturePhase, LocalCapturePhase.recording);
      provider.updateRecordingState(RecordingState.pause);
      audio.add([3, 0, 0, 7, 8]);
      expect(provider.localCapturePhase, LocalCapturePhase.paused);
      provider.updateRecordingState(RecordingState.stop);
      expect(provider.localCapturePhase, LocalCapturePhase.idle);
      provider.updateRecordingState(RecordingState.deviceRecord);
      audio.add([4, 0, 0, 9, 10]); // Old subscription cannot prove a new capture.
      expect(provider.localCapturePhase, LocalCapturePhase.waitingAudio);
      provider.dispose();
      audio.close();
      async.flushMicrotasks();
      async.elapse(const Duration(seconds: 4));
    });
  });

  test('active recording source follows capture state, not the connected device', () {
    final provider = CaptureProvider();
    addTearDown(provider.dispose);
    provider.updateRecordingDevice(_device(id: 'synthetic-cv1', type: DeviceType.omi));
    expect(provider.havingRecordingDevice, isTrue);
    expect(provider.activeRecordingSource, isNull);

    for (final state in [RecordingState.initialising, RecordingState.record, RecordingState.interrupted]) {
      provider.updateRecordingState(state);
      expect(provider.activeRecordingSource, ConversationSource.phone);
    }
    provider.updateRecordingState(RecordingState.systemAudioRecord);
    expect(provider.activeRecordingSource, ConversationSource.desktop);
    provider.updateRecordingState(RecordingState.error);
    expect(provider.activeRecordingSource, isNull);
    provider.updateRecordingState(RecordingState.stop);
    expect(provider.activeRecordingSource, isNull);
    expect(provider.havingRecordingDevice, isTrue);
  });

  test('local single tap starts, stops muted session, and starts again using the idle subscription', () async {
    Env.setRuntimeModeForTesting(OmiRuntimeMode.offline);
    final buttons = StreamController<List<int>>.broadcast(sync: true);
    var subscriptions = 0;
    final provider = _ButtonCaptureProvider(buttonListenerLoader: (_, callback) async {
      subscriptions++;
      return buttons.stream.listen(callback);
    });
    addTearDown(() async {
      provider.dispose();
      await buttons.close();
      Env.setRuntimeModeForTesting(null);
    });
    final device = _device(id: 'synthetic-cv1', type: DeviceType.omi);
    await provider.streamDeviceRecording(device: device);
    expect(provider.recordingState, RecordingState.stop);
    expect(subscriptions, 1);
    buttons.add([1]); // Incomplete BLE notification must not start capture.
    buttons.add([]);
    expect(provider.calls, isEmpty);

    buttons.add([1, 0, 0, 0]);
    await pumpEventQueue();
    expect(provider.recordingState, RecordingState.deviceRecord);
    provider.updateRecordingState(RecordingState.pause);
    buttons.add([1, 0, 0, 0]);
    await pumpEventQueue();
    expect(provider.recordingState, RecordingState.stop);
    expect(provider.localOmiButtonFeedback, LocalOmiButtonAction.stopped);
    expect(provider.recordingDevice, same(device));
    buttons.add([1, 0, 0, 0]);
    await pumpEventQueue();
    expect(provider.calls, ['start', 'stop', 'start']);
    expect(subscriptions, 1);
  });

  test('local button ignores overlapping taps and recovers after failed start', () async {
    Env.setRuntimeModeForTesting(OmiRuntimeMode.offline);
    final buttons = StreamController<List<int>>.broadcast(sync: true);
    final provider =
        _ButtonCaptureProvider(buttonListenerLoader: (_, callback) async => buttons.stream.listen(callback))
          ..startGate = Completer<void>()
          ..failStart = true;
    addTearDown(() async {
      provider.dispose();
      await buttons.close();
      Env.setRuntimeModeForTesting(null);
    });
    await provider.streamDeviceRecording(device: _device(id: 'synthetic-cv1', type: DeviceType.omi));
    buttons.add([1, 0, 0, 0]);
    buttons.add([1, 0, 0, 0]);
    expect(provider.calls, ['start']);
    provider.startGate!.complete();
    await pumpEventQueue();
    expect(provider.calls, ['start', 'stop']);
    expect(provider.recordingState, RecordingState.stop);
    provider.failStart = false;
    expect(provider.localCapturePhase, LocalCapturePhase.failed);
    expect(provider.localOmiButtonFeedback, LocalOmiButtonAction.failed);
    buttons.add([1, 0, 0, 0]);
    await pumpEventQueue();
    expect(provider.calls, ['start', 'stop', 'start']);
    expect(provider.localCapturePhase, LocalCapturePhase.waitingAudio);
  });

  test('local button drops events from a replaced or disconnected device', () async {
    Env.setRuntimeModeForTesting(OmiRuntimeMode.offline);
    final callbacks = <void Function(List<int>)>[];
    final buttons = StreamController<List<int>>.broadcast(sync: true);
    final provider = _ButtonCaptureProvider(buttonListenerLoader: (_, callback) async {
      callbacks.add(callback);
      return buttons.stream.listen(callback);
    });
    addTearDown(() async {
      provider.dispose();
      await buttons.close();
      Env.setRuntimeModeForTesting(null);
    });
    await provider.streamDeviceRecording(device: _device(id: 'synthetic-old', type: DeviceType.omi));
    await provider.streamDeviceRecording(device: _device(id: 'synthetic-new', type: DeviceType.omi));
    callbacks.first([1, 0, 0, 0]);
    expect(provider.calls, isEmpty);
    callbacks.last([1, 0, 0, 0]);
    await pumpEventQueue();
    expect(provider.calls, ['start']);
    await provider.stopStreamDeviceRecording(cleanDevice: true);
    callbacks.last([1, 0, 0, 0]);
    expect(provider.calls, ['start', 'stop']);
    expect(buttons.hasListener, isFalse);
  });

  test('local button cancels a subscription that arrives after disconnect', () async {
    Env.setRuntimeModeForTesting(OmiRuntimeMode.offline);
    final buttons = StreamController<List<int>>.broadcast(sync: true);
    final listenerReady = Completer<StreamSubscription?>();
    final requested = Completer<void>();
    final provider = _ButtonCaptureProvider(buttonListenerLoader: (_, callback) {
      requested.complete();
      return listenerReady.future;
    });
    addTearDown(() async {
      provider.dispose();
      await buttons.close();
      Env.setRuntimeModeForTesting(null);
    });
    final starting = provider.streamDeviceRecording(device: _device(id: 'synthetic-cv1', type: DeviceType.omi));
    await requested.future;
    await provider.stopStreamDeviceRecording(cleanDevice: true);
    listenerReady.complete(buttons.stream.listen((_) => fail('Disconnected button must be cancelled')));
    await starting;
    expect(buttons.hasListener, isFalse);
    expect(provider.recordingDevice, isNull);
    expect(provider.recordingState, RecordingState.stop);
  });

  test('local CV1 needs explicit start; Stop keeps the device and blocks auto-start and mute', () async {
    Env.setRuntimeModeForTesting(OmiRuntimeMode.offline);
    final provider = _CountingSocketCaptureProvider(audioCodecLoader: (_) async => BleAudioCodec.opus);
    addTearDown(() {
      provider.dispose();
      Env.setRuntimeModeForTesting(null);
    });
    final device = _device(id: 'synthetic-cv1', type: DeviceType.omi);
    await provider.streamDeviceRecording(device: device);
    expect(provider.havingRecordingDevice, isTrue);
    expect(provider.recordingState, RecordingState.stop);
    expect(provider.openCalls, 0);
    await provider.resumeDeviceRecording();
    expect(provider.recordingState, RecordingState.stop);

    // No physical BLE subscription: fail before opening a socket or claiming capture.
    await expectLater(provider.streamDeviceRecording(userInitiated: true), throwsStateError);
    expect(provider.openCalls, 0);
    await provider.stopStreamDeviceRecording();
    expect(provider.recordingDevice, same(device));
    expect(provider.recordingState, RecordingState.stop);
    await provider.streamDeviceRecording(device: device);
    await provider.onTranscriptionSettingsChanged();
    await provider.resumeDeviceRecording();
    await provider.pauseDeviceRecording();
    expect(provider.recordingState, RecordingState.stop);
    expect(provider.openCalls, 0);
    expect(SharedPreferencesUtil().getBool('nativeBleStreamingEnabled'), isFalse);
  });

  group('local audio start buffering', () {
    late StreamController<List<int>> audio;
    late _BufferedStartProvider provider;
    late Completer<BleAudioCodec> codec;

    setUp(() async {
      Env.setRuntimeModeForTesting(OmiRuntimeMode.offline);
      SharedPreferencesUtil().batchModeEnabled = false;
      SharedPreferencesUtil().unlimitedLocalStorageEnabled = false;
      audio = StreamController<List<int>>.broadcast(sync: true);
      codec = Completer<BleAudioCodec>();
      provider = _BufferedStartProvider(
        audioListenerLoader: (_, callback) async => audio.stream.listen(callback),
        audioCodecLoader: (_) => codec.future,
      );
      await provider.streamDeviceRecording(device: _device(id: 'synthetic-buffered', type: DeviceType.omi));
      expect(provider.activeRecordingSource, isNull);
    });

    tearDown(() async {
      provider.dispose();
      await audio.close();
      Env.setRuntimeModeForTesting(null);
    });

    Future<void> reachSocket() async {
      codec.complete(BleAudioCodec.opusFS320);
      await pumpEventQueue();
      expect(provider.gates, hasLength(1));
    }

    test('captures before codec and socket readiness, then sends FIFO once', () async {
      final start = provider.streamDeviceRecording(userInitiated: true);
      await pumpEventQueue();
      expect(audio.hasListener, isTrue);
      expect(provider.recordingState, RecordingState.deviceRecord);
      expect(provider.activeRecordingSource, ConversationSource.omi);
      expect(provider.gates, isEmpty);
      final packet = [0, 0, 0, 11];
      audio.add(packet);
      packet[3] = 99; // The buffer must own the notification snapshot.
      await reachSocket();
      audio.add([1, 0, 0, 22]);
      final socket = provider.connect(0);
      await start;
      provider.updateRecordingDevice(_device(id: 'synthetic-replacement', type: DeviceType.openglass));
      expect(provider.activeRecordingSource, isNull);
      audio.add([2, 0, 0, 33]);
      expect(
          socket.packets,
          [
            [11],
            [22],
          ],
          reason: 'Notifications from a replaced device cannot enter another source');
      await provider.stopStreamDeviceRecording();
      expect(socket.stopped, isTrue);
      expect(audio.hasListener, isFalse);
      expect(provider.activeRecordingSource, isNull);
    });

    test('automatic Omi capture resumes after BLE disconnect and respects explicit Stop', () async {
      expect(provider.shouldAutomaticallyStartDeviceRecording, isTrue);
      final start = provider.streamDeviceRecording(userInitiated: provider.shouldAutomaticallyStartDeviceRecording);
      await reachSocket();
      final first = provider.connect(0);
      await start;
      audio.add([0, 0, 0, 11]);
      provider.updateRecordingDevice(null);
      expect(provider.shouldAutomaticallyStartDeviceRecording, isTrue);
      final resume = provider.streamDeviceRecording(
        device: _device(id: 'synthetic-buffered', type: DeviceType.omi),
        userInitiated: provider.shouldAutomaticallyStartDeviceRecording,
      );
      await pumpEventQueue();
      // The connected socket can be retained, but the audio source is reattached.
      if (provider.gates.length == 2) provider.connect(1);
      await resume;
      audio.add([0, 0, 0, 22]);
      expect(provider.localCapturePhase, LocalCapturePhase.recording);
      expect(provider.sockets.expand((socket) => socket.packets), [
        [11],
        [22]
      ]);
      await provider.stopStreamDeviceRecording();
      expect(provider.shouldAutomaticallyStartDeviceRecording, isFalse);
      provider.updateRecordingDevice(null);
      await provider.streamDeviceRecording(device: _device(id: 'synthetic-buffered', type: DeviceType.omi));
      expect(provider.recordingState, RecordingState.stop);
      expect(audio.hasListener, isFalse);
      expect(first.packets.first, [11]);
    });

    test('Stop during connect drains accepted audio and keeps the next session separate', () async {
      final start = provider.streamDeviceRecording(userInitiated: true);
      await pumpEventQueue();
      audio.add([0, 0, 0, 11]);
      await reachSocket();
      final stop = provider.stopStreamDeviceRecording();
      await pumpEventQueue();
      expect(audio.hasListener, isFalse);
      expect(provider.recordingState, RecordingState.stop);
      expect(provider.activeRecordingSource, isNull);
      audio.add([1, 0, 0, 99]);
      final nextStart = provider.streamDeviceRecording(userInitiated: true);
      final first = provider.connect(0);
      await start;
      await stop;
      await pumpEventQueue();
      expect(first.packets, [
        [11]
      ]);
      expect(first.stopped, isTrue);
      expect(provider.gates, hasLength(2));
      audio.add([0, 0, 0, 22]);
      final second = provider.connect(1);
      await nextStart;
      expect(second.packets, [
        [22]
      ]);
      await provider.stopStreamDeviceRecording();
    });

    test('failed socket clears the buffered session and stops its audio subscription', () async {
      final start = provider.streamDeviceRecording(userInitiated: true);
      final failure = expectLater(start, throwsStateError);
      await pumpEventQueue();
      audio.add([0, 0, 0, 11]);
      await reachSocket();
      provider.gates.single.complete(null);
      await failure;
      expect(audio.hasListener, isFalse);
      expect(provider.recordingState, RecordingState.stop);
      expect(provider.activeRecordingSource, isNull);
      await provider.stopStreamDeviceRecording();
      expect(provider.recordingState, RecordingState.stop);
    });

    test('disconnect during connect closes a late socket without sending old audio', () async {
      final start = provider.streamDeviceRecording(userInitiated: true);
      await pumpEventQueue();
      audio.add([0, 0, 0, 11]);
      await reachSocket();
      provider.updateRecordingDevice(null);
      final socket = provider.connect(0);
      await start;
      expect(socket.packets, isEmpty);
      expect(socket.stopped, isTrue);
      expect(audio.hasListener, isFalse);
      expect(provider.recordingState, RecordingState.stop);
      expect(provider.activeRecordingSource, isNull);
      await provider.stopStreamDeviceRecording();
    });

    test('pause during connect preserves accepted audio without restarting input', () async {
      final start = provider.streamDeviceRecording(userInitiated: true);
      await pumpEventQueue();
      audio.add([0, 0, 0, 11]);
      await reachSocket();
      await provider.pauseDeviceRecording();
      audio.add([1, 0, 0, 99]);
      final socket = provider.connect(0);
      await start;
      expect(provider.recordingState, RecordingState.pause);
      expect(provider.activeRecordingSource, ConversationSource.omi);
      expect(socket.packets, [
        [11]
      ]);
      expect(audio.hasListener, isFalse);
      await provider.stopStreamDeviceRecording();
    });

    test('physical Stop during startup cancels input and drains before closing', () async {
      provider.dispose();
      final buttons = StreamController<List<int>>.broadcast(sync: true);
      addTearDown(buttons.close);
      provider = _BufferedStartProvider(
        audioListenerLoader: (_, callback) async => audio.stream.listen(callback),
        buttonListenerLoader: (_, callback) async => buttons.stream.listen(callback),
        audioCodecLoader: (_) async => BleAudioCodec.opusFS320,
      );
      await provider.streamDeviceRecording(device: _device(id: 'synthetic-buffered', type: DeviceType.omi));
      buttons.add([1, 0, 0, 0]);
      await pumpEventQueue();
      expect(provider.gates, hasLength(1));
      audio.add([0, 0, 0, 11]);
      buttons.add([1, 0, 0, 0]);
      await pumpEventQueue();
      expect(audio.hasListener, isFalse);
      expect(provider.stopOperations, hasLength(1));
      final socket = provider.connect(0);
      // Draining may await file/platform work; event-loop turns do not prove Stop finished.
      await provider.stopOperations.single;
      expect(socket.packets, [
        [11]
      ]);
      expect(socket.stopped, isTrue);
      expect(provider.recordingState, RecordingState.stop);
    });
  });

  test('initial phone microphone stream identifies its PCM16 source', () async {
    const permissionChannel = MethodChannel('flutter.baseflow.com/permissions/methods');
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(
      permissionChannel,
      (call) async => {for (final permission in call.arguments as List<dynamic>) permission: 1},
    );
    final provider = _PhoneStartProbe();
    try {
      await expectLater(provider.streamRecording(), throwsA(isA<_PhoneProfileCaptured>()));
      expect(provider.requestedUri!.queryParameters['source'], 'phone');
      expect(provider.requestedUri!.queryParameters['codec'], 'pcm16');
      expect(provider.requestedUri!.queryParameters['sample_rate'], '16000');
    } finally {
      provider.dispose();
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
          .setMockMethodCallHandler(permissionChannel, null);
    }
  });

  test('removes segments and related state on deletion event', () {
    final provider = CaptureProvider();
    final first = _segment('a', 'one');
    final second = _segment('b', 'two');

    provider.segments = [first, second];
    provider.suggestionsBySegmentId['a'] = SpeakerLabelSuggestionEvent(
      speakerId: 1,
      personId: 'p1',
      personName: 'Test',
      segmentId: 'a',
    );
    provider.taggingSegmentIds = ['a', 'b'];
    provider.hasTranscripts = true;

    provider.onMessageEventReceived(SegmentsDeletedEvent(segmentIds: ['a']));

    expect(provider.segments.length, 1);
    expect(provider.segments.first.id, 'b');
    expect(provider.suggestionsBySegmentId.containsKey('a'), false);
    expect(provider.taggingSegmentIds.contains('a'), false);
    expect(provider.hasTranscripts, true);
  });

  test('first transcript refreshes conversation location without a foreground task', () async {
    final locationCapture = _CountingConversationLocationCapture();
    final provider = CaptureProvider(
      conversationLocationCapture: locationCapture,
      inProgressConversationLoader: () async {},
    );

    provider.onSegmentReceived([_segment('first', 'hello')]);
    await Future<void>.delayed(Duration.zero);

    expect(locationCapture.calls, 1);
    provider.dispose();
  });

  group('atomic local transcript snapshots', () {
    MessageEvent snapshot(String previewId, int revision, List<Map<String, dynamic>> rows) => MessageEvent.fromJson({
          'type': 'local_transcript_snapshot',
          'preview_id': previewId,
          'revision': revision,
          'segments': rows,
        });
    Map<String, dynamic> row(String id, String text, {String? speaker, double start = 0, bool draft = false}) => {
          'id': id,
          'text': text,
          'start': start,
          'end': start + 2,
          'speaker': speaker,
          'is_user': false,
          'is_draft': draft,
          'stt_provider': 'whisperx',
        };

    test('corrects, splits and retracts only preview rows without loading a conversation', () {
      var loads = 0;
      var notifications = 0;
      final provider = CaptureProvider(inProgressConversationLoader: () async => loads++);
      addTearDown(provider.dispose);
      provider.addListener(() => notifications++);

      provider.onMessageEventReceived(snapshot('local-preview-one', 1, [row('local-preview-one-draft', 'Черновик')]));
      expect(provider.segments.single.text, 'Черновик');
      expect(loads, 0, reason: 'a snapshot must apply synchronously without fetching server conversation state');
      provider.onSegmentReceived([_segment('ordinary', 'Existing conversation')]);
      notifications = 0;
      final version = provider.segmentsPhotosVersion;
      provider.onMessageEventReceived(snapshot('local-preview-one', 2, [
        row('local-preview-one-line-1', 'Первый голос.', speaker: 'SPEAKER_00'),
        row('local-preview-one-line-2', 'Второй голос.', speaker: 'SPEAKER_01', start: 3),
        row('local-preview-one-draft', 'Ещё', start: 6, draft: true),
      ]));
      expect(notifications, 1, reason: 'UI observes the replacement as a single state change');
      expect(provider.segments.map((s) => s.id), [
        'ordinary',
        'local-preview-one-line-1',
        'local-preview-one-line-2',
        'local-preview-one-draft',
      ]);
      expect(provider.segments.map((s) => s.speakerId), [0, 0, 1, -1]);
      expect(provider.segments.last.isDraft, isTrue);
      expect(provider.segments[2].start, 3);
      expect(provider.segmentsPhotosVersion, greaterThan(version));
      provider.onMessageEventReceived(snapshot('local-preview-one', 3, [
        row('local-preview-one-line-1', 'Исправленный первый голос.', speaker: 'SPEAKER_00'),
      ]));
      expect(provider.segments.map((s) => s.text), ['Existing conversation', 'Исправленный первый голос.']);
      provider.onMessageEventReceived(snapshot('local-preview-one', 4, []));
      expect(provider.segments.single.id, 'ordinary');
      expect(provider.hasTranscripts, isTrue);
    });

    test('ignores old revisions and retired sessions, including after clearing user data', () {
      final provider = CaptureProvider();
      addTearDown(provider.dispose);
      provider.onMessageEventReceived(snapshot('local-preview-one', 2, [row('one', 'Newer')]));
      provider.onMessageEventReceived(snapshot('local-preview-one', 1, [row('one', 'Older')]));
      provider.onMessageEventReceived(snapshot('local-preview-one', 2, [row('one', 'Duplicate revision')]));
      expect(provider.segments.single.text, 'Newer');

      provider.onMessageEventReceived(snapshot('local-preview-two', 1, [row('two', 'Next session')]));
      provider.onMessageEventReceived(snapshot('local-preview-one', 3, [row('one', 'Late old session')]));
      expect(provider.segments.single.text, 'Next session');
      provider.onMessageEventReceived(snapshot('local-preview-two', 2, []));
      expect(provider.segments, isEmpty);
      expect(provider.hasTranscripts, isFalse);
      provider.clearUserData();
      provider.onMessageEventReceived(snapshot('local-preview-two', 3, [row('two', 'Late after reset')]));
      expect(provider.segments, isEmpty);
      provider.onMessageEventReceived(snapshot('local-preview-three', 1, [row('three', 'Fresh session')]));
      expect(provider.segments.single.text, 'Fresh session');
      provider.clearTranscripts();
      provider.onMessageEventReceived(snapshot('local-preview-three', 2, [row('three', 'Late after clear')]));
      expect(provider.segments, isEmpty);
    });

    test('a pending ordinary segment load cannot reinsert stale rows after a snapshot', () async {
      final pendingLoad = Completer<void>();
      final provider = CaptureProvider(
        conversationLocationCapture: _CountingConversationLocationCapture(),
        inProgressConversationLoader: () => pendingLoad.future,
      );
      addTearDown(provider.dispose);
      provider.onSegmentReceived([_segment('old', 'Pending old row')]);
      provider.onMessageEventReceived(snapshot('local-preview-one', 1, [row('one', 'Current snapshot')]));
      expect(provider.segments.single.text, 'Current snapshot');
      pendingLoad.complete();
      await Future<void>.delayed(Duration.zero);
      expect(provider.segments.single.text, 'Current snapshot');
    });
  });

  test('local Omi to phone to Omi handoff owns one input and survives Bluetooth reconnect', () async {
    Env.setRuntimeModeForTesting(OmiRuntimeMode.offline);
    addTearDown(() => Env.setRuntimeModeForTesting(null));
    final mic = _FakeLiveMicRecorder();
    final audio = StreamController<List<int>>.broadcast();
    final provider = _BufferedStartProvider(
      audioListenerLoader: (_, receive) async => audio.stream.listen(receive),
      audioCodecLoader: (_) async => BleAudioCodec.opusFS320,
      buttonListenerLoader: (_, __) async => null,
      microphonePermissionRequester: () async => true,
      phoneMicRecorder: mic,
    );
    addTearDown(() async {
      provider.dispose();
      await audio.close();
    });
    final device = _device(id: 'synthetic-omi', type: DeviceType.omi);
    final omiStart = provider.streamDeviceRecording(device: device, userInitiated: true);
    while (provider.gates.isEmpty) {
      await Future<void>.delayed(Duration.zero);
    }
    final omiSocket = provider.connect(0);
    await omiStart;
    await provider.stopStreamDeviceRecording();
    expect(omiSocket.stopped, isTrue);
    final phoneStart = provider.streamRecording();
    while (provider.gates.length < 2) {
      await Future<void>.delayed(Duration.zero);
    }
    final phoneSocket = provider.connect(1, codec: BleAudioCodec.pcm16);
    await phoneStart;
    expect(provider.recordingState, RecordingState.record);
    expect(provider.havingRecordingDevice, isTrue);
    expect(provider.activeRecordingSource, ConversationSource.phone);
    mic.receive!(Uint8List(320));
    expect(phoneSocket.packets, hasLength(1));
    provider.updateRecordingDevice(null);
    await provider.streamDeviceRecording(device: device);
    expect(provider.recordingState, RecordingState.record);
    expect(provider.gates, hasLength(2));
    final nextOmi = provider.streamDeviceRecording(userInitiated: true);
    while (provider.gates.length < 3) {
      await Future<void>.delayed(Duration.zero);
    }
    provider.connect(2);
    await nextOmi;
    expect(phoneSocket.stopped, isTrue);
    expect(mic.stops, greaterThan(0));
    expect(provider.activeRecordingSource, ConversationSource.omi);
    expect(provider.isPhoneMicSelected, isFalse);
    // A queued callback from the stopped microphone cannot write into Omi.
    mic.receive!(Uint8List(320));
    expect(provider.sockets.last.packets, isEmpty);
    await provider.stopStreamDeviceRecording();
  });

  test('phone socket recovery resumes the same microphone without stopping capture', () async {
    Env.setRuntimeModeForTesting(OmiRuntimeMode.offline);
    addTearDown(() => Env.setRuntimeModeForTesting(null));
    final mic = _FakeLiveMicRecorder();
    final provider = _BufferedStartProvider(
      audioListenerLoader: (_, __) async => null,
      microphonePermissionRequester: () async => true,
      phoneMicRecorder: mic,
    );
    addTearDown(provider.dispose);
    final start = provider.streamRecording();
    await pumpEventQueue();
    provider.connect(0, codec: BleAudioCodec.pcm16);
    await start;
    final starts = mic.starts;
    final stops = mic.stops;
    provider.onClosed(1006);
    expect(provider.recordingState, RecordingState.interrupted);
    final reconnect = provider.reconnectActiveCaptureForTesting();
    await pumpEventQueue();
    final resumed = provider.connect(1, codec: BleAudioCodec.pcm16);
    await reconnect;
    expect(provider.recordingState, RecordingState.record);
    expect(mic.starts, starts);
    expect(mic.stops, stops);
    mic.receive!(Uint8List(320));
    expect(resumed.packets, hasLength(1));
    await provider.stopStreamRecording();
    expect(provider.keepAliveScheduledForTesting, isFalse);
  });

  test('local phone start keeps Bluetooth connected and stops device input before permission', () async {
    Env.setRuntimeModeForTesting(OmiRuntimeMode.offline);
    addTearDown(() => Env.setRuntimeModeForTesting(null));
    late CaptureProvider provider;
    provider = CaptureProvider(microphonePermissionRequester: () async {
      expect(provider.havingRecordingDevice, isTrue);
      expect(provider.isPhoneMicSelected, isTrue);
      expect(provider.isPaused, isFalse);
      expect(SharedPreferencesUtil().getBool('nativeBleStreamingEnabled'), isFalse);
      return false;
    });
    addTearDown(provider.dispose);
    provider.updateRecordingDevice(_device(id: 'synthetic-device', type: DeviceType.omi));
    provider.updateRecordingState(RecordingState.deviceRecord);
    await provider.pauseDeviceRecording();
    await provider.streamRecording();
    expect(provider.recordingState, RecordingState.stop);
    expect(provider.havingRecordingDevice, isTrue);
    expect(provider.isPhoneMicSelected, isTrue);
    // A reconnect/home entry can restore button subscription, never steal input.
    await provider.streamDeviceRecording(device: provider.recordingDevice);
    expect(provider.isPhoneMicSelected, isTrue);
    expect(provider.recordingState, RecordingState.stop);
  });

  test('local phone socket failure does not start the mic and permits a fresh retry', () async {
    Env.setRuntimeModeForTesting(OmiRuntimeMode.offline);
    addTearDown(() => Env.setRuntimeModeForTesting(null));
    final mic = _FakeLiveMicRecorder();
    final provider = _CountingSocketCaptureProvider(
      microphonePermissionRequester: () async => true,
      phoneMicRecorder: mic,
    );
    addTearDown(provider.dispose);
    await expectLater(provider.streamRecording(), throwsStateError);
    await expectLater(provider.streamRecording(), throwsStateError);
    expect(provider.openCalls, 2);
    expect(mic.starts, 0);
    expect(provider.recordingState, RecordingState.stop);
    expect(provider.keepAliveScheduledForTesting, isFalse);
  });

  test('immediate phone Stop cancels Start before permission or a socket opens', () async {
    Env.setRuntimeModeForTesting(OmiRuntimeMode.offline);
    addTearDown(() => Env.setRuntimeModeForTesting(null));
    var requests = 0;
    final provider = CaptureProvider(microphonePermissionRequester: () async {
      requests++;
      return false;
    });
    addTearDown(provider.dispose);
    final start = provider.streamRecording();
    final stop = provider.stopStreamRecording();
    await Future.wait([start, stop]);
    expect(requests, 0);
    expect(provider.recordingState, RecordingState.stop);
  });

  test('phone Stop cancels a pending permission request and serializes the next Start', () async {
    Env.setRuntimeModeForTesting(OmiRuntimeMode.offline);
    addTearDown(() => Env.setRuntimeModeForTesting(null));
    final permission = Completer<bool>();
    var requests = 0;
    final provider = CaptureProvider(microphonePermissionRequester: () {
      requests++;
      return requests == 1 ? permission.future : Future.value(false);
    });
    addTearDown(provider.dispose);
    final start = provider.streamRecording();
    while (requests == 0) {
      await Future<void>.delayed(Duration.zero);
    }
    final stop = provider.stopStreamRecording();
    final restart = provider.streamRecording();
    permission.complete(true);
    await Future.wait([start, stop, restart]);
    expect(requests, 2);
    expect(provider.recordingState, RecordingState.stop);
    expect(provider.isPhoneMicSelected, isTrue);
  });

  test('local phone start clears the previous preview before requesting microphone permission', () async {
    Env.setRuntimeModeForTesting(OmiRuntimeMode.offline);
    addTearDown(() => Env.setRuntimeModeForTesting(null));
    final provider = CaptureProvider();
    addTearDown(provider.dispose);
    provider.segments.add(_segment('old', 'Synthetic previous draft'));
    provider.testSessionStartSeconds = 100;
    var permissionRequested = false;
    const channel = MethodChannel('flutter.baseflow.com/permissions/methods');
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(channel, (call) async {
      if (call.method == 'requestPermissions') {
        permissionRequested = true;
        expect(provider.segments, isEmpty);
        expect(provider.activeCaptureSessionId, isNull);
        return {7: 0}; // Denied: the native microphone must not start.
      }
      return null;
    });
    addTearDown(() =>
        TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(channel, null));
    await provider.streamRecording();
    expect(permissionRequested, isTrue);
    expect(provider.recordingState, RecordingState.stop);
  });

  test('streamDeviceRecording does not wait for location capture', () async {
    final locationCapture = _HangingConversationLocationCapture();
    final provider = CaptureProvider(conversationLocationCapture: locationCapture);

    await provider.streamDeviceRecording().timeout(
          const Duration(seconds: 2),
          onTimeout: () => fail('streamDeviceRecording blocked on location capture'),
        );
    expect(locationCapture.calls, 1);
    locationCapture.complete();
    provider.dispose();
  });

  test('phone batch starts native audio before waiting for location metadata', () async {
    await SharedPreferencesUtil().remove('phoneBatchGeolocation');
    final locationCapture = _HangingConversationLocationCapture();
    final micRecorder = _FakeBatchMicRecorder();
    final provider = CaptureProvider(
      conversationLocationCapture: locationCapture,
      microphonePermissionRequester: () async => true,
      phoneMicRecorder: micRecorder,
    );

    await provider.startPhoneMicBatchForTesting().timeout(
          const Duration(seconds: 2),
          onTimeout: () => fail('phone batch start blocked on location capture'),
        );

    expect(micRecorder.startBatchCalls, 1);
    expect(provider.recordingState, RecordingState.record);
    expect(locationCapture.calls, 1);
    var preferences = await SharedPreferences.getInstance();
    expect(preferences.getString('phoneBatchGeolocation'), isNull);

    locationCapture.complete();
    await Future<void>.delayed(Duration.zero);
    await Future<void>.delayed(Duration.zero);
    preferences = await SharedPreferences.getInstance();
    expect(preferences.getString('phoneBatchGeolocation'), isNotNull);
    provider.dispose();
  });

  test('homepage no-device streamDeviceRecording is check-only', () async {
    final locationCapture = _CountingConversationLocationCapture();
    final provider = CaptureProvider(conversationLocationCapture: locationCapture);

    await provider.streamDeviceRecording();
    expect(locationCapture.calls, 1);
    expect(locationCapture.promptIfDeniedArgs, [false]);
    provider.dispose();
  });

  test('active capture identity survives deletion of the first segment', () {
    final provider = CaptureProvider();
    provider.testSessionStartSeconds = 12345;
    provider.segments = [_segment('first', 'one'), _segment('second', 'two')];

    final captureIdentity = provider.activeCaptureSessionId;
    provider.onMessageEventReceived(SegmentsDeletedEvent(segmentIds: ['first']));

    expect(captureIdentity, 'live-12345');
    expect(provider.activeCaptureSessionId, captureIdentity);
    expect(provider.segments.single.id, 'second');
  });

  group('metricsNotifyEnabled', () {
    test('defaults to not notifying on metrics update', () {
      final provider = CaptureProvider();
      // By default, metrics notify is disabled
      // We can verify this by checking that the provider was created successfully
      // and bleReceiveRateKbps/wsSendRateKbps are accessible (default 0)
      expect(provider.bleReceiveRateKbps, 0.0);
      expect(provider.wsSendRateKbps, 0.0);
    });

    test('addMetricsListener() enables metrics notifications on first listener', () {
      final provider = CaptureProvider();
      var notifyCount = 0;
      provider.addListener(() => notifyCount++);

      provider.addMetricsListener();

      // Should notify when first listener is added
      expect(notifyCount, 1);
    });

    test('removeMetricsListener() handles multiple listeners correctly', () {
      final provider = CaptureProvider();
      var notifyCount = 0;
      provider.addListener(() => notifyCount++);

      // Add two listeners
      provider.addMetricsListener();
      provider.addMetricsListener();
      expect(notifyCount, 1); // Only first add triggers notification

      // Remove one listener - metrics should still be enabled
      provider.removeMetricsListener();
      // Provider still has one listener, so metrics are still enabled

      // Remove second listener - metrics now disabled
      provider.removeMetricsListener();

      // Verify count doesn't go negative
      provider.removeMetricsListener();
    });
  });

  group('metricsNotifyEnabled gating', () {
    test('metrics update does NOT call listeners when no metrics listeners registered', () {
      final provider = CaptureProvider();
      var notifyCount = 0;
      provider.addListener(() => notifyCount++);

      // Don't add any metrics listeners - should NOT notify on metrics update
      final initialCount = notifyCount;
      provider.calculateMetricsForTesting();

      // Should not have triggered additional notifications
      expect(notifyCount, initialCount);
    });

    test('metrics update DOES call listeners when at least one metrics listener registered', () {
      final provider = CaptureProvider();
      var notifyCount = 0;
      provider.addListener(() => notifyCount++);

      // Add a metrics listener - this triggers one notification
      provider.addMetricsListener();
      final countAfterAdd = notifyCount;

      // Now metrics update should notify
      provider.calculateMetricsForTesting();

      // Should have triggered an additional notification
      expect(notifyCount, greaterThan(countAfterAdd));
    });
  });

  group('segmentsPhotosVersion', () {
    test('increments on translation event', () {
      final provider = CaptureProvider();
      final segment = _segment('a', 'hello');
      provider.segments = [segment];

      final initialVersion = provider.segmentsPhotosVersion;

      // Simulate translation event
      provider.onMessageEventReceived(
        TranslationEvent(
          segments: [
            TranscriptSegment(
              id: 'a',
              text: 'hello (translated)',
              speaker: 'SPEAKER_00',
              isUser: false,
              personId: null,
              start: 0.0,
              end: 1.0,
              translations: [],
            ),
          ],
        ),
      );

      expect(provider.segmentsPhotosVersion, greaterThan(initialVersion));
    });

    test('increments on segments deleted event', () {
      final provider = CaptureProvider();
      provider.segments = [_segment('a', 'one'), _segment('b', 'two')];

      final initialVersion = provider.segmentsPhotosVersion;

      provider.onMessageEventReceived(SegmentsDeletedEvent(segmentIds: ['a']));

      expect(provider.segmentsPhotosVersion, greaterThan(initialVersion));
    });

    test('increments on new segment received', () {
      final provider = CaptureProvider();
      provider.segments = [_segment('seed', 'seed')];
      final initialVersion = provider.segmentsPhotosVersion;

      provider.onSegmentReceived([_segment('x', 'new')]);

      expect(provider.segmentsPhotosVersion, greaterThan(initialVersion));
    });

    test('increments on photo processing event and updates id', () {
      final provider = CaptureProvider();
      provider.photos = [ConversationPhoto(id: 'temp-photo', base64: 'img', createdAt: DateTime.now())];
      final initialVersion = provider.segmentsPhotosVersion;

      provider.onMessageEventReceived(PhotoProcessingEvent(tempId: 'temp-photo', photoId: 'permanent-photo'));

      expect(provider.photos.first.id, 'permanent-photo');
      expect(provider.segmentsPhotosVersion, greaterThan(initialVersion));
    });

    test('increments on photo described event and updates description', () {
      final provider = CaptureProvider();
      provider.photos = [ConversationPhoto(id: 'photo-1', base64: 'img', createdAt: DateTime.now())];
      final initialVersion = provider.segmentsPhotosVersion;

      provider.onMessageEventReceived(PhotoDescribedEvent(photoId: 'photo-1', description: 'desc', discarded: true));

      expect(provider.photos.first.description, 'desc');
      expect(provider.photos.first.discarded, true);
      expect(provider.segmentsPhotosVersion, greaterThan(initialVersion));
    });
  });

  group('SpeakerLabelSuggestionEvent', () {
    test('ignores event when personId is empty', () {
      final provider = CaptureProvider();
      provider.segments = [_segment('seg1', 'hello')];

      // Empty personId: backend didn't assign, nothing happens
      final event = SpeakerLabelSuggestionEvent(speakerId: 0, personId: '', personName: 'Alice', segmentId: 'seg1');

      provider.onMessageEventReceived(event);

      // Nothing stored, nothing applied
      expect(provider.suggestionsBySegmentId.containsKey('seg1'), false);
      expect(provider.segments.first.personId, isNull);
    });

    test('auto-applies assignment when personId is provided', () {
      final provider = CaptureProvider();
      // Create segment with speakerId 1 to match the event
      final segment = TranscriptSegment(
        id: 'seg1',
        text: 'hello',
        speaker: 'SPEAKER_01',
        isUser: false,
        personId: null,
        start: 0.0,
        end: 1.0,
        translations: [],
      );
      provider.segments = [segment];

      // New app path: personId is provided, auto-apply to segment
      final event = SpeakerLabelSuggestionEvent(
        speakerId: 1,
        personId: 'person-123',
        personName: 'Alice',
        segmentId: 'seg1',
      );

      provider.onMessageEventReceived(event);

      // Suggestion should NOT be stored (auto-applied instead)
      expect(provider.suggestionsBySegmentId.containsKey('seg1'), false);
      // Segment should be updated with personId
      expect(provider.segments.first.personId, 'person-123');
    });

    test('ignores suggestion for segments being tagged', () {
      final provider = CaptureProvider();
      provider.segments = [_segment('seg-tagging', 'text')];
      provider.taggingSegmentIds = ['seg-tagging'];

      final event = SpeakerLabelSuggestionEvent(
        speakerId: 1,
        personId: 'person-456',
        personName: 'Bob',
        segmentId: 'seg-tagging',
      );

      provider.onMessageEventReceived(event);

      // Should not store suggestion for segment being tagged
      expect(provider.suggestionsBySegmentId.containsKey('seg-tagging'), false);
    });

    test('ignores suggestion for already assigned segments', () {
      final provider = CaptureProvider();
      final assignedSegment = TranscriptSegment(
        id: 'seg-assigned',
        text: 'hello',
        speaker: 'SPEAKER_00',
        isUser: false,
        personId: 'existing-person',
        start: 0.0,
        end: 1.0,
        translations: [],
      );
      provider.segments = [assignedSegment];

      final event = SpeakerLabelSuggestionEvent(
        speakerId: 1,
        personId: 'new-person',
        personName: 'NewPerson',
        segmentId: 'seg-assigned',
      );

      provider.onMessageEventReceived(event);

      // Should not store suggestion for already assigned segment
      expect(provider.suggestionsBySegmentId.containsKey('seg-assigned'), false);
    });
  });

  group('People cache refresh', () {
    TranscriptSegment _segmentWithPerson(String id, String? personId) {
      return TranscriptSegment(
        id: id,
        text: 'text',
        speaker: 'SPEAKER_00',
        isUser: false,
        personId: personId,
        start: 0.0,
        end: 1.0,
        translations: [],
      );
    }

    test('triggers setPeople when segment has unknown personId', () {
      final provider = CaptureProvider();
      final mockExternalActions = MockCaptureExternalActions();
      provider.updateExternalActions(mockExternalActions);

      // Pre-populate segments to skip platform-specific initialization code
      provider.segments = [_segmentWithPerson('seed', null)];

      // Segment with personId that's not in cache (cachedPeople is empty)
      final segments = [_segmentWithPerson('seg1', 'unknown-person-id')];

      provider.onSegmentReceived(segments);

      // Should have triggered setPeople
      expect(mockExternalActions.setPeopleCallCount, 1);
    });

    test('does not trigger refresh for segments without personId', () {
      final provider = CaptureProvider();
      final mockExternalActions = MockCaptureExternalActions();
      provider.updateExternalActions(mockExternalActions);

      // Pre-populate segments to skip platform-specific initialization code
      provider.segments = [_segmentWithPerson('seed', null)];

      final segments = [_segmentWithPerson('seg2', null)];

      provider.onSegmentReceived(segments);

      // Should NOT trigger setPeople (no personId to check)
      expect(mockExternalActions.setPeopleCallCount, 0);
    });

    test('does not trigger multiple refreshes while one is in-flight', () async {
      final provider = CaptureProvider();
      final mockExternalActions = MockCaptureExternalActions();

      // Set up a completer to control when setPeople completes
      final completer = Completer<void>();
      mockExternalActions.setSetPeopleCompleter(completer);

      provider.updateExternalActions(mockExternalActions);

      // Pre-populate segments to skip platform-specific initialization code
      provider.segments = [_segmentWithPerson('seed', null)];

      // First segment with unknown personId
      final segments1 = [_segmentWithPerson('seg-a', 'unknown-1')];
      provider.onSegmentReceived(segments1);

      // Should trigger first call
      expect(mockExternalActions.setPeopleCallCount, 1);

      // Second segment with different unknown personId while first is still in-flight
      final segments2 = [_segmentWithPerson('seg-b', 'unknown-2')];
      provider.onSegmentReceived(segments2);

      // Should NOT trigger another call (first is still in-flight)
      expect(mockExternalActions.setPeopleCallCount, 1);

      // Complete the first call
      completer.complete();
      await Future.delayed(Duration.zero); // Let the future complete

      // Third segment - now a new call should be allowed
      final segments3 = [_segmentWithPerson('seg-c', 'unknown-3')];
      provider.onSegmentReceived(segments3);

      // Should trigger a new call
      expect(mockExternalActions.setPeopleCallCount, 2);
    });
  });

  group('external actions port', () {
    test('repeated external-action updates do not schedule duplicate startup recovery', () {
      fakeAsync((async) {
        final provider = CaptureProvider();
        final pendingTimersBeforeUpdates = async.pendingTimers.length;

        provider.updateExternalActions(MockCaptureExternalActions());
        provider.updateExternalActions(MockCaptureExternalActions());

        expect(async.pendingTimers, hasLength(pendingTimersBeforeUpdates));
      });
    });

    test('topConversationId delegates through external actions', () {
      final provider = CaptureProvider();
      final mockExternalActions = MockCaptureExternalActions()..topConversationIdOverride = 'conversation-1';

      provider.updateExternalActions(mockExternalActions);

      expect(provider.topConversationId, 'conversation-1');
    });

    test('bare provider does not reset freemium threshold when usage state is unknown', () async {
      final provider = CaptureProvider();
      provider.onMessageEventReceived(
        FreemiumThresholdReachedEvent(remainingSeconds: 120, action: FreemiumAction.setupOnDeviceStt),
      );

      expect(provider.freemiumThresholdReached, isTrue);
      await provider.checkCreditsAndResetThresholdIfNeeded();

      expect(provider.freemiumThresholdReached, isTrue);
    });

    test('resets freemium threshold when wired usage state reports credits restored', () async {
      final provider = CaptureProvider();
      final mockExternalActions = MockCaptureExternalActions()..outOfCreditsOverride = false;
      provider.updateExternalActions(mockExternalActions);
      provider.onMessageEventReceived(
        FreemiumThresholdReachedEvent(remainingSeconds: 120, action: FreemiumAction.setupOnDeviceStt),
      );

      await provider.checkCreditsAndResetThresholdIfNeeded();

      expect(mockExternalActions.fetchSubscriptionCallCount, 1);
      expect(provider.freemiumThresholdReached, isFalse);
    });
  });

  group('onClosed warning snackbar', () {
    Future<void> _pumpAppWithScaffold(WidgetTester tester) async {
      await tester.pumpWidget(
        MaterialApp(
          navigatorKey: globalNavigatorKey,
          localizationsDelegates: const [
            AppLocalizations.delegate,
            GlobalMaterialLocalizations.delegate,
            GlobalWidgetsLocalizations.delegate,
            GlobalCupertinoLocalizations.delegate,
          ],
          supportedLocales: AppLocalizations.supportedLocales,
          home: const Scaffold(body: SizedBox.shrink()),
        ),
      );
      await tester.pump();
    }

    testWidgets('shows reconnecting warning when socket closes during phone mic recording', (tester) async {
      final provider = CaptureProvider();
      provider.onConnectionStateChanged(true);
      provider.updateRecordingState(RecordingState.record);

      await _pumpAppWithScaffold(tester);

      provider.onClosed();
      // Prevent keepalive reconnect branch from attempting websocket work in this test.
      provider.updateRecordingState(RecordingState.stop);
      await tester.pump();

      final context = tester.element(find.byType(Scaffold));
      final expectedText = AppLocalizations.of(context).transcriptionPausedReconnecting;

      expect(find.byType(SnackBar), findsOneWidget);
      expect(find.text(expectedText), findsOneWidget);
      provider.dispose();
    });

    testWidgets('does not show reconnecting warning when not phone mic recording', (tester) async {
      final provider = CaptureProvider();
      provider.onConnectionStateChanged(true);
      provider.updateRecordingState(RecordingState.stop);

      await _pumpAppWithScaffold(tester);

      provider.onClosed();
      await tester.pump();

      final context = tester.element(find.byType(Scaffold));
      final expectedText = AppLocalizations.of(context).transcriptionPausedReconnecting;

      expect(find.byType(SnackBar), findsNothing);
      expect(find.text(expectedText), findsNothing);
      provider.dispose();
    });
  });

  group('terminal live transcription status', () {
    test('preserves server STT failure across socket close until ready', () {
      final provider = CaptureProvider();
      final failure = MessageServiceStatusEvent(
        status: 'stt_failed',
        outcome: 'upstream_error',
        provider: 'deepgram',
        retryable: true,
        reason: 'connection_lost',
      );

      provider.onMessageEventReceived(failure);
      expect(provider.terminalTranscriptionFailure?.outcome, 'upstream_error');
      expect(provider.terminalTranscriptionFailure?.retryable, isTrue);

      provider.onClosed();
      expect(provider.terminalTranscriptionFailure?.status, 'stt_failed');

      provider.onMessageEventReceived(MessageServiceStatusEvent(status: 'ready'));
      expect(provider.terminalTranscriptionFailure, isNull);
      provider.dispose();
    });

    test('parses legacy and populated service-status payloads additively', () {
      final legacy = MessageServiceStatusEvent.fromJson({'type': 'service_status', 'status': 'ready'});
      final failed = MessageServiceStatusEvent.fromJson({
        'type': 'service_status',
        'status': 'stt_failed',
        'outcome': 'timeout',
        'provider': 'parakeet',
        'retryable': true,
        'reason': 'send_failed',
      });

      expect(legacy.outcome, isNull);
      expect(failed.outcome, 'timeout');
      expect(failed.provider, 'parakeet');
      expect(failed.retryable, isTrue);
      expect(failed.reason, 'send_failed');
    });

    test('capture-only readiness cannot clear local preview failure', () {
      final provider = CaptureProvider();
      addTearDown(provider.dispose);
      provider.onMessageEventReceived(MessageServiceStatusEvent.fromJson({
        'type': 'service_status',
        'status': 'stt_failed',
        'provider': 'local_live_preview',
        'outcome': 'unavailable',
        'reason': 'busy',
        'retryable': false,
      }));
      provider.onMessageEventReceived(MessageServiceStatusEvent(status: 'ready', provider: 'offline_capture'));
      expect(provider.terminalTranscriptionFailure?.reason, 'busy');
      provider.onMessageEventReceived(MessageServiceStatusEvent(status: 'ready', provider: 'local_live_preview'));
      expect(provider.terminalTranscriptionFailure, isNull);
    });

    test('local preview failure received before subscription is not lost', () async {
      final provider = CaptureProvider();
      addTearDown(provider.dispose);
      final service = TranscriptSegmentSocketService.withSocket(16000, BleAudioCodec.pcm16, 'en', _AudioPacketSocket());
      service.onMessage(jsonEncode({
        'type': 'service_status',
        'status': 'stt_failed',
        'provider': 'local_live_preview',
        'reason': 'disabled',
        'retryable': false,
      }));
      service.subscribe(provider, provider);
      expect(provider.terminalTranscriptionFailure?.reason, 'disabled');
      await service.stop();
      final next = CaptureProvider();
      addTearDown(next.dispose);
      service.subscribe(next, next);
      expect(next.terminalTranscriptionFailure, isNull);
    });
  });

  // Regression coverage for issue #6499: before this change, a socket drop
  // during phone-mic recording (e.g. triggered by an iOS audio session
  // interruption from an incoming call) left recordingState stuck at `record`,
  // so the UI kept claiming the session was live while the pipeline was dead.
  group('onClosed recordingState reflection (#6499)', () {
    test('flips record to interrupted when socket drops during phone mic', () {
      final provider = CaptureProvider();
      provider.onConnectionStateChanged(true);
      provider.updateRecordingState(RecordingState.record);

      provider.onClosed();

      expect(provider.recordingState, RecordingState.interrupted);
      // Stop the state so the keepalive timer doesn't try to reconnect.
      provider.updateRecordingState(RecordingState.stop);
      provider.dispose();
    });

    test('leaves deviceRecord state untouched when socket drops', () {
      final provider = CaptureProvider();
      provider.onConnectionStateChanged(true);
      provider.updateRecordingState(RecordingState.deviceRecord);

      provider.onClosed();

      expect(provider.recordingState, RecordingState.deviceRecord);
      provider.updateRecordingState(RecordingState.stop);
      provider.dispose();
    });

    test('leaves stop state untouched when onClosed fires after user stop', () {
      final provider = CaptureProvider();
      provider.onConnectionStateChanged(true);
      provider.updateRecordingState(RecordingState.stop);

      provider.onClosed();

      expect(provider.recordingState, RecordingState.stop);
      expect(provider.keepAliveScheduledForTesting, isFalse);
      provider.dispose();
    });

    test('schedules reconnect only for an active device capture', () {
      final provider = CaptureProvider();
      provider.updateRecordingDevice(_device(id: 'AA:BB:CC:DD:EE:FF', type: DeviceType.omi));
      provider.updateRecordingState(RecordingState.deviceRecord);

      provider.onClosed();

      expect(provider.keepAliveScheduledForTesting, isTrue);
      provider.updateRecordingState(RecordingState.stop);
      provider.onClosed();
      expect(provider.keepAliveScheduledForTesting, isFalse);
      provider.dispose();
    });

    test('does not reconnect after device capture stops while codec lookup is pending', () async {
      final codec = Completer<BleAudioCodec>();
      final provider = _CountingSocketCaptureProvider(audioCodecLoader: (_) => codec.future);
      provider.updateRecordingDevice(_device(id: 'AA:BB:CC:DD:EE:FF', type: DeviceType.omi));
      provider.updateRecordingState(RecordingState.deviceRecord);

      final reconnect = provider.reconnectActiveCaptureForTesting();
      await Future<void>.delayed(Duration.zero);
      provider.updateRecordingState(RecordingState.stop);
      codec.complete(BleAudioCodec.opus);
      await reconnect;

      expect(provider.openCalls, 0);
      provider.dispose();
    });

    test('system audio capture remains eligible for websocket reconnect', () async {
      final provider = _CountingSocketCaptureProvider();
      provider.updateRecordingState(RecordingState.systemAudioRecord);

      await provider.reconnectActiveCaptureForTesting();

      expect(provider.openCalls, 1);
      provider.dispose();
    });

    test('onConnected restores record from interrupted', () {
      final provider = CaptureProvider();
      provider.onConnectionStateChanged(true);
      provider.updateRecordingState(RecordingState.record);

      provider.onClosed();
      expect(provider.recordingState, RecordingState.interrupted);

      provider.onConnected();

      expect(provider.recordingState, RecordingState.record);
      provider.updateRecordingState(RecordingState.stop);
      provider.dispose();
    });

    test('onConnected does not alter stop state', () {
      final provider = CaptureProvider();
      provider.onConnectionStateChanged(true);
      provider.updateRecordingState(RecordingState.stop);

      provider.onConnected();

      expect(provider.recordingState, RecordingState.stop);
      provider.dispose();
    });

    test('recordingDeviceServiceReady includes interrupted state', () {
      final provider = CaptureProvider();
      provider.updateRecordingState(RecordingState.interrupted);

      expect(provider.recordingDeviceServiceReady, isTrue);

      provider.updateRecordingState(RecordingState.stop);
      provider.dispose();
    });
  });

  // ------------------------------------------------------------------ //
  // Issue #7548: Background Mode fail-closed guardrail tests           //
  // ------------------------------------------------------------------ //

  group('hasNativeBleAudioRoute', () {
    test('returns false when no device connected', () {
      final provider = CaptureProvider();
      expect(provider.hasNativeBleAudioRoute, isFalse);
      provider.dispose();
    });

    test('returns false for empty device id (stale sentinel)', () {
      final provider = CaptureProvider();
      // Simulate an Omi device with empty id — should be rejected to avoid
      // false positive where a sentinel device is treated as available.
      provider.updateRecordingDevice(_device(id: '', type: DeviceType.omi));
      expect(provider.hasNativeBleAudioRoute, isFalse);
      provider.dispose();
    });

    test('returns true for Omi device with non-empty id', () {
      final provider = CaptureProvider();
      provider.updateRecordingDevice(_device(id: 'AA:BB:CC:DD:EE:FF', type: DeviceType.omi));
      expect(provider.hasNativeBleAudioRoute, isTrue);
      provider.dispose();
    });

    test('returns true for OpenGlass device with non-empty id', () {
      final provider = CaptureProvider();
      provider.updateRecordingDevice(_device(id: '11:22:33:44:55:66', type: DeviceType.openglass));
      expect(provider.hasNativeBleAudioRoute, isTrue);
      provider.dispose();
    });

    test('returns true for Friend Pendant device with non-empty id', () {
      final provider = CaptureProvider();
      provider.updateRecordingDevice(_device(id: 'AA:BB:CC:DD:EE:FF', type: DeviceType.friendPendant));
      expect(provider.hasNativeBleAudioRoute, isTrue);
      provider.dispose();
    });

    test('returns false for Apple Watch', () {
      final provider = CaptureProvider();
      provider.updateRecordingDevice(_device(id: 'AA:BB:CC:DD:EE:FF', type: DeviceType.appleWatch));
      expect(provider.hasNativeBleAudioRoute, isFalse);
      provider.dispose();
    });

    test('returns false for Bee', () {
      final provider = CaptureProvider();
      provider.updateRecordingDevice(_device(id: 'AA:BB:CC:DD:EE:FF', type: DeviceType.bee));
      expect(provider.hasNativeBleAudioRoute, isFalse);
      provider.dispose();
    });

    test('returns false for Fieldy', () {
      final provider = CaptureProvider();
      provider.updateRecordingDevice(_device(id: 'AA:BB:CC:DD:EE:FF', type: DeviceType.fieldy));
      expect(provider.hasNativeBleAudioRoute, isFalse);
      provider.dispose();
    });

    test('returns true for Limitless (flash-drain route) but no background-stream route', () {
      final provider = CaptureProvider();
      provider.updateRecordingDevice(_device(id: 'AA:BB:CC:DD:EE:FF', type: DeviceType.limitless));
      expect(provider.hasNativeBleAudioRoute, isTrue);
      expect(provider.hasNativeBackgroundStreamRoute, isFalse);
      provider.dispose();
    });

    test('returns false for Plaud', () {
      final provider = CaptureProvider();
      provider.updateRecordingDevice(_device(id: 'AA:BB:CC:DD:EE:FF', type: DeviceType.plaud));
      expect(provider.hasNativeBleAudioRoute, isFalse);
      provider.dispose();
    });
  });

  group('setBackgroundModeEnabled', () {
    setUp(() async {
      SharedPreferences.setMockInitialValues({});
      await SharedPreferencesUtil.init();
      SharedPreferencesUtil().batchModeEnabled = false;
      SharedPreferencesUtil().backgroundModeEnabled = false;
      await SharedPreferencesUtil().saveBool('nativeBleStreamingEnabled', false);
      await SharedPreferencesUtil().saveBool('nativeBleForegroundReady', false);
      await SharedPreferencesUtil().remove('nativeBleStreamConfig');
    });

    test('disable clears realtime prefs and stale config when batch mode is off', () async {
      final provider = CaptureProvider();
      // Pre-set prefs to true to verify they get cleared
      SharedPreferencesUtil().backgroundModeEnabled = true;
      await SharedPreferencesUtil().saveBool('nativeBleStreamingEnabled', true);
      await SharedPreferencesUtil().saveBool('nativeBleForegroundReady', true);
      await SharedPreferencesUtil().saveString('nativeBleStreamConfig', '{"test": true}');

      final result = await provider.setBackgroundModeEnabled(false);

      expect(result, isTrue);
      expect(SharedPreferencesUtil().backgroundModeEnabled, isFalse);
      expect(SharedPreferencesUtil().getBool('nativeBleStreamingEnabled'), isFalse);
      expect(SharedPreferencesUtil().getBool('nativeBleForegroundReady'), isFalse);
      expect(SharedPreferencesUtil().getString('nativeBleStreamConfig'), isEmpty);
      provider.dispose();
    });

    test('disable keeps native config when batch mode still needs it without a live route', () async {
      final provider = CaptureProvider();
      SharedPreferencesUtil().batchModeEnabled = true;
      SharedPreferencesUtil().backgroundModeEnabled = true;
      await SharedPreferencesUtil().saveBool('nativeBleStreamingEnabled', true);
      await SharedPreferencesUtil().saveBool('nativeBleForegroundReady', true);
      await SharedPreferencesUtil().saveString('nativeBleStreamConfig', '{"test": true}');

      final result = await provider.setBackgroundModeEnabled(false);

      expect(result, isTrue);
      expect(SharedPreferencesUtil().backgroundModeEnabled, isFalse);
      expect(SharedPreferencesUtil().getBool('nativeBleStreamingEnabled'), isFalse);
      expect(SharedPreferencesUtil().getBool('nativeBleForegroundReady'), isFalse);
      expect(SharedPreferencesUtil().getString('nativeBleStreamConfig'), '{"test": true}');
      provider.dispose();
    });

    test('enable rejects when no device connected', () async {
      final provider = CaptureProvider();

      final result = await provider.setBackgroundModeEnabled(true);

      expect(result, isFalse);
      expect(SharedPreferencesUtil().backgroundModeEnabled, isFalse);
      expect(SharedPreferencesUtil().getBool('nativeBleStreamingEnabled'), isFalse);
      provider.dispose();
    });

    test('enable rejects for device with no native route (Apple Watch)', () async {
      final provider = CaptureProvider();
      provider.updateRecordingDevice(_device(id: 'AA:BB:CC:DD:EE:FF', type: DeviceType.appleWatch));

      final result = await provider.setBackgroundModeEnabled(true);

      expect(result, isFalse);
      expect(SharedPreferencesUtil().backgroundModeEnabled, isFalse);
      provider.dispose();
    });

    test('enable rejects for device with no native route (Bee)', () async {
      final provider = CaptureProvider();
      provider.updateRecordingDevice(_device(id: 'AA:BB:CC:DD:EE:FF', type: DeviceType.bee));

      final result = await provider.setBackgroundModeEnabled(true);

      expect(result, isFalse);
      expect(SharedPreferencesUtil().backgroundModeEnabled, isFalse);
      provider.dispose();
    });

    test('enable rejects for device with no native route (Fieldy)', () async {
      final provider = CaptureProvider();
      provider.updateRecordingDevice(_device(id: 'AA:BB:CC:DD:EE:FF', type: DeviceType.fieldy));

      final result = await provider.setBackgroundModeEnabled(true);

      expect(result, isFalse);
      expect(SharedPreferencesUtil().backgroundModeEnabled, isFalse);
      provider.dispose();
    });

    test('enable rejects for device with no native route (Limitless)', () async {
      final provider = CaptureProvider();
      provider.updateRecordingDevice(_device(id: 'AA:BB:CC:DD:EE:FF', type: DeviceType.limitless));

      final result = await provider.setBackgroundModeEnabled(true);

      expect(result, isFalse);
      expect(SharedPreferencesUtil().backgroundModeEnabled, isFalse);
      provider.dispose();
    });

    test('enable rejects for device with no native route (Plaud)', () async {
      final provider = CaptureProvider();
      provider.updateRecordingDevice(_device(id: 'AA:BB:CC:DD:EE:FF', type: DeviceType.plaud));

      final result = await provider.setBackgroundModeEnabled(true);

      expect(result, isFalse);
      expect(SharedPreferencesUtil().backgroundModeEnabled, isFalse);
      provider.dispose();
    });

    test('enable rejects for empty-id Omi device (stale sentinel)', () async {
      final provider = CaptureProvider();
      provider.updateRecordingDevice(_device(id: '', type: DeviceType.omi));

      final result = await provider.setBackgroundModeEnabled(true);

      expect(result, isFalse);
      expect(SharedPreferencesUtil().backgroundModeEnabled, isFalse);
      provider.dispose();
    });

    test('enable accepts for Omi device with valid id', () async {
      final provider = CaptureProvider();
      provider.updateRecordingDevice(_device(id: 'AA:BB:CC:DD:EE:FF', type: DeviceType.omi));

      final result = await provider.setBackgroundModeEnabled(true);

      expect(result, isTrue);
      expect(SharedPreferencesUtil().backgroundModeEnabled, isTrue);
      // Batch mode is off by default, so nativeBleStreamingEnabled should be true
      expect(SharedPreferencesUtil().getBool('nativeBleStreamingEnabled'), isTrue);
      provider.dispose();
    });

    test('keeps native Omi background audio disabled when Custom STT raw forwarding is off', () async {
      await SharedPreferencesUtil().saveCustomSttConfig(
        const CustomSttConfig(provider: SttProvider.onDeviceWhisper, sendRawAudioToOmi: false),
      );
      final provider = CaptureProvider();
      provider.updateRecordingDevice(_device(id: 'AA:BB:CC:DD:EE:FF', type: DeviceType.omi));

      final result = await provider.setBackgroundModeEnabled(true);

      expect(result, isTrue);
      expect(SharedPreferencesUtil().backgroundModeEnabled, isTrue);
      expect(SharedPreferencesUtil().getBool('nativeBleStreamingEnabled'), isFalse);
      provider.dispose();
    });

    test('enable preserves foreground-ready when foreground streaming is already active', () async {
      final provider = CaptureProvider();
      provider.updateRecordingDevice(_device(id: 'AA:BB:CC:DD:EE:FF', type: DeviceType.omi));
      await SharedPreferencesUtil().saveBool('nativeBleForegroundReady', true);

      final result = await provider.setBackgroundModeEnabled(true);

      expect(result, isTrue);
      expect(SharedPreferencesUtil().backgroundModeEnabled, isTrue);
      expect(SharedPreferencesUtil().getBool('nativeBleStreamingEnabled'), isTrue);
      expect(SharedPreferencesUtil().getBool('nativeBleForegroundReady'), isTrue);
      provider.dispose();
    });

    test('enable accepts for OpenGlass device with valid id', () async {
      final provider = CaptureProvider();
      provider.updateRecordingDevice(_device(id: '11:22:33:44:55:66', type: DeviceType.openglass));

      final result = await provider.setBackgroundModeEnabled(true);

      expect(result, isTrue);
      expect(SharedPreferencesUtil().backgroundModeEnabled, isTrue);
      expect(SharedPreferencesUtil().getBool('nativeBleStreamingEnabled'), isTrue);
      provider.dispose();
    });

    test('enable accepts for Friend Pendant device with valid id', () async {
      final provider = CaptureProvider();
      provider.updateRecordingDevice(_device(id: 'AA:BB:CC:DD:EE:FF', type: DeviceType.friendPendant));

      final result = await provider.setBackgroundModeEnabled(true);

      expect(result, isTrue);
      expect(SharedPreferencesUtil().backgroundModeEnabled, isTrue);
      expect(SharedPreferencesUtil().getBool('nativeBleStreamingEnabled'), isTrue);
      provider.dispose();
    });

    test('enable with batch mode on sets backgroundModeEnabled but not nativeBleStreaming', () async {
      final provider = CaptureProvider();
      provider.updateRecordingDevice(_device(id: 'AA:BB:CC:DD:EE:FF', type: DeviceType.omi));
      SharedPreferencesUtil().batchModeEnabled = true;

      final result = await provider.setBackgroundModeEnabled(true);

      expect(result, isTrue);
      expect(SharedPreferencesUtil().backgroundModeEnabled, isTrue);
      // Batch mode is on, so native streaming should stay false
      expect(SharedPreferencesUtil().getBool('nativeBleStreamingEnabled'), isFalse);
      provider.dispose();
    });

    test('rejected enable clears stale config', () async {
      final provider = CaptureProvider();
      // No device connected — should reject
      await SharedPreferencesUtil().saveString('nativeBleStreamConfig', '{"stale": true}');
      await SharedPreferencesUtil().saveBool('nativeBleStreamingEnabled', true);
      await SharedPreferencesUtil().saveBool('nativeBleForegroundReady', true);
      SharedPreferencesUtil().backgroundModeEnabled = true;

      final result = await provider.setBackgroundModeEnabled(true);

      expect(result, isFalse);
      expect(SharedPreferencesUtil().backgroundModeEnabled, isFalse);
      expect(SharedPreferencesUtil().getBool('nativeBleStreamingEnabled'), isFalse);
      expect(SharedPreferencesUtil().getBool('nativeBleForegroundReady'), isFalse);
      expect(SharedPreferencesUtil().getString('nativeBleStreamConfig'), isEmpty);
      provider.dispose();
    });

    test('enable/disable cycle works for valid device', () async {
      final provider = CaptureProvider();
      provider.updateRecordingDevice(_device(id: 'AA:BB:CC:DD:EE:FF', type: DeviceType.omi));

      // Enable
      expect(await provider.setBackgroundModeEnabled(true), isTrue);
      expect(SharedPreferencesUtil().backgroundModeEnabled, isTrue);

      // Disable
      expect(await provider.setBackgroundModeEnabled(false), isTrue);
      expect(SharedPreferencesUtil().backgroundModeEnabled, isFalse);

      provider.dispose();
    });
  });

  group('unsupported Custom STT codec privacy recovery', () {
    setUp(() async {
      SharedPreferences.setMockInitialValues({});
      await SharedPreferencesUtil.init();
      await SharedPreferencesUtil().saveCustomSttConfig(
        const CustomSttConfig(provider: SttProvider.onDeviceWhisper, sendRawAudioToOmi: false),
      );
      await SharedPreferencesUtil().saveBool('nativeBleStreamingEnabled', true);
    });

    tearDown(() async {
      await SharedPreferencesUtil().saveCustomSttConfig(CustomSttConfig.defaultConfig);
    });

    test('disables native Omi audio and schedules a websocket retry', () {
      fakeAsync((async) {
        final provider = _NullSocketCaptureProvider();
        provider.updateRecordingDevice(_device(id: 'AA:BB:CC:DD:EE:FF', type: DeviceType.omi));
        provider.updateRecordingState(RecordingState.deviceRecord);
        final timersBefore = async.pendingTimers.length;
        var completed = false;

        provider.changeAudioRecordProfile(audioCodec: BleAudioCodec.lc3FS1030).then((_) => completed = true);
        async.flushMicrotasks();

        expect(completed, isTrue);
        expect(SharedPreferencesUtil().getBool('nativeBleStreamingEnabled'), isFalse);
        expect(async.pendingTimers.length, timersBefore + 1);
        provider.dispose();
      });
    });

    test('does not schedule a websocket retry when capture is idle', () {
      fakeAsync((async) {
        final provider = CaptureProvider();
        final timersBefore = async.pendingTimers.length;

        provider.changeAudioRecordProfile(audioCodec: BleAudioCodec.lc3FS1030);
        async.flushMicrotasks();

        expect(async.pendingTimers.length, timersBefore);
        provider.dispose();
      });
    });
  });

  group('stale reconciliation — hasNativeBleAudioRoute after device switch', () {
    test('switching from valid device to no device clears route', () {
      final provider = CaptureProvider();
      provider.updateRecordingDevice(_device(id: 'AA:BB:CC:DD:EE:FF', type: DeviceType.omi));
      expect(provider.hasNativeBleAudioRoute, isTrue);

      provider.updateRecordingDevice(null);
      expect(provider.hasNativeBleAudioRoute, isFalse);
      provider.dispose();
    });

    test('switching from Omi to Apple Watch clears route', () {
      final provider = CaptureProvider();
      provider.updateRecordingDevice(_device(id: 'AA:BB:CC:DD:EE:FF', type: DeviceType.omi));
      expect(provider.hasNativeBleAudioRoute, isTrue);

      provider.updateRecordingDevice(_device(id: '11:22:33:44:55:66', type: DeviceType.appleWatch));
      expect(provider.hasNativeBleAudioRoute, isFalse);
      provider.dispose();
    });

    test('switching from no-route device to Omi gains route', () {
      final provider = CaptureProvider();
      provider.updateRecordingDevice(_device(id: 'AA:BB:CC:DD:EE:FF', type: DeviceType.fieldy));
      expect(provider.hasNativeBleAudioRoute, isFalse);

      provider.updateRecordingDevice(_device(id: '11:22:33:44:55:66', type: DeviceType.omi));
      expect(provider.hasNativeBleAudioRoute, isTrue);
      provider.dispose();
    });
  });

  group('Background Mode + batch mode interaction', () {
    setUp(() {
      SharedPreferencesUtil().batchModeEnabled = false;
      SharedPreferencesUtil().backgroundModeEnabled = false;
    });

    test('enable background, then enable batch: streaming should be false', () async {
      final provider = CaptureProvider();
      provider.updateRecordingDevice(_device(id: 'AA:BB:CC:DD:EE:FF', type: DeviceType.omi));

      await provider.setBackgroundModeEnabled(true);
      expect(SharedPreferencesUtil().getBool('nativeBleStreamingEnabled'), isTrue);

      // setBatchMode turns batch on
      await provider.setBatchMode(true);
      expect(SharedPreferencesUtil().getBool('nativeBleStreamingEnabled'), isFalse);
      provider.dispose();
    });

    test('batch on, then enable background: streaming should be false (batch wins)', () async {
      final provider = CaptureProvider();
      provider.updateRecordingDevice(_device(id: 'AA:BB:CC:DD:EE:FF', type: DeviceType.omi));
      SharedPreferencesUtil().batchModeEnabled = true;

      await provider.setBackgroundModeEnabled(true);
      // setBackgroundModeEnabled with batch mode on should keep nativeBleStreaming false
      expect(SharedPreferencesUtil().getBool('nativeBleStreamingEnabled'), isFalse);
      provider.dispose();
    });
  });

  // ------------------------------------------------------------------ //
  // Device mute persistence: a double-tap mute must survive an app      //
  // kill/restart, otherwise the device silently resumes recording on    //
  // the next reconnect (Featurebase: "If I turn off recording why       //
  // doesn't it stay off?", "Cv1 unmutes on disconnect/reconnect").      //
  // ------------------------------------------------------------------ //
  group('device mute persistence', () {
    setUp(() {
      SharedPreferencesUtil().deviceMuted = false;
    });

    test('constructor restores muted state when deviceMuted pref is set', () {
      SharedPreferencesUtil().deviceMuted = true;

      final provider = CaptureProvider();

      // _isPaused restored from prefs so the reconnect path re-applies the mute
      // instead of resuming capture.
      expect(provider.isPaused, isTrue);
      provider.dispose();
    });

    test('constructor leaves recording unpaused when deviceMuted pref is unset', () {
      SharedPreferencesUtil().deviceMuted = false;

      final provider = CaptureProvider();

      expect(provider.isPaused, isFalse);
      provider.dispose();
    });

    test('pauseDeviceRecording persists the mute to prefs', () async {
      final provider = CaptureProvider();
      provider.updateRecordingDevice(_device(id: 'AA:BB:CC:DD:EE:FF', type: DeviceType.omi));

      await provider.pauseDeviceRecording();

      expect(provider.isPaused, isTrue);
      expect(SharedPreferencesUtil().deviceMuted, isTrue);
      provider.dispose();
    });
  });

  // Regression coverage for issue #6311: before this change
  // `transcriptServiceReady` was `_transcriptServiceReady && _isConnected`, so a
  // transient ConnectivityService flicker (WiFi↔cellular handoff, brief DNS
  // hiccup) flipped the header to "Recording, reconnecting" even while the
  // transcript WebSocket was alive and segments were flowing. The socket
  // lifecycle is the authoritative signal; a connectivity change must never
  // change transcript-service readiness.
  group('transcriptServiceReady is socket-driven, not connectivity-driven (#6311)', () {
    test('connectivity flicker does not toggle readiness of a ready socket', () {
      final provider = CaptureProvider();

      // Drive the provider into the socket-subscribed state (the scenario the
      // old getter got wrong): onConnected mirrors the transcript WebSocket
      // subscribing, which sets _transcriptServiceReady = true.
      provider.onConnected();
      expect(provider.transcriptServiceReady, isTrue);

      // A connectivity flicker must not toggle readiness off. Before the fix
      // this ANDed _isConnected=false into the getter and manufactured a false
      // "Recording, reconnecting" over a healthy socket.
      provider.onConnectionStateChanged(false);
      expect(provider.transcriptServiceReady, isTrue, reason: 'ready socket must survive a connectivity flicker');

      // And it must not depend on connectivity coming back either.
      provider.onConnectionStateChanged(true);
      expect(provider.transcriptServiceReady, isTrue);

      // A socket close is the only thing that should end readiness.
      provider.onClosed();
      expect(provider.transcriptServiceReady, isFalse, reason: 'socket close must end transcript readiness');
      provider.dispose();
    });
  });

  // Regression coverage for issue #11305: the keep-alive reconnect callback is
  // async, so a tick could call _initiateWebsocket() again while the previous
  // attempt was still connecting. The capture log showed reconnect attempts 24
  // and 25 overlapping (12s apart, both reaching service-ready), which opens
  // duplicate /v4/listen sessions and races the controller state.
  group('no duplicate transcription connection attempt (#11305)', () {
    Future<void> settle() async {
      for (var i = 0; i < 50; i++) {
        await Future<void>.delayed(Duration.zero);
      }
    }

    setUp(() {
      // Batch mode short-circuits the socket entirely; an earlier test leaves
      // the shared preference on.
      SharedPreferencesUtil().batchModeEnabled = false;
    });

    Future<void> startAttempt(
      _GatedSocketCaptureProvider provider, {
      BleAudioCodec codec = BleAudioCodec.pcm16,
      int sampleRate = 16000,
    }) =>
        provider.changeAudioRecordProfile(
          audioCodec: codec,
          sampleRate: sampleRate,
          source: ConversationSource.phone.name,
        );

    test('drops a reconnect attempt while one is still in flight', () async {
      final provider = _GatedSocketCaptureProvider();

      final first = startAttempt(provider);
      await settle();
      expect(provider.openCalls, 1, reason: 'the first attempt must reach the socket service');

      // Second attempt arrives before the first completes: previously it opened
      // a second socket, now it is dropped.
      await startAttempt(provider);
      expect(provider.openCalls, 1, reason: 'an overlapping attempt must not open a second socket');

      provider.release(0);
      await first;

      // The guard clears once the attempt finishes, so reconnects still work.
      final next = startAttempt(provider);
      await settle();
      expect(provider.openCalls, 2, reason: 'a later attempt must connect again');

      provider.releaseAll();
      await next;
      provider.dispose();
    });

    test('does not drop a forced attempt', () async {
      final provider = _GatedSocketCaptureProvider();

      final first = startAttempt(provider);
      await settle();
      expect(provider.openCalls, 1);

      // Settings/profile changes stop the current socket and must replace it
      // even while an attempt is in flight.
      provider.updateRecordingState(RecordingState.record);
      final forced = provider.onTranscriptionSettingsChanged();
      await settle();
      expect(provider.openCalls, 2, reason: 'a forced reconnect must not be gated');

      provider.releaseAll();
      await first;
      await forced;
      provider.dispose();
    });

    test('does not install a stale socket after a newer forced attempt', () async {
      final provider = _GatedSocketCaptureProvider();
      provider.returnSockets = true;

      final first = startAttempt(provider);
      await settle();
      provider.updateRecordingState(RecordingState.record);
      final forced = provider.onTranscriptionSettingsChanged();
      await settle();
      expect(provider.openCalls, 2);

      provider.release(1);
      try {
        await forced;
      } catch (error) {
        expect(error, isA<StateError>());
      }
      expect(provider.lastSubscribedId, 1);

      provider.release(0);
      await first;
      expect(provider.lastSubscribedId, 1);
      provider.dispose();
    });

    test('stays gated while a forced attempt with the same parameters runs', () async {
      final provider = _GatedSocketCaptureProvider();

      final first = startAttempt(provider);
      await settle();
      expect(provider.openCalls, 1);

      // Same parameters as the attempt in flight, so both share a guard key.
      provider.updateRecordingState(RecordingState.record);
      final forced = provider.onTranscriptionSettingsChanged();
      await settle();
      expect(provider.openCalls, 2);

      // The first attempt finishing must not ungate the forced one still
      // connecting, or the next keep-alive tick opens a third socket.
      provider.release(0);
      await first;

      await startAttempt(provider);
      expect(provider.openCalls, 2, reason: 'a repeat must stay gated while the forced attempt is in flight');

      provider.releaseAll();
      await forced;
      provider.dispose();
    });

    test('does not drop an attempt with different parameters', () async {
      final provider = _GatedSocketCaptureProvider();

      final first = startAttempt(provider);
      await settle();
      expect(provider.openCalls, 1);

      // A different codec is a new intent (e.g. the user starts phone mic while
      // a device reconnect is in flight), not the same attempt repeated.
      final other = startAttempt(provider, codec: BleAudioCodec.opus, sampleRate: 16000);
      await settle();
      expect(provider.openCalls, 2, reason: 'a differently configured attempt must not be gated');

      provider.releaseAll();
      await first;
      await other;
      provider.dispose();
    });
  });

  group('in-progress conversation poll cycle', () {
    // The socket starts this cycle on every connect. Restarting an already
    // running cycle put its attempt counter back to zero, so a connection that
    // reconnects more often than the give-up window kept the app polling
    // GET /v1/conversations?...&statuses=in_progress indefinitely instead of
    // ever reaching the cap.
    test('a reconnect mid-cycle does not reset the attempt counter', () {
      fakeAsync((async) {
        final provider = CaptureProvider(inProgressConversationLoader: () async {});
        provider.updateRecordingDevice(_device(id: 'AA:BB:CC:DD:EE:FF', type: DeviceType.omi));
        provider.updateRecordingState(RecordingState.deviceRecord);

        provider.startInProgressConversationRefreshForTesting();
        async.elapse(const Duration(seconds: 10));
        async.flushMicrotasks();

        final attemptsBeforeReconnect = provider.inProgressConversationRefreshAttemptsForTesting;
        expect(attemptsBeforeReconnect, greaterThan(0));

        // Simulate a socket reconnect landing while the cycle is still running.
        provider.startInProgressConversationRefreshForTesting();

        expect(
          provider.inProgressConversationRefreshAttemptsForTesting,
          attemptsBeforeReconnect,
          reason: 'a reconnect must not restart an already-running poll cycle',
        );

        provider.dispose();
      });
    });

    test('the cycle self-terminates at its cap when nothing interrupts it', () {
      fakeAsync((async) {
        var loadCalls = 0;
        final provider = CaptureProvider(inProgressConversationLoader: () async => loadCalls++);
        provider.updateRecordingDevice(_device(id: 'AA:BB:CC:DD:EE:FF', type: DeviceType.omi));
        provider.updateRecordingState(RecordingState.deviceRecord);

        provider.startInProgressConversationRefreshForTesting();
        async.elapse(const Duration(seconds: 90));
        async.flushMicrotasks();

        expect(provider.inProgressConversationRefreshActiveForTesting, isFalse);
        expect(loadCalls, 30);

        provider.dispose();
      });
    });
  });
}
