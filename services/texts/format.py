"""Load and normalize on-disk conversation JSON (iMessage export shape)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from services.texts.config import conversation_file_path


def load_conversation_messages(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    return [m for m in raw if isinstance(m, dict)]


def load_messages_for_key(conversation_key: str) -> List[Dict[str, Any]]:
    return load_conversation_messages(conversation_file_path(conversation_key))


def conversation_label(messages: List[Dict[str, Any]], conversation_key: str) -> str:
    for msg in messages:
        name = str(msg.get("chat_name") or "").strip()
        if name:
            return name
    for msg in messages:
        handle = str(msg.get("handle") or "").strip()
        if handle:
            return handle
    ident = str(messages[0].get("chat_identifier") or "").strip() if messages else ""
    return ident or conversation_key


def conversation_service(messages: List[Dict[str, Any]]) -> str:
    for msg in reversed(messages):
        svc = str(msg.get("service") or "").strip()
        if svc:
            return svc
    return "iMessage"


def primary_source_email(messages: List[Dict[str, Any]], conversation_key: str) -> str:
    for msg in reversed(messages):
        if msg.get("is_from_me"):
            continue
        handle = str(msg.get("handle") or "").strip().lower()
        if handle:
            return handle
    key = conversation_key.strip().lower()
    if "@" in key or key.startswith("+"):
        return key
    return "text@imessage"


def sorted_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(messages, key=lambda m: str(m.get("date") or ""))


def conversation_metadata(
    conversation_key: str, messages: List[Dict[str, Any]]
) -> Dict[str, Any]:
    ordered = sorted_messages(messages)
    last = ordered[-1] if ordered else {}
    first = ordered[0] if ordered else {}
    return {
        "conversation_key": conversation_key,
        "label": conversation_label(ordered, conversation_key),
        "service": conversation_service(ordered),
        "message_count": len(ordered),
        "last_message_at": str(last.get("date") or ""),
        "first_message_at": str(first.get("date") or ""),
        "chat_identifier": str(
            (last or first).get("chat_identifier") or conversation_key
        ).strip(),
    }


def message_source_id(msg: Dict[str, Any]) -> str:
    guid = str(msg.get("guid") or "").strip()
    if guid:
        return guid
    mid = msg.get("message_id")
    if mid is not None:
        return str(mid)
    return ""


def message_sender(msg: Dict[str, Any]) -> str:
    if msg.get("is_from_me"):
        return "me"
    return str(msg.get("handle") or "").strip()


def message_body(msg: Dict[str, Any]) -> str:
    text = msg.get("text")
    if text is None:
        if msg.get("has_attachments"):
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
    """Union cleaned rows by ``source_id``; later groups win; sorted by datetime."""
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
    """Incoming rows whose ``source_id`` is not already in ``existing``."""
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
    """
    For a tracked text thread: full merged history (SQLite + on-disk file) and
    file rows not yet persisted.
    """
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
    """Dashboard / DB ``cleaned`` rows from on-disk messages."""
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
                "datetime": str(msg.get("date") or ""),
                "sender": message_sender(msg),
                "recipients": "",
                "subject": label,
                "raw_text": body,
                "forwarded_from": "",
                "cleaned_content": body,
                "quoted_reply": "",
                "signature": "",
                "api_error": "",
                "channel": "text",
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
    """Build ``cleaned`` / ``summary`` rows compatible with ``build_summaries_bundle``."""
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
                "channel": "text",
                "cleaned_content": row["cleaned_content"],
                "quoted_reply": "",
                "signature": "",
            }
        )
    return cleaned, summary
