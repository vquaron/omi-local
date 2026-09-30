// Framework doubles for executing the real SceneDelegate on the macOS host.
// This tests Omi's event routing, not UIKit or Flutter engine startup.
import Foundation

var events: [String] = []
class UIScene {
  struct ConnectionOptions {
    var urlContexts: Set<UIOpenURLContext> = []
    var userActivities: Set<NSUserActivity> = []
  }
}
class UISceneSession {}
struct UIOpenURLContext: Hashable { let url: URL }
class UIApplication {
  static let shared = UIApplication()
  var delegate: AnyObject? = AppDelegate()
}
class AppDelegate {
  static let unusedForegroundTaskRefreshIdentifier = "unused-refresh"
  func handleRayBanMetaURL(_ url: URL) -> Bool {
    if url.scheme == "rayban" { events.append("rayban"); return true }
    return false
  }
}
class AppLinks {
  static let shared = AppLinks()
  func handleLink(url: URL) { events.append("link:" + url.absoluteString) }
}
class BGTaskScheduler {
  static let shared = BGTaskScheduler()
  func cancel(taskRequestWithIdentifier identifier: String) { events.append("cancel:" + identifier) }
}
class OmiBleManager {
  static let shared = OmiBleManager()
  func markBackgroundTelemetryStart() { events.append("ble-background") }
  func markBackgroundTelemetryEnd() { events.append("ble-active") }
  func reconnectStalePeripherals() { events.append("ble-reconnect") }
}
class FlutterSceneDelegate {
  func scene(_ scene: UIScene, willConnectTo session: UISceneSession, options: UIScene.ConnectionOptions) {
    events.append("engine-connect")
  }
  func scene(_ scene: UIScene, openURLContexts contexts: Set<UIOpenURLContext>) {
    for context in contexts.sorted(by: { $0.url.absoluteString < $1.url.absoluteString }) {
      events.append("plugin-url:" + context.url.absoluteString)
    }
  }
  func scene(_ scene: UIScene, continue activity: NSUserActivity) { events.append("plugin-activity") }
  func sceneDidEnterBackground(_ scene: UIScene) { events.append("plugin-background") }
  func sceneDidBecomeActive(_ scene: UIScene) { events.append("plugin-active") }
  func sceneWillEnterForeground(_ scene: UIScene) { events.append("plugin-foreground") }
}

@main struct SceneLifecycleTests {
  static func expect(_ expected: [String], _ label: String) {
    precondition(events == expected, "\(label): \(events) != \(expected)")
    events = []
  }
  static func main() {
    let delegate = SceneDelegate()
    let scene = UIScene()
    let session = UISceneSession()
    let custom = UIOpenURLContext(url: URL(string: "omi://synthetic")!)
    let rayban = UIOpenURLContext(url: URL(string: "rayban://synthetic")!)
    let web = NSUserActivity(activityType: NSUserActivityTypeBrowsingWeb)
    web.webpageURL = URL(string: "https://example.invalid/synthetic")!

    delegate.scene(scene, willConnectTo: session, options: .init())
    expect(["engine-connect"], "plain launch")
    delegate.scene(scene, willConnectTo: session, options: .init(urlContexts: [custom]))
    expect(["engine-connect", "link:omi://synthetic"], "cold custom link initializes engine first")
    delegate.scene(scene, willConnectTo: session, options: .init(userActivities: [web]))
    expect(["engine-connect", "link:https://example.invalid/synthetic"], "cold universal link")
    delegate.scene(scene, willConnectTo: session, options: .init(urlContexts: [rayban]))
    expect(["engine-connect", "rayban"], "cold Meta callback")
    delegate.scene(scene, openURLContexts: [custom])
    expect(["link:omi://synthetic", "plugin-url:omi://synthetic"], "warm link and plugin propagation")
    delegate.scene(scene, openURLContexts: [rayban])
    expect(["rayban"], "Meta consumes its callback")
    delegate.scene(scene, continue: web)
    expect(["link:https://example.invalid/synthetic", "plugin-activity"], "warm universal link")
    let unrelated = NSUserActivity(activityType: "synthetic-restoration")
    unrelated.webpageURL = web.webpageURL
    delegate.scene(scene, continue: unrelated)
    expect(["plugin-activity"], "non-browsing activity is not a link")
    delegate.sceneDidEnterBackground(scene)
    expect(["plugin-background", "ble-background", "cancel:unused-refresh"], "cancel after plugin scheduling")
    delegate.sceneDidBecomeActive(scene)
    expect(["ble-active", "plugin-active"], "active telemetry")
    delegate.sceneWillEnterForeground(scene)
    expect(["plugin-foreground", "ble-reconnect"], "foreground reconnect")
    print("11 scene routing scenarios passed")
  }
}
