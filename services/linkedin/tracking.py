"""Register which LinkedIn conversations appear in the Threads view."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Set

from services.linkedin.format import load_messages_for_key, primary_source_email
from services.thread_snooze import ACTIVE, is_removed

log = logging.getLogger(__name__)

LINKEDIN_THREAD_PREFIX = "linkedin:"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def linkedin_inbox_thread_id(conversation_key: str) -> str:
    key = (conversation_key or "").strip()
    if not key:
        return ""
    if key.startswith(LINKEDIN_THREAD_PREFIX):
        return key
    return f"{LINKEDIN_THREAD_PREFIX}{key}"


def parse_linkedin_inbox_thread_id(inbox_thread_id: str) -> Optional[str]:
    tid = (inbox_thread_id or "").strip()
    if not tid.startswith(LINKEDIN_THREAD_PREFIX):
        return None
    key = tid[len(LINKEDIN_THREAD_PREFIX) :].strip()
    return key or None


def fetch_tracked_conversation_keys(db_path: str) -> List[str]:
    from utils.database import fetch_thread_tracking_rows

    out: List[str] = []
    for row in fetch_thread_tracking_rows(db_path):
        if is_removed(row.get("snoozed")):
            continue
        key = parse_linkedin_inbox_thread_id(str(row.get("inbox_thread_id") or ""))
        if key:
            out.append(key)
    return sorted(set(out))


def _existing_linkedin_tracking_rows(db_path: str) -> Dict[str, Dict[str, Any]]:
    from utils.database import fetch_thread_tracking_rows

    by_key: Dict[str, Dict[str, Any]] = {}
    for row in fetch_thread_tracking_rows(db_path):
        key = parse_linkedin_inbox_thread_id(str(row.get("inbox_thread_id") or ""))
        if key:
            by_key[key] = row
    return by_key


def set_tracked_conversation_keys(
    db_path: str, conversation_keys: Iterable[str]
) -> Dict[str, Any]:
    """
    Enable tracking for the given ``conversation_key`` values; untrack all other
    ``linkedin:`` rows (``snoozed`` = 2).
    """
    from services.thread_snooze import remove_thread_tracking
    from utils.database import upsert_thread_tracking

    desired: Set[str] = {k.strip() for k in conversation_keys if str(k).strip()}
    now = _utc_now_iso()
    existing = _existing_linkedin_tracking_rows(db_path)

    upsert_rows: List[Dict[str, Any]] = []
    for key in sorted(desired):
        messages = load_messages_for_key(key)
        upsert_rows.append(
            {
                "inbox_thread_id": linkedin_inbox_thread_id(key),
                "source_email": primary_source_email(messages, key),
                "snoozed": ACTIVE,
                "inner_rfc_message_id": "",
                "resolved_oauth_account_id": "",
                "resolution_error": "",
                "inbox_delivery_kind": "linkedin",
                "created_at": str(existing.get(key, {}).get("created_at") or now),
                "updated_at": now,
            }
        )

    applied = upsert_thread_tracking(db_path, upsert_rows) if upsert_rows else 0
    untracked = 0
    for key, row in existing.items():
        if key in desired:
            continue
        if is_removed(row.get("snoozed")):
            continue
        tid = linkedin_inbox_thread_id(key)
        if remove_thread_tracking(db_path, tid):
            untracked += 1

    return {
        "ok": True,
        "tracked": sorted(desired),
        "tracked_count": len(desired),
        "upserted": applied,
        "untracked": untracked,
    }
