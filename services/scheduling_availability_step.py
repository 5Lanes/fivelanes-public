"""
Deterministic availability check for the scheduling-ask-detection step.

``services.prompts.format_scheduling_ask_prompt`` asks a small, focused model call whether
the last message in a thread proposes a specific day/time window and, if so, extracts it.
This module takes that extracted window and checks it against the owner's real busy/free
time — in code, not another model call, since interval overlap is exact arithmetic with one
right answer, not a judgment call to leave to an LLM.

Source is strictly the ``busy_with_buffers_iso`` list from the availability-pull JSON built
by ``services.calendar_availability_export`` — start/end instants only, never event titles,
locations, or attendees, so this never has anything to leak beyond "busy" or "free".
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


def _scheduling_step_disabled() -> bool:
    return (os.getenv("SCHEDULING_AVAILABILITY_STEP_DISABLE") or "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _scheduler_tz_name() -> str:
    return (os.getenv("FIVELANES_SCHEDULER_TZ") or "America/New_York").strip() or "America/New_York"


def load_busy_windows(*, project_root: Optional[Path] = None) -> Tuple[List[Tuple[str, str]], str]:
    """
    Return ``(busy_windows, timezone name)``.

    ``db_path``/the raw ``meetings`` table are deliberately never read here: that table
    carries full event titles and locations (including sensitive personal events unrelated
    to any given thread) and has no place in this check, whose only legitimate purpose is
    "is the owner already busy then."
    """
    tz_name = _scheduler_tz_name()
    from utils.runtime_paths import data_path

    avail_path = data_path("out", "availability_calendar_latest.json")
    if not avail_path.is_file():
        return [], tz_name
    try:
        doc = json.loads(avail_path.read_text(encoding="utf-8"))
    except Exception:
        log.exception("Failed to load availability JSON at %s", avail_path)
        return [], tz_name
    meta = doc.get("meta") if isinstance(doc.get("meta"), dict) else {}
    tz_name = str(meta.get("timezone") or "").strip() or tz_name
    busy_raw = doc.get("busy_with_buffers_iso")
    windows: List[Tuple[str, str]] = []
    if isinstance(busy_raw, list):
        for item in busy_raw:
            if not isinstance(item, dict):
                continue
            start = str(item.get("start") or "").strip()
            end = str(item.get("end") or "").strip()
            if start and end:
                windows.append((start, end))
    windows.sort(key=lambda w: w[0])
    return windows, tz_name


def _parse_local_datetime(date_s: str, time_s: str, tz: Any) -> Optional[datetime]:
    try:
        d = datetime.strptime(date_s.strip(), "%Y-%m-%d").date()
        h, m = (int(p) for p in time_s.strip().split(":", 1))
    except (ValueError, AttributeError):
        return None
    try:
        return datetime(d.year, d.month, d.day, h, m, tzinfo=tz)
    except ValueError:
        return None


def _parse_iso_utc(s: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _overlaps_any(
    window_start_utc: datetime, window_end_utc: datetime, busy_windows: List[Tuple[str, str]]
) -> bool:
    for b_start_s, b_end_s in busy_windows:
        b_start = _parse_iso_utc(b_start_s)
        b_end = _parse_iso_utc(b_end_s)
        if b_start is None or b_end is None:
            continue
        if window_start_utc < b_end and b_start < window_end_utc:
            return True
    return False


def check_proposed_windows_availability(
    proposed_windows: List[Dict[str, str]],
    *,
    project_root: Optional[Path] = None,
) -> Optional[str]:
    """
    Given windows extracted by the scheduling-ask-detection step (each a dict with
    ``date``/``start``/``end`` in the scheduler's local timezone), return one
    ``"Calendar for proposed time: ..."`` sentence, or ``None`` if there's nothing to check
    (feature disabled, no windows given, or no usable date/time in the first window).

    Only the first window is checked — the detection prompt may return several candidates
    when a message names more than one option, but the next_steps item this feeds is a
    single action.
    """
    if _scheduling_step_disabled() or not proposed_windows:
        return None
    from utils.features import is_enabled

    if not is_enabled("availability"):
        return None

    win = proposed_windows[0]
    date_s = str(win.get("date") or "").strip()
    start_s = str(win.get("start") or "").strip()
    end_s = str(win.get("end") or "").strip()
    if not date_s or not start_s:
        return None
    if not end_s:
        end_s = start_s

    busy_windows, tz_name = load_busy_windows(project_root=project_root)
    tz = ZoneInfo(tz_name) if ZoneInfo is not None else timezone.utc

    start_local = _parse_local_datetime(date_s, start_s, tz)
    end_local = _parse_local_datetime(date_s, end_s, tz)
    if start_local is None:
        return None
    if end_local is None or end_local <= start_local:
        end_local = start_local
        end_local = start_local.replace(hour=min(start_local.hour + 1, 23))

    busy = _overlaps_any(start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc), busy_windows)
    window_label = f"{date_s} {start_s}-{end_s}"
    if busy:
        return f"On {window_label} you already have a commitment then."
    return f"On {window_label} you have no commitments in that window."
