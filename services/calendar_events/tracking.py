"""Sync the ``meetings`` table into thread_tracking/timeline_entries as calendar-event items."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from services.email.config import SOURCE_ACCOUNT, SOURCE_OAUTH_ACCOUNT_ID
from services.thread_snooze import ACTIVE
from utils.conversation_sources import SOURCE_PREFIXES, make_source_key, parse_source_key

log = logging.getLogger(__name__)

CALENDAR_THREAD_PREFIX = SOURCE_PREFIXES["calendar"]
CALENDAR_KIND = "calendar_event"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def calendar_inbox_thread_id(dedupe_key: str) -> str:
    return make_source_key("calendar", (dedupe_key or "").strip())


def parse_calendar_inbox_thread_id(inbox_thread_id: str) -> Optional[str]:
    return parse_source_key("calendar", inbox_thread_id)


def _format_event_body(meeting: Dict[str, Any]) -> str:
    lines: List[str] = []
    start = str(meeting.get("start_iso") or "").strip()
    end = str(meeting.get("end_iso") or "").strip()
    if start:
        lines.append(f"When: {start} → {end or '(no end)'}")
    location = str(meeting.get("location") or "").strip()
    if location:
        lines.append(f"Location: {location}")
    attendees = meeting.get("attendees") or []
    if attendees:
        lines.append(f"Attendees: {', '.join(attendees)}")
    link = str(meeting.get("html_link") or "").strip()
    if link:
        lines.append(f"Link: {link}")
    return "\n".join(lines)


def _timeline_row_from_meeting(meeting: Dict[str, Any], thread_id: str) -> Dict[str, Any]:
    title = str(meeting.get("summary") or "").strip() or "(No title)"
    attendees = meeting.get("attendees") or []
    return {
        "source_id": thread_id,
        "type": "meeting",
        "datetime": meeting.get("start_iso") or "",
        "sender": "",
        "recipients": json.dumps(attendees, ensure_ascii=False),
        "participants": ", ".join(attendees),
        "summary": title,
        "body": _format_event_body(meeting),
        "thread_id": thread_id,
        "fetch_oauth_account_id": meeting.get("account_id") or "",
        "body_has_image": 0,
    }


def _tracking_row_for_meeting(
    meeting: Dict[str, Any], *, existing: Optional[Dict[str, Any]], now_iso: str
) -> Dict[str, Any]:
    key = str(meeting.get("dedupe_key") or "").strip()
    return {
        "inbox_thread_id": calendar_inbox_thread_id(key),
        "gmail_inbox_thread_id": "",
        "source_email": (SOURCE_ACCOUNT or "").strip().lower(),
        "snoozed": ACTIVE,
        "inner_rfc_message_id": "",
        "resolved_oauth_account_id": meeting.get("account_id") or SOURCE_OAUTH_ACCOUNT_ID or "",
        "resolution_error": "",
        "inbox_delivery_kind": CALENDAR_KIND,
        "created_at": str((existing or {}).get("created_at") or now_iso),
        "updated_at": now_iso,
    }


def _existing_calendar_tracking_rows(db_path: str) -> Dict[str, Dict[str, Any]]:
    from utils.database import fetch_thread_tracking_rows

    by_key: Dict[str, Dict[str, Any]] = {}
    for row in fetch_thread_tracking_rows(db_path):
        key = parse_calendar_inbox_thread_id(str(row.get("inbox_thread_id") or ""))
        if key:
            by_key[key] = row
    return by_key


def fetch_tracked_calendar_dedupe_keys(db_path: str) -> List[str]:
    """Calendar event dedupe keys currently present as thread_tracking rows."""
    return sorted(_existing_calendar_tracking_rows(db_path).keys())


def sync_calendar_event_threads(db_path: str, *, days: Optional[int] = 60) -> Dict[str, Any]:
    """
    Mirror every row in the ``meetings`` table into ``thread_tracking``/``timeline_entries``
    as a single-item ``cal:``-prefixed thread, the same way a Meet-recording note becomes a
    ``meet:``-prefixed thread. Does not call the Google Calendar API itself; the ``meetings``
    table is kept fresh separately by ``services.calendar_availability_export``.
    """
    from utils.database import fetch_meetings_rows, upsert_thread_tracking, upsert_timeline_entries

    meetings = fetch_meetings_rows(db_path, days=days)
    if not meetings:
        return {"ok": True, "synced": 0, "upserted": 0, "timeline_rows": 0}

    existing = _existing_calendar_tracking_rows(db_path)
    now = _utc_now_iso()

    timeline_rows: List[Dict[str, Any]] = []
    tracking_rows: List[Dict[str, Any]] = []
    for meeting in meetings:
        key = str(meeting.get("dedupe_key") or "").strip()
        if not key:
            continue
        thread_id = calendar_inbox_thread_id(key)
        timeline_rows.append(_timeline_row_from_meeting(meeting, thread_id))
        tracking_rows.append(
            _tracking_row_for_meeting(meeting, existing=existing.get(key), now_iso=now)
        )

    applied = upsert_thread_tracking(db_path, tracking_rows, apply_snooze=True) if tracking_rows else 0
    n_time = upsert_timeline_entries(db_path, timeline_rows) if timeline_rows else 0

    return {
        "ok": True,
        "synced": len(tracking_rows),
        "upserted": applied,
        "timeline_rows": n_time,
    }
