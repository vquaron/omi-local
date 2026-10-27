import 'package:flutter/material.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/env/dev_env.dart';
import 'package:omi/env/env.dart';
import 'package:omi/l10n/app_localizations.dart';
import 'package:omi/mobile/mobile_app.dart';
import 'package:omi/pages/onboarding/auth.dart';
import 'package:omi/pages/onboarding/device_selection.dart';
import 'package:omi/pages/onboarding/wrapper.dart';
import 'package:omi/providers/auth_provider.dart';

class _SignedOutAuthenticationProvider extends AuthenticationProvider {
  _SignedOutAuthenticationProvider() : super(initializeListeners: false);

  @override
  bool get requiresReauthentication => false;

  @override
  bool isSignedIn() => false;
}

Widget _app(Widget home, AuthenticationProvider provider) => ChangeNotifierProvider<AuthenticationProvider>.value(
      value: provider,
      child: MaterialApp(
        theme: ThemeData(useMaterial3: false, colorScheme: const ColorScheme.dark(primary: Colors.black)),
        localizationsDelegates: const [
          AppLocalizations.delegate,
          GlobalMaterialLocalizations.delegate,
          GlobalWidgetsLocalizations.delegate,
          GlobalCupertinoLocalizations.delegate,
        ],
        supportedLocales: AppLocalizations.supportedLocales,
        home: home,
      ),
    );

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  setUpAll(() {
    Env.init(DevEnv());
    Env.setRuntimeModeForTesting(OmiRuntimeMode.offline);
  });
  tearDownAll(() => Env.setRuntimeModeForTesting(null));
  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
  });
  tearDown(() => Env.setPhysicalIosDeviceForTesting(null));

  testWidgets('a physical iPhone gets a visible Local Mac button and no emulator sign-in', (tester) async {
    Env.setPhysicalIosDeviceForTesting(true);
    final provider = _SignedOutAuthenticationProvider();
    addTearDown(provider.dispose);
    await tester.pumpWidget(_app(Scaffold(body: AuthComponent(onSignIn: () {})), provider));
    await tester.pump();

    final open = find.byKey(const ValueKey('local-mac-open'));
    expect(open, findsOneWidget);
    expect(find.descendant(of: open, matching: find.text('Local Mac')), findsOneWidget);
    // The pairing button is the primary action: white on the black card, not theme-primary (black) text.
    final style = tester.widget<ButtonStyleButton>(open).style!;
    expect(style.backgroundColor!.resolve({}), Colors.white);
    expect(style.foregroundColor!.resolve({}), Colors.black);
    expect(find.byKey(const ValueKey('offline-local-sign-in')), findsNothing);
  });

  testWidgets('the simulator keeps the loopback emulator sign-in as a secondary action', (tester) async {
    Env.setPhysicalIosDeviceForTesting(false);
    final provider = _SignedOutAuthenticationProvider();
    addTearDown(provider.dispose);
    await tester.pumpWidget(_app(Scaffold(body: AuthComponent(onSignIn: () {})), provider));
    await tester.pump();

    expect(find.byKey(const ValueKey('local-mac-open')), findsOneWidget);
    expect(find.byKey(const ValueKey('offline-local-sign-in')), findsOneWidget);
  });

  testWidgets('offline runtime opens the sign-in page directly instead of the welcome video', (tester) async {
    Env.setPhysicalIosDeviceForTesting(true);
    final provider = _SignedOutAuthenticationProvider();
    addTearDown(provider.dispose);
    await tester.pumpWidget(_app(const MobileApp(), provider));
    await tester.pump();

    expect(find.byType(DeviceSelectionPage), findsNothing);
    expect(find.byType(OnboardingWrapper), findsOneWidget);
    expect(find.byKey(const ValueKey('local-mac-open')), findsOneWidget);
    await tester.pumpAndSettle();
  });
}
