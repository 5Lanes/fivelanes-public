"""Fallback bundle rows for tracked Meet recordings not yet persisted to SQLite."""

from __future__ import annotations

from typing import Any, Dict, List, Set, Tuple

from services.meet_recordings.config import MEET_RECORDINGS_DIR
from services.meet_recordings.tracking import (
    fetch_tracked_document_keys,
    fetch_visible_document_keys,
    load_imported_note,
    meet_inbox_thread_id,
)
from services.thread_snooze import snooze_map


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


def _note_fingerprint(note: Dict[str, Any]) -> Set[Tuple[str, str]]:
    key = str(note.get("id") or "").strip()
    if not key:
        return set()
    return {(f"docs:{key}", str(note.get("datetime") or ""))}


def rows_for_thread(
    thread_id: str,
    note: Dict[str, Any],
    *,
    snoozed: int = 0,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Build ``cleaned`` / ``summary`` rows compatible with ``build_summaries_bundle``."""
    from services.meet_recordings.summarize import _cleaned_row_from_note

    cleaned_row = _cleaned_row_from_note(note, thread_id=thread_id)
    if not cleaned_row:
        return [], []

    title = str(note.get("label") or note.get("name") or "").strip()
    display_label = title or str(note.get("name") or "").strip() or thread_id
    summary_row: Dict[str, Any] = {
        "thread_id": thread_id,
        "source_id": cleaned_row["source_id"],
        "datetime": cleaned_row["datetime"],
        "sender": cleaned_row["sender"],
        "subject": cleaned_row["subject"],
        "suggested_thread_label": display_label,
        "latest_updates": [],
        "latest_status": "",
        "snoozed": snoozed,
        "channel": "meet_recording",
        "cleaned_content": cleaned_row["cleaned_content"],
        "quoted_reply": "",
        "signature": "",
    }
    return [cleaned_row], [summary_row]


def append_unsynced_meet_threads_to_bundle(db_path: str, bundle: Dict[str, Any]) -> None:
    """
    Show imported Meet recording notes in the dashboard bundle.

    Tracked threads missing from ``claude_message_outputs`` are added from on-disk
    imported notes so every selected recording appears before LLM summarization finishes.
    """
    keys = fetch_visible_document_keys(db_path)
    if not keys:
        return

    sync_keys = set(fetch_tracked_document_keys(db_path))
    snooze_by_thread = snooze_map(db_path)
    cleaned: List[Dict[str, Any]] = list(bundle.get("cleaned") or [])
    summary: List[Dict[str, Any]] = list(bundle.get("summary") or [])

    for key in keys:
        note = load_imported_note(key)
        if not note or not str(note.get("body") or "").strip():
            continue

        thread_id = meet_inbox_thread_id(key)
        bundle_fp = _bundle_fingerprint(cleaned, thread_id)
        note_fp = _note_fingerprint(note)
        new_in_note = note_fp - bundle_fp
        if not new_in_note:
            continue
        if key not in sync_keys and bundle_fp:
            continue

        snoozed = snooze_by_thread.get(thread_id, 0)
        c_rows, s_rows = rows_for_thread(thread_id, note, snoozed=snoozed)
        new_sids = {sid for sid, _ in new_in_note}
        cleaned.extend(r for r in c_rows if r["source_id"] in new_sids)
        summary.extend(r for r in s_rows if r["source_id"] in new_sids)

    bundle["cleaned"] = cleaned
    bundle["summary"] = summary
    bundle["meet_recordings_dir"] = str(MEET_RECORDINGS_DIR)
