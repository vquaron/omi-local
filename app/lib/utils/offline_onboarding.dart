import 'package:omi/backend/preferences.dart';
import 'package:omi/env/env.dart';

/// The offline runtime has one synthetic local owner and no cloud AI processing,
/// so the consent, profile and knowledge-graph onboarding steps carry no decision.
/// Runtime permissions (microphone, Bluetooth, notifications) are still requested.
void applyOfflineOnboardingDefaults() {
  if (!Env.isOfflineRuntime) return;
  SharedPreferencesUtil().aiConsentGiven = true;
  SharedPreferencesUtil().onboardingCompleted = true;
}
