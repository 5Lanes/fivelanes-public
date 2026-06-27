"""Fallback bundle rows for tracked Slack DMs not yet persisted to SQLite."""

from __future__ import annotations

from typing import Any, Dict, List, Set, Tuple

from services.slack.config import SLACK_DMS_DIR
from services.slack.format import (
    load_messages_for_key,
    message_source_id,
    rows_for_thread,
)
from services.slack.tracking import fetch_tracked_conversation_keys, slack_inbox_thread_id
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


def _file_fingerprint(messages: List[Dict[str, Any]]) -> Set[Tuple[str, str]]:
    out: Set[Tuple[str, str]] = set()
    for msg in messages:
        sid = message_source_id(msg)
        if sid:
            out.add((sid, str(msg.get("datetime") or "")))
    return out


def append_unsynced_slack_threads_to_bundle(db_path: str, bundle: Dict[str, Any]) -> None:
    keys = fetch_tracked_conversation_keys(db_path)
    if not keys:
        return

    snooze_by_thread = snooze_map(db_path)
    cleaned: List[Dict[str, Any]] = list(bundle.get("cleaned") or [])
    summary: List[Dict[str, Any]] = list(bundle.get("summary") or [])

    for key in keys:
        thread_id = slack_inbox_thread_id(key)
        messages = load_messages_for_key(key)
        if not messages:
            continue

        bundle_fp = _bundle_fingerprint(cleaned, thread_id)
        file_fp = _file_fingerprint(messages)
        new_in_file = file_fp - bundle_fp
        if not new_in_file:
            continue

        snoozed = snooze_by_thread.get(thread_id, 0)
        c_rows, s_rows = rows_for_thread(thread_id, key, messages, snoozed=snoozed)
        new_sids = {sid for sid, _ in new_in_file}
        cleaned.extend(r for r in c_rows if r["source_id"] in new_sids)
        summary.extend(r for r in s_rows if r["source_id"] in new_sids)

    bundle["cleaned"] = cleaned
    bundle["summary"] = summary
    bundle["slack_dms_dir"] = str(SLACK_DMS_DIR)
