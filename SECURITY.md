# Security

## Self-hosted deployment

Fivelanes is designed for **local or LAN self-hosting**, not direct exposure to the public internet.

- The dashboard binds to `0.0.0.0` by default (`DASHBOARD_HOST` in your data directory `.env`), so it is reachable from other devices on your network.
- There is **no authentication** on the HTTP API or static UI.
- `GET /timeline.db` downloads the full SQLite database.

Do not expose the dashboard to the internet without a reverse proxy, TLS, and authentication.

## Secrets

Keep your private data directory (see `FIVELANES_DATA_ROOT` in the repo bootstrap `.env`) **outside git**. Never commit:

- Your data directory (default name: `fivelanes-data/`, or any path you set)
- `.env` bootstrap stub in the repo root (contains `FIVELANES_DATA_ROOT`)
- `$FIVELANES_DATA_ROOT/.env` (API keys, account settings)
- `$FIVELANES_DATA_ROOT/credentials/credentials.json` and `tokens.json`
- `$FIVELANES_DATA_ROOT/prompts.json` (if you treat prompts as proprietary)
- `$FIVELANES_DATA_ROOT/conversations/`, `out/`, `logs/`, `timeline.db`

Example OAuth templates under `credentials/` in the repo are safe to commit; real OAuth files belong in your data directory.

## Reporting issues

If you discover a security vulnerability, please report it privately to the repository maintainers rather than opening a public issue.
