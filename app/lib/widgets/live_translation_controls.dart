import 'package:flutter/material.dart';

import 'package:omi/providers/home_provider.dart';
import 'package:omi/utils/l10n_extensions.dart';

/// Relay-side live translation controls shared by the capture page and Local Mac settings.
///
/// The target is a plain ISO 639 code (`en`, `de`, `yue`); regional variants of the
/// primary-language picker are folded away because the relay translates, not transcribes.
class LiveTranslationSettings {
  const LiveTranslationSettings._();

  static final RegExp _code = RegExp(r'^[a-z]{2,3}$');

  /// name -> code, one entry per base language, in picker order.
  static Map<String, String> languages([Map<String, String>? source]) {
    final result = <String, String>{};
    final seen = <String>{};
    for (final entry in (source ?? HomeProvider.bundledLanguages).entries) {
      final code = entry.value.toLowerCase();
      if (_code.hasMatch(code) && seen.add(code)) result[entry.key] = code;
    }
    return result;
  }

  static String label(String code, [Map<String, String>? source]) {
    for (final entry in languages(source).entries) {
      if (entry.value == code) return entry.key;
    }
    return code.toUpperCase();
  }
}

Future<String?> showLiveTranslationLanguagePicker(BuildContext context, {required String current, String? exclude}) {
  final languages = LiveTranslationSettings.languages()..removeWhere((_, code) => code == exclude);
  return showModalBottomSheet<String>(
    context: context,
    backgroundColor: const Color(0xFF1C1C1E),
    shape: const RoundedRectangleBorder(borderRadius: BorderRadius.vertical(top: Radius.circular(16))),
    isScrollControlled: true,
    builder: (sheetContext) => DraggableScrollableSheet(
      initialChildSize: 0.7,
      minChildSize: 0.5,
      maxChildSize: 0.9,
      expand: false,
      builder: (context, scrollController) => Column(
        children: [
          Container(
            margin: const EdgeInsets.only(top: 12, bottom: 16),
            width: 36,
            height: 4,
            decoration: BoxDecoration(color: const Color(0xFF3C3C43), borderRadius: BorderRadius.circular(2)),
          ),
          Text(
            context.l10n.liveTranslationTargetLanguage,
            style: const TextStyle(color: Colors.white, fontSize: 17, fontWeight: FontWeight.w600),
          ),
          const SizedBox(height: 16),
          Expanded(
            child: ListView.builder(
              controller: scrollController,
              itemCount: languages.length,
              itemBuilder: (context, index) {
                final entry = languages.entries.elementAt(index);
                final selected = entry.value == current;
                return ListTile(
                  key: ValueKey('live-translation-language-${entry.value}'),
                  title: Text(
                    entry.key,
                    style: TextStyle(
                      color: selected ? Colors.white : const Color(0xFF8E8E93),
                      fontWeight: selected ? FontWeight.w500 : FontWeight.w400,
                    ),
                  ),
                  trailing: selected ? const Icon(Icons.check, color: Colors.white, size: 20) : null,
                  onTap: () => Navigator.pop(sheetContext, entry.value),
                );
              },
            ),
          ),
        ],
      ),
    ),
  );
}

typedef LiveTranslationChanged = Future<void> Function({
  bool? enabled,
  String? language,
  bool? bidirectional,
  String? otherLanguage,
});

class LiveTranslationSelection {
  const LiveTranslationSelection(this.language, this.otherLanguage, this.bidirectional);
  final String language;
  final String otherLanguage;
  final bool bidirectional;
}

/// Edit the pair atomically: dismissing this sheet keeps the current direction.
Future<LiveTranslationSelection?> showLiveTranslationSettings(
  BuildContext context, {
  required String language,
  required String otherLanguage,
  required bool bidirectional,
}) {
  return showModalBottomSheet<LiveTranslationSelection>(
    context: context,
    backgroundColor: const Color(0xFF1C1C1E),
    isScrollControlled: true,
    builder: (sheetContext) => StatefulBuilder(builder: (context, refresh) {
      Future<void> pick({required bool other}) async {
        final selected = await showLiveTranslationLanguagePicker(
          context,
          current: other ? otherLanguage : language,
          exclude: bidirectional ? (other ? language : otherLanguage) : null,
        );
        if (selected == null || !sheetContext.mounted) return;
        refresh(() {
          if (other) {
            otherLanguage = selected;
          } else {
            language = selected;
          }
          if (language == otherLanguage) otherLanguage = language == 'en' ? 'es' : 'en';
        });
      }

      return SafeArea(
        child: SingleChildScrollView(
          child: Padding(
            padding: const EdgeInsets.symmetric(vertical: 16),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(context.l10n.liveTranslation,
                    style: const TextStyle(color: Colors.white, fontSize: 18, fontWeight: FontWeight.w600)),
                SwitchListTile(
                  key: const Key('live_translation_bidirectional'),
                  value: bidirectional,
                  activeThumbColor: Colors.white,
                  title: Text(context.l10n.liveTranslationBidirectional),
                  subtitle: Text(context.l10n.liveTranslationPairHelp),
                  onChanged: (value) => refresh(() => bidirectional = value),
                ),
                if (bidirectional)
                  ListTile(
                    key: const Key('live_translation_first_language'),
                    title: Text('${context.l10n.language} 1'),
                    subtitle: Text(LiveTranslationSettings.label(otherLanguage)),
                    trailing: const Icon(Icons.chevron_right),
                    onTap: () => pick(other: true),
                  ),
                ListTile(
                  key: const Key('live_translation_second_language'),
                  title:
                      Text(bidirectional ? '${context.l10n.language} 2' : context.l10n.liveTranslationTargetLanguage),
                  subtitle: Text(LiveTranslationSettings.label(language)),
                  trailing: const Icon(Icons.chevron_right),
                  onTap: () => pick(other: false),
                ),
                FilledButton(
                  key: const Key('live_translation_apply'),
                  style: FilledButton.styleFrom(backgroundColor: Colors.white, foregroundColor: Colors.black),
                  onPressed: () =>
                      Navigator.pop(sheetContext, LiveTranslationSelection(language, otherLanguage, bidirectional)),
                  child: Text(context.l10n.done),
                ),
              ],
            ),
          ),
        ),
      );
    }),
  );
}

/// Compact app-bar control: a translate toggle plus the current target as a tappable chip.
class LiveTranslationToggle extends StatelessWidget {
  const LiveTranslationToggle({
    super.key,
    required this.enabled,
    required this.language,
    required this.otherLanguage,
    required this.bidirectional,
    required this.onChanged,
  });

  final bool enabled;
  final String language;
  final String otherLanguage;
  final bool bidirectional;
  final LiveTranslationChanged onChanged;

  @override
  Widget build(BuildContext context) {
    final color = enabled ? Colors.white : Colors.white38;
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        if (enabled)
          InkWell(
            key: const Key('live_translation_language_chip'),
            borderRadius: BorderRadius.circular(12),
            onTap: () async {
              final picked = await showLiveTranslationSettings(context,
                  language: language, otherLanguage: otherLanguage, bidirectional: bidirectional);
              if (picked != null) {
                await onChanged(
                    language: picked.language,
                    otherLanguage: picked.otherLanguage,
                    bidirectional: picked.bidirectional);
              }
            },
            child: Container(
              padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
              decoration: BoxDecoration(
                border: Border.all(color: Colors.white38),
                borderRadius: BorderRadius.circular(12),
              ),
              child: Text(
                bidirectional
                    ? '${otherLanguage.toUpperCase()} ↔ ${language.toUpperCase()}'
                    : '→ ${language.toUpperCase()}',
                style: const TextStyle(fontSize: 12, fontWeight: FontWeight.w600, color: Colors.white),
              ),
            ),
          ),
        Semantics(
          toggled: enabled,
          child: IconButton(
            key: const Key('live_translation_toggle'),
            tooltip: context.l10n.liveTranslation,
            color: color,
            icon: const Icon(Icons.translate, size: 22),
            onPressed: () => onChanged(enabled: !enabled),
          ),
        ),
      ],
    );
  }
}

/// One-line explanation when the relay cannot translate; nothing while it is fine or off.
class LiveTranslationStatusHint extends StatelessWidget {
  const LiveTranslationStatusHint({super.key, required this.enabled, required this.status});

  final bool enabled;
  final String? status;

  @override
  Widget build(BuildContext context) {
    final text = switch (status) {
      'unconfigured' => context.l10n.liveTranslationUnconfigured,
      'failed' => context.l10n.liveTranslationFailed,
      _ => null,
    };
    if (!enabled || text == null) return const SizedBox.shrink();
    return Padding(
      key: const Key('live_translation_status_hint'),
      padding: const EdgeInsets.fromLTRB(16, 0, 16, 12),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Icon(Icons.translate, size: 16, color: Color(0xFFFFB4AB)),
          const SizedBox(width: 8),
          Expanded(child: Text(text, style: const TextStyle(fontSize: 13, color: Colors.white70, height: 1.4))),
        ],
      ),
    );
  }
}
