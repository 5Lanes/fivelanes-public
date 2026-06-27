"""Load and normalize on-disk Slack DM JSON exports."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from services.slack.config import conversation_file_path


def load_conversation_export(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def load_messages_for_key(conversation_key: str) -> List[Dict[str, Any]]:
    export = load_conversation_export(conversation_file_path(conversation_key))
    messages = export.get("messages")
    if not isinstance(messages, list):
        return []
    return [m for m in messages if isinstance(m, dict)]


def conversation_label(messages: List[Dict[str, Any]], conversation_key: str) -> str:
    export = load_conversation_export(conversation_file_path(conversation_key))
    name = str(export.get("user_name") or "").strip()
    if name:
        return name
    for msg in messages:
        handle = str(msg.get("user") or "").strip()
        if handle and not msg.get("is_from_me"):
            return handle
    return conversation_key


def conversation_service(_messages: List[Dict[str, Any]]) -> str:
    return "Slack"


def primary_source_email(messages: List[Dict[str, Any]], conversation_key: str) -> str:
    export = load_conversation_export(conversation_file_path(conversation_key))
    user_id = str(export.get("user_id") or "").strip().lower()
    if user_id:
        return f"{user_id}@slack"
    return "slack@slack"


def sorted_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(messages, key=lambda m: str(m.get("datetime") or m.get("ts") or ""))


def conversation_metadata(
    conversation_key: str, messages: List[Dict[str, Any]]
) -> Dict[str, Any]:
    ordered = sorted_messages(messages)
    last = ordered[-1] if ordered else {}
    first = ordered[0] if ordered else {}
    export = load_conversation_export(conversation_file_path(conversation_key))
    label = conversation_label(ordered, conversation_key)
    return {
        "conversation_key": conversation_key,
        "label": label,
        "service": conversation_service(ordered),
        "message_count": len(ordered),
        "last_message_at": str(last.get("datetime") or ""),
        "first_message_at": str(first.get("datetime") or ""),
        "chat_identifier": str(export.get("user_id") or conversation_key).strip(),
        "user_id": str(export.get("user_id") or "").strip(),
    }


def message_source_id(msg: Dict[str, Any]) -> str:
    ts = str(msg.get("ts") or "").strip()
    if ts:
        return ts
    return ""


def message_sender(msg: Dict[str, Any]) -> str:
    if msg.get("is_from_me"):
        return "me"
    return str(msg.get("user") or "").strip()


def message_body(msg: Dict[str, Any]) -> str:
    text = msg.get("text")
    if text is None:
        if msg.get("has_files"):
            return "(attachment)"
        return ""
    return str(text)


def cleaned_fingerprint(rows: List[Dict[str, Any]]) -> Set[Tuple[str, str]]:
    out: Set[Tuple[str, str]] = set()
    for row in rows:
        sid = str(row.get("source_id") or "").strip()
        if sid:
            out.add((sid, str(row.get("datetime") or "")))
    return out


def merge_cleaned_rows(*groups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_sid: Dict[str, Dict[str, Any]] = {}
    for group in groups:
        for row in group:
            sid = str(row.get("source_id") or "").strip()
            if sid:
                by_sid[sid] = row
    merged = list(by_sid.values())
    merged.sort(key=lambda r: str(r.get("datetime") or ""))
    return merged


def new_cleaned_vs_existing(
    existing: List[Dict[str, Any]], incoming: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    seen = {str(r.get("source_id") or "").strip() for r in existing}
    out: List[Dict[str, Any]] = []
    for row in incoming:
        sid = str(row.get("source_id") or "").strip()
        if sid and sid not in seen:
            out.append(row)
    return out


def tracked_thread_cleaned_rows(
    db_path: str, conversation_key: str, thread_id: str
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    from utils.database import load_processed_cleaned_for_thread

    file_cleaned = cleaned_rows_for_conversation(
        conversation_key, thread_id, load_messages_for_key(conversation_key)
    )
    db_cleaned = load_processed_cleaned_for_thread(db_path, thread_id)
    new_from_file = new_cleaned_vs_existing(db_cleaned, file_cleaned)
    merged = merge_cleaned_rows(db_cleaned, file_cleaned)
    return merged, new_from_file


def cleaned_rows_for_conversation(
    conversation_key: str,
    thread_id: str,
    messages: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    ordered = sorted_messages(messages)
    label = conversation_label(ordered, conversation_key)
    cleaned: List[Dict[str, Any]] = []
    for msg in ordered:
        sid = message_source_id(msg)
        if not sid:
            continue
        body = message_body(msg)
        cleaned.append(
            {
                "thread_id": thread_id,
                "source_id": sid,
                "datetime": str(msg.get("datetime") or ""),
                "sender": message_sender(msg),
                "recipients": "",
                "subject": label,
                "raw_text": body,
                "forwarded_from": "",
                "cleaned_content": body,
                "quoted_reply": "",
                "signature": "",
                "api_error": "",
                "channel": "slack",
            }
        )
    return cleaned


def rows_for_thread(
    thread_id: str,
    conversation_key: str,
    messages: List[Dict[str, Any]],
    *,
    snoozed: int = 0,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    ordered = sorted_messages(messages)
    label = conversation_label(ordered, conversation_key)
    service = conversation_service(ordered)
    display_label = f"{label} · {service}" if service else label
    cleaned = cleaned_rows_for_conversation(conversation_key, thread_id, messages)
    summary: List[Dict[str, Any]] = []
    if not cleaned:
        return cleaned, summary

    for row in cleaned:
        summary.append(
            {
                "thread_id": thread_id,
                "source_id": row["source_id"],
                "datetime": row["datetime"],
                "sender": row["sender"],
                "subject": label,
                "suggested_thread_label": display_label,
                "latest_updates": [],
                "latest_status": "",
                "snoozed": snoozed,
                "channel": "slack",
                "cleaned_content": row["cleaned_content"],
                "quoted_reply": "",
                "signature": "",
            }
        )
    return cleaned, summary
