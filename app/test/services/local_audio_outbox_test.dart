import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:crypto/crypto.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:flutter/widgets.dart';
import 'package:omi/services/capture/local_audio_outbox.dart';
import 'package:omi/services/auth/local_mac_session.dart';
import 'package:omi/services/capture/local_opus_preroll.dart';

class _PairedSession extends Fake implements LocalMacSession {
  @override
  bool get isSignedIn => true;
  @override
  String get address => 'https://synthetic.example/';
  @override
  String? get accessKey => 'new-key';
}

class _VersionOneSession extends Fake implements LocalMacSession {
  int capabilityChecks = 0;
  @override
  bool get isSignedIn => true;
  @override
  String get address => 'https://synthetic.example/';
  @override
  String? get accessKey => 'same-key';
  @override
  Future<void> ensureAudioDelivery() async {
    capabilityChecks++;
    throw LocalAudioDeliveryUnsupported();
  }
}

class _FaultingJournal extends Fake implements RandomAccessFile {
  _FaultingJournal(this.delegate);
  final RandomAccessFile delegate;
  int writes = 0;
  @override
  Future<RandomAccessFile> writeFrom(List<int> buffer, [int start = 0, int? end]) async {
    writes++;
    if (writes == 7) throw const FileSystemException('synthetic failure after second packet length');
    await delegate.writeFrom(buffer, start, end);
    return this;
  }

  @override
  Future<RandomAccessFile> flush() async {
    await delegate.flush();
    return this;
  }

  @override
  Future<void> close() => delegate.close();
}

void main() {
  late Directory directory;
  final received = <Uint8List>[];
  late LocalAudioOutbox outbox;

  Map<String, dynamic> receipt(Uint8List bytes) {
    final size = ByteData.sublistView(bytes, 8, 12).getUint32(0, Endian.big);
    final header = jsonDecode(utf8.decode(bytes.sublist(12, 12 + size))) as Map<String, dynamic>;
    return {'id': header['id'], 'sha256': sha256.convert(bytes).toString(), 'stored': true};
  }

  ({Map<String, dynamic> header, List<Uint8List> packets, int payloadOffset}) envelope(Uint8List bytes) {
    final size = ByteData.sublistView(bytes, 8, 12).getUint32(0, Endian.big);
    final header = jsonDecode(utf8.decode(bytes.sublist(12, 12 + size))) as Map<String, dynamic>;
    var position = 12 + size;
    final packets = <Uint8List>[];
    while (position < bytes.length) {
      final length = ByteData.sublistView(bytes, position, position + 4).getUint32(0, Endian.big);
      position += 4;
      packets.add(Uint8List.fromList(bytes.sublist(position, position + length)));
      position += length;
    }
    return (header: header, packets: packets, payloadOffset: 12 + size);
  }

  setUp(() async {
    directory = await Directory.systemTemp.createTemp('local-audio-outbox-');
    received.clear();
    outbox = LocalAudioOutbox(
      directory: directory,
      upload: (body, serverId) async {
        received.add(body);
        return receipt(body);
      },
    );
    await outbox.initialize();
  });
  tearDown(() async {
    outbox.dispose();
    await directory.delete(recursive: true);
  });

  test('pre-roll ring helper retains the minimal ordered 80 ms suffix and owns its snapshots', () {
    final ring = LocalOpusPreroll();
    final first = [0x18, 1];
    ring.remember(first, 60000);
    expect(ring.packets, isEmpty);
    first[1] = 99;
    ring.remember([0x18, 2], 60000);
    expect(ring.packets, [
      [0x18, 1],
      [0x18, 2],
    ]);
    ring.remember([0x78, 3], 20000);
    expect(ring.packets, [
      [0x18, 2],
      [0x78, 3],
    ]);
    final exposed = ring.packets;
    exposed.first[1] = 88;
    expect(ring.packets.first, [0x18, 2]);
  });

  test('pre-roll ring helper stays at 32 packets across long minimum-duration streams', () {
    final ring = LocalOpusPreroll();
    for (var value = 0; value < 1000; value++) {
      ring.remember([0x80, value & 255], 2500);
    }
    expect(ring.packets, hasLength(32));
    expect(ring.packets.map((packet) => packet[1]), List.generate(32, (index) => (968 + index) & 255));
    expect(() => ring.remember([0x80], 0), throwsStateError);
    expect(ring.packets, hasLength(32));
  });

  test('legacy version-one journal recovery preserves original UUID and exact receipt checksum', () async {
    final header = utf8.encode(
      jsonEncode({
        'version': 1,
        'id': '00000000-0000-0000-0000-000000000001',
        'recording_id': '00000000-0000-0000-0000-000000000002',
        'index': 0,
        'source': 'phone',
        'codec': 'pcm16',
        'server_id': LocalAudioOutbox.serverIdentity('https://synthetic.example/', 'same-key'),
        'started_at': '2026-10-27T08:00:00Z',
      }),
    );
    final length = (ByteData(4)..setUint32(0, header.length, Endian.big)).buffer.asUint8List();
    final body = Uint8List.fromList([...LocalAudioOutbox.magic, ...length, ...header, 0, 0, 0, 2, 1, 0]);
    await File('${directory.path}/legacy.part').writeAsBytes(body, flush: true);
    await outbox.initialize();
    await outbox.drain();
    expect(received.single, body);
    expect(receipt(received.single)['id'], '00000000-0000-0000-0000-000000000001');
    expect(directory.listSync(), isEmpty);
  });

  test('version-two parts probe capability before transmitting audio to an old version-one server', () async {
    var requests = 0;
    final session = _VersionOneSession();
    final guarded = LocalAudioOutbox(
      directory: directory,
      upload: (body, id) => LocalAudioOutbox.uploadPairedPart(
        body,
        id,
        session: session,
        createClient: () {
          requests++;
          throw StateError('Old server must receive no version-two audio');
        },
      ),
    );
    await guarded.initialize();
    final recording = guarded.begin(source: 'phone', codec: 'pcm16', address: session.address, key: session.accessKey!);
    await recording.add([1, 0]);
    await recording.stop();
    await guarded.drain();
    expect(session.capabilityChecks, greaterThan(0));
    expect(requests, 0);
    expect(directory.listSync().whereType<File>().single.path, endsWith('.ready'));
    guarded.dispose();
  });

  test('audio is readable from disk before the live-send callback; Stop seals and uploads it', () async {
    final recording = outbox.begin(
      source: 'phone',
      codec: 'pcm16',
      address: 'https://synthetic.example/',
      key: 'synthetic-key',
    );
    var persistedBeforeSend = false;
    await recording.add(
      [1, 0, 2, 0],
      afterPersisted: () {
        final file = directory.listSync().whereType<File>().single;
        persistedBeforeSend = file.readAsBytesSync().sublist(file.lengthSync() - 4).join(',') == '1,0,2,0';
      },
    );
    expect(persistedBeforeSend, isTrue);
    await recording.stop();
    await outbox.drain();
    expect(received, hasLength(1));
    expect(directory.listSync(), isEmpty);
    await expectLater(recording.add([3, 0]), throwsStateError);
  });

  test('lost ACK retains exact bytes for retry after a new outbox instance', () async {
    final failed = LocalAudioOutbox(
      directory: directory,
      upload: (_, __) async => throw const SocketException('offline'),
    );
    await failed.initialize();
    final recording = failed.begin(
      source: 'omi',
      codec: 'opus',
      address: 'https://synthetic.example/',
      key: 'synthetic-key',
    );
    await recording.add([1, 2, 3]);
    await recording.stop();
    await failed.drain();
    final before = directory.listSync().whereType<File>().single.readAsBytesSync();
    expect(failed.deliveryFailed, isTrue);
    failed.dispose();
    await outbox.initialize();
    await outbox.drain();
    expect(received.single, before);
    expect(directory.listSync(), isEmpty);
  });

  test('reconnection delivers the current flushed part before rotation and keeps recording without gaps', () async {
    var online = false;
    final delivered = <Uint8List>[];
    final recovering = LocalAudioOutbox(
      directory: directory,
      upload: (body, _) async {
        if (!online) throw const SocketException('synthetic elevator outage');
        delivered.add(body);
        return receipt(body);
      },
    );
    await recovering.initialize();
    final recording = recovering.begin(
      source: 'phone',
      codec: 'pcm16',
      address: 'https://synthetic.example/',
      key: 'same-key',
    );
    final expected = List.generate(120, (i) => List<int>.filled(320, i));
    for (final packet in expected.take(80)) {
      await recording.add(packet);
    }
    expect(delivered, isEmpty);
    expect(directory.listSync().whereType<File>().single.path, endsWith('.part'));

    // A foreground return without connectivity must still leave the prefix
    // intact. Only its exact receipt can remove it on the next available route.
    await recovering.recoverConnection();
    await recovering.drain();
    final retained = directory.listSync().whereType<File>().single.readAsBytesSync();
    expect(recovering.deliveryFailed, isTrue);
    online = true;
    for (final packet in expected.skip(80).take(20)) {
      await recording.add(packet);
    }
    await recovering.recoverConnection();
    await recovering.drain();
    expect(delivered, contains(equals(retained)));
    expect(recovering.activeRecordings, 1, reason: 'Delivery cannot stop acquisition');
    for (final packet in expected.skip(100)) {
      await recording.add(packet);
    }
    await recording.stop();
    await recovering.drain();
    final parts = delivered.map(envelope).toList()
      ..sort((a, b) => (a.header['index'] as int).compareTo(b.header['index'] as int));
    expect(parts.map((part) => part.header['index']), [0, 1, 2]);
    expect(parts.map((part) => part.header['recording_id']).toSet(), hasLength(1));
    expect(parts.expand((part) => part.packets).toList(), expected);
    expect(directory.listSync(), isEmpty);
    expect(recovering.deliveryFailed, isFalse);
    recovering.dispose();
  });

  test('a checkpoint made during an in-flight upload drains without waiting for another retry tick', () async {
    final firstReceipt = Completer<Map<String, dynamic>>();
    final started = Completer<void>();
    final delivered = <Uint8List>[];
    final recovering = LocalAudioOutbox(
      directory: directory,
      upload: (body, _) async {
        delivered.add(body);
        if (delivered.length == 1) {
          started.complete();
          return firstReceipt.future;
        }
        return receipt(body);
      },
    );
    await recovering.initialize();
    final recording = recovering.begin(
      source: 'phone',
      codec: 'pcm16',
      address: 'https://synthetic.example/',
      key: 'same-key',
    );
    await recording.add([1, 0]);
    await recording.checkpoint();
    await started.future;
    final inFlight = recovering.drain();
    await recording.add([2, 0]);
    await recovering.recoverConnection();
    firstReceipt.complete(receipt(delivered.first));
    await inFlight;
    expect(delivered, hasLength(2), reason: 'No second manual drain or 15-second timer is needed');
    expect(directory.listSync(), isEmpty);
    expect(recovering.activeRecordings, 1);
    await recording.stop();
    await recovering.drain();
    recovering.dispose();
  });

  test('foreground return publishes pending audio without stopping or losing Opus context', () async {
    final recording = outbox.begin(
      source: 'omi',
      codec: 'opus_fs320',
      address: 'https://synthetic.example/',
      key: 'same-key',
    );
    for (var i = 1; i <= 5; i++) {
      await recording.add([0x78, i]);
    }
    outbox.didChangeAppLifecycleState(AppLifecycleState.resumed);
    await outbox.flushPendingWrites();
    await pumpEventQueue();
    expect(received, hasLength(1));
    expect(outbox.activeRecordings, 1);
    await recording.add([0x78, 6]);
    await recording.stop();
    await outbox.drain();
    final parts = received.map(envelope).toList();
    expect(parts.last.header['preroll_frames'], 4);
    expect(parts.last.packets.take(4).toList(), [
      [0x78, 2],
      [0x78, 3],
      [0x78, 4],
      [0x78, 5],
    ]);
    final fresh = parts.expand((part) => part.packets.skip(part.header['preroll_frames'] as int)).toList();
    expect(fresh, List.generate(6, (i) => [0x78, i + 1]));
  });

  test('a mismatched receipt cannot delete unacknowledged audio', () async {
    final wrong = LocalAudioOutbox(directory: directory, upload: (_, __) async => {'stored': true});
    await wrong.initialize();
    final recording = wrong.begin(
      source: 'phone',
      codec: 'pcm16',
      address: 'https://synthetic.example/',
      key: 'synthetic-key',
    );
    await recording.add([1, 0]);
    await recording.stop();
    await wrong.drain();
    expect(directory.listSync().whereType<File>(), hasLength(1));
    wrong.dispose();
  });

  test('restart trims only an incomplete final packet and preserves every complete packet', () async {
    final recording = outbox.begin(
      source: 'omi',
      codec: 'opus',
      address: 'https://synthetic.example/',
      key: 'synthetic-key',
    );
    await recording.add([1, 2, 3]);
    final file = directory.listSync().whereType<File>().single;
    final complete = file.readAsBytesSync();
    // A copied .part models process death without a clean Stop.
    final crashedDirectory = await Directory.systemTemp.createTemp('local-audio-crash-');
    try {
      await File('${crashedDirectory.path}/recovered.part').writeAsBytes([...complete, 0, 0, 0, 5, 4, 5], flush: true);
      final recovered = LocalAudioOutbox(
        directory: crashedDirectory,
        upload: (body, _) async {
          expect(body, complete);
          return receipt(body);
        },
      );
      await recovered.initialize();
      await recovered.drain();
      expect(crashedDirectory.listSync(), isEmpty);
      recovered.dispose();
    } finally {
      await recording.stop();
      await outbox.drain();
      await crashedDirectory.delete(recursive: true);
    }
  });

  test('bounded queue rejects before send and retains earlier audio', () async {
    final bounded = LocalAudioOutbox(directory: directory, maxQueuedBytes: 2, upload: (_, __) async => {});
    await bounded.initialize();
    final recording = bounded.begin(
      source: 'phone',
      codec: 'pcm16',
      address: 'https://synthetic.example/',
      key: 'synthetic-key',
    );
    var sent = false;
    await expectLater(recording.add([1, 0, 2, 0], afterPersisted: () => sent = true), throwsStateError);
    expect(sent, isFalse);
    await recording.stop();
    bounded.dispose();
  });

  test('audio time rotates burst-delivered PCM even when the wall clock moves backwards', () async {
    var now = DateTime.utc(2026, 10, 27);
    final rotating = LocalAudioOutbox(
      directory: directory,
      clock: () => now,
      partDuration: const Duration(milliseconds: 20),
      upload: (body, _) async {
        received.add(body);
        return receipt(body);
      },
    );
    await rotating.initialize();
    final recording = rotating.begin(
      source: 'phone',
      codec: 'pcm16',
      address: 'https://synthetic.example/',
      key: 'synthetic-key',
    );
    for (var i = 0; i < 3; i++) {
      await recording.add(List.filled(320, i));
      now = now.subtract(const Duration(hours: 1));
    }
    await recording.stop();
    await rotating.drain();
    // A seal racing the current drain is picked up by a subsequent pass.
    await rotating.drain();
    expect(received, hasLength(2));
    expect(received.map((body) => envelope(body).header['preroll_frames']), everyElement(0));
    rotating.dispose();
  });

  test('rotated Opus parts contain preceding 80 ms but count only fresh audio toward their duration', () async {
    final rotating = LocalAudioOutbox(
      directory: directory,
      partDuration: const Duration(milliseconds: 100),
      upload: (body, _) async {
        received.add(body);
        return receipt(body);
      },
    );
    await rotating.initialize();
    final recording = rotating.begin(
      source: 'omi',
      codec: 'opus_fs320',
      address: 'https://synthetic.example/',
      key: 'synthetic-key',
    );
    for (var value = 1; value <= 11; value++) {
      await recording.add([0x78, value]);
    }
    await recording.stop();
    await rotating.drain();
    await rotating.drain();
    final parts = received.map(envelope).toList()
      ..sort((a, b) => (a.header['index'] as int).compareTo(b.header['index'] as int));
    expect(parts, hasLength(3));
    expect(parts.map((part) => part.header['preroll_frames']), [0, 4, 4]);
    expect(parts[1].packets.take(4).map((packet) => packet[1]), [2, 3, 4, 5]);
    expect(parts[2].packets.take(4).map((packet) => packet[1]), [7, 8, 9, 10]);
    expect(
      parts.expand((part) => part.packets.skip(part.header['preroll_frames'] as int)).map((packet) => packet[1]),
      List.generate(11, (index) => index + 1),
    );
    expect(parts.map((part) => part.header['recording_id']).toSet(), hasLength(1));
    rotating.dispose();
  });

  test('minimum-duration Opus packets keep the pre-roll ring bounded to 32 packets', () async {
    final rotating = LocalAudioOutbox(
      directory: directory,
      partDuration: const Duration(milliseconds: 100),
      upload: (body, _) async {
        received.add(body);
        return receipt(body);
      },
    );
    await rotating.initialize();
    final recording = rotating.begin(
      source: 'omi',
      codec: 'opus',
      address: 'https://synthetic.example/',
      key: 'synthetic-key',
    );
    for (var value = 0; value < 41; value++) {
      await recording.add([0x80, value]);
    }
    await recording.stop();
    await rotating.drain();
    await rotating.drain();
    final second = received.map(envelope).singleWhere((part) => part.header['index'] == 1);
    expect(second.header['preroll_frames'], 32);
    expect(second.packets.take(32).map((packet) => packet[1]), List.generate(32, (index) => index + 8));
    expect(second.packets.last, [0x80, 40]);
    rotating.dispose();
  });

  test('pre-roll bytes count toward quota before a rotated journal is created', () async {
    final fixedTime = DateTime.utc(2026, 10, 27);
    final probe = LocalAudioOutbox(
      directory: directory,
      clock: () => fixedTime,
      partDuration: const Duration(milliseconds: 100),
      upload: (body, _) async {
        received.add(body);
        return receipt(body);
      },
    );
    await probe.initialize();
    final first = probe.begin(
      source: 'omi',
      codec: 'opus_fs320',
      address: 'https://synthetic.example/',
      key: 'synthetic-key',
    );
    for (var value = 1; value <= 5; value++) {
      await first.add([0x78, value]);
    }
    await first.stop();
    await probe.drain();
    final partSize = received.single.length;
    probe.dispose();
    final bounded = LocalAudioOutbox(
      directory: directory,
      clock: () => fixedTime,
      partDuration: const Duration(milliseconds: 100),
      maxStorageBytes: 2 * partSize - 1,
      upload: (_, __) async => throw const SocketException('offline'),
    );
    await bounded.initialize();
    final recording = bounded.begin(
      source: 'omi',
      codec: 'opus_fs320',
      address: 'https://synthetic.example/',
      key: 'synthetic-key',
    );
    for (var value = 1; value <= 5; value++) {
      await recording.add([0x78, value]);
    }
    await expectLater(recording.add([0x78, 6]), throwsStateError);
    await recording.stop();
    await bounded.drain();
    final files = directory.listSync().whereType<File>().toList();
    expect(files, hasLength(1));
    expect(files.single.path, endsWith('.ready'));
    expect(files.single.lengthSync(), lessThanOrEqualTo(bounded.maxStorageBytes));
    bounded.dispose();
  });

  test('restart preserves Opus prefix and fresh packets but never uploads a prefix-only journal', () async {
    final pending = LocalAudioOutbox(
      directory: directory,
      partDuration: const Duration(milliseconds: 100),
      upload: (_, __) async => throw const SocketException('offline'),
    );
    await pending.initialize();
    final recording = pending.begin(
      source: 'omi',
      codec: 'opus_fs320',
      address: 'https://synthetic.example/',
      key: 'synthetic-key',
    );
    for (var value = 1; value <= 6; value++) {
      await recording.add([0x78, value]);
    }
    final current = directory.listSync().whereType<File>().singleWhere((file) => file.path.endsWith('.part'));
    final complete = current.readAsBytesSync();
    final parsed = envelope(complete);
    expect(parsed.header['preroll_frames'], 4);
    final prefixEnd = parsed.payloadOffset + parsed.packets.take(4).fold<int>(0, (n, packet) => n + 4 + packet.length);
    final crashed = await Directory.systemTemp.createTemp('opus-preroll-crash-');
    try {
      final prefixOnly = File('${crashed.path}/prefix.part');
      await prefixOnly.writeAsBytes(complete.sublist(0, prefixEnd), flush: true);
      await File('${crashed.path}/complete.part').writeAsBytes([...complete, 0, 0, 0, 5, 1], flush: true);
      final recovered = LocalAudioOutbox(
        directory: crashed,
        upload: (body, _) async {
          received.add(body);
          return receipt(body);
        },
      );
      await recovered.initialize();
      await recovered.drain();
      expect(received.single, complete);
      expect(crashed.listSync().whereType<File>().single.path, prefixOnly.path);
      expect(prefixOnly.readAsBytesSync(), complete.sublist(0, prefixEnd));
      recovered.dispose();
    } finally {
      await recording.stop();
      await pending.drain();
      pending.dispose();
      await crashed.delete(recursive: true);
    }
  });

  test('a queued part bound to another server cannot starve this server', () async {
    final pending = LocalAudioOutbox(
      directory: directory,
      upload: (_, __) async => throw const SocketException('offline'),
    );
    await pending.initialize();
    for (final address in ['https://old.example/', 'https://current.example/']) {
      final recording = pending.begin(source: 'phone', codec: 'pcm16', address: address, key: 'synthetic-key');
      await recording.add([1, 0]);
      await recording.stop();
    }
    await pending.drain();
    pending.dispose();
    final current = LocalAudioOutbox(
      directory: directory,
      upload: (body, serverId) async {
        if (serverId != LocalAudioOutbox.serverIdentity('https://current.example/', 'synthetic-key')) {
          throw const SocketException('wrong server');
        }
        received.add(body);
        return receipt(body);
      },
    );
    await current.initialize();
    await current.drain();
    expect(received, hasLength(1));
    expect(directory.listSync().whereType<File>(), hasLength(1));
    current.dispose();
  });

  test('Opus packet duration uses TOC and frame count from RFC 6716', () {
    expect(LocalAudioRecording.packetMicroseconds('opus', [0x00]), 10000);
    expect(LocalAudioRecording.packetMicroseconds('opus_fs320', [0x78]), 20000);
    expect(LocalAudioRecording.packetMicroseconds('opus', [0x81]), 5000);
    expect(LocalAudioRecording.packetMicroseconds('opus', [0x03, 3]), 30000);
    expect(() => LocalAudioRecording.packetMicroseconds('opus', [0x03, 63]), throwsStateError);
  });

  test('corrupt journal is retained and surfaced instead of being silently reset', () async {
    final corrupt = File('${directory.path}/corrupt.part');
    await corrupt.writeAsBytes([1, 2, 3], flush: true);
    await outbox.initialize();
    expect(outbox.deliveryFailed, isTrue);
    expect(corrupt.readAsBytesSync(), [1, 2, 3]);
  });

  test('a torn packet latches failure before queued writes and Stop preserves recoverable prefix', () async {
    late _FaultingJournal writer;
    final failing = LocalAudioOutbox(
      directory: directory,
      upload: (_, __) async => {},
      openJournal: (file) async {
        return writer = _FaultingJournal(await file.open(mode: FileMode.writeOnly));
      },
    );
    await failing.initialize();
    final recording = failing.begin(
      source: 'phone',
      codec: 'pcm16',
      address: 'https://synthetic.example/',
      key: 'synthetic-key',
    );
    await recording.add([1, 0]);
    final completePrefix = directory.listSync().whereType<File>().single.readAsBytesSync();
    final second = expectLater(recording.add([2, 0]), throwsA(isA<FileSystemException>()));
    final third = expectLater(recording.add([3, 0]), throwsStateError);
    await second;
    await third;
    await recording.stop();
    await failing.drain();
    expect(writer.writes, 7, reason: 'No queued packet may append after a failed write');
    expect(directory.listSync().whereType<File>().single.path, endsWith('.part'));
    failing.dispose();
    await outbox.initialize();
    await outbox.drain();
    expect(received.single, completePrefix);
    expect(directory.listSync(), isEmpty);
  });

  test('production uploader rejects a replacement key before creating a request and retains old audio', () async {
    var requests = 0;
    final guarded = LocalAudioOutbox(
      directory: directory,
      upload: (body, serverId) => LocalAudioOutbox.uploadPairedPart(
        body,
        serverId,
        session: _PairedSession(),
        createClient: () {
          requests++;
          throw StateError('Replacement server must receive no connection or body');
        },
      ),
    );
    await guarded.initialize();
    final recording = guarded.begin(
      source: 'phone',
      codec: 'pcm16',
      address: 'https://synthetic.example/',
      key: 'old-key',
    );
    await recording.add([1, 0]);
    await recording.stop();
    await guarded.drain();
    expect(requests, 0);
    expect(guarded.deliveryFailed, isTrue);
    expect(directory.listSync().whereType<File>().single.path, endsWith('.ready'));
    guarded.dispose();
  });
}
