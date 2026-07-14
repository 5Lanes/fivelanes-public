import SwiftUI
import UserNotifications

@main
struct FivelanesApp: App {
    @Environment(\.scenePhase) private var scenePhase
    @StateObject private var webSession = WebSession()

    init() {
        LocalNotifications.requestAuthorizationIfNeeded()
    }

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(webSession)
        }
        .onChange(of: scenePhase) { _, newPhase in
            if newPhase == .active {
                webSession.handleBecameActive()
            }
        }
    }
}
