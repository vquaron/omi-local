"""UIScene tripwires, scene routing with doubles, and generated iOS manifests.

These guards catch the legacy startup regression; they do not prove an iPhone
launch. Contract: https://docs.flutter.dev/release/breaking-changes/uiscenedelegate
"""
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
APP = ROOT / 'app'


def swift_body(source, signature):
    """Select a whole Swift method, including its nested blocks."""
    start = source.index('{', source.index(signature)) + 1
    depth = 1
    for end in range(start, len(source)):
        depth += (source[end] == '{') - (source[end] == '}')
        if depth == 0:
            return source[start:end]
    raise AssertionError(f'Unclosed method: {signature}')


def assert_scene_manifest(info):
    manifest = info['UIApplicationSceneManifest']
    assert manifest['UIApplicationSupportsMultipleScenes'] is False
    scenes = manifest['UISceneConfigurations']['UIWindowSceneSessionRoleApplication']
    assert len(scenes) == 1
    assert scenes[0]['UISceneClassName'] == 'UIWindowScene'
    assert scenes[0]['UISceneDelegateClassName'] == '$(PRODUCT_MODULE_NAME).SceneDelegate'
    assert scenes[0]['UISceneStoryboardFile'] == 'Main'
    assert scenes[0]['UISceneConfigurationName'] == 'flutter'
    assert info['FlutterDeepLinkingEnabled'] is False


def test_scene_manifest_and_all_app_target_membership():
    for name in ('Info.plist', 'Info-Dev.plist'):
        assert_scene_manifest(plistlib.loads((APP / 'ios/Runner' / name).read_bytes()))
    project = (APP / 'ios/Runner.xcodeproj/project.pbxproj').read_text()
    phases = re.findall(r'isa = PBXSourcesBuildPhase;.*?files = \((.*?)\);', project, re.S)
    app_phases = [phase for phase in phases if 'AppDelegate.swift in Sources' in phase]
    assert len(app_phases) == 2
    assert all(phase.count('SceneDelegate.swift in Sources') == 1 for phase in app_phases)
    assert 'path = SceneDelegate.swift;' in project


def test_static_tripwire_engine_owns_all_native_channel_registration():
    source = (APP / 'ios/Runner/AppDelegate.swift').read_text()
    launch = swift_body(source, 'didFinishLaunchingWithOptions launchOptions:')
    assert 'FlutterImplicitEngineDelegate' in source
    for forbidden in ('window', 'rootViewController', 'FlutterViewController',
                      'FlutterMethodChannel', 'controller!', 'AppLinks', 'setUp('):
        assert forbidden not in launch
    assert 'rootViewController' not in source
    assert 'GeneratedPluginRegistrant.register(with: self)' not in source
    engine = swift_body(source, 'func didInitializeImplicitFlutterEngine(')
    assert 'GeneratedPluginRegistrant.register(with: engineBridge.pluginRegistry)' in engine
    assert 'messenger: engineBridge.applicationRegistrar.messenger()' in engine
    assert 'registry: engineBridge.pluginRegistry' in engine
    channels = swift_body(source, 'private func configureFlutterChannels(')
    for api in ('BleHostApiSetup', 'PhoneMicHostApiSetup', 'WatchRecorderHostAPISetup',
                'RayBanMetaHostAPISetup'):
        assert f'{api}.setUp(binaryMessenger: messenger,' in channels
    for channel in ('com.friend.ios/notifyOnKill', 'com.omi.apple_reminders',
                    'com.omi.apple_health', 'com.omi.ios/speech', 'com.omi/environment',
                    'com.omi.ios/audioSession', 'com.omi.battery_widget'):
        assert f'name: "{channel}", binaryMessenger: messenger' in channels
    assert 'WifiNetworkPlugin(messenger: messenger)' in channels
    assert 'registry.registrar(forPlugin: "OmiPhoneCallsPlugin")' in channels
    assert 'takeLocalMacSettings' in channels
    assert 'localMacSettings = nil' in channels
    assert 'unsetenv("OMI_LOCAL_MAC_KEY")' in channels


def test_static_tripwire_process_setup_stays_before_launch_returns():
    source = (APP / 'ios/Runner/AppDelegate.swift').read_text()
    launch = swift_body(source, 'didFinishLaunchingWithOptions launchOptions:')
    assert 'setPluginRegistrantCallback' in launch
    assert 'registerAppRefreshForBackgroundLaunch()' in launch
    assert 'UNUserNotificationCenter.current().delegate = self' in launch
    assert 'super.application(application, didFinishLaunchingWithOptions: launchOptions)' in launch
    for retired in ('applicationDidEnterBackground(', 'applicationDidBecomeActive(',
                    'applicationWillEnterForeground('):
        assert retired not in source


def test_static_tripwire_scene_forwards_plugins_ble_and_cold_and_warm_links():
    source = (APP / 'ios/Runner/SceneDelegate.swift').read_text()
    assert 'class SceneDelegate: FlutterSceneDelegate' in source
    cold = swift_body(source, 'willConnectTo session:')
    assert 'super.scene(scene, willConnectTo: session, options: connectionOptions)' in cold
    assert 'connectionOptions.urlContexts' in cold and 'handleURL(context.url)' in cold
    assert 'connectionOptions.userActivities' in cold and 'handleUserActivity(activity)' in cold
    warm = swift_body(source, 'openURLContexts URLContexts:')
    assert 'handleURL($0.url)' in warm
    assert 'super.scene(scene, openURLContexts: unhandled)' in warm
    universal = swift_body(source, 'continue userActivity:')
    assert 'handleUserActivity(userActivity)' in universal
    assert 'super.scene(scene, continue: userActivity)' in universal
    assert 'delegate.handleRayBanMetaURL(url)' in source
    assert 'AppLinks.shared.handleLink(url: url)' in source
    background = swift_body(source, 'override func sceneDidEnterBackground(')
    assert background.index('super.sceneDidEnterBackground') < background.index('BGTaskScheduler.shared.cancel')
    assert 'markBackgroundTelemetryStart()' in background
    assert 'markBackgroundTelemetryEnd()' in swift_body(source, 'override func sceneDidBecomeActive(')
    assert 'reconnectStalePeripherals()' in swift_body(source, 'override func sceneWillEnterForeground(')


@pytest.mark.skipif(sys.platform != 'darwin', reason='production plist generator uses PlistBuddy')
@pytest.mark.parametrize('mode', ['standard', 'personal'])
def test_generated_plist_preserves_scene_lifecycle(tmp_path, mode):
    output = tmp_path / 'Info.plist'
    subprocess.run(['bash', str(APP / 'scripts/generate_ios_dev_info_plist.sh'),
                    str(APP / 'ios/Runner/Info.plist'), str(output), mode],
                   check=True, capture_output=True, text=True)
    info = plistlib.loads(output.read_bytes())
    assert_scene_manifest(info)
    if mode == 'personal':
        assert 'BGTaskSchedulerPermittedIdentifiers' not in info
        assert info['UIBackgroundModes'] == ['audio', 'bluetooth-central']


@pytest.mark.skipif(sys.platform != 'darwin', reason='host Swift/Foundation required')
def test_scene_routing_executes_production_delegate_with_framework_doubles(tmp_path):
    compiler = shutil.which('swiftc')
    assert compiler, 'Swift is required for the macOS native routing test'
    source = (APP / 'ios/Runner/SceneDelegate.swift').read_text()
    # Substitute only framework imports. Compile the production class unchanged.
    source = re.sub(r'^import (UIKit|Flutter|BackgroundTasks|app_links)\n', '', source, flags=re.M)
    delegate = tmp_path / 'SceneDelegate.swift'
    delegate.write_text('import Foundation\n' + source)
    executable = tmp_path / 'scene-routing-test'
    harness = Path(__file__).parent / 'fixtures/scene_lifecycle_harness.swift'
    compiled = subprocess.run([compiler, '-swift-version', '5', '-module-cache-path',
                               str(tmp_path / 'modules'), str(harness), str(delegate),
                               '-o', str(executable)], capture_output=True, text=True, timeout=90)
    assert compiled.returncode == 0, compiled.stderr
    result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert '11 scene routing scenarios passed' in result.stdout
