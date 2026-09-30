import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:provider/provider.dart';
import 'package:omi/pages/settings/local_runtime_status_card.dart';
import 'package:omi/providers/capture_provider.dart';
import 'package:omi/services/auth/local_mac_session.dart';
import 'package:omi/services/local_runtime_status.dart';
import 'package:omi/services/connectivity_service.dart';
import 'package:omi/utils/l10n_extensions.dart';
import 'package:omi/utils/enums.dart';

class LocalMacPage extends StatefulWidget {
  const LocalMacPage({super.key, this.session, this.stopRecording, this.refreshConnection, this.statusFetcher});
  final LocalMacSession? session;
  final Future<void> Function()? stopRecording;
  final Future<void> Function()? refreshConnection;
  final Future<LocalRuntimeStatus> Function()? statusFetcher;
  @override
  State<LocalMacPage> createState() => _LocalMacPageState();
}

enum _LocalMacOperation { loading, saving, connecting, selecting }

class _LocalMacPageState extends State<LocalMacPage> with WidgetsBindingObserver {
  LocalMacSession get _session => widget.session ?? LocalMacSession.instance;
  late final _address = TextEditingController(text: _session.address);
  final _key = TextEditingController();
  final _name = TextEditingController();
  List<LocalMacServer> _servers = [];
  String? _selectedServerId;
  _LocalMacOperation? _operation = _LocalMacOperation.loading;
  bool get _busy => _operation != null;
  String? _success;
  int _statusRevision = 0;
  bool _showKey = false;
  String? _error;
  Future<void> _pendingSave = Future<void>.value();

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _loadSettings();
  }

  Future<void> _loadSettings() async {
    try {
      final servers = await _session.readServers();
      final saved = await _session.readSettings();
      if (!mounted) return;
      _servers = servers;
      final selected = servers
          .where((server) => saved.serverId == null
              ? server.address == saved.address && server.key == saved.key
              : server.id == saved.serverId)
          .firstOrNull;
      _selectedServerId = selected?.id;
      _name.text = saved.name.isEmpty ? selected?.name ?? '' : saved.name;
      _address.text = saved.address;
      _key.text = saved.key;
    } catch (_) {
      if (mounted) setState(() => _error = context.l10n.localMacConnectionFailed);
    } finally {
      if (mounted) setState(() => _operation = null);
    }
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state != AppLifecycleState.resumed && _showKey) {
      setState(() => _showKey = false);
    }
  }

  void _editSettings(String _) {
    setState(() {
      _error = null;
      _success = null;
    });
    _autosave();
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _address.dispose();
    _key.dispose();
    _name.dispose();
    super.dispose();
  }

  Future<void> _saveSettings() async {
    setState(() {
      _operation = _LocalMacOperation.saving;
      _error = null;
      _success = null;
    });
    try {
      await _saveEntry();
      if (mounted) {
        setState(() {
          _error = null;
          _success = context.l10n.saved;
        });
      }
    } catch (_) {
      if (mounted) setState(() => _error = context.l10n.failedToSaveCheckConnection);
    } finally {
      if (mounted) setState(() => _operation = null);
    }
  }

  Future<void> _queueSave() {
    final address = _address.text;
    final key = _key.text;
    final name = _name.text;
    final serverId = _selectedServerId;
    final save = _pendingSave.then((_) => _session.saveSettings(address, key, name: name, serverId: serverId));
    // Each caller reports its own failure; keep later edits writable after an error.
    _pendingSave = save.onError((_, __) {});
    return save;
  }

  Future<void> _saveEntry() async {
    await _queueSave();
    final server =
        await _session.saveServer(id: _selectedServerId, name: _name.text, address: _address.text, key: _key.text);
    if (!mounted) return;
    setState(() {
      _servers = [..._servers.where((entry) => entry.id != server.id), server];
      _selectedServerId = server.id;
      _name.text = server.name;
    });
    await _queueSave();
  }

  Future<void> _selectServer(LocalMacServer? server, {bool delete = false}) async {
    FocusManager.instance.primaryFocus?.unfocus();
    setState(() {
      _operation = _LocalMacOperation.selecting;
      _showKey = false;
      _error = null;
      _success = null;
    });
    try {
      await _pendingSave;
      if (delete) await _session.deleteServer(_selectedServerId!);
      await _session.saveSettings(server?.address ?? '', server?.key ?? '',
          name: server?.name ?? '', serverId: server?.id);
      final servers = await _session.readServers();
      if (!mounted) return;
      setState(() {
        _servers = servers;
        _selectedServerId = server?.id;
        _name.text = server?.name ?? '';
        _address.text = server?.address ?? '';
        _key.text = server?.key ?? '';
      });
    } catch (_) {
      if (mounted) setState(() => _error = context.l10n.failedToSaveCheckConnection);
    } finally {
      if (mounted) setState(() => _operation = null);
    }
  }

  Future<void> _autosave() async {
    try {
      await _queueSave();
    } catch (_) {
      if (mounted) setState(() => _error = context.l10n.failedToSaveCheckConnection);
    }
  }

  Future<void> _connect() async {
    FocusManager.instance.primaryFocus?.unfocus();
    setState(() {
      _operation = _LocalMacOperation.connecting;
      _showKey = false;
      _error = null;
      _success = null;
    });
    try {
      await _saveEntry();
      if (!mounted) return;
      if (widget.stopRecording != null) {
        await widget.stopRecording!();
      } else {
        final capture = context.read<CaptureProvider>();
        if (capture.recordingState != RecordingState.stop || capture.havingRecordingDevice) {
          await capture.stopStreamRecording(reason: 'local_mac_changed');
          await capture.stopStreamDeviceRecording(cleanDevice: true);
        }
      }
      await _session.connect(_address.text.trim(), _key.text.trim());
      await (widget.refreshConnection ?? ConnectivityService().refreshLocalTunnel)();
      if (mounted) {
        setState(() {
          _error = null;
          _success = context.l10n.connected;
        });
      }
    } on LocalMacUnauthorized {
      if (mounted) setState(() => _error = context.l10n.localMacKeyRejected);
    } catch (_) {
      if (mounted) setState(() => _error = context.l10n.localMacConnectionFailed);
    } finally {
      if (mounted) {
        setState(() {
          _operation = null;
          _statusRevision++;
        });
      }
    }
  }

  String _readinessLabel(String status) => switch (status) {
        'unknown' => context.l10n.unknown,
        'disabled' => context.l10n.off,
        _ => context.l10n.transcriptionUnavailable,
      };

  InputDecoration _fieldDecoration(String label, {String? hint, Widget? suffix}) => InputDecoration(
        labelText: label,
        hintText: hint,
        suffixIcon: suffix,
        counterText: '',
        filled: true,
        fillColor: const Color(0xFF222222),
        contentPadding: const EdgeInsets.symmetric(horizontal: 16, vertical: 18),
        border: OutlineInputBorder(borderRadius: BorderRadius.circular(12), borderSide: BorderSide.none),
        enabledBorder: OutlineInputBorder(borderRadius: BorderRadius.circular(12), borderSide: BorderSide.none),
        focusedBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(12),
          borderSide: const BorderSide(color: Colors.white70),
        ),
      );

  Widget _buttonLabel(String label, IconData icon, _LocalMacOperation operation) => Row(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          if (_operation == operation)
            SizedBox(
              width: 18,
              height: 18,
              child: CircularProgressIndicator(
                strokeWidth: 2,
                color: operation == _LocalMacOperation.connecting ? Colors.black : Colors.white,
              ),
            )
          else
            Icon(icon, size: 18),
          const SizedBox(width: 10),
          Flexible(child: Text(label, textAlign: TextAlign.center)),
        ],
      );

  @override
  Widget build(BuildContext context) => Scaffold(
        appBar: AppBar(title: Text(context.l10n.localMacTitle)),
        body: Align(
          alignment: Alignment.topCenter,
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 560),
            child: SingleChildScrollView(
              padding: const EdgeInsets.fromLTRB(20, 16, 20, 32),
              keyboardDismissBehavior: ScrollViewKeyboardDismissBehavior.onDrag,
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  LocalRuntimeStatusCard(
                    session: _session,
                    fetcher: widget.statusFetcher,
                    active: _operation != _LocalMacOperation.connecting,
                    refreshRevision: _statusRevision,
                  ),
                  const SizedBox(height: 20),
                  Text(context.l10n.localMacHelp,
                      style: const TextStyle(color: Colors.white70, fontSize: 14, height: 1.5)),
                  const SizedBox(height: 24),
                  InputDecorator(
                    decoration: _fieldDecoration(context.l10n.localMacServers),
                    child: DropdownButtonHideUnderline(
                      child: DropdownButton<String>(
                        key: const ValueKey('local-mac-servers'),
                        value: _selectedServerId,
                        isExpanded: true,
                        isDense: true,
                        hint: Text(context.l10n.add),
                        items: _servers
                            .map((server) => DropdownMenuItem(
                                value: server.id, child: Text(server.name, overflow: TextOverflow.ellipsis)))
                            .toList(),
                        onChanged:
                            _busy ? null : (id) => _selectServer(_servers.firstWhere((server) => server.id == id)),
                      ),
                    ),
                  ),
                  Wrap(
                    alignment: WrapAlignment.spaceBetween,
                    spacing: 8,
                    children: [
                      TextButton.icon(
                        key: const ValueKey('local-mac-add-server'),
                        onPressed: _busy ? null : () => _selectServer(null),
                        style: TextButton.styleFrom(foregroundColor: Colors.white70),
                        icon: const Icon(Icons.add),
                        label: Text(context.l10n.add),
                      ),
                      TextButton.icon(
                        key: const ValueKey('local-mac-delete-server'),
                        onPressed: _busy || _selectedServerId == null ? null : () => _selectServer(null, delete: true),
                        style: TextButton.styleFrom(foregroundColor: Colors.white70),
                        icon: const Icon(Icons.delete_outline),
                        label: Text(context.l10n.delete),
                      ),
                    ],
                  ),
                  const SizedBox(height: 8),
                  TextField(
                    key: const ValueKey('local-mac-name'),
                    controller: _name,
                    enabled: !_busy,
                    textInputAction: TextInputAction.next,
                    maxLength: 80,
                    onChanged: _editSettings,
                    decoration: _fieldDecoration(context.l10n.name),
                  ),
                  const SizedBox(height: 16),
                  TextField(
                    key: const ValueKey('local-mac-address'),
                    controller: _address,
                    enabled: !_busy,
                    keyboardType: TextInputType.url,
                    textInputAction: TextInputAction.next,
                    autocorrect: false,
                    enableSuggestions: false,
                    onChanged: _editSettings,
                    decoration: _fieldDecoration(context.l10n.localMacAddress, hint: '100.64.0.1:20000'),
                  ),
                  const SizedBox(height: 16),
                  TextField(
                    key: const ValueKey('local-mac-key'),
                    controller: _key,
                    enabled: !_busy,
                    obscureText: !_showKey,
                    autocorrect: false,
                    enableSuggestions: false,
                    textInputAction: TextInputAction.done,
                    style: const TextStyle(fontFamily: 'monospace', fontSize: 14),
                    maxLength: 43,
                    maxLengthEnforcement: MaxLengthEnforcement.none,
                    inputFormatters: [FilteringTextInputFormatter.deny(RegExp(r'\s'))],
                    onChanged: _editSettings,
                    onSubmitted: (_) {
                      if (!_busy) FocusManager.instance.primaryFocus?.unfocus();
                    },
                    decoration: _fieldDecoration(
                      context.l10n.localMacAccessKey,
                      suffix: Semantics(
                        toggled: _showKey,
                        child: IconButton(
                          key: const ValueKey('local-mac-show-key'),
                          tooltip: context.l10n.localMacAccessKey,
                          color: Colors.white70,
                          onPressed: _busy ? null : () => setState(() => _showKey = !_showKey),
                          icon: Icon(_showKey ? Icons.visibility_off_outlined : Icons.visibility_outlined),
                        ),
                      ),
                    ),
                  ),
                  const SizedBox(height: 24),
                  FilledButton(
                    key: const ValueKey('local-mac-connect'),
                    onPressed: _busy ? null : _connect,
                    style: FilledButton.styleFrom(
                      backgroundColor: Colors.white,
                      foregroundColor: Colors.black,
                      minimumSize: const Size.fromHeight(50),
                      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
                    ),
                    child: _buttonLabel(context.l10n.localMacConnect, Icons.link, _LocalMacOperation.connecting),
                  ),
                  const SizedBox(height: 12),
                  OutlinedButton(
                    key: const ValueKey('local-mac-save'),
                    onPressed: _busy ? null : _saveSettings,
                    style: OutlinedButton.styleFrom(
                      foregroundColor: Colors.white70,
                      minimumSize: const Size.fromHeight(48),
                      side: const BorderSide(color: Colors.white24),
                      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
                    ),
                    child: _buttonLabel(context.l10n.saveCredentials, Icons.save_outlined, _LocalMacOperation.saving),
                  ),
                  if (_error != null || _success != null) ...[
                    const SizedBox(height: 16),
                    Semantics(
                      liveRegion: true,
                      child: Container(
                        key: const ValueKey('local-mac-result'),
                        padding: const EdgeInsets.all(16),
                        decoration:
                            BoxDecoration(color: const Color(0xFF222222), borderRadius: BorderRadius.circular(12)),
                        child: Row(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Icon(
                              _error != null ? Icons.error_outline : Icons.check_circle_outline,
                              size: 20,
                              color: _error != null ? const Color(0xFFFFB4AB) : Colors.white70,
                            ),
                            const SizedBox(width: 10),
                            Expanded(
                              child: Column(
                                crossAxisAlignment: CrossAxisAlignment.start,
                                children: [
                                  Text(_error ?? _success!, style: const TextStyle(height: 1.4)),
                                  if (_error == null && _success == context.l10n.connected) ...[
                                    if (_session.readiness.live != 'ready')
                                      Text(
                                          '${context.l10n.liveTranscript}: ${_readinessLabel(_session.readiness.live)}'),
                                    if (_session.readiness.finalTranscript != 'ready')
                                      Text(
                                          '${context.l10n.transcript}: ${_readinessLabel(_session.readiness.finalTranscript)}'),
                                  ],
                                ],
                              ),
                            ),
                          ],
                        ),
                      ),
                    ),
                  ],
                ],
              ),
            ),
          ),
        ),
      );
}
