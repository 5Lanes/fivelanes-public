"""Read the ``meetings`` table as a catalog for the calendar setup UI."""

from __future__ import annotations

from typing import Any, Dict, List


def list_meeting_catalog(db_path: str) -> List[Dict[str, Any]]:
    """Catalog rows for the setup UI (dedupe_key, label, dates, attendees)."""
    from utils.database import fetch_meetings_rows

    catalog: List[Dict[str, Any]] = []
    for meeting in fetch_meetings_rows(db_path):
        key = str(meeting.get("dedupe_key") or "").strip()
        if not key:
            continue
        catalog.append(
            {
                "id": key,
                "dedupe_key": key,
                "label": str(meeting.get("summary") or "").strip() or "(No title)",
                "start_iso": str(meeting.get("start_iso") or ""),
                "end_iso": str(meeting.get("end_iso") or ""),
                "location": str(meeting.get("location") or ""),
                "attendees": meeting.get("attendees") or [],
                "account_id": str(meeting.get("account_id") or ""),
            }
        )
    catalog.sort(key=lambda row: row.get("start_iso") or "", reverse=True)
    return catalog
