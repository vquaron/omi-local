import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:omi/services/capture/local_device_audio_start.dart';
import 'package:omi/services/audio_sources/ble_device_source.dart';
import 'package:omi/backend/schema/bt_device/bt_device.dart';

void main() {
  test('startup journals accepted BLE payloads before the socket opens and never journals buffered replay twice', () {
    final payloads = <List<int>>[];
    final source = BleDeviceSource(codec: BleAudioCodec.opus, deviceId: 'synthetic', deviceModel: 'Omi');
    final start = LocalDeviceAudioStart('synthetic')
      ..journalPacket = (packet) {
        payloads.addAll(source.processBytes(packet).map((frame) => frame.payload));
      };
    start.receive([1, 0, 0, 0x78, 4]);
    expect(payloads, [
      [0x78, 4],
    ]);
    start.forwardTo((_) {});
    expect(payloads, hasLength(1));
    start.receive([2, 0, 0, 0x78, 5]);
    expect(payloads, [
      [0x78, 4],
      [0x78, 5],
    ]);
    start.discard();
    start.receive([3, 0, 0, 0x78, 6]);
    expect(payloads, hasLength(2));
  });
  test('late BLE subscription is cancelled after the session was discarded', () async {
    final audio = StreamController<List<int>>.broadcast(sync: true);
    final pending = Completer<StreamSubscription?>();
    final start = LocalDeviceAudioStart('synthetic');
    final attaching = start.attach(pending.future);
    start.receive([0, 0, 0, 1]);
    start.discard();
    pending.complete(audio.stream.listen(start.receive));
    expect(await attaching, isFalse);
    expect(audio.hasListener, isFalse);
    final sent = <List<int>>[];
    start.forwardTo(sent.add);
    expect(sent, isEmpty);
    await audio.close();
  });

  test('overflow fails the start rather than returning a truncated recording', () async {
    final start = LocalDeviceAudioStart('synthetic', maxBufferedBytes: 4);
    start.receive([0, 0, 0, 1]);
    start.receive([1, 0, 0, 2]);
    expect(start.inputStopped, isTrue);
    expect(() => start.forwardTo((_) => fail('No incomplete queue should be delivered')), throwsStateError);
    start.discard();
  });
}
