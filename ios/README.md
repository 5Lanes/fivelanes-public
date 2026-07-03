# Fivelanes iOS app

Private iPhone wrapper around the self-hosted Fivelanes dashboard. It loads your existing web UI over **Tailscale** in a `WKWebView`, so the session survives app switching better than a Safari tab.

## Prerequisites

- Mac with **Xcode 16+** (iOS 17 deployment target)
- iPhone with **Tailscale** connected to the same tailnet as your Fivelanes server
- `dashboard_server.py` running on the server (`DASHBOARD_HOST=0.0.0.0`, port `8000`)
- The dashboard URL already works in Safari on the iPhone (sanity check first)

## 1. Configure the dashboard URL

Edit [`Fivelanes/Info.plist`](Fivelanes/Fivelanes/Info.plist) and set:

```xml
<key>FIVELANES_DASHBOARD_URL</key>
<string>http://your-machine.your-tailnet.ts.net:8000/dashboard</string>
```

Use your Tailscale **MagicDNS** hostname (recommended) or `http://100.x.x.x:8000/dashboard`.

`Config.example.plist` is a copy-paste reference for the same value.

### HTTP / App Transport Security

Plain HTTP is allowed for:

- `*.ts.net` (Tailscale MagicDNS)
- local network addresses (`NSAllowsLocalNetworking`)

If you use a raw Tailscale IP and ATS blocks it, either switch to MagicDNS or add a narrow exception in `Info.plist` under `NSAppTransportSecurity` → `NSExceptionDomains`.

Optional: use **Tailscale Serve** for HTTPS and avoid HTTP exceptions:

```bash
tailscale serve http / http://127.0.0.1:8000
```

Then set `FIVELANES_DASHBOARD_URL` to the HTTPS URL Tailscale prints.

## 2. Open in Xcode

```bash
open ios/Fivelanes/Fivelanes.xcodeproj
```

1. Select the **Fivelanes** target → **Signing & Capabilities**
2. Choose your **Team** (Apple ID is fine for personal devices)
3. Confirm **Bundle Identifier** (`com.fivelanes.dashboard`) or change it if needed

## 3. App icon (optional)

A placeholder icon is included (`Assets.xcassets/AppIcon.appiconset/AppIcon.jpg`, copied from `square5.jpg`). For App Store or a polished home screen, replace it with a 1024×1024 PNG in Xcode’s App Icon slot.

## 4. Run on your iPhone

1. Connect the iPhone (or use wireless debugging)
2. Select your device in Xcode’s run destination menu
3. **Product → Run** (⌘R)
4. On first install: **Settings → General → VPN & Device Management** → trust your developer certificate if prompted

## What the app does

- Full-screen dashboard WebView with pull-to-refresh
- Error banner + **Retry** when the server is unreachable (Tailscale off, server down, etc.)
- `mailto:` / `tel:` / `sms:` open in system apps
- External links (different host) open in Safari
- Soft reload when returning from background if the page was discarded
- Dark theme matching the dashboard (`#101216`)

## Install without Xcode (later)

For day-to-day use after initial setup:

- **TestFlight** — archive in Xcode and upload to App Store Connect (private testing group)
- **Ad hoc** — register device UDIDs and distribute an `.ipa`

This repo does not automate signing; use Xcode’s **Archive** flow when you are ready.

## Troubleshooting

| Symptom | Check |
|--------|--------|
| Blank “configure URL” screen | `FIVELANES_DASHBOARD_URL` still has the placeholder or `localhost` |
| Can’t connect | Tailscale running on iPhone; same URL works in Safari |
| ATS / insecure connection | Use `*.ts.net` hostname or Tailscale Serve HTTPS |
| WebView reloads often | Normal after long background; in-app state is still better than Safari tabs |

## Security

The dashboard has **no authentication**. Anyone on your tailnet who can reach port 8000 can use the API and download `timeline.db`. Keep Fivelanes on Tailscale only; do not expose port 8000 to the public internet.
