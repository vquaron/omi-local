// LIFECYCLE: one-time
// DELETE-AFTER: G12-live-capture-controls
import 'package:flutter/material.dart';
import 'package:omi/providers/capture_provider.dart';
import 'package:omi/utils/enums.dart';
import 'package:omi/utils/l10n_extensions.dart';
import 'package:omi/widgets/local_recording_timer.dart';

/// Local WAV controls until the live transcript screen provides a Stop action.
class TemporaryRecordingControls extends StatefulWidget {
  final CaptureProvider provider;
  const TemporaryRecordingControls({super.key, required this.provider});

  @override
  State<TemporaryRecordingControls> createState() => _TemporaryRecordingControlsState();
}

class _TemporaryRecordingControlsState extends State<TemporaryRecordingControls> {
  bool _busy = false;

  Future<void> _run(Future<void> Function() action) async {
    if (_busy) return;
    setState(() => _busy = true);
    try {
      await action();
    } catch (_) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(context.l10n.somethingWentWrong)));
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final provider = widget.provider;
    final deviceActive =
        provider.recordingState == RecordingState.deviceRecord || provider.recordingState == RecordingState.pause;
    final phoneActive =
        provider.recordingState == RecordingState.record || provider.recordingState == RecordingState.interrupted;
    final active = deviceActive || phoneActive || provider.localRecordingDuration != null;
    final paused = deviceActive && provider.isPaused;
    final available = !_busy && provider.recordingState != RecordingState.initialising;
    return Row(
      children: [
        const SizedBox(width: 8),
        Expanded(
          child: Text(
            active ? (paused ? context.l10n.muted : context.l10n.recording) : context.l10n.startRecording,
            style: const TextStyle(color: Color(0xFFC9CBCF), fontSize: 14),
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
          ),
        ),
        LocalRecordingTimer(provider: provider),
        IconButton(
          key: const Key('temporary_start_recording'),
          tooltip: context.l10n.startRecording,
          color: Colors.white,
          disabledColor: Colors.grey.shade700,
          icon: const Icon(Icons.play_arrow_rounded),
          onPressed: available && (provider.havingRecordingDevice || provider.isPhoneMicSelected) && !active
              ? () => _run(() async {
                  if (provider.isPhoneMicSelected) {
                    await provider.streamRecording();
                    return;
                  }
                  try {
                    await provider.streamDeviceRecording(userInitiated: true);
                    if (provider.recordingState != RecordingState.deviceRecord) throw StateError('Capture unavailable');
                  } catch (_) {
                    await provider.stopStreamDeviceRecording();
                    rethrow;
                  }
                })
              : null,
        ),
        IconButton(
          key: const Key('temporary_stop_recording'),
          tooltip: context.l10n.stopRecording,
          color: const Color(0xFFFE5D50),
          disabledColor: Colors.grey.shade700,
          icon: const Icon(Icons.stop_rounded),
          onPressed: !_busy && active ? () => _run(provider.stopLocalCapture) : null,
        ),
        IconButton(
          key: const Key('temporary_recording_mute'),
          tooltip: paused ? context.l10n.unmute : context.l10n.mute,
          color: paused ? const Color(0xFFFE5D50) : Colors.white,
          disabledColor: Colors.grey.shade700,
          icon: Icon(paused ? Icons.mic_off : Icons.mic, size: 20),
          onPressed: available && provider.havingRecordingDevice && deviceActive
              ? () => _run(() => paused ? provider.resumeDeviceRecording() : provider.pauseDeviceRecording())
              : null,
        ),
      ],
    );
  }
}
