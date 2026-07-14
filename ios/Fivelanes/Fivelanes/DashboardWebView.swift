import SwiftUI
import WebKit

struct DashboardWebView: UIViewRepresentable {
    let url: URL
    let reloadToken: UUID

    @Binding var isLoading: Bool
    @Binding var errorMessage: String?

    func makeCoordinator() -> Coordinator {
        Coordinator(parent: self)
    }

    func makeUIView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.defaultWebpagePreferences.allowsContentJavaScript = true
        configuration.userContentController.add(context.coordinator, name: "fivelanesSetBadge")

        let webView = WKWebView(frame: .zero, configuration: configuration)
        webView.isOpaque = false
        webView.backgroundColor = UIColor(red: 16 / 255, green: 18 / 255, blue: 22 / 255, alpha: 1)
        webView.scrollView.backgroundColor = webView.backgroundColor
        webView.navigationDelegate = context.coordinator
        webView.uiDelegate = context.coordinator
        webView.allowsBackForwardNavigationGestures = true

        let refreshControl = UIRefreshControl()
        refreshControl.addTarget(
            context.coordinator,
            action: #selector(Coordinator.handleRefresh(_:)),
            for: .valueChanged
        )
        webView.scrollView.refreshControl = refreshControl
        context.coordinator.webView = webView
        context.coordinator.refreshControl = refreshControl

        NotificationCenter.default.addObserver(
            context.coordinator,
            selector: #selector(Coordinator.handleSoftRefresh),
            name: .fivelanesWebViewSoftRefresh,
            object: nil
        )

        context.coordinator.load(url: url)
        return webView
    }

    func updateUIView(_ webView: WKWebView, context: Context) {
        if context.coordinator.lastReloadToken != reloadToken {
            context.coordinator.lastReloadToken = reloadToken
            context.coordinator.load(url: url)
        }
    }

    static func dismantleUIView(_ uiView: WKWebView, coordinator: Coordinator) {
        NotificationCenter.default.removeObserver(coordinator)
        uiView.configuration.userContentController.removeScriptMessageHandler(forName: "fivelanesSetBadge")
    }

    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
        let parent: DashboardWebView
        weak var webView: WKWebView?
        weak var refreshControl: UIRefreshControl?
        var lastReloadToken: UUID?

        init(parent: DashboardWebView) {
            self.parent = parent
            self.lastReloadToken = parent.reloadToken
        }

        func load(url: URL) {
            parent.errorMessage = nil
            parent.isLoading = true
            webView?.load(URLRequest(url: url, cachePolicy: .useProtocolCachePolicy))
        }

        @objc func handleRefresh(_ sender: UIRefreshControl) {
            webView?.reload()
        }

        func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
            guard message.name == "fivelanesSetBadge",
                  let payload = message.body as? [String: Any],
                  let count = payload["count"] as? Int else { return }
            LocalNotifications.setBadgeCount(count)
        }

        @objc func handleSoftRefresh() {
            guard let webView else { return }
            if webView.url == nil {
                load(url: parent.url)
                return
            }
            webView.evaluateJavaScript("document.visibilityState") { result, _ in
                if let state = result as? String, state == "visible" {
                    return
                }
                webView.reload()
            }
        }

        func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation!) {
            parent.isLoading = true
            parent.errorMessage = nil
        }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            parent.isLoading = false
            refreshControl?.endRefreshing()
        }

        func webView(
            _ webView: WKWebView,
            didFail navigation: WKNavigation!,
            withError error: Error
        ) {
            parent.isLoading = false
            refreshControl?.endRefreshing()
            parent.errorMessage = error.localizedDescription
        }

        func webView(
            _ webView: WKWebView,
            didFailProvisionalNavigation navigation: WKNavigation!,
            withError error: Error
        ) {
            parent.isLoading = false
            refreshControl?.endRefreshing()
            let nsError = error as NSError
            if nsError.domain == NSURLErrorDomain, nsError.code == NSURLErrorCancelled {
                return
            }
            parent.errorMessage = error.localizedDescription
        }

        func webView(
            _ webView: WKWebView,
            decidePolicyFor navigationAction: WKNavigationAction,
            decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
        ) {
            guard let requestURL = navigationAction.request.url else {
                decisionHandler(.allow)
                return
            }

            if let scheme = requestURL.scheme?.lowercased(),
               scheme == "mailto" || scheme == "tel" || scheme == "sms" {
                UIApplication.shared.open(requestURL)
                decisionHandler(.cancel)
                return
            }

            if navigationAction.targetFrame == nil {
                openExternallyIfNeeded(requestURL)
                decisionHandler(.cancel)
                return
            }

            if shouldOpenExternally(requestURL) {
                UIApplication.shared.open(requestURL)
                decisionHandler(.cancel)
                return
            }

            decisionHandler(.allow)
        }

        func webView(
            _ webView: WKWebView,
            createWebViewWith configuration: WKWebViewConfiguration,
            for navigationAction: WKNavigationAction,
            windowFeatures: WKWindowFeatures
        ) -> WKWebView? {
            if let requestURL = navigationAction.request.url {
                openExternallyIfNeeded(requestURL)
            }
            return nil
        }

        private func shouldOpenExternally(_ requestURL: URL) -> Bool {
            guard let scheme = requestURL.scheme?.lowercased(), scheme == "http" || scheme == "https" else {
                return false
            }
            guard let dashboardHost = DashboardConfig.dashboardHost?.lowercased(),
                  let requestHost = requestURL.host?.lowercased() else {
                return false
            }
            if requestHost == dashboardHost { return false }
            if requestHost.hasSuffix(".ts.net"), dashboardHost.hasSuffix(".ts.net") {
                return requestHost != dashboardHost
            }
            return true
        }

        private func openExternallyIfNeeded(_ requestURL: URL) {
            guard shouldOpenExternally(requestURL) else {
                webView?.load(URLRequest(url: requestURL))
                return
            }
            UIApplication.shared.open(requestURL)
        }
    }
}
