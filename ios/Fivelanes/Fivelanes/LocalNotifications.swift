import UserNotifications

enum LocalNotifications {
    static func requestAuthorizationIfNeeded() {
        UNUserNotificationCenter.current().getNotificationSettings { settings in
            guard settings.authorizationStatus == .notDetermined else { return }
            UNUserNotificationCenter.current().requestAuthorization(options: [.badge]) { _, _ in }
        }
    }

    static func setBadgeCount(_ count: Int) {
        UNUserNotificationCenter.current().setBadgeCount(max(0, count))
    }
}
