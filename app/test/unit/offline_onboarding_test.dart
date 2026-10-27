import 'package:flutter_test/flutter_test.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/env/dev_env.dart';
import 'package:omi/env/env.dart';
import 'package:omi/services/auth/auth_token_result.dart';
import 'package:omi/services/auth/local_mac_session.dart';
import 'package:omi/services/auth_service.dart';
import 'package:omi/utils/offline_network_policy.dart';
import 'package:omi/utils/offline_onboarding.dart';

class _SignOutGateway implements AuthTokenGateway {
  var signOutCount = 0;

  @override
  AuthUserSnapshot? get currentUser => null;

  @override
  Future<RefreshedAuthToken?> forceRefresh() async => null;

  @override
  Future<void> signOut() async {
    signOutCount++;
  }
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  setUpAll(() => Env.init(DevEnv()));
  tearDown(() {
    Env.setRuntimeModeForTesting(null);
    Env.localTunnelConfigured = false;
    Env.clearApiBaseUrlOverrideForTesting();
    OfflineNetworkPolicy.resetForTesting();
  });
  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    FlutterSecureStorage.setMockInitialValues({});
    await SharedPreferencesUtil.init();
    Env.localTunnelConfigured = false;
  });

  test('offline runtime skips the cloud onboarding steps but still requires permissions', () {
    Env.setRuntimeModeForTesting(OmiRuntimeMode.offline);
    applyOfflineOnboardingDefaults();
    expect(SharedPreferencesUtil().aiConsentGiven, isTrue);
    expect(SharedPreferencesUtil().onboardingCompleted, isTrue);
    expect(SharedPreferencesUtil().permissionsCompleted, isFalse);
  });

  test('standard runtime keeps its onboarding state untouched', () {
    Env.setRuntimeModeForTesting(OmiRuntimeMode.standard);
    applyOfflineOnboardingDefaults();
    expect(SharedPreferencesUtil().aiConsentGiven, isFalse);
    expect(SharedPreferencesUtil().onboardingCompleted, isFalse);
  });

  for (final paired in [false, true]) {
    test(
      'offline ${paired ? 'Local Mac' : 'emulator'} re-entry keeps cloud onboarding skipped after sign-out',
      () async {
        Env.setRuntimeModeForTesting(OmiRuntimeMode.offline);
        applyOfflineOnboardingDefaults();
        Env.localTunnelConfigured = paired;
        // Match Settings: clearing preferences precedes the production sign-out.
        await SharedPreferencesUtil().clear();
        expect(SharedPreferencesUtil().aiConsentGiven, isFalse);
        expect(SharedPreferencesUtil().onboardingCompleted, isFalse);

        final gateway = _SignOutGateway();
        await AuthService.forTesting(tokenGateway: gateway).signOut();
        expect(gateway.signOutCount, paired ? 0 : 1);
        if (paired) {
          final session = LocalMacSession(probe: (_, __) async => {'uid': 'alice'});
          addTearDown(session.dispose);
          await session.connect('https://synthetic.ngrok.app/', List.filled(43, 's').join());
          expect(session.isSignedIn, isTrue);
        }
        expect(SharedPreferencesUtil().aiConsentGiven, isTrue);
        expect(SharedPreferencesUtil().onboardingCompleted, isTrue);
        expect(SharedPreferencesUtil().permissionsCompleted, isFalse);
      },
    );
  }

  test('standard sign-out does not grant cloud consent or finish onboarding', () async {
    Env.setRuntimeModeForTesting(OmiRuntimeMode.standard);
    await AuthService.forTesting(tokenGateway: _SignOutGateway()).signOut();
    expect(SharedPreferencesUtil().aiConsentGiven, isFalse);
    expect(SharedPreferencesUtil().onboardingCompleted, isFalse);
    expect(SharedPreferencesUtil().permissionsCompleted, isFalse);
  });
}
