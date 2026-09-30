import 'dart:io';
import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/env/dev_env.dart';
import 'package:omi/env/env.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/pages/settings/local_mac_page.dart';
import 'package:omi/services/auth/local_mac_session.dart';
import 'package:omi/services/local_runtime_status.dart';
import 'package:omi/utils/offline_network_policy.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  setUpAll(() {
    Env.init(DevEnv());
    Env.setRuntimeModeForTesting(OmiRuntimeMode.offline);
  });
  tearDownAll(() => Env.setRuntimeModeForTesting(null));
  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    FlutterSecureStorage.setMockInitialValues({});
    await SharedPreferencesUtil.init();
  });
  tearDown(() {
    Env.localTunnelConfigured = false;
    OfflineNetworkPolicy.resetForTesting();
  });

  Future<LocalRuntimeStatus> status() async => const LocalRuntimeStatus(
        captureState: LocalCaptureState.idle,
        audioSeconds: 0,
        framesReceived: 0,
        liveTranscriptState: LocalLiveTranscriptState.disabled,
        transcriptUpdates: 0,
      );

  Future<void> tapFormButton(WidgetTester tester, String key) async {
    final button = find.byKey(ValueKey(key));
    final formScroll = find.descendant(of: find.byType(SingleChildScrollView), matching: find.byType(Scrollable)).first;
    // Exercise controls through the scrollable form on the small test viewport.
    await tester.scrollUntilVisible(button, 160, scrollable: formScroll);
    await tester.pumpAndSettle();
    expect(button.hitTestable(), findsOneWidget);
    await tester.tap(button);
    await tester.pumpAndSettle();
  }

  Future<void> enterField(WidgetTester tester, Finder field, String value) async {
    final scroll = find.descendant(of: find.byType(SingleChildScrollView), matching: find.byType(Scrollable)).first;
    await tester.scrollUntilVisible(field, 160, scrollable: scroll);
    await tester.enterText(field, value);
    await tester.pumpAndSettle();
  }

  testWidgets('pairing screen hides key and stops capture before connecting', (tester) async {
    final order = <String>[];
    final session = LocalMacSession(
      probe: (_, __) async {
        order.add('probe');
        return {'uid': 'alice'};
      },
    );
    await tester.pumpWidget(
      MaterialApp(
        locale: const Locale('ru'),
        localizationsDelegates: const [
          AppLocalizations.delegate,
          GlobalMaterialLocalizations.delegate,
          GlobalWidgetsLocalizations.delegate,
          GlobalCupertinoLocalizations.delegate,
        ],
        supportedLocales: AppLocalizations.supportedLocales,
        home: Builder(
          builder: (context) => Scaffold(
            body: TextButton(
              onPressed: () => Navigator.of(context).push(
                MaterialPageRoute(
                  builder: (_) => LocalMacPage(
                    session: session,
                    statusFetcher: status,
                    stopRecording: () async {
                      order.add('stop');
                    },
                    refreshConnection: () async {},
                  ),
                ),
              ),
              child: const Text('Open'),
            ),
          ),
        ),
      ),
    );
    await tester.tap(find.text('Open'));
    await tester.pumpAndSettle();
    expect(find.descendant(of: find.byType(AppBar), matching: find.text('Локальный Mac')), findsOneWidget);
    final field = tester.widget<TextField>(find.byKey(const ValueKey('local-mac-key')));
    expect(field.obscureText, isTrue);
    await enterField(tester, find.byKey(const ValueKey('local-mac-address')), 'https://synthetic.ngrok.app');
    await enterField(tester, find.byKey(const ValueKey('local-mac-key')), List.filled(43, 's').join());
    await tapFormButton(tester, 'local-mac-connect');
    expect(order, ['stop', 'probe']);
    expect(session.isSignedIn, isTrue);
    expect(find.byType(LocalMacPage), findsOneWidget);
    expect(
      tester.widget<TextField>(find.byKey(const ValueKey('local-mac-key'))).controller!.text,
      List.filled(43, 's').join(),
    );
    await tester.ensureVisible(find.byKey(const ValueKey('local-mac-result')));
    final l10n = AppLocalizations.of(tester.element(find.byType(LocalMacPage)));
    expect(
      find.descendant(of: find.byKey(const ValueKey('local-mac-result')), matching: find.text(l10n.connected)),
      findsOneWidget,
    );
    expect(tester.takeException(), isNull);
    await tester.pumpWidget(const SizedBox());
  });

  for (final readiness in [
    null,
    {
      'live': {'status': 'disabled'},
      'final': {'status': 'unavailable'}
    }
  ]) {
    testWidgets('pairing warns for readiness $readiness while keeping audio connection usable', (tester) async {
      final session = LocalMacSession(probe: (_, __) async => {'uid': 'alice', 'local_transcription': readiness});
      await tester.pumpWidget(MaterialApp(
        locale: const Locale('en'),
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: LocalMacPage(
            session: session, statusFetcher: status, stopRecording: () async {}, refreshConnection: () async {}),
      ));
      await tester.pumpAndSettle();
      await enterField(tester, find.byKey(const ValueKey('local-mac-address')), 'https://synthetic.ngrok.app');
      await enterField(tester, find.byKey(const ValueKey('local-mac-key')), List.filled(43, 's').join());
      await tapFormButton(tester, 'local-mac-connect');
      await tester.pumpAndSettle();
      expect(session.isSignedIn, isTrue);
      expect(find.byType(AlertDialog), findsNothing);
      await tester.ensureVisible(find.byKey(const ValueKey('local-mac-result')));
      expect(find.text(readiness == null ? 'Live Transcript: Unknown' : 'Live Transcript: Off'), findsOneWidget);
      expect(find.text(readiness == null ? 'Transcript: Unknown' : 'Transcript: Transcription unavailable'),
          findsOneWidget);
      expect(find.byType(LocalMacPage), findsOneWidget);
      expect(tester.widget<TextField>(find.byKey(const ValueKey('local-mac-key'))).controller!.text,
          List.filled(43, 's').join());
      expect(session.isSignedIn, isTrue);
      expect(find.byType(AlertDialog), findsNothing);
      expect(tester.takeException(), isNull);
    });
  }

  Future<void> openPage(WidgetTester tester, LocalMacSession session) async {
    await tester.pumpWidget(
      MaterialApp(
        localizationsDelegates: const [
          AppLocalizations.delegate,
          GlobalMaterialLocalizations.delegate,
          GlobalWidgetsLocalizations.delegate,
          GlobalCupertinoLocalizations.delegate,
        ],
        supportedLocales: AppLocalizations.supportedLocales,
        home: LocalMacPage(
          session: session,
          statusFetcher: status,
          stopRecording: () async {},
          refreshConnection: () async {},
        ),
      ),
    );
    await tester.pumpAndSettle();
  }

  testWidgets('saved servers can be added selected edited and deleted without changing the active session',
      (tester) async {
    final key = List.filled(43, 's').join();
    var probes = 0;
    final session = LocalMacSession(probe: (base, _) async {
      probes++;
      if (base.host == '100.64.0.2') throw LocalMacUnauthorized();
      return {'uid': 'alice'};
    });
    await openPage(tester, session);
    await enterField(tester, find.byKey(const ValueKey('local-mac-name')), 'Home Mac');
    await enterField(tester, find.byKey(const ValueKey('local-mac-address')), '100.64.0.1:21000');
    await enterField(tester, find.byKey(const ValueKey('local-mac-key')), key);
    await tapFormButton(tester, 'local-mac-connect');
    expect(probes, 1);
    expect(session.address, 'http://100.64.0.1:21000/');
    await tapFormButton(tester, 'local-mac-add-server');
    expect(tester.widget<TextField>(find.byKey(const ValueKey('local-mac-address'))).controller!.text, isEmpty);
    await enterField(tester, find.byKey(const ValueKey('local-mac-name')), 'Other Mac');
    await enterField(tester, find.byKey(const ValueKey('local-mac-address')), '100.64.0.2:21000');
    await enterField(tester, find.byKey(const ValueKey('local-mac-key')), key);
    await tapFormButton(tester, 'local-mac-save');
    expect((await session.readServers()).length, 2);
    expect(probes, 1);
    await enterField(tester, find.byKey(const ValueKey('local-mac-name')), 'Remote Mac');
    await tapFormButton(tester, 'local-mac-save');
    expect((await session.readServers()).length, 2);
    await tester.pumpWidget(const SizedBox());
    await openPage(tester, session);
    expect(tester.widget<TextField>(find.byKey(const ValueKey('local-mac-name'))).controller!.text, 'Remote Mac');
    await tapFormButton(tester, 'local-mac-connect');
    expect(session.address, 'http://100.64.0.1:21000/');
    expect(session.accessKey, key);
    await tapFormButton(tester, 'local-mac-servers');
    await tester.tap(find.text('Home Mac').last);
    await tester.pumpAndSettle();
    expect(tester.widget<TextField>(find.byKey(const ValueKey('local-mac-name'))).controller!.text, 'Home Mac');
    expect(probes, 2); // Selection never probes or switches.
    await tapFormButton(tester, 'local-mac-delete-server');
    expect((await session.readServers()).single.name, 'Remote Mac');
    expect(session.address, 'http://100.64.0.1:21000/');
    await tester.pumpWidget(const SizedBox());
    await openPage(tester, session);
    expect(tester.widget<TextField>(find.byKey(const ValueKey('local-mac-name'))).controller!.text, isEmpty);
    expect((await session.readServers()).single.name, 'Remote Mac');
    expect(tester.takeException(), isNull);
  });

  testWidgets('Tailscale address saves as a draft and connects using the explicit Docker port', (tester) async {
    var probes = 0;
    final session = LocalMacSession(probe: (base, _) async {
      probes++;
      expect(base.toString(), 'http://100.64.0.1:21000/');
      return {'uid': 'alice'};
    });
    await openPage(tester, session);
    await enterField(tester, find.byKey(const ValueKey('local-mac-address')), '100.64.0.1:21000');
    await enterField(tester, find.byKey(const ValueKey('local-mac-key')), List.filled(43, 's').join());
    await tapFormButton(tester, 'local-mac-save');
    expect(probes, 0);
    expect(session.isSignedIn, isFalse);
    expect((await session.readSettings()).address, '100.64.0.1:21000');
    await tapFormButton(tester, 'local-mac-connect');
    expect(probes, 1);
    expect(session.address, 'http://100.64.0.1:21000/');
    expect(session.isSignedIn, isTrue);
    expect(find.byType(LocalMacPage), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('save and reopen retains settings; eye reveals key and background hides it', (tester) async {
    final session = LocalMacSession(probe: (_, __) async => throw StateError('Saving must not probe'));
    await openPage(tester, session);
    final addressField = find.byKey(const ValueKey('local-mac-address'));
    final keyField = find.byKey(const ValueKey('local-mac-key'));
    await enterField(tester, addressField, 'https://synthetic.ngrok.app');
    await enterField(tester, keyField, 'synthetic-draft-key');
    await tester.tap(find.byKey(const ValueKey('local-mac-show-key')));
    await tester.pumpAndSettle();
    expect(tester.widget<TextField>(keyField).obscureText, isFalse);
    expect(find.byType(AlertDialog), findsNothing);
    expect(tester.widget<TextField>(keyField).controller!.text, 'synthetic-draft-key');
    await tester.tap(find.byKey(const ValueKey('local-mac-show-key')));
    await tester.pumpAndSettle();
    expect(tester.widget<TextField>(keyField).obscureText, isTrue);
    await tester.tap(find.byKey(const ValueKey('local-mac-show-key')));
    await tester.pumpAndSettle();
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.inactive);
    await tester.pumpAndSettle();
    expect(tester.widget<TextField>(keyField).obscureText, isTrue);
    expect(find.byType(LocalMacPage), findsOneWidget);
    expect(find.byType(AlertDialog), findsNothing);
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
    await tapFormButton(tester, 'local-mac-save');
    expect(session.isSignedIn, isFalse);
    await tester.pumpWidget(const SizedBox());
    await openPage(tester, LocalMacSession());
    expect(tester.widget<TextField>(addressField).controller!.text, 'https://synthetic.ngrok.app');
    expect(tester.widget<TextField>(keyField).controller!.text, 'synthetic-draft-key');
    expect(tester.widget<TextField>(keyField).obscureText, isTrue);
  });

  testWidgets('failed connection preserves entered settings across page reopen', (tester) async {
    final session = LocalMacSession(probe: (_, __) async => throw const SocketException('unavailable'));
    await openPage(tester, session);
    final key = List.filled(43, 's').join();
    await enterField(tester, find.byKey(const ValueKey('local-mac-address')), 'https://synthetic.ngrok.app');
    await enterField(tester, find.byKey(const ValueKey('local-mac-key')), key);
    await tapFormButton(tester, 'local-mac-connect');
    expect(session.isSignedIn, isFalse);
    expect(find.byKey(const ValueKey('local-mac-result')), findsOneWidget);
    expect(find.byType(LocalMacPage), findsOneWidget);
    await tester.pumpWidget(const SizedBox());
    await openPage(tester, LocalMacSession());
    expect(
      tester.widget<TextField>(find.byKey(const ValueKey('local-mac-address'))).controller!.text,
      'https://synthetic.ngrok.app',
    );
    expect(tester.widget<TextField>(find.byKey(const ValueKey('local-mac-key'))).controller!.text, key);
  });

  testWidgets('rechecking unchanged credentials refreshes status and keeps expanded details', (tester) async {
    var statusCalls = 0;
    final session = LocalMacSession(probe: (_, __) async => {'uid': 'alice'});
    final key = List.filled(43, 's').join();
    await session.connect('https://synthetic.ngrok.app', key);
    await tester.pumpWidget(
      MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: LocalMacPage(
          session: session,
          stopRecording: () async {},
          refreshConnection: () async {},
          statusFetcher: () {
            statusCalls++;
            return status();
          },
        ),
      ),
    );
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('local-runtime-toggle')));
    await tester.pumpAndSettle();
    final before = statusCalls;
    await tapFormButton(tester, 'local-mac-connect');
    expect(statusCalls, greaterThan(before));
    expect(find.byKey(const ValueKey('local-runtime-details')), findsOneWidget);
    expect(find.byType(LocalMacPage), findsOneWidget);
    await tester.ensureVisible(find.byKey(const ValueKey('local-mac-result')));
    expect(find.text('Connected'), findsOneWidget);
    final keyField = find.byKey(const ValueKey('local-mac-key'));
    await tester.ensureVisible(keyField);
    await enterField(tester, keyField, 'edited-draft');
    await tester.pumpAndSettle();
    expect(find.byKey(const ValueKey('local-mac-result')), findsNothing);
    expect(session.accessKey, key); // Editing does not switch the live origin/key.
    await tester.pumpWidget(const SizedBox());
  });

  testWidgets('pending check disables actions; rejection stays inline and can be retried', (tester) async {
    final probe = Completer<Map<String, dynamic>>();
    var attempts = 0;
    final session = LocalMacSession(probe: (_, __) => ++attempts == 1 ? probe.future : Future.value({'uid': 'alice'}));
    await openPage(tester, session);
    await enterField(tester, find.byKey(const ValueKey('local-mac-address')), 'https://synthetic.ngrok.app');
    await enterField(tester, find.byKey(const ValueKey('local-mac-key')), List.filled(43, 's').join());
    final connect = find.byKey(const ValueKey('local-mac-connect'));
    await tester.ensureVisible(connect);
    await tester.pumpAndSettle();
    await tester.tap(connect);
    await tester.pump();
    expect(tester.widget<FilledButton>(connect).onPressed, isNull);
    expect(tester.widget<OutlinedButton>(find.byKey(const ValueKey('local-mac-save'))).onPressed, isNull);
    expect(find.descendant(of: connect, matching: find.byType(CircularProgressIndicator)), findsOneWidget);
    expect(find.byType(LocalMacPage), findsOneWidget);
    probe.completeError(LocalMacUnauthorized());
    await tester.pumpAndSettle();
    await tester.ensureVisible(find.byKey(const ValueKey('local-mac-result')));
    expect(find.text('The access key was rejected. Check the key on your Mac.'), findsOneWidget);
    await tapFormButton(tester, 'local-mac-connect');
    await tester.ensureVisible(find.byKey(const ValueKey('local-mac-result')));
    expect(find.text('Connected'), findsOneWidget);
    expect(find.text('The access key was rejected. Check the key on your Mac.'), findsNothing);
    expect(find.byType(LocalMacPage), findsOneWidget);
    await tester.pumpWidget(const SizedBox());
  });

  testWidgets('form and actions fit a narrow phone with large text', (tester) async {
    tester.view.physicalSize = const Size(320, 780);
    tester.view.devicePixelRatio = 1;
    tester.platformDispatcher.textScaleFactorTestValue = 2;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    addTearDown(tester.platformDispatcher.clearTextScaleFactorTestValue);
    await openPage(tester, LocalMacSession());
    await tapFormButton(tester, 'local-mac-save');
    expect(tester.takeException(), isNull);
    await tester.pumpWidget(const SizedBox());
  });

  testWidgets('edits autosave without pressing a button and pasted whitespace is removed', (tester) async {
    await openPage(tester, LocalMacSession());
    final addressField = find.byKey(const ValueKey('local-mac-address'));
    final keyField = find.byKey(const ValueKey('local-mac-key'));
    await enterField(tester, addressField, 'https://synthetic.ngrok.app');
    await enterField(tester, keyField, ' pasted\nkey ');
    await tester.pumpAndSettle();
    expect(tester.widget<TextField>(keyField).controller!.text, 'pastedkey');
    await tester.pumpWidget(const SizedBox());
    await openPage(tester, LocalMacSession());
    expect(tester.widget<TextField>(addressField).controller!.text, 'https://synthetic.ngrok.app');
    expect(tester.widget<TextField>(keyField).controller!.text, 'pastedkey');
  });
}
