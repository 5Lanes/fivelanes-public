"""Register which LinkedIn conversations appear in the Threads view."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Set

from services.linkedin.format import load_messages_for_key, primary_source_email
from services.thread_snooze import ACTIVE, is_removed, normalize_state

log = logging.getLogger(__name__)

LINKEDIN_THREAD_PREFIX = "linkedin:"
LINKEDIN_KIND = "linkedin"
LINKEDIN_PAUSED_KIND = "linkedin_paused"

_DEBUG_LOG_PATH = "/home/luisaherrmann/Code/fivelanes-public/.cursor/debug-5d5b20.log"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _debug_log(location: str, message: str, data: Dict[str, Any], hypothesis_id: str) -> None:
    # #region agent log
    try:
        with open(_DEBUG_LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "sessionId": "5d5b20",
                        "location": location,
                        "message": message,
                        "data": data,
                        "timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
                        "hypothesisId": hypothesis_id,
                    }
                )
                + "\n"
            )
    except OSError:
        pass
    # #endregion


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


def _linkedin_delivery_kind(row: Dict[str, Any]) -> str:
    return str(row.get("inbox_delivery_kind") or "").strip()


def _is_linkedin_tracking_row(row: Dict[str, Any]) -> bool:
    tid = str(row.get("inbox_thread_id") or "").strip()
    kind = _linkedin_delivery_kind(row)
    return tid.startswith(LINKEDIN_THREAD_PREFIX) or kind in (LINKEDIN_KIND, LINKEDIN_PAUSED_KIND)


def _is_sync_linkedin_row(row: Dict[str, Any]) -> bool:
    if is_removed(row.get("snoozed")):
        return False
    if not _is_linkedin_tracking_row(row):
        return False
    kind = _linkedin_delivery_kind(row)
    return kind in ("", LINKEDIN_KIND)


def fetch_visible_conversation_keys(db_path: str) -> List[str]:
    """All LinkedIn threads still shown on the dashboard (syncing or paused)."""
    from utils.database import fetch_thread_tracking_rows

    out: List[str] = []
    for row in fetch_thread_tracking_rows(db_path):
        if is_removed(row.get("snoozed")):
            continue
        if not _is_linkedin_tracking_row(row):
            continue
        key = parse_linkedin_inbox_thread_id(str(row.get("inbox_thread_id") or ""))
        if key:
            out.append(key)
    return sorted(set(out))


def fetch_tracked_conversation_keys(db_path: str) -> List[str]:
    """LinkedIn conversations selected for pull, summarize, and sync updates."""
    from utils.database import fetch_thread_tracking_rows

    out: List[str] = []
    for row in fetch_thread_tracking_rows(db_path):
        if not _is_sync_linkedin_row(row):
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
    Enable sync for selected ``conversation_key`` values.

    Other known LinkedIn rows are paused (still visible on the dashboard, but not
    pulled or re-summarized until checked again).
    """
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
                "inbox_delivery_kind": LINKEDIN_KIND,
                "created_at": str(existing.get(key, {}).get("created_at") or now),
                "updated_at": now,
            }
        )

    paused = 0
    for key, row in existing.items():
        if key in desired:
            continue
        if is_removed(row.get("snoozed")):
            continue
        if _linkedin_delivery_kind(row) == LINKEDIN_PAUSED_KIND:
            continue
        upsert_rows.append(
            {
                "inbox_thread_id": linkedin_inbox_thread_id(key),
                "source_email": str(row.get("source_email") or "").strip(),
                "snoozed": normalize_state(row.get("snoozed")),
                "inner_rfc_message_id": str(row.get("inner_rfc_message_id") or ""),
                "resolved_oauth_account_id": str(row.get("resolved_oauth_account_id") or ""),
                "resolution_error": str(row.get("resolution_error") or ""),
                "inbox_delivery_kind": LINKEDIN_PAUSED_KIND,
                "created_at": str(row.get("created_at") or now),
                "updated_at": now,
            }
        )
        paused += 1

    applied = upsert_thread_tracking(db_path, upsert_rows) if upsert_rows else 0

    # #region agent log
    _debug_log(
        "tracking.py:set_tracked_conversation_keys",
        "linkedin tracking saved",
        {
            "desiredCount": len(desired),
            "pausedCount": paused,
            "syncKeys": sorted(desired),
            "visibleKeys": fetch_visible_conversation_keys(db_path),
        },
        "H1",
    )
    # #endregion

    return {
        "ok": True,
        "tracked": sorted(desired),
        "tracked_count": len(desired),
        "upserted": applied,
        "paused": paused,
        "untracked": 0,
    }
