"""
Calendar context for the thread-summary prompt (scheduling availability next_step).

Detection and phrasing are handled by the LLM in ``email_thread_summary``; this module
only loads and formats events for the prompt.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[misc, assignment]

log = logging.getLogger(__name__)

_NONE_BLOCK = "(none — omit scheduling availability next_step)"


def _scheduling_step_disabled() -> bool:
    return (os.getenv("SCHEDULING_AVAILABILITY_STEP_DISABLE") or "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _scheduler_tz_name() -> str:
    return (os.getenv("FIVELANES_SCHEDULER_TZ") or "America/New_York").strip() or "America/New_York"


def _max_calendar_lines() -> int:
    raw = (os.getenv("CALENDAR_SUMMARY_MAX_EVENTS") or "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    try:
        from services.prompts import _load_settings

        return int(_load_settings().get("calendar_summary_max_events") or 100)
    except (FileNotFoundError, OSError, ValueError):
        return 100


def load_calendar_events(
    *,
    db_path: Optional[str] = None,
    project_root: Optional[Path] = None,
) -> Tuple[List[Dict[str, Any]], str]:
    """Return (events, timezone name) for prompt injection."""
    tz_name = _scheduler_tz_name()
    rows: List[Dict[str, Any]] = []
    if db_path and Path(db_path).is_file():
        try:
            from utils.database import fetch_meetings_rows

            rows = fetch_meetings_rows(db_path, days=60)
        except Exception:
            log.warning("Failed to load meetings from %s", db_path, exc_info=True)
    if not rows:
        root = project_root or Path(__file__).resolve().parent.parent
        avail_path = root / "out" / "availability_calendar_latest.json"
        if avail_path.is_file():
            try:
                doc = json.loads(avail_path.read_text(encoding="utf-8"))
                from utils.database import meetings_rows_from_availability_doc

                rows = meetings_rows_from_availability_doc(doc)
                meta = doc.get("meta") if isinstance(doc.get("meta"), dict) else {}
                tz_name = str(meta.get("timezone") or "").strip() or tz_name
            except Exception:
                log.exception("Failed to load availability JSON at %s", avail_path)
    if rows and rows[0].get("timezone"):
        tz_name = str(rows[0]["timezone"]).strip() or tz_name
    events = [
        {
            "summary": r.get("summary") or "",
            "start_iso": r.get("start_iso") or "",
            "end_iso": r.get("end_iso") or "",
            "location": r.get("location") or "",
            "kind": r.get("kind") or "",
        }
        for r in rows
        if str(r.get("start_iso") or "").strip()
    ]
    events.sort(key=lambda e: str(e.get("start_iso") or ""))
    return events, tz_name


def format_calendar_events_block(
    events: List[Dict[str, Any]],
    *,
    max_lines: Optional[int] = None,
) -> str:
    """One line per event for the summary prompt."""
    if not events:
        return _NONE_BLOCK
    cap = max_lines if max_lines is not None else _max_calendar_lines()
    lines: List[str] = []
    for ev in events[:cap]:
        start = str(ev.get("start_iso") or "").strip()
        end = str(ev.get("end_iso") or "").strip() or "(no end)"
        title = str(ev.get("summary") or "(No title)").strip()
        loc = str(ev.get("location") or "").strip()
        kind = str(ev.get("kind") or "").strip()
        loc_part = f" | {loc}" if loc else ""
        kind_part = f" | {kind}" if kind else ""
        lines.append(f"- {start} → {end} | {title}{loc_part}{kind_part}")
    omitted = len(events) - len(lines)
    if omitted > 0:
        lines.append(f"- … {omitted} more event(s) omitted")
    return "\n".join(lines)


def calendar_context_for_summary_prompt(
    *,
    db_path: Optional[str] = None,
    project_root: Optional[Path] = None,
) -> Tuple[str, str]:
    """
    Return ``(calendar_events_block, calendar_timezone)`` for ``format_thread_summary_prompt``.
    """
    if _scheduling_step_disabled():
        return _NONE_BLOCK, _scheduler_tz_name()
    events, tz_name = load_calendar_events(db_path=db_path, project_root=project_root)
    return format_calendar_events_block(events), tz_name
