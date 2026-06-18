"""
Gmail API client: OAuth and fetch messages.
Supports multiple accounts. Uses credentials.json and tokens (per account).

Adapted to pull only senders/recipients and timestamps for matching to a contact list
(no body/subject). Uses format="metadata" to avoid fetching message bodies.

FLAG FOR CALLERS: Code that calls pull_messages_for_emails() and expects "body"
in returned dicts (e.g. app.py) must be updated; this module returns:
id, thread_id, from, to, cc, bcc, subject, timestamp, direction (no body).
"""

import json
import logging
import os
import base64
import secrets
import sys
import webbrowser
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional, Tuple, Any

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from services.email.address import normalize_gmail_address

# Scopes for Gmail read and Calendar read (same token for both; re-run add_gmail_account to add Calendar).
# Optional: ``gmail.settings.basic.readonly`` lists send-as aliases (e.g. ``@gmail.com`` vs Workspace).
# It must be added under Google Cloud → OAuth consent screen → Scopes, or OAuth returns ``invalid_scope``.
# Enable with env ``FIVELANES_GMAIL_SETTINGS_SCOPE=1`` after configuring the consent screen.
_SCOPES_BASE = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
]
_SETTINGS_SCOPE = "https://www.googleapis.com/auth/gmail.settings.basic.readonly"
if (os.getenv("FIVELANES_GMAIL_SETTINGS_SCOPE") or "").strip().lower() in (
    "1",
    "true",
    "yes",
):
    SCOPES = _SCOPES_BASE[:1] + [_SETTINGS_SCOPE] + _SCOPES_BASE[1:]
else:
    SCOPES = list(_SCOPES_BASE)

log = logging.getLogger(__name__)

# Paths relative to project root
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CREDENTIALS_DIR = os.path.join(PROJECT_ROOT, "credentials")
CREDENTIALS_PATH = os.path.join(CREDENTIALS_DIR, "credentials.json")
SOURCE_OAUTH_ACCOUNT_ID = os.getenv("SOURCE_OAUTH_ACCOUNT_ID")
TOKEN_PATH = os.path.join(CREDENTIALS_DIR, "token.json")  # legacy single-account
TOKENS_PATH = os.path.join(CREDENTIALS_DIR, "tokens.json")  # multi-account: { "account1": {...}, "account2": {...} }

# In-memory state for OAuth: state -> (redirect_uri, account_id, code_verifier)
_oauth_states: Dict[str, Tuple[str, str, Optional[str]]] = {}

# Process-local cache: account_id -> normalized send-as addresses (from Gmail API).
_send_as_cache: Dict[str, frozenset] = {}


def _load_tokens() -> Dict[str, dict]:
    """Load all account tokens. Migrates legacy token.json to account1 if present."""
    tokens: Dict[str, dict] = {}
    if os.path.exists(TOKENS_PATH):
        try:
            with open(TOKENS_PATH, "r") as f:
                data = json.load(f)
                tokens = data.get("accounts", data) if isinstance(data, dict) else {}
        except Exception:
            pass
    # Migrate legacy token.json to account1
    if not tokens and os.path.exists(TOKEN_PATH):
        try:
            with open(TOKEN_PATH, "r") as f:
                tokens["account1"] = json.load(f)
            with open(TOKENS_PATH, "w") as f:
                json.dump({"accounts": tokens}, f, indent=2)
        except Exception:
            pass
    return tokens


def _save_tokens(tokens: Dict[str, dict]) -> None:
    with open(TOKENS_PATH, "w") as f:
        json.dump({"accounts": tokens}, f, indent=2)


def _can_open_browser() -> bool:
    """True when the default webbrowser backend is runnable on this machine."""
    try:
        webbrowser.get()
        return True
    except webbrowser.Error:
        return False


def _auto_reauthorize_account(account_id: str) -> Optional[Credentials]:
    """
    Launch interactive OAuth locally for one account and persist the new token.

    Controlled by env ``FIVELANES_AUTO_OAUTH_REAUTH`` (default on). Returns refreshed
    credentials or ``None`` when auto-reauth is disabled or fails.
    """
    enabled = (os.getenv("FIVELANES_AUTO_OAUTH_REAUTH") or "1").strip().lower()
    if enabled in ("0", "false", "no", "off"):
        return None
    if not sys.stdin.isatty():
        log.warning(
            "Token invalid for %s but auto-reauth skipped (non-interactive session).",
            account_id,
        )
        return None
    if not os.path.exists(CREDENTIALS_PATH):
        log.error(
            "Token invalid for %s and credentials.json is missing; cannot auto-reauth.",
            account_id,
        )
        return None
    try:
        log.warning(
            "Token invalid for %s. Launching browser OAuth re-authorization.",
            account_id,
        )
        flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
        open_browser = _can_open_browser()
        if not open_browser:
            log.warning(
                "No runnable browser on this machine; open the printed OAuth URL manually."
            )
        creds = flow.run_local_server(
            host="127.0.0.1",
            port=0,
            authorization_prompt_message=(
                f"Open this URL to re-authorize account '{account_id}':\n{{url}}"
            ),
            success_message="Authorization complete. You can close this tab.",
            open_browser=open_browser,
        )
        if not creds:
            return None
        tokens = _load_tokens()
        tokens[account_id] = json.loads(creds.to_json())
        _save_tokens(tokens)
        return creds if creds.valid else None
    except Exception as exc:
        log.exception("Auto OAuth re-authorization failed for %s: %s", account_id, exc)
        return None


def _get_credentials(account_id: str) -> Optional[Credentials]:
    """Load credentials for an account; refresh if expired."""
    tokens = _load_tokens()
    if account_id not in tokens:
        return None
    creds = Credentials.from_authorized_user_info(tokens[account_id], SCOPES)
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            tokens[account_id] = json.loads(creds.to_json())
            _save_tokens(tokens)
        except Exception:
            log.warning("Refresh token failed for %s; attempting interactive reauth.", account_id)
            return _auto_reauthorize_account(account_id)
    if creds.valid:
        return creds
    return _auto_reauthorize_account(account_id)


def list_connected_accounts() -> List[str]:
    """Return list of account IDs that have valid credentials."""
    tokens = _load_tokens()
    return [aid for aid in tokens if _get_credentials(aid) is not None]


def is_gmail_connected(account_id: Optional[str] = None) -> bool:
    """True if we have valid Gmail credentials. If account_id=None, checks if any account is connected."""
    if account_id:
        return _get_credentials(account_id) is not None
    return len(list_connected_accounts()) > 0


def get_authorization_url(redirect_uri: str, account_id: str = "account1") -> Tuple[str, str]:
    """Return (authorization_url, state). Redirect user to URL; use state in callback."""
    if not os.path.exists(CREDENTIALS_PATH):
        raise FileNotFoundError("credentials.json not found")
    flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
    flow.redirect_uri = redirect_uri
    state = secrets.token_urlsafe(32)
    url, _ = flow.authorization_url(access_type="offline", prompt="consent", state=state)
    _oauth_states[state] = (redirect_uri, account_id, getattr(flow, "code_verifier", None))
    return url, state


def exchange_code_for_token(redirect_uri: str, code: str, state: str) -> Optional[str]:
    """Exchange auth code for tokens; save to tokens.json. Returns account_id on success, None on failure."""
    if state not in _oauth_states:
        return None
    stored_uri, account_id, code_verifier = _oauth_states[state]
    if stored_uri != redirect_uri:
        return None
    del _oauth_states[state]
    if not os.path.exists(CREDENTIALS_PATH):
        return None
    flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
    flow.redirect_uri = redirect_uri
    if code_verifier:
        flow.code_verifier = code_verifier
    try:
        flow.fetch_token(code=code)
        creds = flow.credentials
        tokens = _load_tokens()
        tokens[account_id] = json.loads(creds.to_json())
        _save_tokens(tokens)
        return account_id
    except Exception as e:
        log.exception("OAuth token exchange failed for %s: %s", account_id, e)
        return None


def get_gmail_service(account_id: Optional[str] = None):
    """Build Gmail API service. If account_id=None, uses first connected account. Returns None if not authorized."""
    creds = None
    if account_id:
        creds = _get_credentials(account_id)
    else:
        for aid in list_connected_accounts():
            creds = _get_credentials(aid)
            if creds:
                break
    if not creds:
        return None
    return build("gmail", "v1", credentials=creds)


def get_all_gmail_services() -> List[Tuple[str, Any]]:
    """Return list of (account_id, service) for all connected accounts."""
    result: List[Tuple[str, Any]] = []
    for aid in list_connected_accounts():
        creds = _get_credentials(aid)
        if creds:
            svc = build("gmail", "v1", credentials=creds)
            result.append((aid, svc))
    return result


def gmail_account_has_valid_credentials(account_id: str) -> bool:
    """True if ``tokens.json`` has this id and access token loads (may refresh)."""
    aid = (account_id or "").strip()
    if not aid:
        return False
    return _get_credentials(aid) is not None


def get_gmail_services_for_account_id(account_id: Optional[str]) -> List[Tuple[str, Any]]:
    """Single OAuth account: ``(account_id, gmail_service)`` or empty if not connected."""
    if not account_id or not str(account_id).strip():
        log.warning("Gmail OAuth account id is empty (set SOURCE_OAUTH_ACCOUNT_ID in .env)")
        return []
    account_id = str(account_id).strip()
    creds = _get_credentials(account_id)
    if not creds:
        log.debug("No Gmail credentials for account %s", account_id)
        return []
    return [(account_id, build("gmail", "v1", credentials=creds))]


def _normalized_send_as_addresses(service: Any, cache_key: str) -> frozenset:
    """
    Normalized ``sendAsEmail`` values for this mailbox (cached per process).

    Requires ``gmail.settings.basic.readonly``; re-authorize accounts after scope changes.
    """
    if cache_key in _send_as_cache:
        return _send_as_cache[cache_key]
    found: set[str] = set()
    try:
        resp = (
            service.users()
            .settings()
            .sendAs()
            .list(userId="me")
            .execute()
        )
        for entry in resp.get("sendAs") or []:
            e = (entry.get("sendAsEmail") or "").strip()
            if e and "@" in e:
                found.add(normalize_gmail_address(e))
    except HttpError as e:
        log.debug("sendAs.list failed for %s (re-auth may be needed for settings scope): %s", cache_key, e)
    except Exception as e:
        log.debug("sendAs.list error for %s: %s", cache_key, e)
    fs = frozenset(found)
    _send_as_cache[cache_key] = fs
    return fs


def mailbox_identity_emails(service: Any, account_id: str) -> frozenset:
    """Profile email plus normalized send-as addresses for sent/received detection."""
    emails: set[str] = set()
    prof = _get_account_email(service)
    if prof:
        emails.add(prof)
    emails.update(_normalized_send_as_addresses(service, str(account_id or "").strip()))
    return frozenset(emails)


def oauth_account_id_for_email(target_email: str) -> Optional[str]:
    """
    Return the ``tokens.json`` account key for ``target_email``, or ``None``.

    Matches (in order):

    1. Any **account id** in ``tokens.json`` that is itself an email address and equals
       ``target_email`` after :func:`normalize_gmail_address`. No credential check: the file
       key is the canonical id for that mailbox even if the refresh token is currently invalid.
    2. For each **connected** account: Gmail **profile** email, then **send-as** addresses
       (so ``alice@gmail.com`` can map to the same account as ``alice@custom.tld``).
    """
    want = normalize_gmail_address(target_email)
    if not want or "@" not in want:
        return None
    for aid in _load_tokens():
        aid_s = str(aid).strip()
        if "@" not in aid_s:
            continue
        if normalize_gmail_address(aid_s) == want:
            return aid_s
    for aid in list_connected_accounts():
        svc = get_gmail_service(aid)
        if not svc:
            continue
        aid_s = str(aid).strip()
        profile = _get_account_email(svc)
        if profile and normalize_gmail_address(profile) == want:
            return aid_s
        if want in _normalized_send_as_addresses(svc, aid_s):
            return aid_s
    return None


def profile_email_to_account_id_map() -> Dict[str, str]:
    """
    ``normalize_gmail_address`` -> OAuth ``account_id``.

    Includes **email-shaped** keys from ``tokens.json`` (even if not currently connected),
    Gmail profile addresses, and **send-as** addresses for connected accounts.
    """
    out: Dict[str, str] = {}
    for aid in _load_tokens():
        aid_s = str(aid).strip()
        if "@" in aid_s:
            out.setdefault(normalize_gmail_address(aid_s), aid_s)
    for aid in list_connected_accounts():
        aid_s = str(aid).strip()
        if "@" in aid_s:
            out.setdefault(normalize_gmail_address(aid_s), aid_s)
        svc = get_gmail_service(aid)
        if not svc:
            continue
        profile = _get_account_email(svc)
        if profile:
            out.setdefault(normalize_gmail_address(profile), aid_s)
        for em in _normalized_send_as_addresses(svc, aid_s):
            out.setdefault(em, aid_s)
    return out


def find_thread_id_by_rfc_message_id(service: Any, rfc_message_id: str) -> Optional[str]:
    """
    Return Gmail ``threadId`` for a message in this mailbox matching ``rfc822msgid``, or None.
    """
    mid = (rfc_message_id or "").strip().strip("<>")
    if not mid:
        return None
    q = f"rfc822msgid:{mid}"
    try:
        resp = (
            service.users()
            .messages()
            .list(userId="me", q=q, maxResults=3)
            .execute()
        )
        refs = resp.get("messages") or []
        if not refs:
            return None
        msg_id = refs[0].get("id")
        if not msg_id:
            return None
        meta = (
            service.users()
            .messages()
            .get(userId="me", id=msg_id, format="metadata", metadataHeaders=[])
            .execute()
        )
        tid = (meta.get("threadId") or "").strip()
        return tid or None
    except HttpError as e:
        log.warning("rfc822msgid search failed: %s", e)
        return None


def _decode_body(payload: dict) -> str:
    """Extract plain-text body from Gmail message payload."""
    if not payload:
        return ""
    data = payload.get("body", {}).get("data")
    if data:
        try:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        except Exception:
            return ""
    for part in payload.get("parts", []):
        if part.get("mimeType") == "text/plain":
            b = part.get("body", {}).get("data")
            if b:
                try:
                    return base64.urlsafe_b64decode(b).decode("utf-8", errors="replace")
                except Exception:
                    pass
    return ""


def _decode_part_body(part: dict, service, msg_id: str) -> str:
    """Decode a single part's body (inline data or attachment). Returns decoded text or ''."""
    body = part.get("body") or {}
    data = body.get("data")
    if data:
        try:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        except Exception:
            return ""
    attachment_id = body.get("attachmentId")
    if attachment_id and service and msg_id:
        try:
            att = (
                service.users()
                .messages()
                .attachments()
                .get(userId="me", messageId=msg_id, id=attachment_id)
                .execute()
            )
            raw = att.get("data")
            if raw:
                return base64.urlsafe_b64decode(raw).decode("utf-8", errors="replace")
        except Exception:
            pass
    return ""


def _extract_attachments_text(service, msg_id: str, payload: dict, max_chars: int = 500_000) -> str:
    """
    Recursively extract text from attachment-like parts (e.g. transcript files).
    Skips the main body text/plain; collects text from other text parts and text-like attachments.
    """
    if not payload:
        return ""
    parts = payload.get("parts") or []
    texts: List[str] = []
    total = 0

    def walk(part_list: list) -> None:
        nonlocal total
        for part in part_list:
            if total >= max_chars:
                return
            mime = (part.get("mimeType") or "").lower()
            filename = (part.get("filename") or "").lower()
            # Skip inline main body (handled by _decode_body)
            if mime == "text/plain" and not filename and not part.get("body", {}).get("attachmentId"):
                continue
            # Nested multipart
            if mime.startswith("multipart/"):
                walk(part.get("parts") or [])
                continue
            # Text parts or common transcript extensions (e.g. Gemini meeting notes)
            if mime.startswith("text/") or any(filename.endswith(ext) for ext in (".txt", ".md", ".transcript", ".log")):
                raw = _decode_part_body(part, service, msg_id)
                if raw and raw.strip():
                    texts.append(raw.strip())
                    total += len(raw)

    walk(parts)
    return "\n\n".join(texts) if texts else ""


def _get_header(headers: List[dict], name: str) -> str:
    for h in headers or []:
        if (h.get("name") or "").lower() == name.lower():
            return (h.get("value") or "").strip()
    return ""

def _extract_emails_lower(header_value: str) -> set:
    """Extract email addresses from header (e.g. 'Name <a@x.com>, b@y.com') and return as lowercase set."""
    from email.utils import getaddresses
    if not header_value or not header_value.strip():
        return set()
    addrs = getaddresses([header_value])
    return {addr.lower() for _, addr in addrs if addr and "@" in addr}


def _matches_contact_emails(
    from_addr: str, to_addr: str, cc_addr: str, bcc_addr: str,
    contact_emails: set,
) -> bool:
    """True if any sender or recipient is in the given contact_emails set."""
    all_addrs = (
        _extract_emails_lower(from_addr)
        | _extract_emails_lower(to_addr)
        | _extract_emails_lower(cc_addr)
        | _extract_emails_lower(bcc_addr)
    )
    return bool(all_addrs & contact_emails)


# Gmail query has practical ~1500 char limit; batch to stay under
QUERY_BATCH_SIZE = 8  # ~8 emails per query keeps it safe


def _get_account_email(service: Any) -> Optional[str]:
    """Return the authenticated account's email from Gmail profile, or None."""
    try:
        profile = service.users().getProfile(userId="me").execute()
        return (profile.get("emailAddress") or "").strip().lower() or None
    except Exception:
        return None


def _pull_from_account(
    account_id: str,
    service: Any,
    emails: set,
    contact_query: str,
    q: str,
    max_results: int,
    use_account_prefix: bool,
) -> List[dict]:
    """Fetch messages from one Gmail account."""
    account_email = _get_account_email(service)
    list_kw: Dict[str, Any] = {
        "userId": "me",
        "maxResults": min(max_results, 500),
        "q": q,
    }
    try:
        response = service.users().messages().list(**list_kw).execute()
    except HttpError as e:
        log.error("Gmail list error for %s: %s", account_id, e)
        raise
    messages = response.get("messages", [])
    log.info("Account %s: API returned %d message refs", account_id, len(messages))

    result = []
    for msg_ref in messages[:max_results]:
        try:
            # Metadata only: headers (From, To, Cc, Bcc, Date, Subject), no body
            msg = (
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=msg_ref["id"],
                    format="metadata",
                    metadataHeaders=["From", "To", "Cc", "Bcc", "Date", "Subject"],
                )
                .execute()
            )
        except HttpError:
            continue

        payload = msg.get("payload", {})
        headers = payload.get("headers", [])
        from_addr = _get_header(headers, "From")
        to_addr = _get_header(headers, "To")
        cc_addr = _get_header(headers, "Cc")
        bcc_addr = _get_header(headers, "Bcc")
        subject = _get_header(headers, "Subject")
        date_str = _get_header(headers, "Date")

        if not _matches_contact_emails(from_addr, to_addr, cc_addr, bcc_addr, emails):
            continue

        ts = datetime.now(timezone.utc).isoformat()
        if date_str:
            try:
                from email.utils import parsedate_to_datetime
                dt = parsedate_to_datetime(date_str)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                ts = dt.isoformat()
            except Exception:
                pass

        msg_id = msg_ref["id"]
        stored_id = f"{account_id}:{msg_id}" if use_account_prefix else msg_id

        # Direction: sent if this account's email is in the From header, else received
        from_emails = _extract_emails_lower(from_addr)
        direction = "sent" if (account_email and account_email in from_emails) else "received"

        result.append({
            "id": stored_id,
            "thread_id": msg.get("threadId", ""),
            "from": from_addr or "",
            "to": to_addr or "",
            "cc": cc_addr or "",
            "bcc": bcc_addr or "",
            "subject": subject or "(No subject)",
            "timestamp": ts,
            "direction": direction,
        })
    return result


def pull_messages_for_emails(
    contact_emails: List[str],
    max_results: int = 500,
    label_ids: Optional[List[str]] = None,
    after_days: Optional[int] = None,
    after_date: Optional[str] = None,
) -> List[dict]:
    """
    Fetch messages from Gmail where sender, to, cc, or bcc is in the given contact emails.
    Returns senders/recipients, when, and subject (for matching to contact list); no body.
    Searches all labels and all connected accounts.
    Returns list of dicts: id, thread_id, from, to, cc, bcc, subject, timestamp, direction.
    """
    services = get_all_gmail_services()
    if not services:
        log.warning("No Gmail service available (not connected)")
        return []

    emails = {e.strip().lower() for e in contact_emails if e and "@" in str(e).strip()}
    if not emails:
        log.warning("No valid contact emails provided")
        return []

    log.info("Querying %d Gmail account(s) for %d contact emails", len(services), len(emails))

    email_list = list(emails)
    date_prefix = ""
    if after_date and len(after_date) >= 10:
        y, m, d = after_date[:4], int(after_date[5:7]), int(after_date[8:10])
        date_prefix = f"after:{y}/{m}/{d} "
    elif after_days is not None and after_days > 0:
        d = (datetime.now(timezone.utc) - timedelta(days=after_days)).date()
        date_prefix = f"after:{d.year}/{d.month:02d}/{d.day:02d} "

    per_batch = max(100, max_results // max(1, (len(email_list) + QUERY_BATCH_SIZE - 1) // QUERY_BATCH_SIZE))
    use_account_prefix = len(services) > 1

    all_results: List[dict] = []
    seen_ids: set = set()

    for account_id, service in services:
        for i in range(0, len(email_list), QUERY_BATCH_SIZE):
            if len(all_results) >= max_results:
                break
            batch_emails = email_list[i : i + QUERY_BATCH_SIZE]
            parts = [f"(from:{e} OR to:{e} OR cc:{e})" for e in batch_emails]
            contact_query = " OR ".join(parts)
            q = f"{date_prefix}({contact_query})".strip()
            log.info("Query batch %d: %d emails, q length=%d", i // QUERY_BATCH_SIZE + 1, len(batch_emails), len(q))

            try:
                batch = _pull_from_account(
                    account_id, service, emails, contact_query, q, per_batch, use_account_prefix
                )
                for m in batch:
                    if m["id"] not in seen_ids:
                        seen_ids.add(m["id"])
                        all_results.append(m)
            except HttpError as e:
                log.error("Gmail API error for %s: %s", account_id, e)

    all_results.sort(key=lambda m: m.get("timestamp", ""), reverse=True)
    log.info("Matched %d messages for contacts across %d account(s)", len(all_results), len(services))
    return all_results[:max_results]
