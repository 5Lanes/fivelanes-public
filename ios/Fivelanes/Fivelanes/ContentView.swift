import SwiftUI

struct ContentView: View {
    @EnvironmentObject private var webSession: WebSession
    @State private var isLoading = true
    @State private var errorMessage: String?

    private let dashboardURL = DashboardConfig.dashboardURL

    var body: some View {
        ZStack {
            Color(red: 16 / 255, green: 18 / 255, blue: 22 / 255)
                .ignoresSafeArea()

            if DashboardConfig.isConfigured {
                DashboardWebView(
                    url: dashboardURL,
                    reloadToken: webSession.reloadToken,
                    isLoading: $isLoading,
                    errorMessage: $errorMessage
                )
                .ignoresSafeArea(edges: .bottom)
            } else {
                configurationNeededView
            }

            if let errorMessage, !errorMessage.isEmpty {
                errorOverlay(message: errorMessage)
            } else if isLoading, DashboardConfig.isConfigured {
                ProgressView()
                    .tint(Color(red: 61 / 255, green: 191 / 255, blue: 176 / 255))
                    .scaleEffect(1.1)
            }
        }
        .preferredColorScheme(.dark)
    }

    private var configurationNeededView: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Fivelanes")
                .font(.largeTitle.bold())

            Text("Set your Tailscale dashboard URL in Info.plist:")
                .foregroundStyle(.secondary)

            Text("FIVELANES_DASHBOARD_URL")
                .font(.system(.body, design: .monospaced))
                .padding(12)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Color.white.opacity(0.08))
                .clipShape(RoundedRectangle(cornerRadius: 10))

            Text("Example:\nhttp://your-machine.your-tailnet.ts.net:8000/dashboard")
                .font(.footnote)
                .foregroundStyle(.secondary)

            Text("Open ios/README.md in the repo for setup steps.")
                .font(.footnote)
                .foregroundStyle(.secondary)
        }
        .padding(24)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }

    private func errorOverlay(message: String) -> some View {
        VStack {
            Spacer()
            VStack(alignment: .leading, spacing: 12) {
                Text("Can't reach Fivelanes")
                    .font(.headline)

                Text(message)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)

                Text("Make sure Tailscale is connected on this iPhone and dashboard_server.py is running.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)

                HStack {
                    Button("Retry") {
                        errorMessage = nil
                        webSession.reload()
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(Color(red: 61 / 255, green: 191 / 255, blue: 176 / 255))

                    Button("Dismiss") {
                        errorMessage = nil
                    }
                    .buttonStyle(.bordered)
                }
            }
            .padding(16)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(.ultraThinMaterial)
            .clipShape(RoundedRectangle(cornerRadius: 14))
            .padding()
        }
    }
}

#Preview {
    ContentView()
        .environmentObject(WebSession())
}
