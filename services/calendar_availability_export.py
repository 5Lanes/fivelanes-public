"""
Build availability JSON from Google Calendar (connected accounts; optionally a
subset of calendars from scheduling rules) plus optional scheduling rules for
buffers and timezone.

Used by ``scripts/pull_calendar_availability.py`` and ``dashboard_server`` scheduler.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .calendar_service import list_calendar_list_entries, pull_calendar_events_time_window
from .gmail_client import list_connected_accounts

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[misc, assignment]

log = logging.getLogger(__name__)


def load_scheduling_rules(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        log.warning("Scheduling rules not found at %s — using built-in buffer defaults", path)
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.warning("Could not read %s: %s — using defaults", path, e)
        return {}


def _rules_timezone(rules: Dict[str, Any]) -> str:
    tz = (rules.get("timezone") or "").strip()
    if tz and not tz.startswith("REPLACE"):
        return tz
    return "America/New_York"


def include_calendar_pairs_from_rules(rules: Dict[str, Any]) -> Optional[Set[Tuple[str, str]]]:
    """
    If ``rules`` contains non-empty ``availability_include_calendars`` (list of
    ``{"account_id", "calendar_id"}``), return that set of pairs. Otherwise return
    ``None`` meaning every calendar is included.
    """
    raw = rules.get("availability_include_calendars")
    if not isinstance(raw, list) or not raw:
        return None
    out: Set[Tuple[str, str]] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        aid = str(item.get("account_id") or "").strip()
        cid = str(item.get("calendar_id") or "").strip()
        if aid and cid:
            out.add((aid, cid))
    if not out:
        log.warning("availability_include_calendars is set but no valid entries — including all calendars")
        return None
    return out


def write_availability_calendar_selection(
    rules_path: Path,
    pairs: Optional[List[Tuple[str, str]]],
) -> None:
    """
    Merge ``availability_include_calendars`` into the rules JSON at ``rules_path``.

    Pass ``pairs=None`` to remove the key and include every calendar again.
    """
    rules_path = rules_path.resolve()
    data: Dict[str, Any] = {}
    if rules_path.is_file():
        try:
            with open(rules_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            log.warning("Could not read %s (%s); writing new file", rules_path, e)
            data = {}
    if not isinstance(data, dict):
        data = {}
    if pairs is None:
        data.pop("availability_include_calendars", None)
    else:
        uniq = sorted({(str(a).strip(), str(c).strip()) for a, c in pairs if a and c})
        data["availability_include_calendars"] = [
            {"account_id": a, "calendar_id": c} for a, c in uniq
        ]
    rules_path.parent.mkdir(parents=True, exist_ok=True)
    with open(rules_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _buffer_minutes(rules: Dict[str, Any]) -> Tuple[int, int, int, int]:
    mb = rules.get("meeting_buffers_minutes") or {}
    ip = mb.get("in_person") or {}
    vi = mb.get("virtual") or {}
    return (
        int(ip.get("before", 60)),
        int(ip.get("after", 60)),
        int(vi.get("before", 30)),
        int(vi.get("after", 30)),
    )


def _virtual_substrings(rules: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    vd = (rules.get("meeting_buffers_minutes") or {}).get("virtual_detection") or {}
    loc = [str(x).lower() for x in (vd.get("location_substrings_case_insensitive") or []) if x]
    tit = [str(x).lower() for x in (vd.get("title_substrings_case_insensitive") or []) if x]
    if not loc:
        loc = [
            "zoom.us",
            "teams.microsoft",
            "meet.google",
            "google meet",
            "webex",
            "http://",
            "https://",
        ]
    if not tit:
        tit = ["zoom:", "google meet", "teams meeting", "webex"]
    return loc, tit


def _is_all_day(ev: Dict[str, Any]) -> bool:
    start = ev.get("start") or {}
    return bool(start.get("date") and not start.get("dateTime"))


def _event_blocks_time(ev: Dict[str, Any]) -> bool:
    """False when Google marks the event as free (TRANSPARENT / transparency=transparent)."""
    t = str(ev.get("transparency") or "opaque").strip().lower()
    return t != "transparent"


def _classify_event(ev: Dict[str, Any], rules: Dict[str, Any]) -> str:
    if _is_all_day(ev):
        return "all_day"
    loc_s = (ev.get("location") or "").strip().lower()
    title_s = (ev.get("summary") or "").strip().lower()
    hangout = (ev.get("hangoutLink") or "").strip()
    conf = ev.get("conferenceData") or {}
    entry_points = conf.get("entryPoints") or []
    loc_subs, title_subs = _virtual_substrings(rules)
    for s in loc_subs:
        if s in loc_s:
            return "virtual"
    for s in title_subs:
        if s in title_s:
            return "virtual"
    if hangout or entry_points:
        if not loc_s or "http://" in loc_s or "https://" in loc_s:
            return "virtual"
    if not loc_s:
        return "virtual"
    return "in_person"


def _parse_iso_utc(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _to_rfc3339_z(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _utc_to_local_date_hm(dt_utc: datetime, tz_name: str) -> Tuple[str, str]:
    if ZoneInfo is None:
        raise RuntimeError("zoneinfo is required (Python 3.9+).")
    loc = dt_utc.astimezone(ZoneInfo(tz_name))
    return loc.strftime("%Y-%m-%d"), loc.strftime("%H:%M")


def _buffered_bounds(
    start_iso: str,
    end_iso: Optional[str],
    kind: str,
    ib: int,
    ia: int,
    vb: int,
    va: int,
) -> Tuple[datetime, datetime]:
    start = _parse_iso_utc(start_iso)
    if start is None:
        raise ValueError("invalid start")
    end = _parse_iso_utc(end_iso) if end_iso else None
    if end is None or end <= start:
        end = start + timedelta(hours=1)
    if kind == "all_day":
        b_before, b_after = 0, 0
    elif kind == "virtual":
        b_before, b_after = vb, va
    else:
        b_before, b_after = ib, ia
    return start - timedelta(minutes=b_before), end + timedelta(minutes=b_after)


def _week_key_local(start_utc: datetime, tz_name: str) -> str:
    if ZoneInfo is None:
        return ""
    loc = start_utc.astimezone(ZoneInfo(tz_name))
    return f"{loc.isocalendar().year}-W{loc.isocalendar().week:02d}"


_WEEKDAY_CODES = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]


def _weekday_code(d: date) -> str:
    return _WEEKDAY_CODES[d.weekday()]


def _parse_hm(s: Optional[str]) -> Optional[Tuple[int, int]]:
    if not isinstance(s, str):
        return None
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", s)
    if not m:
        return None
    h, mm = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mm <= 59):
        return None
    return h, mm


def _local_dt(d: date, hm: Tuple[int, int], tz: Any) -> datetime:
    return datetime(d.year, d.month, d.day, hm[0], hm[1], tzinfo=tz)


def _merge_simple(ivs: List[Tuple[datetime, datetime]]) -> List[Tuple[datetime, datetime]]:
    if not ivs:
        return []
    s = sorted(ivs, key=lambda x: x[0])
    out: List[List[datetime]] = [[s[0][0], s[0][1]]]
    for ns, ne in s[1:]:
        if ns <= out[-1][1]:
            if ne > out[-1][1]:
                out[-1][1] = ne
        else:
            out.append([ns, ne])
    return [(a, b) for a, b in out]


def _merge_with_ids(
    ivs: List[Tuple[datetime, datetime, str]],
) -> List[Tuple[datetime, datetime, str]]:
    if not ivs:
        return []
    s = sorted(ivs, key=lambda x: (x[0], x[1]))
    out: List[List[Any]] = [[s[0][0], s[0][1], s[0][2]]]
    for ns, ne, nid in s[1:]:
        if ns <= out[-1][1]:
            if ne > out[-1][1]:
                out[-1][1] = ne
            existing = [x for x in str(out[-1][2]).split(" + ") if x]
            if nid and nid not in existing:
                out[-1][2] = " + ".join(existing + [nid])
        else:
            out.append([ns, ne, nid])
    return [(a, b, str(c)) for a, b, c in out]


def _subtract_intervals(
    base: List[Tuple[datetime, datetime]],
    to_remove: List[Tuple[datetime, datetime]],
) -> List[Tuple[datetime, datetime]]:
    """Return ``base`` minus ``to_remove``. All datetimes must be timezone-aware."""
    if not base:
        return []
    if not to_remove:
        return list(base)
    removes = _merge_simple(to_remove)
    result: List[Tuple[datetime, datetime]] = []
    for bs, be in base:
        cur = bs
        for rs, re_ in removes:
            if re_ <= cur:
                continue
            if rs >= be:
                break
            if rs > cur:
                result.append((cur, rs))
            if re_ > cur:
                cur = re_
            if cur >= be:
                break
        if cur < be:
            result.append((cur, be))
    return [(s, e) for s, e in result if e > s]


def _parenting_blocks(rules: Dict[str, Any]) -> Dict[str, Any]:
    """Scheduling rules block for recurring parenting-time unavailable windows."""
    block = rules.get("parenting_blocks")
    if isinstance(block, dict):
        return block
    legacy = rules.get("child_custody_blocks")
    return legacy if isinstance(legacy, dict) else {}


def _child_home_meeting_policy(rules: Dict[str, Any]) -> Dict[str, Any]:
    block = rules.get("child_home_meeting_policy")
    if isinstance(block, dict):
        return block
    legacy = rules.get("son_home_meeting_policy")
    return legacy if isinstance(legacy, dict) else {}


def _alternating_anchor(rules: Dict[str, Any]) -> Tuple[Optional[date], Optional[bool]]:
    """Returns (anchor_thursday, anchor_has_thursday_morning) or (None, None) if unusable."""
    awa = _parenting_blocks(rules).get("alternating_week_anchor") or {}
    anchor_iso = str(awa.get("anchor_date_iso") or "").strip()
    if not anchor_iso or anchor_iso.startswith("REPLACE"):
        return None, None
    try:
        anchor = date.fromisoformat(anchor_iso)
    except ValueError:
        log.warning("alternating_week_anchor.anchor_date_iso is not a valid YYYY-MM-DD; alternating parenting rules will be skipped.")
        return None, None
    if anchor.weekday() != 3:  # Thursday
        log.warning("alternating_week_anchor.anchor_date_iso (%s) is not a Thursday; alternating parenting rules will be skipped.", anchor_iso)
        return None, None
    return anchor, bool(awa.get("anchor_has_thursday_morning", True))


def _thursday_morning_on(
    d_local: date,
    anchor: Optional[date],
    anchor_on: Optional[bool],
) -> Optional[bool]:
    """Whether the given local date's week has Thursday-morning parenting time, or None if unresolvable."""
    if anchor is None or anchor_on is None:
        return None
    week_start = d_local - timedelta(days=d_local.weekday())
    anchor_week_start = anchor - timedelta(days=anchor.weekday())
    weeks_delta = (week_start - anchor_week_start).days // 7
    return anchor_on if (weeks_delta % 2 == 0) else (not anchor_on)


def _parenting_intervals_for_date(
    d_local: date,
    tz: Any,
    rules: Dict[str, Any],
    anchor: Optional[date],
    anchor_on: Optional[bool],
) -> List[Tuple[datetime, datetime, str]]:
    """Build the merged local-time parenting intervals applying to ``d_local``."""
    parenting = _parenting_blocks(rules)
    items = parenting.get("recurring_occupied") or []
    if not isinstance(items, list):
        return []
    wcode = _weekday_code(d_local)
    thu_on = _thursday_morning_on(d_local, anchor, anchor_on)
    raw: List[Tuple[datetime, datetime, str]] = []
    for r in items:
        if not isinstance(r, dict):
            continue
        days = [str(x).upper() for x in (r.get("weekdays") or []) if x]
        if wcode not in days:
            continue
        hs = _parse_hm(str(r.get("from_local") or ""))
        he = _parse_hm(str(r.get("to_local") or ""))
        if not hs or not he:
            continue
        every_week = bool(r.get("every_week", True))
        if not every_week:
            spec = str(r.get("on_alternating_weeks") or "").strip().lower()
            if thu_on is None:
                continue
            if spec == "same_parity_as_anchor_thursday_morning":
                if not thu_on:
                    continue
            elif spec == "opposite_parity_to_wednesday_afternoon_week":
                # Wednesday-afternoon week == Thursday-morning week (per rules).
                # "Opposite" therefore means: skip on Thursday-morning weeks.
                if thu_on:
                    continue
            else:
                continue
        start_l = _local_dt(d_local, hs, tz)
        end_l = _local_dt(d_local, he, tz)
        if end_l > start_l:
            raw.append((start_l, end_l, str(r.get("id") or "")))
    return _merge_with_ids(raw)


def _scheduling_window(
    rules: Dict[str, Any],
) -> Tuple[List[str], Tuple[int, int], Tuple[int, int], Dict[str, Tuple[Tuple[int, int], Tuple[int, int]]]]:
    """Working-hours config with sensible defaults (Mon-Fri 09:00-17:00)."""
    cfg = rules.get("scheduling_window_local") or {}
    days_raw = cfg.get("weekdays") if isinstance(cfg, dict) else None
    days = [str(x).upper() for x in (days_raw or []) if x] or ["MO", "TU", "WE", "TH", "FR"]
    f = _parse_hm(str((cfg or {}).get("from_local") or "")) or (9, 0)
    t = _parse_hm(str((cfg or {}).get("to_local") or "")) or (17, 0)
    overrides: Dict[str, Tuple[Tuple[int, int], Tuple[int, int]]] = {}
    per = (cfg or {}).get("per_weekday") if isinstance(cfg, dict) else None
    if isinstance(per, dict):
        for k, v in per.items():
            if not isinstance(v, dict):
                continue
            pf = _parse_hm(str(v.get("from_local") or ""))
            pt = _parse_hm(str(v.get("to_local") or ""))
            if pf and pt:
                overrides[str(k).upper()] = (pf, pt)
    return days, f, t, overrides


def _open_windows_for_date(
    d_local: date,
    tz: Any,
    rules: Dict[str, Any],
    parenting_local: List[Tuple[datetime, datetime, str]],
    busy_utc: List[Tuple[datetime, datetime]],
    floor_utc: datetime,
) -> List[Tuple[datetime, datetime]]:
    days, default_from, default_to, overrides = _scheduling_window(rules)
    wcode = _weekday_code(d_local)
    if wcode in overrides:
        f_hm, t_hm = overrides[wcode]
    elif wcode in days:
        f_hm, t_hm = default_from, default_to
    else:
        return []
    start_l = _local_dt(d_local, f_hm, tz)
    end_l = _local_dt(d_local, t_hm, tz)
    if end_l <= start_l:
        return []
    base_utc: List[Tuple[datetime, datetime]] = [
        (start_l.astimezone(timezone.utc), end_l.astimezone(timezone.utc))
    ]
    parenting_utc = [
        (s.astimezone(timezone.utc), e.astimezone(timezone.utc)) for s, e, _ in parenting_local
    ]
    remaining = _subtract_intervals(base_utc, parenting_utc)
    remaining = _subtract_intervals(remaining, busy_utc)
    floor_clean = floor_utc.replace(microsecond=0)
    trimmed: List[Tuple[datetime, datetime]] = []
    for s, e in remaining:
        if e <= floor_clean:
            continue
        if s < floor_clean:
            s = floor_clean
        if e > s:
            trimmed.append((s, e))
    return trimmed


def _normalized_attendee_emails(ev: Dict[str, Any]) -> List[str]:
    """Lowercase unique emails from ``attendees_emails`` on a pulled event row."""
    raw = ev.get("attendees_emails")
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    seen: Set[str] = set()
    for item in raw:
        e = (item if isinstance(item, str) else str(item)).strip().lower()
        if e and "@" in e and e not in seen:
            seen.add(e)
            out.append(e)
    return out


def _merge_duplicate_event_fields(kept: Dict[str, Any], other: Dict[str, Any]) -> None:
    """Union attendees and backfill sparse fields when collapsing duplicate rows."""
    merged = set(_normalized_attendee_emails(kept)) | set(_normalized_attendee_emails(other))
    kept["attendees_emails"] = sorted(merged)

    if not (kept.get("location") or "").strip() and (other.get("location") or "").strip():
        kept["location"] = other.get("location")
    if not (kept.get("htmlLink") or "").strip() and (other.get("htmlLink") or "").strip():
        kept["htmlLink"] = other.get("htmlLink")
    if not (kept.get("description") or "").strip() and (other.get("description") or "").strip():
        kept["description"] = other.get("description")
    if not (kept.get("hangoutLink") or "").strip() and (other.get("hangoutLink") or "").strip():
        kept["hangoutLink"] = other.get("hangoutLink")


def dedupe_calendar_events(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Collapse duplicate calendar rows for the same logical meeting.

    Prefer ``iCalUID`` when present (same invite on multiple calendars); otherwise
    match on title plus start/end instants. Keeps the first sorted row per key and
    merges ``attendees_emails`` (and other sparse fields) from later duplicates.
    """
    kept_by_key: Dict[str, Dict[str, Any]] = {}
    out: List[Dict[str, Any]] = []

    for ev in sorted(events, key=lambda x: (x.get("start_ts") or 0, x.get("summary") or "")):
        ical = (ev.get("iCalUID") or "").strip()
        if ical:
            key = f"ical:{ical}"
        else:
            key = (
                f"fb:{(ev.get('summary') or '').strip()}|"
                f"{(ev.get('start_iso') or '').strip()}|"
                f"{(ev.get('end_iso') or '').strip()}"
            )
        if key in kept_by_key:
            _merge_duplicate_event_fields(kept_by_key[key], ev)
            continue
        kept_by_key[key] = ev
        out.append(ev)
    return out


def build_availability_document(
    events: List[Dict[str, Any]],
    rules: Dict[str, Any],
    *,
    rules_file_display: str,
    weeks: int,
    window_start_utc: datetime,
    window_end_exclusive_utc: datetime,
    calendar_filter_pairs: Optional[Set[Tuple[str, str]]] = None,
) -> Dict[str, Any]:
    event_count_raw = len(events)
    events = dedupe_calendar_events(events)
    events_deduplicated = event_count_raw - len(events)

    tz = _rules_timezone(rules)
    ib, ia, vb, va = _buffer_minutes(rules)
    loc_subs, _tit_subs = _virtual_substrings(rules)

    commitments: List[Dict[str, Any]] = []
    busy: List[Dict[str, Any]] = []
    slim_events: List[Dict[str, Any]] = []

    transparent_skipped = sum(1 for ev in events if not _event_blocks_time(ev))

    for ev in events:
        if not _event_blocks_time(ev):
            continue
        kind = _classify_event(ev, rules)
        start_iso = ev.get("start_iso") or ""
        end_iso = ev.get("end_iso")
        start_u = _parse_iso_utc(start_iso)
        if start_u is None:
            continue
        end_u = _parse_iso_utc(end_iso) if end_iso else None
        if end_u is None or end_u <= start_u:
            end_u = start_u + timedelta(hours=1)

        date_s, hm_start = _utc_to_local_date_hm(start_u, tz)
        _, hm_end = _utc_to_local_date_hm(end_u, tz)
        duration_min = max(1, int((end_u - start_u).total_seconds() // 60))

        summary = ev.get("summary") or "(No title)"
        cal_label = ev.get("calendar_summary") or ""
        acct = ev.get("account_id") or ""

        commitments.append(
            {
                "date": date_s,
                "title": summary,
                "kind": kind,
                "location_hint": (ev.get("location") or "")[:200] or None,
                "start_local": hm_start,
                "assumed_end_local": hm_end,
                "assumed_duration_minutes": duration_min,
                "calendar_summary": cal_label,
                "account_id": acct,
                "html_link": ev.get("htmlLink") or "",
            }
        )

        try:
            a, b = _buffered_bounds(start_iso, end_iso, kind, ib, ia, vb, va)
        except ValueError:
            continue
        src = f"{summary} [{cal_label}] ({kind}+buffer)"
        busy.append({"start": _to_rfc3339_z(a), "end": _to_rfc3339_z(b), "source": src})

        slim_events.append(
            {
                "summary": summary,
                "start_iso": start_iso,
                "end_iso": end_iso,
                "location": (ev.get("location") or "")[:500] or "",
                "html_link": ev.get("htmlLink") or "",
                "kind": kind,
                "calendar_summary": cal_label,
                "account_id": acct,
                "week_local": _week_key_local(start_u, tz),
                "attendees": ev.get("attendees_emails") or [],
            }
        )

    busy.sort(key=lambda x: x.get("start", ""))
    commitments.sort(key=lambda x: (x.get("date", ""), x.get("start_local", "")))

    if ZoneInfo is None:
        raise RuntimeError("zoneinfo is required (Python 3.9+).")
    z = ZoneInfo(tz)

    anchor, anchor_on = _alternating_anchor(rules)
    derive_child_home = bool(
        _child_home_meeting_policy(rules).get(
            "derive_intervals_from_parenting_blocks",
            _child_home_meeting_policy(rules).get("derive_intervals_from_custody_blocks", True),
        )
    )
    busy_utc_pairs: List[Tuple[datetime, datetime]] = []
    for b in busy:
        bs = _parse_iso_utc(b.get("start"))
        be = _parse_iso_utc(b.get("end"))
        if bs and be and be > bs:
            busy_utc_pairs.append((bs, be))
    busy_utc_pairs.sort(key=lambda x: x[0])

    loc_start_date = window_start_utc.astimezone(z).date()
    loc_end_date = (window_end_exclusive_utc - timedelta(seconds=1)).astimezone(z).date()

    parenting_rows: List[Dict[str, Any]] = []
    child_home_rows: List[Dict[str, Any]] = []
    open_rows: List[Dict[str, Any]] = []

    cur = loc_start_date
    while cur <= loc_end_date:
        parenting_local = _parenting_intervals_for_date(cur, z, rules, anchor, anchor_on)
        if parenting_local:
            ivs = [
                {
                    "start": _to_rfc3339_z(s.astimezone(timezone.utc)),
                    "end": _to_rfc3339_z(e.astimezone(timezone.utc)),
                    "id": idv,
                }
                for s, e, idv in parenting_local
            ]
            parenting_rows.append({"date": cur.isoformat(), "intervals_iso": ivs})
            if derive_child_home:
                child_home_rows.append({"date": cur.isoformat(), "intervals_iso": ivs})
        open_intervals = _open_windows_for_date(
            cur, z, rules, parenting_local, busy_utc_pairs, window_start_utc
        )
        if open_intervals:
            open_rows.append(
                {
                    "date": cur.isoformat(),
                    "likely_open_windows": [
                        {"start": _to_rfc3339_z(s), "end": _to_rfc3339_z(e)}
                        for s, e in open_intervals
                    ],
                }
            )
        cur = cur + timedelta(days=1)

    accounts = list_connected_accounts()
    cal_filter_meta: Dict[str, Any] = {
        "mode": "subset" if calendar_filter_pairs else "all",
    }
    if calendar_filter_pairs:
        by_pair = {
            (str(r.get("account_id") or ""), str(r.get("calendar_id") or "")): r
            for r in list_calendar_list_entries()
        }
        details = []
        for aid, cid in sorted(calendar_filter_pairs):
            row = by_pair.get((aid, cid)) or {}
            details.append(
                {
                    "account_id": aid,
                    "calendar_id": cid,
                    "summary": row.get("summary") or cid,
                }
            )
        cal_filter_meta["included_calendars"] = details
        cal_filter_meta["included_count"] = len(calendar_filter_pairs)
    wk_start_s = loc_start_date.isoformat()
    wk_end_s = loc_end_date.isoformat()

    sched_days, sched_from, sched_to, sched_overrides = _scheduling_window(rules)
    scheduling_window_meta: Dict[str, Any] = {
        "weekdays": sched_days,
        "from_local": f"{sched_from[0]:02d}:{sched_from[1]:02d}",
        "to_local": f"{sched_to[0]:02d}:{sched_to[1]:02d}",
    }
    if sched_overrides:
        scheduling_window_meta["per_weekday"] = {
            k: {
                "from_local": f"{v[0][0]:02d}:{v[0][1]:02d}",
                "to_local": f"{v[1][0]:02d}:{v[1][1]:02d}",
            }
            for k, v in sched_overrides.items()
        }

    if _parenting_blocks(rules).get("recurring_occupied"):
        if anchor is None:
            parenting_model = (
                "Computed from credentials/calendar_scheduling_rules.json "
                "(every_week rules only — alternating_week_anchor.anchor_date_iso is missing/invalid)."
            )
        else:
            parenting_model = (
                "Computed from credentials/calendar_scheduling_rules.json "
                f"(recurring_occupied + alternating_week_anchor anchor={anchor.isoformat()})."
            )
    else:
        parenting_model = "No parenting_blocks.recurring_occupied in scheduling rules."

    return {
        "schema_version": 2,
        "document_kind": "availability_calendar_pull",
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "timezone": tz,
            "week_start_date": wk_start_s,
            "week_end_date": wk_end_s,
            "weeks_requested": weeks,
            "window_start_utc": _to_rfc3339_z(window_start_utc),
            "window_end_utc_exclusive": _to_rfc3339_z(window_end_exclusive_utc),
            "event_count": len(events),
            "event_count_before_dedupe": event_count_raw,
            "events_deduplicated": events_deduplicated,
            "transparent_events_excluded": transparent_skipped,
            "accounts_connected": accounts,
            "availability_calendar_filter": cal_filter_meta,
            "scheduling_rules_file": rules_file_display,
            "buffer_rules_applied": {
                "in_person_minutes_before_after": [ib, ia],
                "virtual_minutes_before_after": [vb, va],
                "virtual_detection_location_substrings_count": len(loc_subs),
            },
            "scheduling_window_local": scheduling_window_meta,
            "parenting_model": parenting_model,
        },
        "busy_with_buffers_iso": busy,
        "calendar_events_index": slim_events,
        "parenting_unavailable_local": parenting_rows,
        "child_home_virtual_only_local": child_home_rows,
        "availability_for_new_meetings_iso": open_rows,
    }


def run_calendar_availability_pull(
    project_root: Path,
    *,
    weeks: int = 4,
    rules_path: Optional[Path] = None,
    out_path: Path,
) -> Dict[str, Any]:
    """
    Fetch calendars for ``weeks`` from now, build availability JSON, write ``out_path``.

    Returns the document dict (same as written to disk).
    """
    if ZoneInfo is None:
        raise RuntimeError("Python 3.9+ with zoneinfo is required for calendar availability export.")

    rp = (rules_path or (project_root / "credentials" / "calendar_scheduling_rules.json")).resolve()
    rules = load_scheduling_rules(rp)
    try:
        rules_display = str(rp.relative_to(project_root.resolve()))
    except ValueError:
        rules_display = str(rp)

    now = datetime.now(timezone.utc)
    window_end = now + timedelta(days=7 * weeks)
    cal_pairs = include_calendar_pairs_from_rules(rules)
    if cal_pairs is not None:
        log.info("Calendar availability: restricting to %d calendar(s) from rules", len(cal_pairs))
    events = pull_calendar_events_time_window(
        now,
        window_end,
        max_results_per_page=250,
        include_calendar_pairs=cal_pairs,
    )
    if not events:
        log.warning("Calendar availability: no events returned (check OAuth / calendar scope).")

    doc = build_availability_document(
        events,
        rules,
        rules_file_display=rules_display,
        weeks=weeks,
        window_start_utc=now,
        window_end_exclusive_utc=window_end,
        calendar_filter_pairs=cal_pairs,
    )

    out_path = out_path.resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, ensure_ascii=False)

    db_path = project_root / (os.getenv("DATABASE_NAME") or "timeline.db")
    try:
        from utils.database import replace_meetings_from_availability_doc

        n_meetings = replace_meetings_from_availability_doc(str(db_path.resolve()), doc)
        log.info("Calendar availability: upserted %d meeting(s) into %s", n_meetings, db_path)
    except Exception:
        log.exception("Calendar availability: failed to persist meetings to %s", db_path)

    deduped = doc.get("meta", {}).get("event_count", len(events))
    dropped = doc.get("meta", {}).get("events_deduplicated", 0)
    log.info(
        "Calendar availability: wrote %s (%d events after dedupe, %d dropped, %d weeks)",
        out_path,
        deduped,
        dropped,
        weeks,
    )
    return doc
