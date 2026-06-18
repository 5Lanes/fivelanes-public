#!/usr/bin/env python3
"""
Add a Google account for Gmail + Calendar API access (OAuth).

Requires credentials/credentials.json (Desktop app OAuth client) and that the
redirect URI you use is listed in Google Cloud Console for that client.

Examples (from repository root):
  python3 utils/add_account.py work --serve --open
  python3 utils/add_account.py you@example.com --serve --open
  python3 utils/add_account.py --account-id personal --serve
  python3 utils/add_account.py --list

  Wrong:  python3 utils/add_account.py --you@example.com ... (leading -- is only for flags)
"""

from __future__ import annotations

import argparse
import logging
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Repo root must be on path when running ``python3 utils/add_account.py`` from the project.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from services.gmail_client import (
    exchange_code_for_token,
    get_authorization_url,
    list_connected_accounts,
)

log = logging.getLogger(__name__)


def _parse_redirect(redirect_uri: str) -> tuple[str, int, str]:
    p = urlparse(redirect_uri)
    if p.scheme not in ("http", "https"):
        raise ValueError(f"redirect_uri must be http(s), got: {redirect_uri!r}")
    host = p.hostname or "127.0.0.1"
    if p.port is None:
        port = 443 if p.scheme == "https" else 80
    else:
        port = p.port
    return host, port, p.path or "/"


def _wait_for_callback(
    redirect_uri: str,
    expected_state: str,
    *,
    timeout_s: int = 300,
) -> tuple[str | None, str | None]:
    """Run a one-shot HTTP server; return (code, error_message)."""
    host, port, _path = _parse_redirect(redirect_uri)
    result: dict[str, str | None] = {"code": None, "error": None}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:
            log.debug("%s - %s", self.address_string(), fmt % args)

        def do_GET(self) -> None:
            qs = parse_qs(urlparse(self.path).query)
            if qs.get("error"):
                result["error"] = qs.get("error", [""])[0] or "unknown_error"
                self.send_response(200)
                self.send_header("Content-type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    b"<html><body><p>Authorization failed. You can close this tab.</p></body></html>"
                )
                self.server.should_stop = True
                return
            code = (qs.get("code") or [None])[0]
            state = (qs.get("state") or [None])[0]
            if not code or state != expected_state:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Missing code or invalid state")
                self.server.should_stop = True
                return
            result["code"] = code
            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"<html><body><p>Success. You can close this tab and return to the terminal.</p></body></html>"
            )
            self.server.should_stop = True

    server = HTTPServer((host, port), Handler)
    server.should_stop = False
    server.timeout = 1.0
    elapsed = 0.0
    try:
        while not server.should_stop and elapsed < timeout_s:
            server.handle_request()
            elapsed += server.timeout
    finally:
        server.server_close()

    return result["code"], result["error"]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Add a Google account (OAuth) for fivelanes.",
        epilog=(
            "Tip: put the account key first without -- "
            "(e.g. alice@gmail.com), or use --account-id KEY. "
            "Flags like --serve must start with --; your email does not."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "account_id_pos",
        nargs="?",
        default=None,
        metavar="ACCOUNT_ID",
        help=(
            "Key stored in credentials/tokens.json (e.g. work or you@domain.com). "
            "Default: account1 if omitted."
        ),
    )
    parser.add_argument(
        "--account-id",
        dest="account_id_flag",
        default=None,
        help="Same as positional ACCOUNT_ID; overrides positional if both given",
    )
    parser.add_argument(
        "--redirect-uri",
        default="http://127.0.0.1:8080/",
        help="Must match an authorized redirect URI in Google Cloud Console",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Listen on redirect_uri host/port and capture the callback (no paste)",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="Open the authorization URL in a browser",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List account IDs that currently have valid tokens",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)

    if args.list:
        accounts = list_connected_accounts()
        if not accounts:
            print("No connected accounts (or tokens invalid / missing).")
        else:
            for aid in accounts:
                print(aid)
        return 0

    account_id = args.account_id_flag or args.account_id_pos or "account1"

    redirect_uri = args.redirect_uri.rstrip()
    if not redirect_uri.endswith("/") and urlparse(redirect_uri).path == "":
        redirect_uri = redirect_uri + "/"

    try:
        url, state = get_authorization_url(redirect_uri, account_id=account_id)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 1

    print("Open this URL in your browser (sign in with the Google account to add):\n")
    print(url)
    print()

    if args.serve:
        host, port, _ = _parse_redirect(redirect_uri)
        print(f"Waiting for OAuth redirect at {redirect_uri} (host={host!r} port={port}) ...")
        if args.open:
            webbrowser.open(url)
        code, err = _wait_for_callback(redirect_uri, state)
        if err:
            print(f"OAuth error: {err}", file=sys.stderr)
            return 1
        if not code:
            print("Timed out or no authorization code received.", file=sys.stderr)
            return 1
        saved = exchange_code_for_token(redirect_uri, code, state)
    else:
        if args.open:
            webbrowser.open(url)
        code = input("Paste the full ?code= value from the redirect URL: ").strip()
        if code.startswith("http"):
            q = parse_qs(urlparse(code).query)
            code = (q.get("code") or [""])[0]
            returned_state = (q.get("state") or [""])[0]
            if returned_state and returned_state != state:
                print("State mismatch; try again.", file=sys.stderr)
                return 1
        saved = exchange_code_for_token(redirect_uri, code, state)

    if not saved:
        print("Failed to exchange code for token (check redirect_uri and credentials).", file=sys.stderr)
        return 1

    print(f"Saved tokens for account_id={saved!r} in credentials/tokens.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
