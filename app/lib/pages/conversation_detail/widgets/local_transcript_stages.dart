import 'package:flutter/material.dart';

import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/utils/l10n_extensions.dart';

/// One line above a local recording's transcript: whether speakers were separated.
class LocalTranscriptStagesLine extends StatelessWidget {
  const LocalTranscriptStagesLine({super.key, required this.stages});

  final LocalTranscriptStages stages;

  String? _diarizationText(BuildContext context) {
    final stage = stages.diarization;
    if (stage == null) return null;
    final outcome = switch (stage.status) {
      'ready' => stage.speakers != null ? context.l10n.speakersCount(stage.speakers!) : context.l10n.modelReady,
      'disabled' || 'no_speech' => context.l10n.diarizationNotPerformed,
      'failed' => context.l10n.diarizationFailed,
      'pending' || 'processing' => context.l10n.processing,
      _ => context.l10n.unknown,
    };
    final provider = stage.provider;
    final detail = provider == null || stage.status == 'disabled' ? outcome : '$provider · $outcome';
    return '${context.l10n.diarization}: $detail';
  }

  @override
  Widget build(BuildContext context) {
    final text = _diarizationText(context);
    if (text == null) return const SizedBox.shrink();
    final failed = stages.diarization?.status == 'failed';
    return Padding(
      padding: const EdgeInsets.fromLTRB(4, 8, 4, 4),
      child: Row(children: [
        Icon(failed ? Icons.error_outline : Icons.people_outline, size: 16, color: Colors.white60),
        const SizedBox(width: 6),
        Expanded(
          child: Text(
            text,
            key: const ValueKey('local-transcript-diarization'),
            maxLines: 2,
            overflow: TextOverflow.ellipsis,
            style: const TextStyle(color: Colors.white60, fontSize: 12),
          ),
        ),
      ]),
    );
  }
}
