import 'dart:async';
import 'dart:collection';

import 'package:omi/backend/schema/bt_device/bt_device.dart';

/// Audio received after local Start, while the existing socket is being opened.
/// The queue belongs to one start attempt and never survives that attempt.
class LocalDeviceAudioStart {
  LocalDeviceAudioStart(this.deviceId, {this.maxBufferedBytes = 1024 * 1024});

  final String deviceId;
  // Covers the 15-second socket timeout even at the maximum mono Opus packet
  // size. Fail a stalled start rather than silently dropping its first frames.
  final int maxBufferedBytes;
  final completed = Completer<void>();
  final _packets = Queue<List<int>>();
  StreamSubscription? subscription;
  BleAudioCodec? codec;
  void Function(List<int>)? _receiver;
  int _bufferedBytes = 0;
  bool inputStopped = false;
  bool stopping = false;
  bool invalidated = false;
  // Production supplies the same BleDeviceSource parser used after startup.
  void Function(List<int>)? journalPacket;
  StateError? _failure;

  void receive(List<int> packet) {
    if (inputStopped || invalidated || packet.length <= 3) return;
    final snapshot = List<int>.from(packet);
    journalPacket?.call(snapshot);
    final receiver = _receiver;
    if (receiver != null) {
      receiver(snapshot);
    } else if (_bufferedBytes + snapshot.length <= maxBufferedBytes) {
      _packets.add(snapshot);
      _bufferedBytes += snapshot.length;
    } else {
      _failure = StateError('Local audio start buffer is full');
      unawaited(stopInput());
    }
  }

  Future<bool> attach(Future<StreamSubscription?> pending) async {
    subscription = await pending;
    if (inputStopped || invalidated) await subscription?.cancel();
    return subscription != null && !invalidated;
  }

  void journalTo(void Function(List<int>) journal) {
    if (invalidated) return;
    if (_failure != null) throw _failure!;
    for (final packet in _packets) {
      journal(packet);
    }
    journalPacket = journal;
  }

  void forwardTo(void Function(List<int>) receiver) {
    if (invalidated) return;
    if (_failure != null) throw _failure!;
    while (_packets.isNotEmpty) {
      final packet = _packets.removeFirst();
      _bufferedBytes -= packet.length;
      receiver(packet);
    }
    _receiver = receiver;
  }

  Future<void> stopInput() async {
    inputStopped = true;
    await subscription?.cancel();
  }

  void discard() {
    invalidated = true;
    _packets.clear();
    _bufferedBytes = 0;
    _receiver = null;
    unawaited(stopInput());
  }
}
