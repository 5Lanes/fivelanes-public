import Foundation

enum DashboardConfig {
  /// Set `FIVELANES_DASHBOARD_URL` in Info.plist to your Tailscale dashboard URL, e.g.
  /// `http://your-machine.your-tailnet.ts.net:8000/dashboard`
  static var dashboardURL: URL {
    let raw = Bundle.main.object(forInfoDictionaryKey: "FIVELANES_DASHBOARD_URL") as? String
    let trimmed = raw?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    guard !trimmed.isEmpty, let url = URL(string: trimmed), let host = url.host, !host.isEmpty else {
      return URL(string: "http://localhost:8000/dashboard")!
    }
    return url
  }

  static var isConfigured: Bool {
    let raw = Bundle.main.object(forInfoDictionaryKey: "FIVELANES_DASHBOARD_URL") as? String
    let trimmed = raw?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    guard !trimmed.isEmpty, let url = URL(string: trimmed), let host = url.host else {
      return false
    }
    return host != "localhost"
  }

  static var dashboardHost: String? {
    dashboardURL.host
  }
}
