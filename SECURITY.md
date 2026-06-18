# Security

## Self-hosted deployment

Fivelanes is designed for **local or LAN self-hosting**, not direct exposure to the public internet.

- The dashboard binds to `0.0.0.0` by default (`DASHBOARD_HOST` in `.env`), so it is reachable from other devices on your network.
- There is **no authentication** on the HTTP API or static UI.
- `GET /timeline.db` downloads the full SQLite database.

Do not expose the dashboard to the internet without a reverse proxy, TLS, and authentication.

## Secrets

Never commit:

- `.env` (API keys, account settings)
- `credentials/credentials.json` and `credentials/tokens.json`
- `services/prompts.json` (if you treat prompts as proprietary)
- `conversations/`, `out/`, `logs/`, `timeline.db`

## Reporting issues

If you discover a security vulnerability, please report it privately to the repository maintainers rather than opening a public issue.
