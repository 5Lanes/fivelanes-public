"""Fallback bundle rows for tracked email threads not yet in ``claude_message_outputs``."""

from __future__ import annotations

from typing import Any, Dict, List, Set, Tuple

from services.email.forwarding import primary_email_from_sender
from services.email.inbox_delivery import timeline_row_process_body, timeline_row_raw_body
from services.pipeline.process import load_timeline_entries_by_thread
from services.thread_snooze import is_removed, snooze_map
from utils.database import fetch_thread_tracking_rows


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


def _timeline_fingerprint(rows: List[Dict[str, Any]]) -> Set[Tuple[str, str]]:
    out: Set[Tuple[str, str]] = set()
    for row in rows:
        sid = str(row.get("source_id") or "").strip()
        if sid:
            out.add((sid, str(row.get("datetime") or "")))
    return out


def _rows_for_thread(
    thread_id: str,
    timeline_rows: List[Dict[str, Any]],
    *,
    snoozed: int = 0,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Build ``cleaned`` / ``summary`` rows compatible with ``build_summaries_bundle``."""
    ordered = sorted(timeline_rows, key=lambda r: str(r.get("datetime") or ""))
    cleaned: List[Dict[str, Any]] = []
    summary: List[Dict[str, Any]] = []
    subject = ""
    for row in ordered:
        subj = str(row.get("summary") or "").strip()
        if subj and not subject:
            subject = subj

    display_label = subject or "(No subject)"
    for row in ordered:
        source_id = str(row.get("source_id") or "").strip()
        if not source_id:
            continue
        raw_body = timeline_row_raw_body(row)
        process_body = timeline_row_process_body(row)
        subj = str(row.get("summary") or "").strip() or display_label
        cleaned.append(
            {
                "thread_id": thread_id,
                "source_id": source_id,
                "datetime": str(row.get("datetime") or ""),
                "sender": str(row.get("sender") or ""),
                "recipients": str(row.get("recipients") or ""),
                "subject": subj,
                "raw_text": raw_body,
                "forwarded_from": primary_email_from_sender(str(row.get("sender") or "")),
                "cleaned_content": process_body or raw_body,
                "quoted_reply": "",
                "signature": "",
                "api_error": "",
            }
        )
        summary.append(
            {
                "thread_id": thread_id,
                "source_id": source_id,
                "datetime": str(row.get("datetime") or ""),
                "sender": str(row.get("sender") or ""),
                "subject": subj,
                "suggested_thread_label": display_label,
                "latest_updates": [],
                "latest_status": "",
                "snoozed": snoozed,
                "cleaned_content": process_body or raw_body,
                "quoted_reply": "",
                "signature": "",
            }
        )
    return cleaned, summary


def append_unsynced_email_threads_to_bundle(
    db_path: str,
    bundle: Dict[str, Any],
    *,
    lookback_days: int,
) -> None:
    """
    Show tracked inbox threads in the dashboard bundle before LLM segmentation.

    Tracked threads missing from ``claude_message_outputs`` are added from
    ``timeline_entries``. When a thread gains new timeline rows, only the new
    messages are appended.
    """
    tracked_ids = {
        str(row.get("inbox_thread_id") or "").strip()
        for row in fetch_thread_tracking_rows(db_path)
        if str(row.get("inbox_thread_id") or "").strip()
        and not is_removed(row.get("snoozed"))
        and not str(row.get("inbox_thread_id") or "").startswith("text:")
        and not str(row.get("inbox_thread_id") or "").startswith("slack:")
        and not str(row.get("inbox_thread_id") or "").startswith("linkedin:")
    }
    if not tracked_ids:
        return

    grouped = load_timeline_entries_by_thread(db_path, lookback_days=lookback_days)
    if not grouped:
        return

    snooze_by_thread = snooze_map(db_path)
    cleaned: List[Dict[str, Any]] = list(bundle.get("cleaned") or [])
    summary: List[Dict[str, Any]] = list(bundle.get("summary") or [])

    for thread_id in sorted(tracked_ids):
        timeline_rows = grouped.get(thread_id)
        if not timeline_rows:
            continue

        bundle_fp = _bundle_fingerprint(cleaned, thread_id)
        timeline_fp = _timeline_fingerprint(timeline_rows)
        new_in_timeline = timeline_fp - bundle_fp
        if not new_in_timeline:
            continue

        snoozed = snooze_by_thread.get(thread_id, 0)
        c_rows, s_rows = _rows_for_thread(thread_id, timeline_rows, snoozed=snoozed)
        new_sids = {sid for sid, _ in new_in_timeline}
        cleaned.extend(r for r in c_rows if r["source_id"] in new_sids)
        summary.extend(r for r in s_rows if r["source_id"] in new_sids)

    bundle["cleaned"] = cleaned
    bundle["summary"] = summary
