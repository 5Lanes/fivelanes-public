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
_NAMED_DATE_NO_YEAR = re.compile(
    r"\b("
    + "|".join(sorted(_MONTHS.keys(), key=len, reverse=True))
    + r")\s+(\d{1,2})(?:st|nd|rd|th)?(?!\s*,?\s*\d{4})\b",
    re.IGNORECASE,
)
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_SLASH_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")

_FUTURE_FRAMING = re.compile(
    r"\b(?:"
    r"upcoming|"
    r"prepar(?:e|es|ed|ing)\s+for|"
    r"(?:has|have)\s+scheduled\b|"
    r"(?:has|have)\s+set\s+up\b|"
    r"(?:is|are)\s+scheduled\b|"
    r"scheduled\s+for|"
    r"set\s+up|"
    r"confirming|"
    r"next\s+steps?\s+include|"
    r"ahead\s+of|"
    r"before\s+the|"
    r"leading\s+up\s+to|"
    r"in\s+advance\s+of"
    r")\b",
    re.IGNORECASE,
)

_MONTH_WORD = "|".join(sorted(_MONTHS.keys(), key=len, reverse=True))

_SCHEDULING_PRESENT = re.compile(
    r"\b(?:"
    r"(?:has|have)\s+scheduled\b|"
    r"(?:has|have)\s+set\s+up\b|"
    r"(?:is|are)\s+scheduled\b|"
    r"prepar(?:e|es|ed|ing)\s+for|"
    r"upcoming|"
    r"scheduled\s+for"
    r")\b",
    re.IGNORECASE,
)

_FOR_MONTH_DAY = re.compile(
    rf"\bfor ({_MONTH_WORD})\s+(\d{{1,2}})",
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


def _choose_year(month: int, day: int, *, as_of: date) -> int:
    """Pick the most likely calendar year for a month/day without an explicit year."""
    candidate = date(as_of.year, month, day)
    window_days = 180
    if (as_of - candidate).days > window_days:
        return as_of.year + 1
    if (candidate - as_of).days > window_days:
        return as_of.year - 1
    return as_of.year


def _add_parsed_date(out: List[date], seen: set[date], parsed: date) -> None:
    if parsed not in seen:
        seen.add(parsed)
        out.append(parsed)


def dates_in_text(text: str, *, as_of: date | None = None) -> List[date]:
    """Extract calendar dates embedded in free-form summary prose."""
    out: List[date] = []
    seen: set[date] = set()
    raw = str(text or "")
    anchor = as_of or _scheduler_today()

    for match in _NAMED_DATE.finditer(raw):
        month = _MONTHS.get(match.group(1).lower())
        if not month:
            continue
        try:
            parsed = date(int(match.group(3)), month, int(match.group(2)))
        except ValueError:
            continue
        _add_parsed_date(out, seen, parsed)

    for match in _NAMED_DATE_NO_YEAR.finditer(raw):
        month = _MONTHS.get(match.group(1).lower())
        if not month:
            continue
        day = int(match.group(2))
        try:
            year = _choose_year(month, day, as_of=anchor)
            parsed = date(year, month, day)
        except ValueError:
            continue
        _add_parsed_date(out, seen, parsed)

    for match in _ISO_DATE.finditer(raw):
        try:
            parsed = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            continue
        _add_parsed_date(out, seen, parsed)

    for match in _SLASH_DATE.finditer(raw):
        try:
            parsed = date(int(match.group(3)), int(match.group(1)), int(match.group(2)))
        except ValueError:
            continue
        _add_parsed_date(out, seen, parsed)

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
    return any(d < today for d in dates_in_text(body, as_of=today))


def _sentence_references_past_date(sentence: str, *, as_of: date) -> bool:
    return any(d < as_of for d in dates_in_text(sentence, as_of=as_of))


def _reframe_sentence_to_past(sentence: str) -> str:
    out = sentence
    out = re.sub(r"\bhas scheduled\b", "had", out, flags=re.I)
    out = re.sub(r"\bhave scheduled\b", "had", out, flags=re.I)
    out = re.sub(r"\bhas set up\b", "had set up", out, flags=re.I)
    out = re.sub(r"\bhave set up\b", "had set up", out, flags=re.I)
    out = re.sub(r"\bis scheduled\b", "had", out, flags=re.I)
    out = re.sub(r"\bare scheduled\b", "had", out, flags=re.I)
    out = _FOR_MONTH_DAY.sub(r"on \1 \2", out)
    return out


def reframe_past_events_in_text(text: str, *, as_of: date | None = None) -> str:
    """Rewrite present-tense scheduling language when the sentence cites a past date."""
    body = str(text or "").strip()
    if not body:
        return body
    today = as_of or _scheduler_today()
    parts = re.split(r"(?<=[.!?])\s+", body)
    reframed: List[str] = []
    for part in parts:
        sent = part.strip()
        if not sent:
            continue
        if _sentence_references_past_date(sent, as_of=today) and _SCHEDULING_PRESENT.search(sent):
            sent = _reframe_sentence_to_past(sent)
        reframed.append(sent)
    return " ".join(reframed)


def reframe_summary_temporal_fields(summary: Dict[str, Any], *, as_of: date | None = None) -> Dict[str, Any]:
    """Apply sentence-level past-tense reframing across summary-shaped dicts."""
    if not isinstance(summary, dict):
        return summary
    today = as_of or _scheduler_today()
    out = dict(summary)
    for key in ("summary", "suggested_thread_label", "tone_overview", "tone"):
        if isinstance(out.get(key), str):
            out[key] = reframe_past_events_in_text(str(out[key]), as_of=today)
    for key in ("latest_updates", "highlights", "current_priorities", "waiting_on_others", "pending_items"):
        val = out.get(key)
        if isinstance(val, list):
            out[key] = [reframe_past_events_in_text(str(item), as_of=today) for item in val]
    steps = out.get("next_steps")
    if isinstance(steps, list):
        new_steps: List[Any] = []
        for step in steps:
            if isinstance(step, dict):
                row = dict(step)
                for field in ("action", "by_when"):
                    if isinstance(row.get(field), str):
                        row[field] = reframe_past_events_in_text(str(row[field]), as_of=today)
                new_steps.append(row)
            else:
                new_steps.append(reframe_past_events_in_text(str(step), as_of=today))
        out["next_steps"] = new_steps
    return out


def summary_as_of_is_stale(summary: Dict[str, Any], *, as_of: date | None = None) -> bool:
    """True when a cached summary was generated on a prior calendar day."""
    if not isinstance(summary, dict):
        return False
    from services.prompts import summary_as_of_date

    today = summary_as_of_date() if as_of is None else as_of.isoformat()
    stored = str(summary.get("summary_as_of_date") or "").strip()
    if stored:
        return stored != today
    updated = str(summary.get("updated_at") or "").strip()[:10]
    return bool(updated and len(updated) >= 10 and updated != today)


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
    return any(
        text_frames_past_event_as_future(text, as_of=today)
        for text in _iter_summary_strings(summary)
    )


def lane_summary_is_stale(summary: Dict[str, Any], *, as_of: date | None = None) -> bool:
    """True when a lane roll-up should be regenerated for today's date."""
    return summary_as_of_is_stale(summary, as_of=as_of)


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
