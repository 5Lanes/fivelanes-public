"""
Thread snooze state and auto-unsnooze on new messages.

``thread_tracking.snoozed`` and ``claude_message_outputs.snoozed`` stay in sync:
  0 = active, 1 = snoozed, 2 = removed from tracking.

Auto-unsnooze runs during email inbox refresh and when the dashboard bundle is built
(for on-disk text threads).
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

log = logging.getLogger(__name__)

ACTIVE = 0
SNOOZED = 1
REMOVED = 2


def normalize_state(value: Any) -> int:
    raw = int(value or 0)
    return raw if raw in (ACTIVE, SNOOZED, REMOVED) else ACTIVE


def is_snoozed(value: Any) -> bool:
    return normalize_state(value) == SNOOZED


def is_removed(value: Any) -> bool:
    return normalize_state(value) == REMOVED


def is_tracked(value: Any) -> bool:
    return normalize_state(value) != REMOVED


def set_thread_snooze(db_path: str, thread_id: str, state: int) -> bool:
    """Persist snooze state on ``thread_tracking`` and ``claude_message_outputs``."""
    from utils.database import (
        set_claude_outputs_thread_snoozed,
        set_thread_tracking_snoozed,
    )

    tid = (thread_id or "").strip()
    if not tid:
        return False
    state_norm = normalize_state(state)
    ok_tracking = set_thread_tracking_snoozed(
        db_path, inbox_thread_id=tid, snoozed=state_norm
    )
    ok_claude = set_claude_outputs_thread_snoozed(
        db_path, thread_id=tid, snoozed=state_norm
    )
    return ok_tracking or ok_claude


def unsnooze_threads(db_path: str, thread_ids: Sequence[str]) -> None:
    """Clear snooze (1 → 0) on both stores; leaves removed (2) rows unchanged."""
    from utils.database import clear_snooze_only_for_threads

    clear_snooze_only_for_threads(db_path, thread_ids)


def remove_thread_tracking(db_path: str, thread_id: str) -> bool:
    """Mark thread removed (``REMOVED``); delete persisted text thread outputs."""
    from utils.database import (
        delete_claude_outputs_for_thread,
        set_claude_outputs_thread_snoozed,
        set_thread_tracking_snoozed,
    )

    tid = (thread_id or "").strip()
    if not tid:
        return False
    ok_tracking = set_thread_tracking_snoozed(
        db_path, inbox_thread_id=tid, snoozed=REMOVED
    )
    if tid.startswith("text:"):
        deleted = delete_claude_outputs_for_thread(db_path, tid)
        ok_claude = deleted > 0 or ok_tracking
    else:
        ok_claude = set_claude_outputs_thread_snoozed(
            db_path, thread_id=tid, snoozed=REMOVED
        )
    return ok_tracking or ok_claude


def snooze_map(db_path: str) -> Dict[str, int]:
    """``inbox_thread_id`` → snooze state from ``thread_tracking``."""
    from utils.database import fetch_thread_tracking_rows

    return {
        str(r.get("inbox_thread_id") or ""): normalize_state(r.get("snoozed"))
        for r in fetch_thread_tracking_rows(db_path)
    }


def known_source_ids_for_thread(
    db_path: str,
    thread_id: str,
    candidate_source_ids: set[str] | Sequence[str],
) -> set[str]:
    """
    Source ids already in ``timeline_entries`` or successful ``claude_message_outputs``.
    """
    from utils.database import _normalize_field

    clean_ids = sorted({str(x).strip() for x in candidate_source_ids if str(x).strip()})
    if not clean_ids:
        return set()
    tid = _normalize_field(thread_id)
    if not tid:
        return set()
    ph = ",".join("?" for _ in clean_ids)
    known: set[str] = set()
    try:
        with sqlite3.connect(Path(db_path)) as conn:
            for row in conn.execute(
                f"SELECT source_id FROM timeline_entries WHERE source_id IN ({ph})",
                clean_ids,
            ):
                sid = _normalize_field(row[0])
                if sid:
                    known.add(sid)
            for row in conn.execute(
                f"""
                SELECT DISTINCT source_id
                FROM claude_message_outputs
                WHERE COALESCE(thread_id, '') = ?
                  AND source_id IN ({ph})
                  AND COALESCE(TRIM(api_error), '') = ''
                """,
                [tid, *clean_ids],
            ):
                sid = _normalize_field(row[0])
                if sid:
                    known.add(sid)
    except sqlite3.Error:
        pass
    return known


def unseen_source_ids(
    db_path: str,
    thread_id: str,
    candidate_source_ids: set[str] | Sequence[str],
) -> set[str]:
    """Source ids in ``candidate_source_ids`` not yet stored for this thread."""
    clean = {str(x).strip() for x in candidate_source_ids if str(x).strip()}
    if not clean:
        return set()
    return clean - known_source_ids_for_thread(db_path, thread_id, clean)


def maybe_unsnooze_email_thread(
    db_path: str,
    tracking_row: Dict[str, Any],
    expanded: List[Dict[str, Any]],
) -> bool:
    """Unsnooze when an existing snoozed thread has any new message activity."""
    inbox_thread_id = (tracking_row.get("inbox_thread_id") or "").strip()
    if not inbox_thread_id or not is_snoozed(tracking_row.get("snoozed")):
        return False

    pulled = {
        str(row.get("source_id") or "").strip()
        for row in expanded
        if str(row.get("source_id") or "").strip()
    }
    known = known_source_ids_for_thread(db_path, inbox_thread_id, pulled)
    if not known or not unseen_source_ids(db_path, inbox_thread_id, pulled):
        return False

    unsnooze_threads(db_path, [inbox_thread_id])
    log.info(
        "Cleared snooze for inbox_thread_id=%r (new thread activity)",
        inbox_thread_id,
    )
    return True


def maybe_unsnooze_text_thread(db_path: str, conversation_key: str) -> bool:
    """Unsnooze a text thread when on-disk messages are not yet in SQLite."""
    from services.texts.format import (
        cleaned_rows_for_conversation,
        load_messages_for_key,
        new_cleaned_vs_existing,
    )
    from services.texts.tracking import text_inbox_thread_id
    from utils.database import fetch_thread_tracking_rows, load_processed_cleaned_for_thread

    key = (conversation_key or "").strip()
    if not key:
        return False

    thread_id = text_inbox_thread_id(key)
    tracking = next(
        (
            r
            for r in fetch_thread_tracking_rows(db_path)
            if (r.get("inbox_thread_id") or "").strip() == thread_id
        ),
        None,
    )
    if not tracking or not is_snoozed(tracking.get("snoozed")):
        return False

    messages = load_messages_for_key(key)
    if not messages:
        return False

    file_cleaned = cleaned_rows_for_conversation(key, thread_id, messages)
    db_cleaned = load_processed_cleaned_for_thread(db_path, thread_id)
    if not new_cleaned_vs_existing(db_cleaned, file_cleaned):
        return False

    unsnooze_threads(db_path, [thread_id])
    log.info("Cleared snooze for inbox_thread_id=%r (new on-disk messages)", thread_id)
    return True


def refresh_text_threads_auto_unsnooze(db_path: str) -> int:
    """Check all tracked text threads for new on-disk messages; return count unsnoozed."""
    from services.texts.tracking import fetch_tracked_conversation_keys

    cleared = 0
    for key in fetch_tracked_conversation_keys(db_path):
        if maybe_unsnooze_text_thread(db_path, key):
            cleared += 1
    return cleared
