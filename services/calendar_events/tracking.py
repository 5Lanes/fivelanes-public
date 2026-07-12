"""Register which calendar events appear in Threads (opt-in, like Slack DMs/Meet notes)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Set

from services.email.config import SOURCE_ACCOUNT, SOURCE_OAUTH_ACCOUNT_ID
from services.thread_snooze import ACTIVE, is_removed, normalize_state
from utils.conversation_sources import SOURCE_PREFIXES, make_source_key, parse_source_key

log = logging.getLogger(__name__)

CALENDAR_THREAD_PREFIX = SOURCE_PREFIXES["calendar"]
CALENDAR_KIND = "calendar_event"
CALENDAR_PAUSED_KIND = "calendar_event_paused"


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
    attendees_str = ", ".join(attendees)
    return {
        "source_id": thread_id,
        "type": "meeting",
        "datetime": meeting.get("start_iso") or "",
        "sender": attendees_str,
        "recipients": attendees_str,
        "participants": attendees_str,
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


def _calendar_delivery_kind(row: Dict[str, Any]) -> str:
    return str(row.get("inbox_delivery_kind") or "").strip()


def _is_calendar_tracking_row(row: Dict[str, Any]) -> bool:
    tid = str(row.get("inbox_thread_id") or "").strip()
    kind = _calendar_delivery_kind(row)
    return tid.startswith(CALENDAR_THREAD_PREFIX) or kind in (CALENDAR_KIND, CALENDAR_PAUSED_KIND)


def _is_sync_calendar_row(row: Dict[str, Any]) -> bool:
    if is_removed(row.get("snoozed")):
        return False
    if not _is_calendar_tracking_row(row):
        return False
    kind = _calendar_delivery_kind(row)
    return kind in ("", CALENDAR_KIND)


def fetch_visible_meeting_keys(db_path: str) -> List[str]:
    """All calendar events still shown on the dashboard (syncing or paused)."""
    from utils.database import fetch_thread_tracking_rows, load_lane_thread_memberships

    out: Set[str] = set()
    for row in fetch_thread_tracking_rows(db_path):
        if is_removed(row.get("snoozed")):
            continue
        if not _is_calendar_tracking_row(row):
            continue
        key = parse_calendar_inbox_thread_id(str(row.get("inbox_thread_id") or ""))
        if key:
            out.add(key)
    for thread_ids in load_lane_thread_memberships(db_path).values():
        for tid in thread_ids:
            key = parse_calendar_inbox_thread_id(tid)
            if key:
                out.add(key)
    return sorted(out)


def fetch_tracked_calendar_dedupe_keys(db_path: str) -> List[str]:
    """Calendar event dedupe keys selected for tracking, summarize, and sync updates."""
    from utils.database import fetch_thread_tracking_rows

    out: List[str] = []
    for row in fetch_thread_tracking_rows(db_path):
        if not _is_sync_calendar_row(row):
            continue
        key = parse_calendar_inbox_thread_id(str(row.get("inbox_thread_id") or ""))
        if key:
            out.append(key)
    return sorted(set(out))


def _existing_calendar_tracking_rows(db_path: str) -> Dict[str, Dict[str, Any]]:
    from utils.database import fetch_thread_tracking_rows

    by_key: Dict[str, Dict[str, Any]] = {}
    for row in fetch_thread_tracking_rows(db_path):
        key = parse_calendar_inbox_thread_id(str(row.get("inbox_thread_id") or ""))
        if key:
            by_key[key] = row
    return by_key


def _paused_tracking_row(row: Dict[str, Any], *, now_iso: str) -> Dict[str, Any]:
    return {
        "inbox_thread_id": str(row.get("inbox_thread_id") or "").strip(),
        "gmail_inbox_thread_id": str(row.get("gmail_inbox_thread_id") or ""),
        "source_email": str(row.get("source_email") or "").strip(),
        "snoozed": normalize_state(row.get("snoozed")),
        "inner_rfc_message_id": str(row.get("inner_rfc_message_id") or ""),
        "resolved_oauth_account_id": str(row.get("resolved_oauth_account_id") or ""),
        "resolution_error": str(row.get("resolution_error") or ""),
        "inbox_delivery_kind": CALENDAR_PAUSED_KIND,
        "created_at": str(row.get("created_at") or now_iso),
        "updated_at": now_iso,
    }


def set_tracked_meeting_keys(db_path: str, dedupe_keys: Iterable[str]) -> Dict[str, Any]:
    """
    Enable sync for selected calendar events (by ``dedupe_key``): each becomes its own
    ``cal:``-prefixed thread in ``thread_tracking``/``timeline_entries``, the same way a
    Slack DM or Meet recording is opted into tracking.

    Other known calendar rows are paused (still visible on the dashboard, but not
    re-synced or re-summarized until checked again). Selecting a calendar event here only
    tracks it as its own thread — it never merges it into another conversation or lane;
    that always requires a separate, explicit user action (e.g. "add to lane").
    """
    from utils.database import fetch_meetings_rows, upsert_thread_tracking, upsert_timeline_entries

    desired: Set[str] = {str(k).strip() for k in dedupe_keys if str(k).strip()}
    now = _utc_now_iso()
    existing = _existing_calendar_tracking_rows(db_path)

    meetings_by_key = {
        str(m.get("dedupe_key") or "").strip(): m
        for m in fetch_meetings_rows(db_path)
        if str(m.get("dedupe_key") or "").strip()
    }

    timeline_rows: List[Dict[str, Any]] = []
    tracking_rows: List[Dict[str, Any]] = []
    missing: List[str] = []
    for key in sorted(desired):
        meeting = meetings_by_key.get(key)
        if meeting is None:
            missing.append(key)
            continue
        thread_id = calendar_inbox_thread_id(key)
        timeline_rows.append(_timeline_row_from_meeting(meeting, thread_id))
        tracking_rows.append(
            _tracking_row_for_meeting(meeting, existing=existing.get(key), now_iso=now)
        )

    paused = 0
    for key, row in existing.items():
        if key in desired:
            continue
        if is_removed(row.get("snoozed")):
            continue
        if _calendar_delivery_kind(row) == CALENDAR_PAUSED_KIND:
            continue
        tracking_rows.append(_paused_tracking_row(row, now_iso=now))
        paused += 1

    applied = upsert_thread_tracking(db_path, tracking_rows, apply_snooze=True) if tracking_rows else 0
    n_time = upsert_timeline_entries(db_path, timeline_rows) if timeline_rows else 0

    tracked = sorted(desired - set(missing))
    return {
        "ok": True,
        "tracked": tracked,
        "tracked_count": len(tracked),
        "upserted": applied,
        "timeline_rows": n_time,
        "paused": paused,
        "untracked": 0,
        "missing": missing,
    }
