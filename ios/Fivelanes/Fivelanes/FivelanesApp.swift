import SwiftUI

@main
struct FivelanesApp: App {
    @Environment(\.scenePhase) private var scenePhase
    @StateObject private var webSession = WebSession()

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
