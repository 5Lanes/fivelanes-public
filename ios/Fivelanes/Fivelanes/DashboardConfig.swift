import Foundation

enum DashboardConfig {
  /// Set `FIVELANES_DASHBOARD_URL` in `ios/Config.plist` (copy from `Config.example.plist`).
  static var dashboardURL: URL {
    guard let raw = configuredURLString,
          let url = URL(string: raw),
          let host = url.host,
          !host.isEmpty
    else {
      return URL(string: "http://localhost:8000/onebox")!
    }
    return url
  }

  static var isConfigured: Bool {
    guard let raw = configuredURLString,
          let url = URL(string: raw),
          let host = url.host,
          !host.isEmpty
    else {
      return false
    }
    let upper = host.uppercased()
    if host == "localhost" || upper.contains("YOUR-MACHINE") || upper.contains("YOUR-TAILNET") {
      return false
    }
    return true
  }

  static var dashboardHost: String? {
    dashboardURL.host
  }

  private static var configuredURLString: String? {
    guard let url = Bundle.main.url(forResource: "Config", withExtension: "plist"),
          let dict = NSDictionary(contentsOf: url) as? [String: Any],
          let raw = dict["FIVELANES_DASHBOARD_URL"] as? String
    else {
      return nil
    }
    let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
    return trimmed.isEmpty ? nil : trimmed
  }
}
