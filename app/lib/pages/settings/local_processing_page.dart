import 'package:flutter/material.dart';

import 'package:omi/services/auth/local_mac_session.dart';
import 'package:omi/services/local_processing.dart';
import 'package:omi/utils/l10n_extensions.dart';

String _stageLabel(BuildContext context, String stage) => switch (stage) {
      'live' => context.l10n.liveTranscript,
      'stt' => '${context.l10n.transcription} · ${context.l10n.localProcessingFinished}',
      _ => '${context.l10n.diarization} · ${context.l10n.localProcessingFinished}',
    };

String _failureLabel(BuildContext context, Object error) => switch (error) {
      LocalProcessingException(failure: LocalProcessingFailure.unsupported) => context.l10n.localProcessingUnsupported,
      LocalProcessingException(failure: LocalProcessingFailure.unauthorized) => context.l10n.localMacKeyRejected,
      LocalProcessingException(failure: LocalProcessingFailure.invalid) => context.l10n.providerMisconfigured,
      LocalProcessingException(failure: LocalProcessingFailure.conflict) =>
        '${context.l10n.stopRecording}. ${context.l10n.refresh}.',
      _ => '${context.l10n.providerUnavailable}. ${context.l10n.refresh}.',
    };

class LocalProcessingPage extends StatefulWidget {
  const LocalProcessingPage({super.key, this.session, this.client});
  final LocalMacSession? session;
  final LocalProcessingClient? client;

  @override
  State<LocalProcessingPage> createState() => _LocalProcessingPageState();
}

class _LocalProcessingPageState extends State<LocalProcessingPage> {
  late final _session = widget.session ?? LocalMacSession.instance;
  late final _client = widget.client ?? LocalProcessingClient(session: _session);
  LocalProcessingSettings? _data;
  final _selection = <String, String?>{};
  bool _busy = false;
  bool _needsRefresh = false;
  String? _message;
  late String _address;
  String? _key;
  int _generation = 0;

  @override
  void initState() {
    super.initState();
    _address = _session.address;
    _key = _session.accessKey;
    _session.addListener(_pairingChanged);
    _load();
  }

  void _pairingChanged() {
    if (_address == _session.address && _key == _session.accessKey) return;
    _address = _session.address;
    _key = _session.accessKey;
    _generation++;
    setState(() {
      _data = null;
      _selection.clear();
      _message = null;
      _busy = false;
    });
    _load();
  }

  @override
  void dispose() {
    _generation++;
    _session.removeListener(_pairingChanged);
    super.dispose();
  }

  void _accept(LocalProcessingSettings data) {
    _data = data;
    for (final stage in ['live', 'stt', 'diarization']) {
      _selection[stage] = data.active(stage)?['id'] as String?;
    }
    _needsRefresh = false;
  }

  Future<void> _operate(Future<void> Function(int generation) action, {bool mutation = false}) async {
    if (_busy) return;
    final generation = _generation;
    setState(() {
      _busy = true;
      _message = null;
    });
    try {
      await action(generation);
    } catch (error) {
      if (!mounted || generation != _generation) return;
      setState(() {
        _message = _failureLabel(context, error);
        // A lost response can follow a successful write. Require an explicit read before another write.
        if (mutation) _needsRefresh = true;
      });
    } finally {
      if (mounted && generation == _generation) setState(() => _busy = false);
    }
  }

  Future<void> _load() => _operate((generation) async {
        final data = await _client.fetch();
        if (mounted && generation == _generation) setState(() => _accept(data));
      });

  Future<void> _activate(String stage) => _operate((generation) async {
        final data = LocalProcessingSettings.fromJson(
          await _client
              .request('/activate', body: {'revision': _data!.revision, 'stage': stage, 'id': _selection[stage]}),
        );
        if (mounted && generation == _generation) {
          setState(() {
            _accept(data);
            _message = context.l10n.active;
          });
        }
      }, mutation: true);

  Future<void> _edit(String stage, [Map<String, dynamic>? profile]) async {
    final generation = _generation;
    final result = await Navigator.of(context).push<LocalProcessingSettings>(
      MaterialPageRoute(
        builder: (_) => _ProcessingEditor(
          stage: stage,
          profile: profile,
          revision: _data!.revision,
          client: _client,
          session: _session,
        ),
      ),
    );
    if (!mounted || generation != _generation) return;
    if (result != null) {
      setState(() => _accept(result));
    }
    // The editor may have lost a write response; reconcile before offering another mutation.
    await _load();
  }

  Future<void> _delete(Map<String, dynamic> profile) async {
    final generation = _generation;
    final confirmed = await showDialog<bool>(
        context: context,
        builder: (context) => AlertDialog(
              title: Text(context.l10n.delete),
              content: Text(context.l10n.deleteItemConfirmation(profile['name'] as String)),
              actions: [
                TextButton(
                    style: TextButton.styleFrom(foregroundColor: Colors.white),
                    onPressed: () => Navigator.pop(context, false),
                    child: Text(context.l10n.cancel)),
                TextButton(
                    style: TextButton.styleFrom(foregroundColor: Colors.white),
                    onPressed: () => Navigator.pop(context, true),
                    child: Text(context.l10n.delete)),
              ],
            ));
    if (!mounted || generation != _generation || confirmed != true) return;
    await _operate((generation) async {
      final result =
          await _client.request('/profiles/${profile['id']}', method: 'DELETE', body: {'revision': _data!.revision});
      if (mounted && generation == _generation) setState(() => _accept(LocalProcessingSettings.fromJson(result)));
    }, mutation: true);
  }

  Widget _stage(String stage) {
    final profiles = _data!.profiles.where((p) => p['stage'] == stage).toList();
    final active = _data!.active(stage);
    final selected = profiles.where((p) => p['id'] == _selection[stage]).firstOrNull;
    final canEdit = selected != null && selected['can_edit'] == true;
    final disabled = _busy || _needsRefresh;
    final settings = selected?['settings'] as Map?;
    final endpoint = settings?['url'] ?? settings?['provider_url'] ?? settings?['base_url'];
    final effectiveSettings = active?['settings'] as Map?;
    final effectiveEndpoint =
        effectiveSettings?['url'] ?? effectiveSettings?['provider_url'] ?? effectiveSettings?['base_url'];
    final pending = selected != null &&
        selected['id'] == active?['id'] &&
        selected['configuration_revision'] != active?['configuration_revision'];
    return Card(
      color: const Color(0xFF222222),
      margin: const EdgeInsets.only(bottom: 16),
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Text(_stageLabel(context, stage), style: Theme.of(context).textTheme.titleMedium),
            const SizedBox(height: 8),
            Text('${context.l10n.active}: ${active?['name'] ?? context.l10n.off}'),
            if (effectiveEndpoint is String && effectiveEndpoint != endpoint)
              Text('${context.l10n.active}: $effectiveEndpoint',
                  key: ValueKey('processing-$stage-effective-endpoint'), style: const TextStyle(color: Colors.white70)),
            if (pending)
              Padding(
                padding: const EdgeInsets.only(top: 8),
                child: Text(context.l10n.localProcessingPending, key: ValueKey('processing-$stage-pending')),
              ),
            const SizedBox(height: 12),
            DropdownButtonFormField<String>(
              key: ValueKey('processing-$stage-selection-${_data!.revision}'),
              initialValue: selected?['id'] as String? ?? (stage == 'stt' ? null : ''),
              isExpanded: true,
              decoration: InputDecoration(labelText: context.l10n.provider),
              items: [
                if (stage != 'stt') DropdownMenuItem(value: '', child: Text(context.l10n.off)),
                ...profiles.map(
                  (profile) => DropdownMenuItem<String>(
                    value: profile['id'] as String,
                    child: Text(profile['name'] as String, overflow: TextOverflow.ellipsis),
                  ),
                ),
              ],
              onChanged: disabled ? null : (id) => setState(() => _selection[stage] = id == '' ? null : id),
            ),
            if (selected != null) ...[
              const SizedBox(height: 8),
              Text(selected['kind'] as String, style: const TextStyle(color: Colors.white70)),
              if (endpoint is String) Text(endpoint, style: const TextStyle(color: Colors.white70)),
            ],
            const SizedBox(height: 8),
            Wrap(
              spacing: 8,
              children: [
                FilledButton(
                  key: ValueKey('processing-$stage-apply'),
                  style: FilledButton.styleFrom(backgroundColor: Colors.white, foregroundColor: Colors.black),
                  onPressed: disabled || (stage == 'stt' && selected == null) ? null : () => _activate(stage),
                  child: Text(context.l10n.confirm),
                ),
                TextButton.icon(
                  key: ValueKey('processing-$stage-add'),
                  onPressed: disabled ? null : () => _edit(stage),
                  style: TextButton.styleFrom(foregroundColor: Colors.white),
                  icon: const Icon(Icons.add),
                  label: Text(context.l10n.add),
                ),
                if (canEdit)
                  TextButton(
                    key: ValueKey('processing-$stage-edit'),
                    onPressed: disabled ? null : () => _edit(stage, selected),
                    style: TextButton.styleFrom(foregroundColor: Colors.white),
                    child: Text(context.l10n.edit),
                  ),
                if (canEdit && active?['id'] != selected['id'])
                  TextButton(
                    key: ValueKey('processing-$stage-delete'),
                    onPressed: disabled ? null : () => _delete(selected),
                    style: TextButton.styleFrom(foregroundColor: Colors.white),
                    child: Text(context.l10n.delete),
                  ),
              ],
            ),
          ],
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) => Scaffold(
        appBar: AppBar(
          title: Text('${context.l10n.transcription} · ${context.l10n.diarization}'),
          actions: [
            IconButton(
              key: const ValueKey('processing-refresh'),
              tooltip: context.l10n.refresh,
              onPressed: _busy ? null : _load,
              icon: const Icon(Icons.refresh),
            ),
          ],
        ),
        body: ListView(
          padding: const EdgeInsets.all(20),
          children: [
            Text(_address, style: const TextStyle(color: Colors.white70)),
            const SizedBox(height: 12),
            Text(context.l10n.localProcessingHelp),
            const SizedBox(height: 12),
            Text(context.l10n.localProcessingApplyHelp),
            if (_busy)
              const Padding(
                padding: EdgeInsets.symmetric(vertical: 12),
                child: LinearProgressIndicator(color: Colors.white),
              ),
            if (_message != null)
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 16),
                child: Text(_message!, key: const ValueKey('processing-result')),
              ),
            const SizedBox(height: 16),
            if (_data != null) ...['live', 'stt', 'diarization'].map(_stage),
          ],
        ),
      );
}

class _ProcessingEditor extends StatefulWidget {
  const _ProcessingEditor({
    required this.stage,
    required this.revision,
    required this.client,
    required this.session,
    this.profile,
  });
  final String stage;
  final int revision;
  final LocalProcessingClient client;
  final LocalMacSession session;
  final Map<String, dynamic>? profile;
  @override
  State<_ProcessingEditor> createState() => _ProcessingEditorState();
}

class _ProcessingEditorState extends State<_ProcessingEditor> {
  final _form = GlobalKey<FormState>();
  late final _name = TextEditingController(text: widget.profile?['name'] as String? ?? '');
  late final _url = TextEditingController(text: _settings[_urlField] as String? ?? '');
  late final _model = TextEditingController(text: _settings['model'] as String? ?? '');
  late final _language = TextEditingController(text: _settings['language'] as String? ?? 'auto');
  final _key = TextEditingController();
  late final String _address = widget.session.address;
  late final String? _pairingKey = widget.session.accessKey;
  bool _clearKey = false;
  bool _closing = false;
  bool _busy = false;
  bool _needsRefresh = false;
  String? _message;
  List<String> _models = [];
  Map<String, dynamic> get _settings => widget.profile?['settings'] as Map<String, dynamic>? ?? {};
  String get _urlField => widget.stage == 'live'
      ? 'url'
      : widget.stage == 'stt'
          ? 'provider_url'
          : 'base_url';
  String get _kind => widget.stage == 'live'
      ? 'external'
      : widget.stage == 'stt'
          ? 'openai-compatible'
          : 'mycelia';

  @override
  void initState() {
    super.initState();
    widget.session.addListener(_onPairing);
  }

  void _onPairing() {
    if (!_closing && (widget.session.address != _address || widget.session.accessKey != _pairingKey)) {
      _closing = true;
      _key.clear();
      if (ModalRoute.of(context)?.isCurrent == true) Navigator.of(context).pop();
    }
  }

  @override
  void dispose() {
    widget.session.removeListener(_onPairing);
    for (final controller in [_name, _url, _model, _language, _key]) {
      controller.dispose();
    }
    super.dispose();
  }

  Map<String, dynamic> _payload() => LocalProcessingClient.draftPayload(
        {
          if (widget.profile?['id'] != null) 'id': widget.profile!['id'],
          'name': _name.text,
          'stage': widget.stage,
          'kind': _kind,
          'settings': {
            ..._settings,
            _urlField: _url.text.trim(),
            if (widget.stage == 'stt') ...{
              'model': _model.text.trim(),
              'language': _language.text.trim(),
              'diarization_model': 'none',
            },
          },
        },
        apiKey: _key.text,
        clearKey: _clearKey,
      );

  Future<void> _request(String action) async {
    if (_busy || _needsRefresh || !_form.currentState!.validate()) return;
    if (widget.stage == 'stt' && action != 'models' && _model.text.trim().isEmpty) {
      setState(() => _message = context.l10n.modelRequired);
      return;
    }
    setState(() {
      _busy = true;
      _message = null;
    });
    try {
      final result = await widget.client.request(
        '/$action',
        body: {..._payload(), if (action == 'save') 'revision': widget.revision},
      );
      if (!mounted || _closing || ModalRoute.of(context)?.isCurrent != true) return;
      if (action == 'save') {
        _closing = true;
        _key.clear();
        Navigator.of(context).pop(LocalProcessingSettings.fromJson(result));
        return;
      }
      setState(() {
        if (action == 'models') _models = (result['models'] as List? ?? []).whereType<String>().toList();
        _message = switch (result['state']) {
          'ready' => context.l10n.modelReady,
          'available' => context.l10n.connected,
          'misconfigured' => context.l10n.providerMisconfigured,
          _ => context.l10n.providerUnavailable,
        };
      });
    } catch (error) {
      if (mounted) {
        setState(() {
          _message = _failureLabel(context, error);
          if (action == 'save') _needsRefresh = true;
        });
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Widget _field(
    String id,
    String label,
    TextEditingController controller, {
    bool required = true,
    String? hint,
    bool secret = false,
  }) =>
      Padding(
        padding: const EdgeInsets.only(bottom: 16),
        child: TextFormField(
          key: ValueKey('processing-editor-$id'),
          controller: controller,
          enabled: !_busy && !_needsRefresh,
          obscureText: secret,
          onChanged: (_) => setState(() {
            _message = null;
          }),
          autocorrect: false,
          enableSuggestions: false,
          decoration: InputDecoration(labelText: label, hintText: hint),
          validator: (value) => required && (value ?? '').trim().isEmpty ? context.l10n.providerMisconfigured : null,
        ),
      );

  @override
  Widget build(BuildContext context) => Scaffold(
        appBar: AppBar(title: Text(_stageLabel(context, widget.stage))),
        body: Form(
          key: _form,
          child: ListView(
            padding: const EdgeInsets.all(20),
            children: [
              Text(
                _kind == 'external'
                    ? 'WhisperLiveKit · WebSocket'
                    : _kind == 'mycelia'
                        ? 'Mycelia'
                        : 'OpenAI-compatible',
              ),
              const SizedBox(height: 20),
              _field('name', context.l10n.name, _name),
              _field(
                'url',
                context.l10n.serverUrl,
                _url,
                hint: widget.stage == 'live'
                    ? 'wss://server.example/asr'
                    : widget.stage == 'stt'
                        ? 'https://server.example/v1'
                        : 'https://server.example',
              ),
              if (widget.stage == 'stt') ...[
                _field('model', context.l10n.model, _model, required: false),
                if (_models.isNotEmpty)
                  Wrap(
                    spacing: 8,
                    children: _models
                        .map(
                          (model) => ActionChip(
                            label: Text(model),
                            onPressed: _busy ? null : () => setState(() => _model.text = model),
                          ),
                        )
                        .toList(),
                  ),
                _field('language', context.l10n.language, _language, hint: 'auto / ru / en'),
              ],
              _field('key', context.l10n.apiKey, _key, required: false, secret: true),
              Text(context.l10n.localProcessingKeyHelp),
              if (widget.profile?['has_key'] == true)
                CheckboxListTile(
                  key: const ValueKey('processing-editor-clear-key'),
                  value: _clearKey,
                  onChanged: _busy ? null : (value) => setState(() => _clearKey = value ?? false),
                  title: Text('${context.l10n.delete} · ${context.l10n.apiKey}'),
                  activeColor: Colors.white,
                  checkColor: Colors.black,
                ),
              if (_busy) const LinearProgressIndicator(color: Colors.white),
              if (_message != null)
                Padding(
                  padding: const EdgeInsets.symmetric(vertical: 16),
                  child: Text(_message!, key: const ValueKey('processing-editor-result')),
                ),
              const SizedBox(height: 16),
              Wrap(
                spacing: 8,
                children: [
                  OutlinedButton(
                    key: const ValueKey('processing-editor-probe'),
                    style: OutlinedButton.styleFrom(
                        foregroundColor: Colors.white, side: const BorderSide(color: Colors.white38)),
                    onPressed: _busy || _needsRefresh ? null : () => _request('probe'),
                    child: Text(context.l10n.localProcessingProbe),
                  ),
                  if (widget.stage == 'stt')
                    OutlinedButton(
                      key: const ValueKey('processing-editor-models'),
                      style: OutlinedButton.styleFrom(
                          foregroundColor: Colors.white, side: const BorderSide(color: Colors.white38)),
                      onPressed: _busy || _needsRefresh ? null : () => _request('models'),
                      child: Text('${context.l10n.refresh} · ${context.l10n.model}'),
                    ),
                  FilledButton(
                    key: const ValueKey('processing-editor-save'),
                    style: FilledButton.styleFrom(backgroundColor: Colors.white, foregroundColor: Colors.black),
                    onPressed: _busy || _needsRefresh ? null : () => _request('save'),
                    child: Text(context.l10n.save),
                  ),
                ],
              ),
            ],
          ),
        ),
      );
}
