"""Load and normalize LinkedIn message CSV exports."""

from __future__ import annotations

import csv
import hashlib
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from services.linkedin.config import messages_csv_path
from utils.owner_config import owner_name

_CSV_FIELDS = {
    "conversation_id": "CONVERSATION ID",
    "conversation_title": "CONVERSATION TITLE",
    "from_name": "FROM",
    "sender_profile_url": "SENDER PROFILE URL",
    "to_name": "TO",
    "recipient_profile_urls": "RECIPIENT PROFILE URLS",
    "date": "DATE",
    "subject": "SUBJECT",
    "content": "CONTENT",
    "folder": "FOLDER",
    "attachments": "ATTACHMENTS",
    "is_message_draft": "IS MESSAGE DRAFT",
    "is_conversation_draft": "IS CONVERSATION DRAFT",
}

_csv_cache: Dict[str, Any] = {"key": None, "rows_by_conversation": {}}


def _owner_profile_url() -> str:
    return (os.getenv("LINKEDIN_PROFILE_URL") or "").strip().lower()


def _profile_slug(url: str) -> str:
    raw = (url or "").strip().rstrip("/").lower()
    if "/in/" in raw:
        return raw.split("/in/", 1)[1].split("?")[0].strip("/")
    return raw


def _is_truthy_draft(value: str) -> bool:
    return (value or "").strip().lower() in ("yes", "true", "1")


def _message_is_from_me(from_name: str, sender_profile_url: str) -> bool:
    profile = _owner_profile_url()
    sender_url = (sender_profile_url or "").strip().lower()
    if profile and sender_url and profile.rstrip("/") in sender_url:
        return True
    sender = (from_name or "").strip().lower()
    owner = owner_name().strip().lower()
    if not owner or not sender:
        return False
    if sender == owner:
        return True
    owner_parts = [p for p in owner.split() if p]
    sender_parts = [p for p in sender.split() if p]
    if owner_parts and sender_parts and owner_parts[0] == sender_parts[0]:
        if len(owner_parts) == 1 or (
            len(sender_parts) >= len(owner_parts)
            and sender_parts[: len(owner_parts)] == owner_parts
        ):
            return True
    return owner in sender or sender in owner


def _message_source_id(conversation_id: str, date: str, from_name: str, content: str) -> str:
    payload = f"{conversation_id}|{date}|{from_name}|{content[:500]}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _normalize_csv_row(raw: Dict[str, str]) -> Optional[Dict[str, Any]]:
    conversation_id = str(raw.get(_CSV_FIELDS["conversation_id"]) or "").strip()
    if not conversation_id:
        return None
    if _is_truthy_draft(str(raw.get(_CSV_FIELDS["is_message_draft"]) or "")):
        return None
    if _is_truthy_draft(str(raw.get(_CSV_FIELDS["is_conversation_draft"]) or "")):
        return None

    from_name = str(raw.get(_CSV_FIELDS["from_name"]) or "").strip()
    sender_profile_url = str(raw.get(_CSV_FIELDS["sender_profile_url"]) or "").strip()
    content = str(raw.get(_CSV_FIELDS["content"]) or "")
    subject = str(raw.get(_CSV_FIELDS["subject"]) or "").strip()
    date = str(raw.get(_CSV_FIELDS["date"]) or "").strip()
    is_from_me = _message_is_from_me(from_name, sender_profile_url)

    body = content.strip()
    if not body and subject:
        body = subject
    if not body and str(raw.get(_CSV_FIELDS["attachments"]) or "").strip():
        body = "(attachment)"

    return {
        "conversation_id": conversation_id,
        "conversation_title": str(raw.get(_CSV_FIELDS["conversation_title"]) or "").strip(),
        "from_name": from_name,
        "sender_profile_url": sender_profile_url,
        "to_name": str(raw.get(_CSV_FIELDS["to_name"]) or "").strip(),
        "recipient_profile_urls": str(
            raw.get(_CSV_FIELDS["recipient_profile_urls"]) or ""
        ).strip(),
        "date": date,
        "subject": subject,
        "text": body,
        "folder": str(raw.get(_CSV_FIELDS["folder"]) or "").strip(),
        "is_from_me": is_from_me,
        "source_id": _message_source_id(conversation_id, date, from_name, content),
    }


def _csv_cache_key(path: Path) -> Tuple[str, float]:
    if not path.is_file():
        return (str(path), 0.0)
    stat = path.stat()
    return (str(path.resolve()), stat.st_mtime)


def _load_rows_by_conversation() -> Dict[str, List[Dict[str, Any]]]:
    path = messages_csv_path()
    key = _csv_cache_key(path)
    if _csv_cache.get("key") == key:
        return _csv_cache["rows_by_conversation"]

    grouped: Dict[str, List[Dict[str, Any]]] = {}
    if path.is_file():
        try:
            with path.open(encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                for raw in reader:
                    if not isinstance(raw, dict):
                        continue
                    msg = _normalize_csv_row(raw)
                    if not msg:
                        continue
                    cid = msg["conversation_id"]
                    grouped.setdefault(cid, []).append(msg)
        except OSError:
            grouped = {}

    _csv_cache["key"] = key
    _csv_cache["rows_by_conversation"] = grouped
    return grouped


def load_messages_for_key(conversation_key: str) -> List[Dict[str, Any]]:
    key = (conversation_key or "").strip()
    if not key:
        return []
    rows = _load_rows_by_conversation().get(key, [])
    return sorted_messages(rows)


def sorted_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(messages, key=lambda m: str(m.get("date") or ""))


def conversation_label(messages: List[Dict[str, Any]], conversation_key: str) -> str:
    for msg in messages:
        title = str(msg.get("conversation_title") or "").strip()
        if title:
            return title
    for msg in reversed(messages):
        if msg.get("is_from_me"):
            continue
        name = str(msg.get("from_name") or "").strip()
        if name:
            return name
    for msg in messages:
        name = str(msg.get("from_name") or "").strip()
        if name:
            return name
    return conversation_key


def conversation_service(_messages: List[Dict[str, Any]]) -> str:
    return "LinkedIn"


def primary_source_email(messages: List[Dict[str, Any]], conversation_key: str) -> str:
    for msg in reversed(messages):
        if msg.get("is_from_me"):
            continue
        slug = _profile_slug(str(msg.get("sender_profile_url") or ""))
        if slug:
            return f"{slug}@linkedin"
    return "linkedin@linkedin"


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
        "chat_identifier": conversation_key,
    }


def message_source_id(msg: Dict[str, Any]) -> str:
    return str(msg.get("source_id") or "").strip()


def message_sender(msg: Dict[str, Any]) -> str:
    if msg.get("is_from_me"):
        return "me"
    slug = _profile_slug(str(msg.get("sender_profile_url") or ""))
    if slug:
        return slug
    return str(msg.get("from_name") or "").strip()


def message_body(msg: Dict[str, Any]) -> str:
    text = msg.get("text")
    if text is None:
        return ""
    return str(text)


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
        subject = str(msg.get("subject") or label).strip() or label
        cleaned.append(
            {
                "thread_id": thread_id,
                "source_id": sid,
                "datetime": str(msg.get("date") or ""),
                "sender": message_sender(msg),
                "recipients": "",
                "subject": subject,
                "raw_text": body,
                "forwarded_from": "",
                "cleaned_content": body,
                "quoted_reply": "",
                "signature": "",
                "api_error": "",
                "channel": "linkedin",
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
                "channel": "linkedin",
                "cleaned_content": row["cleaned_content"],
                "quoted_reply": "",
                "signature": "",
            }
        )
    return cleaned, summary
