import 'dart:async';

import 'package:flutter/material.dart';
import 'package:omi/providers/capture_provider.dart';

/// Shows elapsed session time, including gaps without audio. The capture status
/// remains separate; this ticker refreshes only the visible label.
class LocalRecordingTimer extends StatefulWidget {
  const LocalRecordingTimer({super.key, required this.provider});
  final CaptureProvider provider;

  @override
  State<LocalRecordingTimer> createState() => _LocalRecordingTimerState();
}

class _LocalRecordingTimerState extends State<LocalRecordingTimer> {
  Timer? _ticker;

  @override
  void initState() {
    super.initState();
    _ticker = Timer.periodic(const Duration(seconds: 1), (_) {
      if (mounted && widget.provider.localSessionElapsed != null) setState(() {});
    });
  }

  @override
  void dispose() {
    _ticker?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final duration = widget.provider.localSessionElapsed;
    if (duration == null) return const SizedBox.shrink();
    final seconds = duration.inSeconds;
    String pad(int value) => value.toString().padLeft(2, '0');
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 6),
      child: Text(
        '${pad(seconds ~/ 3600)}:${pad(seconds ~/ 60 % 60)}:${pad(seconds % 60)}',
        key: const Key('local_recording_duration'),
        style: const TextStyle(color: Colors.white70, fontFamily: 'monospace', fontSize: 12),
      ),
    );
  }
}
