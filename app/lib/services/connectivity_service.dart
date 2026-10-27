import 'dart:async';
import 'dart:io';
import 'package:omi/services/auth/local_mac_session.dart';

import 'package:connectivity_plus/connectivity_plus.dart';
import 'package:internet_connection_checker_plus/internet_connection_checker_plus.dart';

import 'package:omi/env/env.dart';

class ConnectivityService {
  static final ConnectivityService _instance = ConnectivityService._internal();
  factory ConnectivityService() => _instance;

  ConnectivityService._internal();

  InternetConnection? _internetConnection;
  InternetConnection get internetConnection => _internetConnection ??= InternetConnection.createInstance(
    useDefaultOptions: false,
    checkInterval: const Duration(seconds: 10),
    customCheckOptions: [
      InternetCheckOption(uri: Uri.parse('https://one.one.one.one'), timeout: const Duration(seconds: 3)),
      InternetCheckOption(
        uri: Uri.parse('https://api.omi.me/v1/health'),
        timeout: const Duration(seconds: 3),
        responseStatusFn: (response) => response.statusCode < 500,
      ),
    ],
  );
  final Connectivity _connectivity = Connectivity();
  StreamSubscription? _connectivitySubscription;
  StreamSubscription? _internetSubscription;

  final _connectionChangeController = StreamController<bool>.broadcast();
  Stream<bool> get onConnectionChange => _connectionChangeController.stream;

  bool _isConnected = true;
  bool get isConnected => _isConnected;
  bool _isInitialized = false;
  bool _offlineRuntime = false;
  Timer? _tunnelProbeTimer;
  bool _probing = false;
  bool _notifyConnectedAfterProbe = false;

  Future<void> init({bool? offlineRuntime}) async {
    if (_isInitialized) return;
    _offlineRuntime = offlineRuntime ?? Env.isOfflineRuntime;

    final connectivityResult = await _connectivity.checkConnectivity();
    if (Env.usesLocalTunnel) {
      await refreshLocalTunnel();
    } else if (_offlineRuntime) {
      _isConnected = hasLocalNetworkTransport(connectivityResult);
    } else if (connectivityResult.contains(ConnectivityResult.none)) {
      _isConnected = false;
    } else {
      _isConnected = await internetConnection.hasInternetAccess;
      _internetSubscription = internetConnection.onStatusChange.listen(_handleInternetStatusChange);
    }

    _connectivitySubscription = _connectivity.onConnectivityChanged.listen(_handleConnectivityChange);
    if (_offlineRuntime) {
      _tunnelProbeTimer = Timer.periodic(const Duration(seconds: 10), (_) {
        if (Env.usesLocalTunnel) unawaited(refreshLocalTunnel());
      });
    }
    _isInitialized = true;
  }

  void dispose() {
    _tunnelProbeTimer?.cancel();
    _connectivitySubscription?.cancel();
    _internetSubscription?.cancel();
    _connectionChangeController.close();
  }

  void _handleConnectivityChange(List<ConnectivityResult> result) {
    if (Env.usesLocalTunnel) {
      // Wi-Fi -> cellular can keep health reachable while the old WebSocket
      // remains half-open. Publish this successful route change even if the
      // preceding reachability value was also true.
      unawaited(refreshLocalTunnel(notifyWhenConnected: true));
      return;
    }
    if (_offlineRuntime) {
      _updateConnectionState(hasLocalNetworkTransport(result));
      return;
    }
    if (result.contains(ConnectivityResult.mobile) ||
        result.contains(ConnectivityResult.wifi) ||
        result.contains(ConnectivityResult.ethernet)) {
      internetConnection.hasInternetAccess.then(_updateConnectionState);
      _internetSubscription ??= internetConnection.onStatusChange.listen(_handleInternetStatusChange);
      return;
    }

    // No internet
    _updateConnectionState(false);
    _internetSubscription?.cancel();
    _internetSubscription = null;
  }

  static bool hasLocalNetworkTransport(List<ConnectivityResult> result) =>
      result.contains(ConnectivityResult.wifi) ||
      result.contains(ConnectivityResult.ethernet) ||
      result.contains(ConnectivityResult.vpn);

  static bool hasTunnelTransport(List<ConnectivityResult> result) =>
      hasLocalNetworkTransport(result) || result.contains(ConnectivityResult.mobile);

  Future<void> refreshLocalTunnel({bool notifyWhenConnected = false}) async {
    if (!Env.usesLocalTunnel) return;
    _notifyConnectedAfterProbe |= notifyWhenConnected;
    if (_probing) return;
    _probing = true;
    final address = Env.apiBaseUrl;
    final client = HttpClient()..connectionTimeout = const Duration(seconds: 3);
    try {
      final transport = await _connectivity.checkConnectivity();
      if (!hasTunnelTransport(transport)) {
        _updateConnectionState(false);
        return;
      }
      final request = await client.getUrl(Uri.parse(address!).resolve('v1/health')).timeout(const Duration(seconds: 3));
      request.followRedirects = false;
      final response = await request.close().timeout(const Duration(seconds: 3));
      if (address == Env.apiBaseUrl) {
        final connected = response.statusCode == 200 && LocalMacSession.instance.isSignedIn;
        _updateConnectionState(connected, notifyIfUnchanged: connected && _notifyConnectedAfterProbe);
      }
    } catch (_) {
      if (address == Env.apiBaseUrl) _updateConnectionState(false);
    } finally {
      client.close(force: true);
      _probing = false;
      _notifyConnectedAfterProbe = false;
    }
  }

  void _handleInternetStatusChange(InternetStatus status) {
    _updateConnectionState(status == InternetStatus.connected);
  }

  void _updateConnectionState(bool newIsConnected, {bool notifyIfUnchanged = false}) {
    if (_isConnected != newIsConnected || notifyIfUnchanged) {
      _isConnected = newIsConnected;
      _connectionChangeController.add(_isConnected);
    }
  }
}
