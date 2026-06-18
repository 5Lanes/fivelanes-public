"""Fallback bundle rows for tracked texts not yet persisted to SQLite."""

from __future__ import annotations

from typing import Any, Dict, List, Set, Tuple

from services.texts.config import CONVERSATIONS_DIR
from services.texts.format import (
    cleaned_rows_for_conversation,
    load_messages_for_key,
    rows_for_thread,
)
from services.texts.tracking import fetch_tracked_conversation_keys, text_inbox_thread_id


def _bundle_fingerprint(
    rows: List[Dict[str, Any]], thread_id: str
) -> Set[Tuple[str, str]]:
    out: Set[Tuple[str, str]] = set()
    for row in rows:
        if str(row.get("thread_id") or "").strip() != thread_id:
            continue
        sid = str(row.get("source_id") or "").strip()
        if sid:
            out.add((sid, str(row.get("datetime") or "")))
    return out


def _file_fingerprint(messages: List[Dict[str, Any]]) -> Set[Tuple[str, str]]:
    from services.texts.format import message_source_id

    out: Set[Tuple[str, str]] = set()
    for msg in messages:
        sid = message_source_id(msg)
        if sid:
            out.add((sid, str(msg.get("date") or "")))
    return out


def append_unsynced_text_threads_to_bundle(db_path: str, bundle: Dict[str, Any]) -> None:
    """
    Show on-disk text messages in the dashboard bundle.

    Tracked threads missing from ``claude_message_outputs`` are added wholesale.
    When a rolling on-disk export gains messages, only the new rows are appended;
    older persisted history is never dropped for tracked threads.
    """
    keys = fetch_tracked_conversation_keys(db_path)
    if not keys:
        return

    from utils.database import fetch_thread_tracking_rows

    snooze_map = {
        str(r.get("inbox_thread_id") or ""): int(r.get("snoozed") or 0)
        for r in fetch_thread_tracking_rows(db_path)
    }

    cleaned: List[Dict[str, Any]] = list(bundle.get("cleaned") or [])
    summary: List[Dict[str, Any]] = list(bundle.get("summary") or [])

    for key in keys:
        thread_id = text_inbox_thread_id(key)
        messages = load_messages_for_key(key)
        if not messages:
            continue

        bundle_fp = _bundle_fingerprint(cleaned, thread_id)
        file_fp = _file_fingerprint(messages)
        new_in_file = file_fp - bundle_fp
        if not new_in_file:
            continue

        snoozed = snooze_map.get(thread_id, 0)
        c_rows, s_rows = rows_for_thread(
            thread_id, key, messages, snoozed=snoozed
        )
        new_sids = {sid for sid, _ in new_in_file}
        cleaned.extend(r for r in c_rows if r["source_id"] in new_sids)
        summary.extend(r for r in s_rows if r["source_id"] in new_sids)

    bundle["cleaned"] = cleaned
    bundle["summary"] = summary
    bundle["texts_conversations_dir"] = str(CONVERSATIONS_DIR)
