"""Detect summary text that treats past events as still upcoming."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Sequence

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[misc, assignment]

_MONTHS: Dict[str, int] = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}

_NAMED_DATE = re.compile(
    r"\b("
    + "|".join(sorted(_MONTHS.keys(), key=len, reverse=True))
    + r")\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b",
    re.IGNORECASE,
)
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_SLASH_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")

_FUTURE_FRAMING = re.compile(
    r"\b(?:"
    r"upcoming|"
    r"prepare(?:s|d|ing)\s+for|"
    r"ahead\s+of|"
    r"before\s+the|"
    r"leading\s+up\s+to|"
    r"in\s+advance\s+of"
    r")\b",
    re.IGNORECASE,
)


def _scheduler_today() -> date:
    tz_name = (
        __import__("os").getenv("FIVELANES_SCHEDULER_TZ") or "America/New_York"
    ).strip() or "America/New_York"
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo(tz_name)).date()
        except Exception:
            pass
    return datetime.now().date()


def dates_in_text(text: str) -> List[date]:
    """Extract calendar dates embedded in free-form summary prose."""
    out: List[date] = []
    seen: set[date] = set()
    raw = str(text or "")

    for match in _NAMED_DATE.finditer(raw):
        month = _MONTHS.get(match.group(1).lower())
        if not month:
            continue
        try:
            parsed = date(int(match.group(3)), month, int(match.group(2)))
        except ValueError:
            continue
        if parsed not in seen:
            seen.add(parsed)
            out.append(parsed)

    for match in _ISO_DATE.finditer(raw):
        try:
            parsed = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            continue
        if parsed not in seen:
            seen.add(parsed)
            out.append(parsed)

    for match in _SLASH_DATE.finditer(raw):
        try:
            parsed = date(int(match.group(3)), int(match.group(1)), int(match.group(2)))
        except ValueError:
            continue
        if parsed not in seen:
            seen.add(parsed)
            out.append(parsed)

    return out


def text_frames_past_event_as_future(text: str, *, as_of: date | None = None) -> bool:
    """
    True when *text* uses future-oriented framing for an event date before *as_of*.

    Example: "preparing for an upcoming sync scheduled for June 4, 2026" on June 27.
    """
    body = str(text or "").strip()
    if not body or not _FUTURE_FRAMING.search(body):
        return False
    today = as_of or _scheduler_today()
    return any(d < today for d in dates_in_text(body))


def _iter_summary_strings(summary: Dict[str, Any]) -> Iterable[str]:
    for key in ("summary", "suggested_thread_label", "tone_overview"):
        val = summary.get(key)
        if isinstance(val, str) and val.strip():
            yield val

    updates = summary.get("latest_updates")
    if isinstance(updates, list):
        for item in updates:
            if str(item or "").strip():
                yield str(item)

    for key in ("highlights", "current_priorities", "waiting_on_others"):
        val = summary.get(key)
        if isinstance(val, list):
            for item in val:
                if str(item or "").strip():
                    yield str(item)

    steps = summary.get("next_steps")
    if isinstance(steps, list):
        for step in steps:
            if isinstance(step, dict):
                action = str(step.get("action") or "").strip()
                if action:
                    yield action
                by_when = str(step.get("by_when") or "").strip()
                if by_when:
                    yield by_when


def summary_is_temporally_stale(summary: Dict[str, Any], *, as_of: date | None = None) -> bool:
    """True when any summary field treats a past calendar date as still upcoming."""
    if not isinstance(summary, dict):
        return False
    today = as_of or _scheduler_today()
    return any(text_frames_past_event_as_future(text, as_of=today) for text in _iter_summary_strings(summary))


def stale_summary_strings(summary: Dict[str, Any], *, as_of: date | None = None) -> List[str]:
    """Return summary strings that fail the timeliness check (for logging/tests)."""
    if not isinstance(summary, dict):
        return []
    today = as_of or _scheduler_today()
    return [
        text
        for text in _iter_summary_strings(summary)
        if text_frames_past_event_as_future(text, as_of=today)
    ]
