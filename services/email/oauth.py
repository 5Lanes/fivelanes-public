"""OAuth install flow."""
import json
import logging
import os
import secrets
from typing import Dict, Optional, Tuple

from google_auth_oauthlib.flow import InstalledAppFlow

from services.gmail_client import SCOPES
from .config import CREDENTIALS_PATH, TOKENS_PATH

log = logging.getLogger(__name__)

_oauth_states: Dict[str, Tuple[str, str, Optional[str]]] = {}
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
    return tokens
def _save_tokens(tokens: Dict[str, dict]) -> None:
    with open(TOKENS_PATH, "w") as f:
        json.dump({"accounts": tokens}, f, indent=2)
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
