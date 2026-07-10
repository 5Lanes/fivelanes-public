"""Calendar events as a Threads source — catalog, opt-in select, summarize."""

from services.calendar_events.catalog import list_meeting_catalog
from services.calendar_events.summarize import (
    summarize_one_calendar_event,
    summarize_tracked_calendar_event_threads,
)
from services.calendar_events.tracking import (
    CALENDAR_THREAD_PREFIX,
    calendar_inbox_thread_id,
    fetch_tracked_calendar_dedupe_keys,
    fetch_visible_meeting_keys,
    parse_calendar_inbox_thread_id,
    set_tracked_meeting_keys,
)

__all__ = [
    "CALENDAR_THREAD_PREFIX",
    "calendar_inbox_thread_id",
    "fetch_tracked_calendar_dedupe_keys",
    "fetch_visible_meeting_keys",
    "list_meeting_catalog",
    "parse_calendar_inbox_thread_id",
    "set_tracked_meeting_keys",
    "summarize_one_calendar_event",
    "summarize_tracked_calendar_event_threads",
]
