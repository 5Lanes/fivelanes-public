"""Register which on-disk conversations appear in the Threads view."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Set

from services.texts.format import (
    cleaned_rows_for_conversation,
    load_messages_for_key,
    message_source_id,
    new_cleaned_vs_existing,
    primary_source_email,
)

log = logging.getLogger(__name__)

TEXT_THREAD_PREFIX = "text:"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def text_inbox_thread_id(conversation_key: str) -> str:
    key = (conversation_key or "").strip()
    if not key:
        return ""
    if key.startswith(TEXT_THREAD_PREFIX):
        return key
    return f"{TEXT_THREAD_PREFIX}{key}"


def parse_text_inbox_thread_id(inbox_thread_id: str) -> Optional[str]:
    tid = (inbox_thread_id or "").strip()
    if not tid.startswith(TEXT_THREAD_PREFIX):
        return None
    key = tid[len(TEXT_THREAD_PREFIX) :].strip()
    return key or None


def fetch_tracked_conversation_keys(db_path: str) -> List[str]:
    from utils.database import fetch_thread_tracking_rows

    out: List[str] = []
    for row in fetch_thread_tracking_rows(db_path):
        if int(row.get("snoozed") or 0) == 2:
            continue
        key = parse_text_inbox_thread_id(str(row.get("inbox_thread_id") or ""))
        if key:
            out.append(key)
    return sorted(set(out))


def maybe_clear_snooze_for_text_thread(db_path: str, conversation_key: str) -> bool:
    """
    Clear snooze (1 → 0) when new on-disk messages arrive, matching email
    ``thread_resolve`` behavior: existing thread activity or new sent-by-me messages.
    """
    from utils.database import (
        clear_snooze_only_for_threads,
        load_processed_cleaned_for_thread,
    )

    key = (conversation_key or "").strip()
    if not key:
        return False

    thread_id = text_inbox_thread_id(key)
    tracking = _existing_text_tracking_rows(db_path).get(key)
    if not tracking or int(tracking.get("snoozed") or 0) != 1:
        return False

    messages = load_messages_for_key(key)
    if not messages:
        return False

    file_cleaned = cleaned_rows_for_conversation(key, thread_id, messages)
    db_cleaned = load_processed_cleaned_for_thread(db_path, thread_id)
    new_cleaned = new_cleaned_vs_existing(db_cleaned, file_cleaned)
    if not new_cleaned:
        return False

    db_sids = {
        str(r.get("source_id") or "").strip()
        for r in db_cleaned
        if str(r.get("source_id") or "").strip()
    }
    new_ids = {
        str(r.get("source_id") or "").strip()
        for r in new_cleaned
        if str(r.get("source_id") or "").strip()
    }
    new_sent_ids = {
        sid
        for msg in messages
        for sid in [message_source_id(msg)]
        if sid and sid not in db_sids and msg.get("is_from_me")
    }
    prior_in_db = bool(db_sids)
    should_clear = bool(new_ids and prior_in_db) or bool(new_sent_ids)
    if not should_clear:
        return False

    clear_snooze_only_for_threads(db_path, [thread_id])
    if new_sent_ids and not (new_ids and prior_in_db):
        reason = "new sent message(s) in thread"
    elif new_sent_ids:
        reason = "new message(s) in thread (including sent)"
    else:
        reason = "new message(s) in thread"
    log.info("Cleared snooze for inbox_thread_id=%r (%s)", thread_id, reason)
    return True


def refresh_snooze_for_tracked_text_threads(db_path: str) -> int:
    """Run ``maybe_clear_snooze_for_text_thread`` for every actively tracked text thread."""
    cleared = 0
    for key in fetch_tracked_conversation_keys(db_path):
        if maybe_clear_snooze_for_text_thread(db_path, key):
            cleared += 1
    return cleared


def _existing_text_tracking_rows(db_path: str) -> Dict[str, Dict[str, Any]]:
    from utils.database import fetch_thread_tracking_rows

    by_key: Dict[str, Dict[str, Any]] = {}
    for row in fetch_thread_tracking_rows(db_path):
        key = parse_text_inbox_thread_id(str(row.get("inbox_thread_id") or ""))
        if key:
            by_key[key] = row
    return by_key


def set_tracked_conversation_keys(
    db_path: str, conversation_keys: Iterable[str]
) -> Dict[str, Any]:
    """
    Enable tracking for the given ``conversation_key`` values; untrack all other
    ``text:`` rows (``snoozed`` = 2).
    """
    from utils.database import set_thread_tracking_snoozed, upsert_thread_tracking

    desired: Set[str] = {k.strip() for k in conversation_keys if str(k).strip()}
    now = _utc_now_iso()
    existing = _existing_text_tracking_rows(db_path)

    upsert_rows: List[Dict[str, Any]] = []
    for key in sorted(desired):
        messages = load_messages_for_key(key)
        upsert_rows.append(
            {
                "inbox_thread_id": text_inbox_thread_id(key),
                "source_email": primary_source_email(messages, key),
                "snoozed": 0,
                "inner_rfc_message_id": "",
                "resolved_oauth_account_id": "",
                "resolution_error": "",
                "inbox_delivery_kind": "imessage",
                "created_at": str(
                    existing.get(key, {}).get("created_at") or now
                ),
                "updated_at": now,
            }
        )

    applied = upsert_thread_tracking(db_path, upsert_rows) if upsert_rows else 0
    untracked = 0
    for key, row in existing.items():
        if key in desired:
            continue
        if int(row.get("snoozed") or 0) == 2:
            continue
        tid = text_inbox_thread_id(key)
        if set_thread_tracking_snoozed(db_path, inbox_thread_id=tid, snoozed=2):
            from utils.database import delete_claude_outputs_for_thread

            delete_claude_outputs_for_thread(db_path, tid)
            untracked += 1

    return {
        "ok": True,
        "tracked": sorted(desired),
        "tracked_count": len(desired),
        "upserted": applied,
        "untracked": untracked,
    }
