"""Normalize counterparty_availability slots from LLM thread summaries."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set, Tuple

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", re.I)


def _normalize_hm(raw: str) -> Optional[str]:
    text = str(raw or "").strip()
    if not text:
        return None
    m = _TIME_RE.match(text)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    mer = (m.group(3) or "").lower()
    if mer == "am":
        if hour == 12:
            hour = 0
    elif mer == "pm":
        if hour != 12:
            hour += 12
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def _normalize_date(raw: str) -> Optional[str]:
    text = str(raw or "").strip()
    if not text or not _DATE_RE.match(text):
        return None
    return text


def _slot_key(date: str, start: str, end: str, party: str) -> Tuple[str, str, str, str]:
    return (date, start, end, party)


def normalize_counterparty_availability(raw: Any) -> List[Dict[str, Any]]:
    """
    Coerce LLM output into ``[{date, start, end, party?, label?}, ...]``.

    ``date`` is ``YYYY-MM-DD``. ``start``/``end`` are local ``HH:MM`` (24h).
    """
    if not isinstance(raw, list):
        return []

    out: List[Dict[str, Any]] = []
    seen: Set[Tuple[str, str, str, str]] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        date = _normalize_date(str(item.get("date") or ""))
        start = _normalize_hm(str(item.get("start") or ""))
        end = _normalize_hm(str(item.get("end") or ""))
        if not date or not start or not end:
            continue
        if start >= end:
            continue
        party = str(item.get("party") or "").strip()
        label = str(item.get("label") or "").strip()
        key = _slot_key(date, start, end, party)
        if key in seen:
            continue
        seen.add(key)
        slot: Dict[str, Any] = {"date": date, "start": start, "end": end}
        if party:
            slot["party"] = party
        if label:
            slot["label"] = label
        out.append(slot)
    return out
