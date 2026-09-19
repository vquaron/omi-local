import 'package:flutter/material.dart';

import 'package:omi/services/capture/local_capture_phase.dart';
import 'package:omi/utils/l10n_extensions.dart';

String localCaptureStatusText(BuildContext context, LocalCapturePhase phase) => switch (phase) {
      LocalCapturePhase.idle => context.l10n.localCaptureIdle,
      LocalCapturePhase.starting => context.l10n.preparingAudioCapture,
      LocalCapturePhase.waitingAudio => context.l10n.waitingForData,
      LocalCapturePhase.recording => context.l10n.recording,
      LocalCapturePhase.paused => context.l10n.paused,
      LocalCapturePhase.stopping => context.l10n.saving,
      LocalCapturePhase.failed => context.l10n.somethingWentWrong,
    };

/// Receipt of a BLE gesture and its command result are separate evidence.
class LocalOmiButtonFeedback extends StatelessWidget {
  const LocalOmiButtonFeedback({super.key, required this.action, this.event});

  final LocalOmiButtonAction? action;
  final OmiButtonEvent? event;

  @override
  Widget build(BuildContext context) {
    final action = this.action;
    final eventText = switch (event) {
      OmiButtonEvent.singleTap => context.l10n.omiButtonSingleTap,
      OmiButtonEvent.doubleTap => context.l10n.doubleTap,
      OmiButtonEvent.longPress => context.l10n.omiButtonLongPress,
      OmiButtonEvent.pressed => context.l10n.omiButtonPressed,
      OmiButtonEvent.released => context.l10n.omiButtonReleased,
      null => null,
    };
    if (action == null && eventText == null) return const SizedBox.shrink();
    final text = switch (action) {
      null => null,
      LocalOmiButtonAction.started => context.l10n.recordingStartedSuccessfully,
      LocalOmiButtonAction.stopped => context.l10n.localCaptureIdle,
      LocalOmiButtonAction.paused => context.l10n.recordingPaused,
      LocalOmiButtonAction.resumed => context.l10n.recording,
      LocalOmiButtonAction.starred => context.l10n.starred,
      LocalOmiButtonAction.unstarred => '${context.l10n.starred} · ${context.l10n.off}',
      LocalOmiButtonAction.processing => context.l10n.processing,
      LocalOmiButtonAction.failed => context.l10n.somethingWentWrong,
    };
    return Padding(
      padding: const EdgeInsets.only(bottom: 10),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          if (eventText != null)
            Row(
              key: const Key('local_omi_button_receipt'),
              children: [
                const Icon(Icons.touch_app_outlined, size: 18, color: Colors.white70),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(
                    '${context.l10n.omiAppName} · $eventText',
                    style: const TextStyle(fontSize: 13, color: Colors.white70),
                  ),
                ),
              ],
            ),
          if (text != null)
            Row(
              key: const Key('local_omi_button_feedback'),
              children: [
                Icon(
                  action == LocalOmiButtonAction.failed ? Icons.error_outline : Icons.check_circle_outline,
                  size: 18,
                  color: Colors.white70,
                ),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(
                    '${context.l10n.omiAppName} · $text',
                    style: const TextStyle(fontSize: 13, color: Colors.white70),
                  ),
                ),
              ],
            ),
        ],
      ),
    );
  }
}
