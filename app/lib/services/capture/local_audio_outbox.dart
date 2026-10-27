import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:crypto/crypto.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/widgets.dart';
import 'package:path_provider/path_provider.dart';
import 'package:uuid/uuid.dart';

import 'package:omi/env/env.dart';
import 'package:omi/services/auth/local_mac_session.dart';
import 'package:omi/services/capture/local_opus_preroll.dart';
import 'package:omi/services/connectivity_service.dart';
import 'package:omi/utils/offline_network_policy.dart';

typedef LocalPartUploader = Future<Map<String, dynamic>> Function(Uint8List body, String serverId);

/// Audio accepted from Flutter is flushed locally before it enters live preview.
/// Immutable parts survive network failure and are removed only on a matching
/// durable server receipt. The cloud WAL and its send-time flags are unrelated.
class LocalAudioOutbox extends ChangeNotifier with WidgetsBindingObserver {
  LocalAudioOutbox({
    required this.directory,
    required this.upload,
    this.openJournal,
    DateTime Function()? clock,
    this.partDuration = const Duration(minutes: 5),
    this.maxStorageBytes = 512 * 1024 * 1024,
    this.maxQueuedBytes = 4 * 1024 * 1024,
  }) : clock = clock ?? DateTime.now;

  static LocalAudioOutbox? shared;
  static const magic = [79, 77, 73, 76, 79, 67, 49, 10];
  static const maxPartBytes = 16 * 1024 * 1024;
  static const maxFrameBytes = 4096;
  static const maxPartPackets = 60000;
  final Directory directory;
  final LocalPartUploader upload;
  final Future<RandomAccessFile> Function(File file)? openJournal;
  final DateTime Function() clock;
  final Duration partDuration;
  final int maxStorageBytes;
  final int maxQueuedBytes;
  Future<void> _writes = Future<void>.value();
  Future<void>? _uploading;
  bool _drainRequested = false;
  Timer? _retry;
  StreamSubscription<bool>? _connectionChanges;
  final _recordings = <LocalAudioRecording>{};
  bool _observingLifecycle = false;
  int _queuedBytes = 0;
  int pendingParts = 0;
  int pendingBytes = 0;
  bool deliveryFailed = false;
  bool _recoveryFailed = false;
  String? captureIssue;
  int get activeRecordings => _recordings.length;
  bool get uploading => _uploading != null;
  bool _disposed = false;

  static String serverIdentity(String address, String key) =>
      sha256.convert(utf8.encode(jsonEncode([address, key]))).toString();
  Future<void> flushPendingWrites() => _writes;

  static Future<void> initializeProduction() async {
    if (!Env.isOfflineRuntime || shared != null) return;
    final documents = await getApplicationDocumentsDirectory();
    final outbox = LocalAudioOutbox(
      directory: Directory('${documents.path}/local-audio-outbox'),
      upload: uploadPairedPart,
    );
    await outbox.initialize();
    shared = outbox;
    LocalMacSession.instance.addListener(outbox.retry);
    outbox._connectionChanges = ConnectivityService().onConnectionChange.listen((connected) {
      if (connected) unawaited(outbox.recoverConnection());
    });
    WidgetsBinding.instance.addObserver(outbox);
    outbox._observingLifecycle = true;
    outbox.retry();
  }

  static Future<Map<String, dynamic>> uploadPairedPart(
    Uint8List body,
    String serverId, {
    LocalMacSession? session,
    HttpClient Function()? createClient,
  }) async {
    final paired = session ?? LocalMacSession.instance;
    if (!paired.isSignedIn || serverIdentity(paired.address, paired.accessKey!) != serverId) {
      throw const HttpException('Audio belongs to another paired server');
    }
    if (_header(body).header['version'] == 2) {
      await paired.ensureAudioDelivery();
      if (!paired.isSignedIn || serverIdentity(paired.address, paired.accessKey!) != serverId) {
        throw const HttpException('Audio pairing changed during capability verification');
      }
    }
    final base = Env.parseLocalTunnelUrl(paired.address);
    final uri = base.resolve('v1/local/audio/parts');
    final authorization = paired.authorizationFor(uri);
    return HttpOverrides.runWithHttpOverrides(() async {
      final client = (createClient?.call() ?? HttpClient())..connectionTimeout = const Duration(seconds: 8);
      try {
        return await (() async {
          final request = await client.postUrl(uri);
          request.followRedirects = false;
          request.headers.set(HttpHeaders.authorizationHeader, authorization);
          request.headers.contentType = ContentType.binary;
          request.contentLength = body.length;
          request.add(body);
          final response = await request.close();
          if (response.statusCode != 200) throw const HttpException('Audio delivery was not acknowledged');
          final bytes = <int>[];
          await for (final chunk in response) {
            if (bytes.length + chunk.length > 4096) throw const FormatException('Invalid audio receipt');
            bytes.addAll(chunk);
          }
          return jsonDecode(utf8.decode(bytes)) as Map<String, dynamic>;
        })().timeout(const Duration(seconds: 35));
      } finally {
        client.close(force: true);
      }
    }, OfflineHttpOverrides(OfflineNetworkPolicy.tunnel(base)));
  }

  Future<T> _serial<T>(Future<T> Function() work) {
    final operation = _writes.then((_) => work());
    _writes = operation.then<void>((_) {}, onError: (Object _, StackTrace __) {});
    return operation;
  }

  static Uint8List _length(int value) => (ByteData(4)..setUint32(0, value, Endian.big)).buffer.asUint8List();

  Future<void> initialize() async {
    await directory.create(recursive: true);
    for (final entity in directory.listSync()) {
      if (entity is File && entity.path.endsWith('.part')) {
        try {
          await _recover(entity);
        } catch (_) {
          // Keep malformed originals for recovery; never replace or delete them.
          deliveryFailed = true;
          _recoveryFailed = true;
        }
      }
    }
    await _countPending();
  }

  static ({Map<String, dynamic> header, int offset}) _header(Uint8List bytes) {
    if (bytes.length < 12 || !listEquals(bytes.sublist(0, 8), magic)) {
      throw const FormatException('Invalid local audio journal');
    }
    final size = ByteData.sublistView(bytes, 8, 12).getUint32(0, Endian.big);
    if (size == 0 || size > 4096 || 12 + size > bytes.length) {
      throw const FormatException('Invalid local audio journal header');
    }
    final header = jsonDecode(utf8.decode(bytes.sublist(12, 12 + size))) as Map<String, dynamic>;
    if (!const {1, 2}.contains(header['version']) || header['id'] is! String || header['server_id'] is! String) {
      throw const FormatException('Invalid local audio journal identity');
    }
    final preroll = header['preroll_frames'] ?? 0;
    if (preroll is! int ||
        preroll < 0 ||
        preroll > 32 ||
        (header['version'] == 1 && preroll != 0) ||
        (header['version'] == 2 && !header.containsKey('preroll_frames')) ||
        (header['codec'] == 'pcm16' && preroll != 0)) {
      throw const FormatException('Invalid local audio pre-roll');
    }
    return (header: header, offset: 12 + size);
  }

  Future<void> _recover(File file) async {
    if (await file.length() > maxPartBytes) throw const FormatException('Oversized local audio journal');
    final bytes = await file.readAsBytes();
    final envelope = _header(bytes);
    var position = envelope.offset;
    final start = position;
    var packets = 0;
    while (position + 4 <= bytes.length) {
      final size = ByteData.sublistView(bytes, position, position + 4).getUint32(0, Endian.big);
      if (size == 0 || size > maxFrameBytes) throw const FormatException('Invalid local audio packet');
      if (position + 4 + size > bytes.length) break;
      position += 4 + size;
      packets++;
    }
    if (position == start || packets <= (envelope.header['preroll_frames'] as int? ?? 0)) {
      return; // Decoder context alone is not a new recording to upload.
    }
    final handle = await file.open(mode: FileMode.append);
    try {
      await handle.truncate(position);
      await handle.flush();
    } finally {
      await handle.close();
    }
    await file.rename(file.path.replaceFirst(RegExp(r'\.part$'), '.ready'));
  }

  LocalAudioRecording begin({
    required String source,
    required String codec,
    required String address,
    required String key,
  }) {
    if (!const {'omi:opus', 'omi:opus_fs320', 'phone:pcm16'}.contains('$source:$codec')) {
      throw StateError('Unsupported local recording format');
    }
    captureIssue = null;
    final recording = LocalAudioRecording._(this, source, codec, serverIdentity(address, key));
    _recordings.add(recording);
    return recording;
  }

  void reportCaptureIssue(String? issue) {
    captureIssue = issue;
    if (!_disposed) notifyListeners();
  }

  void _notifyChanged() {
    if (!_disposed) notifyListeners();
  }

  Future<void> _countPending() async {
    var count = 0;
    var bytes = 0;
    for (final entity in directory.listSync()) {
      if (entity is File && (entity.path.endsWith('.ready') || entity.path.endsWith('.part'))) {
        count++;
        bytes += await entity.length();
      }
    }
    pendingParts = count;
    pendingBytes = bytes;
    if (!_disposed) notifyListeners();
  }

  void retry() {
    if (_disposed) return;
    unawaited(drain());
  }

  /// Publish the flushed prefix now, without stopping input or waiting for a
  /// five-minute rotation. New packets retain this recording's next part index.
  Future<void> recoverConnection() async {
    if (_disposed) return;
    for (final recording in _recordings.toList()) {
      try {
        await recording.checkpoint();
      } catch (_) {
        // The original journal remains. Acquisition reports the latched write
        // failure instead of appending behind a damaged or unpublishable tail.
        reportCaptureIssue('storage');
      }
    }
    retry();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) unawaited(recoverConnection());
  }

  Future<void> drain() {
    if (_disposed) return Future<void>.value();
    final active = _uploading;
    if (active != null) {
      _drainRequested = true;
      return active;
    }
    final run =
        (() async {
          do {
            _drainRequested = false;
            await _drain();
          } while (_drainRequested && !_disposed);
        })().whenComplete(() {
          _uploading = null;
          if (!_disposed) notifyListeners();
        });
    _uploading = run;
    return run;
  }

  Future<void> _drain() async {
    _retry?.cancel();
    deliveryFailed = _recoveryFailed;
    try {
      final files = directory.listSync().whereType<File>().where((file) => file.path.endsWith('.ready')).toList()
        ..sort((a, b) => a.path.compareTo(b.path));
      for (final file in files) {
        try {
          final body = await file.readAsBytes();
          if (body.length > maxPartBytes) throw const FormatException('Oversized local audio part');
          final header = _header(body).header;
          final receipt = await upload(body, header['server_id'] as String);
          if (receipt['stored'] != true ||
              receipt['id'] != header['id'] ||
              receipt['sha256'] != sha256.convert(body).toString()) {
            throw const FormatException('Mismatched durable audio receipt');
          }
          await file.delete(); // Only this exact immutable body has been acknowledged.
        } catch (_) {
          // A part for another server must not starve this server's deliveries.
          deliveryFailed = true;
        }
      }
    } catch (_) {
      deliveryFailed = true;
    } finally {
      await _serial(_countPending);
      if (pendingParts > 0 && !_disposed) _retry = Timer(const Duration(seconds: 15), retry);
    }
  }

  @override
  void dispose() {
    _disposed = true;
    _retry?.cancel();
    _connectionChanges?.cancel();
    if (_observingLifecycle) {
      WidgetsBinding.instance.removeObserver(this);
      LocalMacSession.instance.removeListener(retry);
    }
    super.dispose();
  }
}

class LocalAudioRecording {
  LocalAudioRecording._(this.outbox, this.source, this.codec, this.serverId);

  final LocalAudioOutbox outbox;
  final String source;
  final String codec;
  final String serverId;
  final String recordingId = const Uuid().v4();
  int _index = 0;
  File? _file;
  RandomAccessFile? _handle;
  DateTime? _started;
  int _bytes = 0;
  int _audioMicroseconds = 0;
  int _totalAudioMicroseconds = 0;
  double get audioSeconds => _totalAudioMicroseconds / 1000000;
  int _packetCount = 0;
  final _preroll = LocalOpusPreroll();
  bool _closed = false;
  bool _writeFailed = false;
  Future<void>? _stopping;

  Future<void> add(List<int> payload, {void Function()? afterPersisted}) {
    if (_closed) return Future<void>.error(StateError('Local recording is closed'));
    if (payload.isEmpty || payload.length > LocalAudioOutbox.maxFrameBytes) {
      return Future<void>.error(StateError('Invalid local audio packet'));
    }
    if (outbox._queuedBytes + payload.length > outbox.maxQueuedBytes) {
      return Future<void>.error(StateError('Local audio storage cannot keep up'));
    }
    final snapshot = Uint8List.fromList(payload);
    final acceptedAt = outbox.clock().toUtc();
    final int duration;
    try {
      duration = packetMicroseconds(codec, snapshot);
    } catch (error, stack) {
      return Future<void>.error(error, stack);
    }
    outbox._queuedBytes += snapshot.length;
    return outbox._serial(() async {
      try {
        if (_writeFailed) throw StateError('Local journal write failed; original part is retained');
        if (_handle != null &&
            (_audioMicroseconds + duration > outbox.partDuration.inMicroseconds ||
                _packetCount >= LocalAudioOutbox.maxPartPackets ||
                _bytes + snapshot.length + 4 > LocalAudioOutbox.maxPartBytes)) {
          await _seal();
        }
        if (_handle == null) await _open(acceptedAt, snapshot.length + 4);
        if (outbox.pendingBytes + snapshot.length + 4 > outbox.maxStorageBytes) {
          throw StateError('Local audio storage is full; pending audio is retained');
        }
        await _handle!.writeFrom(LocalAudioOutbox._length(snapshot.length));
        await _handle!.writeFrom(snapshot);
        await _handle!.flush();
        _bytes += snapshot.length + 4;
        _audioMicroseconds += duration;
        _totalAudioMicroseconds += duration;
        _packetCount++;
        outbox.pendingBytes += snapshot.length + 4;
        if (codec != 'pcm16') _preroll.remember(snapshot, duration);
        afterPersisted?.call();
      } catch (_) {
        _writeFailed = true;
        outbox.reportCaptureIssue('storage');
        rethrow;
      } finally {
        outbox._queuedBytes -= snapshot.length;
      }
    });
  }

  /// RFC 6716 section 3.1: TOC selects frame duration and packet frame count.
  /// Count audio time, independent of delivery bursts or a changing wall clock.
  static int packetMicroseconds(String codec, List<int> packet) {
    if (codec == 'pcm16') {
      if (packet.length.isOdd) throw StateError('Unaligned PCM16 packet');
      return packet.length * 1000000 ~/ 32000;
    }
    final config = packet.first >> 3;
    final duration = config < 12
        ? const [10000, 20000, 40000, 60000][config % 4]
        : config < 16
        ? const [10000, 20000][config % 2]
        : const [2500, 5000, 10000, 20000][config % 4];
    final code = packet.first & 3;
    if (code == 3 && packet.length < 2) throw StateError('Invalid Opus frame count');
    final frames = code == 0
        ? 1
        : code == 3
        ? packet[1] & 63
        : 2;
    if (frames == 0 || frames * duration > 120000) throw StateError('Invalid Opus duration');
    return duration * frames;
  }

  Future<void> _open(DateTime acceptedAt, int freshPacketBytes) async {
    final id = const Uuid().v4();
    _started = acceptedAt;
    _audioMicroseconds = 0;
    final preroll = _preroll.packets;
    final header = utf8.encode(
      jsonEncode({
        'version': 2,
        'id': id,
        'recording_id': recordingId,
        'index': _index++,
        'source': source,
        'codec': codec,
        'started_at': _started!.toIso8601String(),
        'server_id': serverId,
        'preroll_frames': preroll.length,
      }),
    );
    final openingBytes = 12 + header.length + preroll.fold<int>(0, (total, packet) => total + 4 + packet.length);
    if (outbox.pendingBytes + openingBytes + freshPacketBytes > outbox.maxStorageBytes) {
      throw StateError('Local audio storage is full; pending audio is retained');
    }
    _file = File('${outbox.directory.path}/$id.part');
    await _file!.create(exclusive: true);
    _handle = await (outbox.openJournal?.call(_file!) ?? _file!.open(mode: FileMode.writeOnly));
    await _handle!.writeFrom(LocalAudioOutbox.magic);
    await _handle!.writeFrom(LocalAudioOutbox._length(header.length));
    await _handle!.writeFrom(header);
    for (final packet in preroll) {
      await _handle!.writeFrom(LocalAudioOutbox._length(packet.length));
      await _handle!.writeFrom(packet);
    }
    await _handle!.flush();
    _bytes = openingBytes;
    _packetCount = preroll.length;
    outbox.pendingBytes += _bytes;
    outbox.pendingParts++;
    outbox._notifyChanged();
  }

  Future<void> _seal() async {
    if (_handle == null) return;
    if (_writeFailed) {
      // Never append after a torn packet or publish its journal as upload-ready.
      // Recovery on restart keeps the complete prefix and trims only the tail.
      await _handle!.close();
      _handle = null;
      outbox.retry();
      return;
    }
    await _handle!.flush();
    await _handle!.close();
    _handle = null;
    await _file!.rename(_file!.path.replaceFirst(RegExp(r'\.part$'), '.ready'));
    _file = null;
    outbox.retry();
  }

  Future<void> stop() {
    if (_stopping != null) return _stopping!;
    _closed = true; // Fence future hardware callbacks before draining accepted ones.
    outbox._recordings.remove(this);
    return _stopping = outbox._serial(_seal);
  }

  Future<void> checkpoint() => outbox._serial(() async {
    if (_closed || _writeFailed) return;
    try {
      await _seal();
    } catch (_) {
      _writeFailed = true;
      rethrow;
    }
  });
}
