import 'dart:async';

import 'package:flutter/material.dart';

import 'package:omi/env/env.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/widgets/recording_source_label.dart';
import 'package:omi/services/auth/local_mac_session.dart';
import 'package:omi/services/local_runtime_status.dart';
import 'package:omi/utils/l10n_extensions.dart';

/// Polls only while this route and its containing tab are visible and foreground.
class LocalRuntimeStatusCard extends StatefulWidget {
  const LocalRuntimeStatusCard({
    super.key,
    this.session,
    this.source,
    this.fetcher,
    this.active = true,
    this.refreshRevision = 0,
    this.refreshInterval = const Duration(seconds: 5),
  });

  final ConversationSource? source;
  final LocalMacSession? session;
  final Future<LocalRuntimeStatus> Function()? fetcher;
  final bool active;
  final int refreshRevision;
  final Duration refreshInterval;

  @override
  State<LocalRuntimeStatusCard> createState() => _LocalRuntimeStatusCardState();
}

enum _ConnectionState { unknown, loading, connected, unavailable, rejected }

class _LocalRuntimeStatusCardState extends State<LocalRuntimeStatusCard> with WidgetsBindingObserver {
  LocalMacSession get _session => widget.session ?? LocalMacSession.instance;
  Timer? _timer;
  int _generation = 0;
  bool _visible = false;
  bool _foreground = true;
  bool _loading = false;
  bool _expanded = false;
  _ConnectionState _connection = _ConnectionState.unknown;
  LocalRuntimeStatus? _status;
  late ({String address, String? key, bool signedIn}) _pairing;

  ({String address, String? key, bool signedIn}) get _currentPairing =>
      (address: _session.address, key: _session.accessKey, signedIn: _session.isSignedIn);

  bool get _canPoll =>
      mounted && Env.isOfflineRuntime && widget.active && _visible && _foreground && _session.isSignedIn;

  @override
  void initState() {
    super.initState();
    _foreground = WidgetsBinding.instance.lifecycleState == null ||
        WidgetsBinding.instance.lifecycleState == AppLifecycleState.resumed;
    WidgetsBinding.instance.addObserver(this);
    _pairing = _currentPairing;
    _session.addListener(_sessionChanged);
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    final visible = (ModalRoute.isCurrentOf(context) ?? true) && TickerMode.valuesOf(context).enabled;
    if (_visible != visible) {
      _visible = visible;
      _restart();
    }
  }

  @override
  void didUpdateWidget(LocalRuntimeStatusCard oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.session != widget.session) {
      (oldWidget.session ?? LocalMacSession.instance).removeListener(_sessionChanged);
      _session.addListener(_sessionChanged);
      _pairing = _currentPairing;
    }
    if (oldWidget.session != widget.session ||
        oldWidget.active != widget.active ||
        oldWidget.fetcher != widget.fetcher ||
        oldWidget.refreshInterval != widget.refreshInterval ||
        oldWidget.refreshRevision != widget.refreshRevision) {
      _restart(clear: oldWidget.session != widget.session || oldWidget.refreshRevision != widget.refreshRevision);
    }
  }

  void _sessionChanged() {
    final pairing = _currentPairing;
    // Draft saves and other notifications are not a new authenticated session.
    if (pairing == _pairing) return;
    _pairing = pairing;
    _restart(clear: true);
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    _foreground = state == AppLifecycleState.resumed;
    _restart();
  }

  void _restart({bool clear = false}) {
    _generation++;
    _timer?.cancel();
    _timer = null;
    _loading = false;
    if (clear) {
      _status = null;
      _connection = _ConnectionState.unknown;
    }
    if (mounted) setState(() {});
    if (_canPoll) unawaited(_refresh());
  }

  Future<void> _refresh() async {
    if (!_canPoll || _loading) return;
    _timer?.cancel();
    _timer = null;
    final generation = _generation;
    setState(() {
      _loading = true;
      if (_connection == _ConnectionState.unknown) _connection = _ConnectionState.loading;
    });
    try {
      final status = await (widget.fetcher ?? LocalRuntimeStatusClient(session: _session).fetch)();
      if (generation != _generation || !_canPoll) return;
      setState(() {
        _status = status;
        _connection = _ConnectionState.connected;
      });
    } on LocalMacUnauthorized {
      if (generation != _generation || !_canPoll) return;
      setState(() {
        _status = null;
        _connection = _ConnectionState.rejected;
      });
    } catch (_) {
      if (generation != _generation || !_canPoll) return;
      setState(() {
        _status = null;
        _connection = _ConnectionState.unavailable;
      });
    } finally {
      if (generation == _generation && _canPoll) {
        setState(() => _loading = false);
        _timer = Timer(widget.refreshInterval, () => unawaited(_refresh()));
      }
    }
  }

  @override
  void dispose() {
    _generation++;
    _timer?.cancel();
    _session.removeListener(_sessionChanged);
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }

  String _connectionText(BuildContext context) => switch (_connection) {
        _ConnectionState.connected => '${context.l10n.connected} · ${context.l10n.apiKeyAuth}',
        _ConnectionState.loading => context.l10n.loading,
        _ConnectionState.rejected => context.l10n.localMacKeyRejected,
        _ConnectionState.unavailable => context.l10n.localMacConnectionFailed,
        _ConnectionState.unknown => _session.isSignedIn ? context.l10n.unknown : context.l10n.notConnectedStatus,
      };

  String? _errorText(BuildContext context) {
    if (_connection == _ConnectionState.rejected) return context.l10n.localMacKeyRejected;
    if (_connection == _ConnectionState.unavailable) return context.l10n.localMacConnectionFailed;
    if (_status?.captureState == LocalCaptureState.decodeError) return context.l10n.error;
    if (_status?.liveTranscriptState == LocalLiveTranscriptState.failed) return context.l10n.transcriptionFailed;
    if (_status?.liveTranscriptState == LocalLiveTranscriptState.unavailable) {
      return context.l10n.transcriptionUnavailable;
    }
    return null;
  }

  String _summaryText(BuildContext context) {
    final error = _errorText(context);
    final state = error != null
        ? (_expanded ? context.l10n.error : error)
        : _connection == _ConnectionState.connected
            ? context.l10n.connected
            : _connectionText(context);
    return '${context.l10n.localMacTitle} · $state';
  }

  String _audioText(BuildContext context) {
    final status = _status;
    if (status == null) return context.l10n.unknown;
    final total = '${context.l10n.total}: ${context.l10n.secondsCount(status.audioSeconds.floor())}';
    return switch (status.captureState) {
      LocalCaptureState.idle || LocalCaptureState.waitingAudio => '${context.l10n.waitingForData} · $total',
      LocalCaptureState.received => total,
      LocalCaptureState.decodeError => '${context.l10n.error} · $total',
    };
  }

  String _transcriptText(BuildContext context) => switch (_status?.liveTranscriptState) {
        LocalLiveTranscriptState.disabled => context.l10n.off,
        LocalLiveTranscriptState.unavailable => context.l10n.transcriptionUnavailable,
        LocalLiveTranscriptState.ready => context.l10n.modelReady,
        LocalLiveTranscriptState.busy => context.l10n.processing,
        LocalLiveTranscriptState.streaming => '${context.l10n.transcriptReceived}: ${_status!.transcriptUpdates}',
        LocalLiveTranscriptState.failed => context.l10n.transcriptionFailed,
        null => context.l10n.unknown,
      };

  String _diarizationText(BuildContext context) => switch (_status?.diarizationState) {
        LocalDiarizationState.disabled => context.l10n.off,
        LocalDiarizationState.ready => context.l10n.modelReady,
        LocalDiarizationState.busy => context.l10n.processing,
        LocalDiarizationState.pending => context.l10n.waitingForData,
        LocalDiarizationState.labeled => context.l10n.nProcessed(_status!.labeledSegments),
        LocalDiarizationState.degraded ||
        LocalDiarizationState.failed ||
        LocalDiarizationState.unavailable =>
          context.l10n.cancelConsequenceSpeakers,
        LocalDiarizationState.unknown || null => context.l10n.unknown,
      };

  Widget _row(String name, String title, String value, IconData icon) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 6),
        child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Icon(icon, size: 18, color: Colors.white70),
          const SizedBox(width: 10),
          Expanded(
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Text(title, style: const TextStyle(color: Colors.white70, fontSize: 12)),
              const SizedBox(height: 2),
              Text(value, key: ValueKey('local-runtime-$name'), style: const TextStyle(color: Colors.white)),
            ]),
          ),
        ]),
      );

  @override
  Widget build(BuildContext context) {
    if (!Env.isOfflineRuntime) return const SizedBox.shrink();
    final error = _errorText(context);
    return Material(
      key: const ValueKey('local-runtime-status'),
      color: const Color(0xFF222222),
      borderRadius: BorderRadius.circular(_expanded ? 12 : 24),
      clipBehavior: Clip.antiAlias,
      child: Column(mainAxisSize: MainAxisSize.min, children: [
        Semantics(
          button: true,
          expanded: _expanded,
          child: InkWell(
            key: const ValueKey('local-runtime-toggle'),
            onTap: () => setState(() => _expanded = !_expanded),
            child: Padding(
              padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 12),
              child: Row(children: [
                if (widget.source == ConversationSource.omi || widget.source == ConversationSource.phone) ...[
                  Flexible(child: RecordingSourceLabel(source: widget.source)),
                  const SizedBox(width: 12),
                ],
                Icon(error == null ? Icons.computer : Icons.error_outline, size: 18, color: Colors.white70),
                const SizedBox(width: 8),
                Expanded(
                  child: Tooltip(
                    message: _summaryText(context),
                    child: Text(_summaryText(context),
                        key: const ValueKey('local-runtime-summary'),
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(color: Colors.white, fontSize: 13)),
                  ),
                ),
                const SizedBox(width: 8),
                Icon(_expanded ? Icons.expand_less : Icons.expand_more, size: 20, color: Colors.white70),
              ]),
            ),
          ),
        ),
        if (_expanded)
          Padding(
            key: const ValueKey('local-runtime-details'),
            padding: const EdgeInsets.fromLTRB(12, 0, 12, 12),
            child: Column(mainAxisSize: MainAxisSize.min, children: [
              _row('connection', context.l10n.localMacTitle, _connectionText(context), Icons.computer),
              _row('audio', context.l10n.audioDataReceived, _audioText(context), Icons.graphic_eq),
              _row('transcript', context.l10n.realtimeTranscript, _transcriptText(context), Icons.subtitles_outlined),
              _row('diarization', context.l10n.instantSpeakerLabels, _diarizationText(context), Icons.people_outline),
              Align(
                alignment: Alignment.centerRight,
                child: TextButton.icon(
                  key: const ValueKey('local-runtime-refresh'),
                  onPressed: _canPoll && !_loading ? _refresh : null,
                  style: TextButton.styleFrom(foregroundColor: Colors.white70),
                  icon: const Icon(Icons.refresh, size: 18),
                  label: Text(context.l10n.refresh),
                ),
              ),
            ]),
          ),
      ]),
    );
  }
}
