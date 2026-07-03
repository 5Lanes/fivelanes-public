import Foundation

@MainActor
final class WebSession: ObservableObject {
    @Published private(set) var reloadToken = UUID()

    func reload() {
        reloadToken = UUID()
    }

    /// Soft refresh when returning from background — avoids losing in-page state when possible.
    func handleBecameActive() {
        NotificationCenter.default.post(name: .fivelanesWebViewSoftRefresh, object: nil)
    }
}

extension Notification.Name {
    static let fivelanesWebViewSoftRefresh = Notification.Name("fivelanesWebViewSoftRefresh")
}
