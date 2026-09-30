import UIKit
import Flutter
import BackgroundTasks
import app_links

class SceneDelegate: FlutterSceneDelegate {
  override func scene(
    _ scene: UIScene,
    willConnectTo session: UISceneSession,
    options connectionOptions: UIScene.ConnectionOptions
  ) {
    // This initializes the implicit engine and registers its plugin delegates.
    // Always forward: a cold-start link must not skip native channel setup.
    super.scene(scene, willConnectTo: session, options: connectionOptions)
    for context in connectionOptions.urlContexts {
      handleURL(context.url)
    }
    for activity in connectionOptions.userActivities {
      handleUserActivity(activity)
    }
  }

  override func scene(_ scene: UIScene, openURLContexts URLContexts: Set<UIOpenURLContext>) {
    let unhandled = Set(URLContexts.filter { !handleURL($0.url) })
    if !unhandled.isEmpty {
      super.scene(scene, openURLContexts: unhandled)
    }
  }

  override func scene(_ scene: UIScene, continue userActivity: NSUserActivity) {
    handleUserActivity(userActivity)
    super.scene(scene, continue: userActivity)
  }

  // app_links 6.x only implements UIApplicationDelegate callbacks. Feed its
  // initial-link/event storage from both cold and warm scene events explicitly.
  @discardableResult
  private func handleURL(_ url: URL) -> Bool {
    if let delegate = UIApplication.shared.delegate as? AppDelegate,
       delegate.handleRayBanMetaURL(url) {
      return true
    }
    AppLinks.shared.handleLink(url: url)
    return false
  }

  private func handleUserActivity(_ activity: NSUserActivity) {
    if activity.activityType == NSUserActivityTypeBrowsingWeb, let url = activity.webpageURL {
      AppLinks.shared.handleLink(url: url)
    }
  }

  override func sceneDidEnterBackground(_ scene: UIScene) {
    super.sceneDidEnterBackground(scene)
    OmiBleManager.shared.markBackgroundTelemetryStart()
    // The foreground-task plugin schedules this during super's dispatch.
    BGTaskScheduler.shared.cancel(
      taskRequestWithIdentifier: AppDelegate.unusedForegroundTaskRefreshIdentifier
    )
  }

  override func sceneDidBecomeActive(_ scene: UIScene) {
    OmiBleManager.shared.markBackgroundTelemetryEnd()
    super.sceneDidBecomeActive(scene)
  }

  override func sceneWillEnterForeground(_ scene: UIScene) {
    super.sceneWillEnterForeground(scene)
    OmiBleManager.shared.reconnectStalePeripherals()
  }
}
