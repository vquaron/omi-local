import 'dart:async';

import 'package:connectivity_plus_platform_interface/connectivity_plus_platform_interface.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/bt_device/bt_device.dart';
import 'package:omi/providers/capture_provider.dart';
import 'package:omi/providers/device_onboarding_provider.dart';
import 'package:omi/providers/device_provider.dart';
import 'package:omi/services/devices/connectors/device_connection.dart';
import 'package:omi/services/devices.dart';
import 'package:omi/services/devices/transports/device_transport.dart';
import 'package:omi/services/services.dart';

class _TestConnectivityPlatform extends ConnectivityPlatform {
  @override
  Future<List<ConnectivityResult>> checkConnectivity() async => [ConnectivityResult.none];

  @override
  Stream<List<ConnectivityResult>> get onConnectivityChanged => const Stream.empty();
}

class _IdleTransport implements DeviceTransport {
  final _state = StreamController<DeviceTransportState>.broadcast();

  @override
  Stream<DeviceTransportState> get connectionStateStream => _state.stream;

  @override
  dynamic noSuchMethod(Invocation invocation) => throw UnimplementedError('${invocation.memberName}');
}

/// A connection whose reported status is fixed; nothing else is ever touched.
class _FixedConnection extends DeviceConnection {
  _FixedConnection(BtDevice device, DeviceConnectionState state) : super(device, _IdleTransport()) {
    connectionState = state;
  }

  @override
  dynamic noSuchMethod(Invocation invocation) => throw UnimplementedError('${invocation.memberName}');
}

class _SetupDeviceProvider extends DeviceProvider {
  _SetupDeviceProvider({required super.connectionLoader, required super.batteryLevelLoader});

  @override
  Future<void> getDeviceInfo() async {}

  @override
  Future<void> setisDeviceStorageSupport() async {}

  @override
  Future<void> initiateBleBatteryListener() async {}

  @override
  Future<void> initiateChargingStatusListener() async {}
}

class _CaptureProbe implements CaptureProvider {
  final starts = <({BtDevice? device, bool userInitiated})>[];
  final started = Completer<void>();

  @override
  bool get shouldAutomaticallyStartDeviceRecording => true;

  @override
  DeviceOnboardingProvider? get deviceOnboardingProvider => null;

  @override
  void updateRecordingDevice(BtDevice? device) {}

  @override
  Future<void> streamDeviceRecording({BtDevice? device, bool userInitiated = false}) async {
    starts.add((device: device, userInitiated: userInitiated));
    await started.future;
  }

  @override
  dynamic noSuchMethod(Invocation invocation) => throw UnimplementedError('${invocation.memberName}');
}

void main() {
  final device = BtDevice(id: 'AA:AA:AA:AA:AA:07', name: 'Omi', type: DeviceType.omi, rssi: -50);

  setUpAll(() async {
    TestWidgetsFlutterBinding.ensureInitialized();
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
    ConnectivityPlatform.instance = _TestConnectivityPlatform();
    try {
      await ServiceManager.init();
    } catch (_) {}
  });

  test('a connection result that lands after the link dropped does not resurrect the device', () async {
    SharedPreferencesUtil().btDevice = device; // the paired CV1, as in the field
    final pending = Completer<DeviceConnection?>();
    final provider = DeviceProvider(connectionLoader: (id, {force = false}) => pending.future);
    addTearDown(provider.dispose);

    provider.onDeviceConnectionStateChanged(device.id, DeviceConnectionState.connected);
    await Future<void>.delayed(const Duration(milliseconds: 150)); // connect debounce fired, loader awaiting
    provider.onDeviceConnectionStateChanged(device.id, DeviceConnectionState.disconnected);
    await Future<void>.delayed(const Duration(milliseconds: 600)); // disconnect debounce fired
    expect(provider.connectedDevice, isNull);

    pending.complete(_FixedConnection(device, DeviceConnectionState.connected));
    await Future<void>.delayed(const Duration(milliseconds: 50));

    expect(provider.connectedDevice, isNull);
    expect(provider.isConnected, isFalse);
    await Future<void>.delayed(const Duration(milliseconds: 100));
  });

  test('a connection result whose transport is already disconnected is ignored', () async {
    final provider = DeviceProvider(
      connectionLoader: (id, {force = false}) async => _FixedConnection(device, DeviceConnectionState.disconnected),
    );
    addTearDown(provider.dispose);

    provider.onDeviceConnectionStateChanged(device.id, DeviceConnectionState.connected);
    await Future<void>.delayed(const Duration(milliseconds: 200));

    expect(provider.connectedDevice, isNull);
    expect(provider.isConnected, isFalse);
  });

  for (final disconnectDuringSetup in [false, true]) {
    testWidgets(
        disconnectDuringSetup
            ? 'a disconnect during battery setup prevents capture before the disconnect debounce'
            : 'the current connection starts capture after battery setup', (tester) async {
      SharedPreferencesUtil().btDevice = device;
      final battery = Completer<int>();
      var batteryRequested = false;
      final capture = _CaptureProbe();
      final provider = _SetupDeviceProvider(
        connectionLoader: (id, {force = false}) async => _FixedConnection(device, DeviceConnectionState.connected),
        batteryLevelLoader: (_) {
          batteryRequested = true;
          return battery.future;
        },
      )..captureProvider = capture;
      addTearDown(provider.dispose);

      provider.onDeviceConnectionStateChanged(device.id, DeviceConnectionState.connected);
      await tester.pump(const Duration(milliseconds: 100));
      expect(batteryRequested, isTrue);
      expect(provider.connectedDevice, device);
      expect(capture.starts, isEmpty);

      if (disconnectDuringSetup) {
        provider.onDeviceConnectionStateChanged(device.id, DeviceConnectionState.disconnected);
      }
      battery.complete(-1);
      await tester.pump();
      if (disconnectDuringSetup) {
        expect(capture.starts, isEmpty,
            reason: 'the raw disconnect must fence capture while the connected UI is still debounced');
      } else {
        expect(capture.starts, [(device: device, userInitiated: true)]);
        provider.onDeviceConnectionStateChanged(device.id, DeviceConnectionState.disconnected);
      }
      capture.started.complete();
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 500));
      expect(provider.connectedDevice, isNull);
      expect(provider.isConnected, isFalse);
      await tester.pump(const Duration(seconds: 1));
    });
  }

  test('resuming the app reconciles a stale connected state with the transport', () async {
    var live = true;
    final provider = DeviceProvider(
      connectionLoader: (id, {force = false}) async => live ? _FixedConnection(device, DeviceConnectionState.connected) : null,
    );
    addTearDown(provider.dispose);
    await provider.setConnectedDevice(device);
    provider.setIsConnected(true);

    await provider.reconcileConnection();
    expect(provider.connectedDevice, isNotNull);
    expect(provider.isConnected, isTrue);

    live = false;
    await provider.reconcileConnection();
    expect(provider.connectedDevice, isNull);
    expect(provider.isConnected, isFalse);
    await Future<void>.delayed(const Duration(milliseconds: 100)); // let the session-ended bookkeeping settle
  });
}
