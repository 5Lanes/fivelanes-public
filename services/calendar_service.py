"""
Google Calendar API client using same OAuth tokens as gmail_client.
Pulls events (meetings) from connected calendar accounts; matches by attendee emails.
Requires calendar.readonly scope (re-run add_gmail_account.py to add it).
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional, Any, Tuple, Set

from googleapiclient.errors import HttpError

from .gmail_client import list_connected_accounts, _get_credentials
from googleapiclient.discovery import build

log = logging.getLogger(__name__)


def get_all_calendar_services() -> List[tuple]:
    """Return list of (account_id, calendar_service) for all connected accounts."""
    log.info("get_all_calendar_services")
    result: List[tuple] = []
    for aid in list_connected_accounts():
        creds = _get_credentials(aid)
        if creds:
            try:
                svc = build("calendar", "v3", credentials=creds)
                result.append((aid, svc))
            except Exception as e:
                log.warning("Calendar service for %s: %s", aid, e)
    return result


def get_calendar_services_for_account_id(account_id: Optional[str]) -> List[Tuple[str, Any]]:
    """Calendar API for a single OAuth ``account_id`` (key in ``tokens.json``)."""
    if not account_id or not str(account_id).strip():
        log.warning("Calendar OAuth account id is empty (set SOURCE_OAUTH_ACCOUNT_ID in .env)")
        return []
    account_id = str(account_id).strip()
    creds = _get_credentials(account_id)
    if not creds:
        log.warning("No credentials for Calendar account %s", account_id)
        return []
    try:
        return [(account_id, build("calendar", "v3", credentials=creds))]
    except Exception as e:
        log.warning("Calendar service for %s: %s", account_id, e)
        return []


def list_calendar_list_entries() -> List[Dict[str, Any]]:
    """
    Return one row per calendar visible in each connected account's calendar list.

    Each dict has: ``account_id``, ``calendar_id``, ``summary``, ``primary`` (bool),
    ``access_role`` (str). Sorted by ``account_id`` then case-insensitive ``summary``.
    """
    rows: List[Dict[str, Any]] = []
    for account_id, service in get_all_calendar_services():
        try:
            calendar_items = (
                service.calendarList()
                .list(maxResults=250)
                .execute()
                .get("items", [])
            )
        except HttpError as e:
            log.error("Calendar list retrieval error for %s: %s", account_id, e)
            continue

        for cal in calendar_items:
            calendar_id = (cal.get("id") or "").strip()
            if not calendar_id:
                continue
            summary = (cal.get("summary") or "").strip() or calendar_id
            rows.append(
                {
                    "account_id": account_id,
                    "calendar_id": calendar_id,
                    "summary": summary,
                    "primary": bool(cal.get("primary")),
                    "access_role": (cal.get("accessRole") or "").strip(),
                }
            )

    rows.sort(key=lambda r: (r.get("account_id") or "", (r.get("summary") or "").lower()))
    return rows


def _event_start_iso(event: dict) -> Optional[str]:
    """Return event start as ISO string; prefer dateTime, else date (all-day)."""
    start = event.get("start") or {}
    if start.get("dateTime"):
        return start["dateTime"]
    if start.get("date"):
        return start["date"] + "T00:00:00Z"
    return None


def _event_end_iso(event: dict) -> Optional[str]:
    """Return event end as ISO string."""
    end = event.get("end") or {}
    if end.get("dateTime"):
        return end["dateTime"]
    if end.get("date"):
        return end["date"] + "T23:59:59Z"
    return None


def _attendee_emails(event: dict) -> set:
    """Extract lowercase attendee emails from an event."""
    emails = set()
    for a in event.get("attendees") or []:
        e = (a.get("email") or "").strip().lower()
        if e and "@" in e:
            emails.add(e)
    return emails


def _event_contact_emails(event: dict) -> set:
    """Extract participant emails usable for contact matching."""
    emails = set(_attendee_emails(event))
    organizer = ((event.get("organizer") or {}).get("email") or "").strip().lower()
    creator = ((event.get("creator") or {}).get("email") or "").strip().lower()
    if organizer and "@" in organizer:
        emails.add(organizer)
    if creator and "@" in creator:
        emails.add(creator)
    return emails


def _parse_iso_to_ts(iso_str: Optional[str]) -> float:
    """Parse ISO string to Unix timestamp; invalid => 0."""
    if not iso_str:
        return 0.0
    try:
        # Handle Z and ±HH:MM
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


def _to_utc_rfc3339(dt: datetime) -> str:
    """Format datetime as RFC3339 UTC string."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def pull_todays_events_all_calendars(
    max_results_per_calendar: int,
    *,
    lookback_days: int,
    lookforward_days: int,
) -> List[dict]:
    """
    Fetch events from every available calendar for all connected accounts.
    The window is configurable via lookback_days/lookforward_days.

    Returns normalized events:
    id, summary, start_iso, end_iso, start_ts, end_ts, attendees_emails, account_id, calendar_id, calendar_summary.
    """
    services = get_all_calendar_services()
    if not services:
        log.warning("No Calendar service available (not connected or missing calendar scope)")
        return []
    if lookback_days < 0 or lookforward_days < 0:
        raise ValueError("lookback_days and lookforward_days must be >= 0")
    now = datetime.now(timezone.utc)
    today_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    time_min = _to_utc_rfc3339(today_start - timedelta(days=max(0, lookback_days)))
    time_max = _to_utc_rfc3339(today_start + timedelta(days=max(0, lookforward_days) + 1))

    all_events: List[dict] = []
    seen_keys = set()

    for account_id, service in services:
        try:
            calendar_items = (
                service.calendarList()
                .list(maxResults=250)
                .execute()
                .get("items", [])
            )
        except HttpError as e:
            log.error("Calendar list retrieval error for %s: %s", account_id, e)
            continue

        for cal in calendar_items:
            calendar_id = (cal.get("id") or "").strip()
            if not calendar_id:
                continue
            calendar_summary = (cal.get("summary") or "").strip() or calendar_id
            try:
                result = (
                    service.events()
                    .list(
                        calendarId=calendar_id,
                        timeMin=time_min,
                        timeMax=time_max,
                        singleEvents=True,
                        orderBy="startTime",
                        maxResults=max_results_per_calendar,
                    )
                    .execute()
                )
            except HttpError:
                continue

            for event in result.get("items") or []:
                if event.get("status") == "cancelled":
                    continue
                start_iso = _event_start_iso(event)
                end_iso = _event_end_iso(event)
                if not start_iso:
                    continue
                event_id = (event.get("id") or "").strip()
                dedupe_key = f"{account_id}|{calendar_id}|{event_id}|{start_iso}"
                if dedupe_key in seen_keys:
                    continue
                seen_keys.add(dedupe_key)
                all_events.append({
                    "id": event_id,
                    "summary": (event.get("summary") or "").strip() or "(No title)",
                    "start_iso": start_iso,
                    "end_iso": end_iso,
                    "start_ts": _parse_iso_to_ts(start_iso),
                    "end_ts": _parse_iso_to_ts(end_iso),
                    "attendees_emails": list(_attendee_emails(event)),
                    "account_id": account_id,
                    "calendar_id": calendar_id,
                    "calendar_summary": calendar_summary,
                })

    all_events.sort(key=lambda x: (x.get("start_ts") or 0, x.get("summary") or ""))
    return all_events


def pull_calendar_events_time_window(
    time_min: datetime,
    time_max_exclusive: datetime,
    *,
    max_results_per_page: int = 250,
    include_calendar_pairs: Optional[Set[Tuple[str, str]]] = None,
) -> List[Dict[str, Any]]:
    """
    Fetch non-cancelled events from every calendar on every connected OAuth account
    within ``[time_min, time_max_exclusive)`` (``time_max_exclusive`` follows Google’s
    exclusive ``timeMax`` semantics).

    If ``include_calendar_pairs`` is set, only calendars whose
    ``(account_id, calendar_id)`` appear in the set are queried (others are skipped).

    Paginates ``events().list`` with ``nextPageToken``. Returns one row per
    (account_id, calendar_id, event id, occurrence start) with fields useful for
    availability / buffer logic. Each row includes ``transparency`` (opaque = busy,
    transparent = show as free); callers should omit transparent events from busy.
    """
    if time_max_exclusive <= time_min:
        raise ValueError("time_max_exclusive must be after time_min")
    services = get_all_calendar_services()
    if not services:
        log.warning("No Calendar service available (not connected or missing calendar scope)")
        return []

    time_min_s = _to_utc_rfc3339(time_min)
    time_max_s = _to_utc_rfc3339(time_max_exclusive)
    all_events: List[Dict[str, Any]] = []
    seen_keys: set = set()

    fields = (
        "items(id,iCalUID,status,summary,description,location,htmlLink,hangoutLink,"
        "conferenceData,organizer,creator,attendees,start,end,transparency),nextPageToken"
    )

    entries_by_account: Dict[str, List[Dict[str, Any]]] = {}
    for row in list_calendar_list_entries():
        aid = row.get("account_id") or ""
        entries_by_account.setdefault(aid, []).append(row)

    for account_id, service in services:
        calendar_rows = entries_by_account.get(account_id) or []
        if not calendar_rows:
            continue

        for cal in calendar_rows:
            calendar_id = (cal.get("calendar_id") or "").strip()
            if not calendar_id:
                continue
            if include_calendar_pairs is not None and (account_id, calendar_id) not in include_calendar_pairs:
                continue
            calendar_summary = (cal.get("summary") or "").strip() or calendar_id
            page_token: Optional[str] = None
            while True:
                try:
                    req = (
                        service.events()
                        .list(
                            calendarId=calendar_id,
                            timeMin=time_min_s,
                            timeMax=time_max_s,
                            singleEvents=True,
                            orderBy="startTime",
                            maxResults=max_results_per_page,
                            fields=fields,
                            pageToken=page_token,
                        )
                    )
                    result = req.execute()
                except HttpError as e:
                    log.warning(
                        "events.list failed account=%s calendar=%s: %s",
                        account_id,
                        calendar_id,
                        e,
                    )
                    break

                for event in result.get("items") or []:
                    if event.get("status") == "cancelled":
                        continue
                    start_iso = _event_start_iso(event)
                    end_iso = _event_end_iso(event)
                    if not start_iso:
                        continue
                    event_id = (event.get("id") or "").strip()
                    dedupe_key = f"{account_id}|{calendar_id}|{event_id}|{start_iso}"
                    if dedupe_key in seen_keys:
                        continue
                    seen_keys.add(dedupe_key)
                    desc = (event.get("description") or "").strip()
                    if len(desc) > 2000:
                        desc = desc[:2000] + "…"
                    all_events.append(
                        {
                            "id": event_id,
                            "iCalUID": (event.get("iCalUID") or "").strip(),
                            "summary": (event.get("summary") or "").strip() or "(No title)",
                            "description": desc,
                            "location": (event.get("location") or "").strip(),
                            "htmlLink": (event.get("htmlLink") or "").strip(),
                            "hangoutLink": (event.get("hangoutLink") or "").strip(),
                            "conferenceData": event.get("conferenceData"),
                            "start": event.get("start") or {},
                            "end": event.get("end") or {},
                            # Google Calendar: "transparent" = show as free (does not block time).
                            "transparency": (event.get("transparency") or "opaque").strip(),
                            "start_iso": start_iso,
                            "end_iso": end_iso,
                            "start_ts": _parse_iso_to_ts(start_iso),
                            "end_ts": _parse_iso_to_ts(end_iso or start_iso),
                            "attendees_emails": list(_attendee_emails(event)),
                            "account_id": account_id,
                            "calendar_id": calendar_id,
                            "calendar_summary": calendar_summary,
                        }
                    )

                page_token = (result.get("nextPageToken") or "").strip() or None
                if not page_token:
                    break

    all_events.sort(key=lambda x: (x.get("start_ts") or 0, x.get("summary") or ""))
    filter_note = ""
    if include_calendar_pairs is not None:
        filter_note = f", {len(include_calendar_pairs)} calendar(s) in allowlist"
    log.info(
        "pull_calendar_events_time_window: %d event(s) from %d account(s)%s",
        len(all_events),
        len(services),
        filter_note,
    )
    return all_events
