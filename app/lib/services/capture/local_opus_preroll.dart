import 'dart:typed_data';

/// Decoder context for an independently decoded Opus part (RFC 7845 section 4.6).
/// This bounded suffix contains previous packets, never new recording samples.
class LocalOpusPreroll {
  static const minimumMicroseconds = 80000;
  final _history = <({Uint8List payload, int microseconds})>[];
  int _microseconds = 0;

  List<Uint8List> get packets => _microseconds < minimumMicroseconds
      ? const []
      : _history.map((packet) => Uint8List.fromList(packet.payload)).toList(growable: false);

  void remember(List<int> payload, int microseconds) {
    if (payload.isEmpty || payload.length > 4096 || microseconds < 2500 || microseconds > 120000) {
      throw StateError('Invalid Opus decoder context packet');
    }
    _history.add((payload: Uint8List.fromList(payload), microseconds: microseconds));
    _microseconds += microseconds;
    while (_microseconds - _history.first.microseconds >= minimumMicroseconds) {
      _microseconds -= _history.removeAt(0).microseconds;
    }
  }
}
