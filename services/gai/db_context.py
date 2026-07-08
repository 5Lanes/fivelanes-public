"""Read-only database context and safe SQL execution for GAI."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time as time_module
from datetime import datetime, time
from pathlib import Path
from typing import Any, Dict, List

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[misc, assignment]

from utils.database import (
    connect_sqlite,
    load_all_lane_areas,
    load_all_lanes,
    load_lane_thread_memberships,
    new_since_refresh_counts_by_thread,
)
from utils.owner_config import owner_name_variants

_MAX_ROWS = 100
_PERSON_RESPOND_RE = re.compile(
    r"(?:has|have|did)\s+([a-z][a-z'\-]+)\s+respond",
    re.IGNORECASE,
)
_WHAT_SAID_RE = re.compile(r"what did (.+?) say\??$", re.IGNORECASE)
_HEARD_FROM_RE = re.compile(
    r"(?:have i |did i )?(?:heard|hear) from\s+([a-z][a-z'\-]+)",
    re.IGNORECASE,
)
_PRONOUNS = frozenset({"she", "he", "they", "her", "him", "them"})
_STOP_NAMES = frozenset(
    {
        "the",
        "your",
        "you",
        "july",
        "june",
        "yes",
        "not",
        "yet",
        "re",
        "about",
        "from",
        "last",
        "message",
        "most",
        "recent",
    }
)
_FORBIDDEN_SQL = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|ATTACH|DETACH|VACUUM|REINDEX|TRUNCATE)\b",
    re.IGNORECASE,
)
_DATETIME_COLUMN_RE = re.compile(
    r"^(datetime|timestamp|sent_at|created_at|updated_at|finished_at|.*_at|.*_time)$",
    re.IGNORECASE,
)

_DATETIME_TABLE_NOTES = {
    "timeline_entries": (
        "Authoritative ingest log for emails and meetings. datetime is ISO 8601 UTC "
        "(e.g. 2026-07-06T17:53:32+00:00). For 'arrived today', compare datetime against "
        "local midnight converted to UTC (see message_activity.since_local_midnight_utc). "
        "Use columns: datetime, type, sender, summary, thread_id."
    ),
    "message_outputs": (
        "Processed/cleaned messages from all channels (email, Slack, SMS, LinkedIn, etc.). "
        "datetime is ISO 8601 or 'YYYY-MM-DD HH:MM:SS UTC'. "
        "Slack DMs use thread_id like 'slack:...' and subject often holds the channel name. "
        "For 'did X respond?' questions, query recent rows here (not timeline_entries alone). "
        "Only filter by today when the user explicitly asks about today."
    ),
}


def _pipeline_last_completed_iso() -> str:
    from utils.pipeline_run_log import load_last_pipeline_run

    last = load_last_pipeline_run() or {}
    since = str(last.get("last_completed_at") or "").strip()
    if since:
        return since
    if last.get("ok") is True:
        return str(last.get("finished_at") or "").strip()
    return ""


# Chat turns pay for database_schema_summary()/structured_snapshot() up front, before any
# LLM call. Schema is static for the process lifetime; the snapshot is versioned by the last
# completed pipeline refresh (the same signal the dashboard uses for "new" badges), with a
# short TTL fallback so writes outside the pipeline (e.g. lane edits) aren't stale for long.
_SNAPSHOT_CACHE_TTL_SEC = 30.0
_schema_cache: Dict[str, str] = {}
_snapshot_cache: Dict[str, tuple[str, float, Dict[str, Any]]] = {}


def invalidate_chat_context_cache() -> None:
    """Drop cached schema/snapshot so the next call recomputes from the database."""
    _schema_cache.clear()
    _snapshot_cache.clear()


def warm_chat_context_cache(db_path: str | Path) -> None:
    """Prepopulate the schema/snapshot cache so the next chat turn is served from cache."""
    database_schema_summary(db_path)
    structured_snapshot(db_path)


def local_timezone() -> Any:
    """Resolve the user's local timezone for calendar-day boundaries."""
    if ZoneInfo is None:
        return None
    tz_name = (os.getenv("FIVELANES_SCHEDULER_TZ") or "").strip() or "localtime"
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("localtime")


def local_timezone_name() -> str:
    """IANA timezone name for the user's locale (for display and LLM context)."""
    configured = (os.getenv("FIVELANES_SCHEDULER_TZ") or "").strip()
    if configured:
        return configured
    tz = local_timezone()
    if tz is not None and hasattr(tz, "key"):
        return str(tz.key)
    return "local"


def local_today_iso() -> str:
    """Return today's date in the local timezone as YYYY-MM-DD."""
    tz = local_timezone()
    if tz is not None:
        return datetime.now(tz).date().isoformat()
    return datetime.now().date().isoformat()


def local_day_start_utc_iso() -> str:
    """UTC ISO timestamp for local midnight at the start of today."""
    tz = local_timezone()
    if tz is not None:
        start = datetime.combine(datetime.now(tz).date(), time.min, tzinfo=tz)
        return start.astimezone(ZoneInfo("UTC")).isoformat()
    today = datetime.now().date()
    return datetime.combine(today, time.min).isoformat()


def _count_timeline_since(conn: sqlite3.Connection, since_utc: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM timeline_entries WHERE datetime >= ?",
        (since_utc,),
    ).fetchone()
    return int(row[0] or 0)


def _max_datetime(conn: sqlite3.Connection, table: str) -> str:
    row = conn.execute(f"SELECT MAX(datetime) FROM {table}").fetchone()
    return str(row[0] or "")


def extract_respond_person(question: str) -> str | None:
    """Extract a person's first name from 'did Mark respond?' style questions."""
    match = _PERSON_RESPOND_RE.search(question)
    if not match:
        return None
    return match.group(1).strip()


def is_what_said_question(question: str) -> bool:
    return bool(_WHAT_SAID_RE.search((question or "").strip()))


def is_heard_from_question(question: str) -> bool:
    return bool(_HEARD_FROM_RE.search((question or "").strip()))


def _person_from_history(history: List[Dict[str, str]]) -> str | None:
    patterns = [
        r"(?:heard from|hear from|from|with|about)\s+([A-Za-z][a-z'\-]+)",
        r"([A-Za-z][a-z'\-]+)'s\s+last",
        r"\b([A-Za-z][a-z'\-]+)\s+last\s+(?:responded|message)",
    ]
    for turn in reversed(history):
        text = turn.get("content") or ""
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if not match:
                continue
            name = match.group(1).strip().lower()
            if name and name not in _STOP_NAMES:
                return name
    return None


def resolve_person_reference(
    question: str,
    history: List[Dict[str, str]] | None = None,
    *,
    session_context: Dict[str, Any] | None = None,
) -> str | None:
    """Resolve a person from the question, pronouns, or recent conversation."""
    text = (question or "").strip()
    if not text:
        return None

    person = extract_respond_person(text)
    if person:
        return person

    heard = _HEARD_FROM_RE.search(text)
    if heard:
        return heard.group(1).strip().lower()

    said = _WHAT_SAID_RE.search(text)
    if said:
        ref = said.group(1).strip().lower()
        if ref in _PRONOUNS:
            if session_context and session_context.get("last_person"):
                return str(session_context["last_person"]).lower()
            return _person_from_history(history or [])
        return ref.split()[0]

    if re.search(r"\b(she|he|they|her|him|them)\b", text, re.IGNORECASE):
        if session_context and session_context.get("last_person"):
            return str(session_context["last_person"]).lower()
        return _person_from_history(history or [])

    return None


def _is_owner_sender(sender: str) -> bool:
    value = (sender or "").strip().lower()
    if not value or value == "me":
        return True
    return any(hint in value for hint in owner_name_variants())


def _parse_message_datetime(value: str) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    normalized = raw.replace(" UTC", "").replace("Z", "+00:00")
    try:
        if "T" in normalized:
            return datetime.fromisoformat(normalized)
        if ZoneInfo is not None:
            return datetime.strptime(normalized[:19], "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=ZoneInfo("UTC")
            )
        return datetime.strptime(normalized[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _is_datetime_column(name: str) -> bool:
    return bool(_DATETIME_COLUMN_RE.match((name or "").strip()))


def format_datetime_local(value: str) -> str:
    """Format a UTC database timestamp in the user's local timezone."""
    parsed = _parse_message_datetime(value)
    if parsed is None:
        return value
    tz = local_timezone()
    if tz is not None:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo("UTC"))
        local = parsed.astimezone(tz)
    else:
        local = parsed
    hour = int(local.strftime("%I"))
    minute = local.strftime("%M")
    ampm = local.strftime("%p")
    tz_abbr = (local.strftime("%Z") or local_timezone_name()).strip()
    return f"{local.strftime('%B')} {local.day}, {local.year} at {hour}:{minute} {ampm} {tz_abbr}".strip()


def localize_datetime_fields(value: Any) -> Any:
    """Add *_local fields for datetime-like strings in nested dicts/lists."""
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(item, str) and _is_datetime_column(key) and _parse_message_datetime(item):
                out[key] = item
                out[f"{key}_local"] = format_datetime_local(item)
            elif isinstance(item, (dict, list)):
                out[key] = localize_datetime_fields(item)
            else:
                out[key] = item
        return out
    if isinstance(value, list):
        return [localize_datetime_fields(item) for item in value]
    return value


def load_person_response_context(db_path: str | Path, person_name: str) -> Dict[str, Any]:
    """
    Find threads involving a person and analyze whether they have responded.

    Slack/chat rows often store the person's name in ``subject``, not ``sender``.
    """
    name = person_name.strip()
    needle = name.lower()
    chat_messages: List[Dict[str, Any]] = []
    email_messages: List[Dict[str, Any]] = []

    with connect_sqlite(db_path) as conn:
        for dt, sender, subject, thread_id, content in conn.execute(
            """
            SELECT datetime, sender, subject, thread_id, cleaned_content
            FROM message_outputs
            WHERE subject = ? COLLATE NOCASE
               OR sender LIKE ? ESCAPE '\\' COLLATE NOCASE
            ORDER BY datetime DESC
            LIMIT ?
            """,
            (name, f"%{needle}%", _MAX_ROWS),
        ).fetchall():
            chat_messages.append(
                {
                    "datetime": str(dt or ""),
                    "sender": str(sender or ""),
                    "subject": str(subject or ""),
                    "thread_id": str(thread_id or ""),
                    "content": str(content or "")[:240],
                    "channel": _channel_label(str(thread_id or "")),
                }
            )

        for dt, sender, summary, thread_id in conn.execute(
            """
            SELECT datetime, sender, summary, thread_id
            FROM timeline_entries
            WHERE sender LIKE ? ESCAPE '\\' COLLATE NOCASE
               OR summary LIKE ? ESCAPE '\\' COLLATE NOCASE
            ORDER BY datetime DESC
            LIMIT ?
            """,
            (f"%{needle}%", f"%{needle}%", _MAX_ROWS),
        ).fetchall():
            email_messages.append(
                {
                    "datetime": str(dt or ""),
                    "sender": str(sender or ""),
                    "summary": str(summary or ""),
                    "thread_id": str(thread_id or ""),
                    "channel": "email",
                }
            )

    threads: Dict[str, List[Dict[str, Any]]] = {}
    for message in chat_messages:
        key = message.get("thread_id") or message.get("subject") or "unknown"
        threads.setdefault(str(key), []).append(message)

    thread_summaries: List[Dict[str, Any]] = []
    for thread_id, messages in threads.items():
        ordered = sorted(
            messages,
            key=lambda row: _parse_message_datetime(str(row.get("datetime") or ""))
            or datetime.min.replace(tzinfo=ZoneInfo("UTC") if ZoneInfo else None),
        )
        latest = ordered[-1]
        latest_from_person = next(
            (row for row in reversed(ordered) if not _is_owner_sender(str(row.get("sender") or ""))),
            None,
        )
        latest_from_owner = next(
            (row for row in reversed(ordered) if _is_owner_sender(str(row.get("sender") or ""))),
            None,
        )
        responded_after_owner = False
        if latest_from_person and latest_from_owner:
            person_dt = _parse_message_datetime(str(latest_from_person.get("datetime") or ""))
            owner_dt = _parse_message_datetime(str(latest_from_owner.get("datetime") or ""))
            if person_dt and owner_dt:
                responded_after_owner = person_dt > owner_dt
        thread_summaries.append(
            {
                "thread_id": thread_id,
                "channel": latest.get("channel") or _channel_label(thread_id),
                "subject": latest.get("subject") or latest.get("summary") or "",
                "message_count": len(ordered),
                "latest_sender": latest.get("sender"),
                "latest_datetime": latest.get("datetime"),
                "latest_content_preview": latest.get("content") or latest.get("summary") or "",
                "person_last_datetime": (latest_from_person or {}).get("datetime"),
                "person_last_sender": (latest_from_person or {}).get("sender"),
                "person_last_content_preview": (latest_from_person or {}).get("content")
                or (latest_from_person or {}).get("summary")
                or "",
                "owner_last_datetime": (latest_from_owner or {}).get("datetime"),
                "responded_after_owner_last_message": responded_after_owner,
                "waiting_on_person": bool(
                    latest_from_owner
                    and (
                        not latest_from_person
                        or not responded_after_owner
                        and _parse_message_datetime(str(latest.get("datetime") or ""))
                        == _parse_message_datetime(str(latest_from_owner.get("datetime") or ""))
                    )
                ),
            }
        )

    thread_summaries.sort(
        key=lambda row: _parse_message_datetime(str(row.get("latest_datetime") or ""))
        or datetime.min.replace(tzinfo=ZoneInfo("UTC") if ZoneInfo else None),
        reverse=True,
    )

    return {
        "person_name": name,
        "chat_message_count": len(chat_messages),
        "email_message_count": len(email_messages),
        "threads": thread_summaries,
        "recent_email_messages": email_messages[:5],
    }


def _channel_label(thread_id: str) -> str:
    if thread_id.startswith("slack:"):
        return "slack"
    if thread_id.startswith("text:"):
        return "text"
    if thread_id.startswith("linkedin:"):
        return "linkedin"
    if thread_id.startswith("meet:"):
        return "meet"
    return "chat"


def format_person_response_context(context: Dict[str, Any]) -> str:
    return json.dumps(
        localize_datetime_fields(context),
        indent=2,
        default=str,
    )


def format_person_response_answer(context: Dict[str, Any]) -> str:
    """Deterministic answer for person-response lookups."""
    person = str(context.get("person_name") or "they").strip()
    threads = context.get("threads") or []
    if not threads and not context.get("recent_email_messages"):
        return f"I couldn't find any messages involving {person} in the database."

    if not threads:
        latest_email = (context.get("recent_email_messages") or [None])[0]
        if latest_email:
            when = format_datetime_local(str(latest_email.get("datetime") or ""))
            return (
                f"I found email threads with {person}, but no chat messages. "
                f"The latest email is from {latest_email.get('sender')} at {when}: "
                f"{latest_email.get('summary')}"
            )

    primary = threads[0]
    channel = primary.get("channel") or "chat"
    person_last = format_datetime_local(str(primary.get("person_last_datetime") or ""))
    owner_last = format_datetime_local(str(primary.get("owner_last_datetime") or ""))

    if not person_last:
        return f"I found a {channel} thread with {person}, but no messages from them yet."

    if primary.get("responded_after_owner_last_message"):
        preview = (primary.get("person_last_content_preview") or "").strip()
        tail = f' He said: "{preview}"' if preview else ""
        return (
            f"Yes. {person.title()} last responded on {person_last} ({channel}), "
            f"after your most recent message.{tail}"
        )

    if primary.get("waiting_on_person"):
        return (
            f"Not yet. Your last message was on {owner_last} ({channel}). "
            f"{person.title()}'s last response was on {person_last}, before that."
        )

    preview = (primary.get("person_last_content_preview") or "").strip()
    tail = f' Latest from them: "{preview}"' if preview else ""
    return (
        f"{person.title()} has responded before; their last message was on {person_last} ({channel}).{tail}"
    )


def format_person_heard_answer(context: Dict[str, Any]) -> str:
    """Answer whether the user has heard from someone."""
    person = str(context.get("person_name") or "they").strip()
    threads = context.get("threads") or []
    emails = context.get("recent_email_messages") or []
    if not threads and not emails:
        return f"No, I don't see any messages from {person} in the database."

    if threads:
        primary = threads[0]
        when = format_datetime_local(
            str(primary.get("person_last_datetime") or primary.get("latest_datetime") or "")
        )
        channel = primary.get("channel") or "message"
        subject = (primary.get("subject") or "").strip()
        preview = (primary.get("person_last_content_preview") or "").strip()
        subject_line = f' about "{subject}"' if subject else ""
        tail = f' She wrote: "{preview}"' if preview else ""
        return f"Yes. {person.title()} last wrote on {when} ({channel}){subject_line}.{tail}"

    latest_email = emails[0]
    when = format_datetime_local(str(latest_email.get("datetime") or ""))
    return (
        f"Yes. {person.title()} last emailed on {when} "
        f"about \"{latest_email.get('summary')}\"."
    )


def format_person_said_answer(context: Dict[str, Any]) -> str:
    """Return the person's most recent message content."""
    person = str(context.get("person_name") or "they").strip()
    threads = context.get("threads") or []
    if not threads and not context.get("recent_email_messages"):
        return f"I couldn't find any messages from {person} in the database."

    if threads:
        primary = threads[0]
        content = (primary.get("person_last_content_preview") or "").strip()
        when = format_datetime_local(
            str(primary.get("person_last_datetime") or primary.get("latest_datetime") or "")
        )
        channel = primary.get("channel") or "message"
        subject = (primary.get("subject") or "").strip()
        if not content:
            return f"I found a thread with {person}, but their latest message had no readable content."
        subject_line = f' (subject: "{subject}")' if subject else ""
        return f"On {when} ({channel}){subject_line}, {person.title()} wrote:\n\n{content}"

    latest_email = (context.get("recent_email_messages") or [None])[0] or {}
    body = (latest_email.get("summary") or "").strip()
    when = format_datetime_local(str(latest_email.get("datetime") or ""))
    if body:
        return f"On {when} (email), {person.title()} wrote:\n\n{body}"
    return f"I found email from {person}, but could not read the message body."


def load_arrivals_today(db_path: str | Path) -> Dict[str, Any]:
    """
    Messages that actually arrived today using Fivelanes semantics.

    - timeline_entries since local midnight: newly ingested emails/meetings
    - new_since_refresh: unprocessed messages since the last pipeline refresh
      (matches the dashboard "new" badge logic)
    """
    db_file = Path(db_path)
    path = str(db_file)
    since_local = local_day_start_utc_iso()
    timeline_arrivals: List[Dict[str, Any]] = []

    with connect_sqlite(db_file) as conn:
        rows = conn.execute(
            """
            SELECT datetime, type, sender, summary, thread_id
            FROM timeline_entries
            WHERE datetime >= ?
            ORDER BY datetime DESC
            LIMIT ?
            """,
            (since_local, _MAX_ROWS),
        ).fetchall()
        for dt, msg_type, sender, summary, thread_id in rows:
            timeline_arrivals.append(
                {
                    "datetime": str(dt or ""),
                    "type": str(msg_type or ""),
                    "sender": str(sender or ""),
                    "summary": str(summary or ""),
                    "thread_id": str(thread_id or ""),
                }
            )

    new_by_thread = new_since_refresh_counts_by_thread(path)
    return {
        "local_today": local_today_iso(),
        "user_timezone": local_timezone_name(),
        "since_local_midnight_utc": since_local,
        "last_pipeline_refresh": _pipeline_last_completed_iso(),
        "timeline_arrivals": timeline_arrivals,
        "timeline_arrivals_count": len(timeline_arrivals),
        "new_since_refresh_by_thread": new_by_thread,
        "new_since_refresh_total": sum(new_by_thread.values()),
    }


def database_schema_summary(db_path: str | Path) -> str:
    """Return a compact schema description for LLM context (cached for the process lifetime)."""
    cache_key = str(Path(db_path))
    cached = _schema_cache.get(cache_key)
    if cached is not None:
        return cached

    lines: List[str] = []
    with connect_sqlite(Path(db_path)) as conn:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        ).fetchall()
        for (table_name,) in tables:
            cols = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
            col_desc = ", ".join(f"{c[1]} {c[2]}" for c in cols)
            note = _DATETIME_TABLE_NOTES.get(table_name, "")
            if note:
                lines.append(f"- {table_name}({col_desc})\n  {note}")
            else:
                lines.append(f"- {table_name}({col_desc})")
    summary = "\n".join(lines)
    _schema_cache[cache_key] = summary
    return summary


def structured_snapshot(db_path: str | Path) -> Dict[str, Any]:
    """Lightweight counts and lane metadata without loading full message bodies.

    Cached per db path, invalidated when the last completed pipeline refresh changes or
    after ``_SNAPSHOT_CACHE_TTL_SEC`` seconds, whichever comes first.
    """
    cache_key = str(Path(db_path))
    version = _pipeline_last_completed_iso()
    cached = _snapshot_cache.get(cache_key)
    now = time_module.monotonic()
    if cached is not None:
        cached_version, cached_at, cached_data = cached
        if cached_version == version and (now - cached_at) < _SNAPSHOT_CACHE_TTL_SEC:
            return cached_data

    db_file = Path(db_path)
    path = str(db_file)
    lanes = load_all_lanes(path)
    areas = load_all_lane_areas(path)
    memberships = load_lane_thread_memberships(path)
    since_local = local_day_start_utc_iso()
    today = local_today_iso()

    with connect_sqlite(db_file) as conn:
        counts = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT 'thread_tracking', COUNT(*) FROM thread_tracking "
                "UNION ALL SELECT 'timeline_entries', COUNT(*) FROM timeline_entries "
                "UNION ALL SELECT 'message_outputs', COUNT(*) FROM message_outputs "
                "UNION ALL SELECT 'lanes', COUNT(*) FROM lanes "
                "UNION ALL SELECT 'lane_areas', COUNT(*) FROM lane_areas "
                "UNION ALL SELECT 'lane_threads', COUNT(*) FROM lane_threads "
                "UNION ALL SELECT 'meetings', COUNT(*) FROM meetings "
                "UNION ALL SELECT 'thread_plans', COUNT(*) FROM thread_plans"
            ).fetchall()
        }
        snoozed = conn.execute(
            "SELECT COUNT(*) FROM thread_tracking WHERE snoozed = 1"
        ).fetchone()[0]
        timeline_today = _count_timeline_since(conn, since_local)

    new_by_thread = new_since_refresh_counts_by_thread(path)

    with connect_sqlite(db_file) as conn:
        latest_timeline = _max_datetime(conn, "timeline_entries")

    arrivals = {
        "local_today": today,
        "user_timezone": local_timezone_name(),
        "since_local_midnight_utc": since_local,
        "last_pipeline_refresh": version,
        "timeline_arrivals_today": timeline_today,
        "new_since_refresh_total": sum(new_by_thread.values()),
        "latest_timeline_entry": latest_timeline,
    }

    area_by_id = {a["id"]: a["name"] for a in areas}
    lane_rows = [
        {
            "id": lane["id"],
            "name": lane["name"],
            "area": area_by_id.get(lane.get("area_id"), "(none)"),
            "thread_count": len(memberships.get(str(lane["id"]), [])),
            "archived": lane.get("archived", False),
        }
        for lane in lanes
    ]

    snapshot = {
        "table_counts": counts,
        "message_activity": arrivals,
        "snoozed_threads": int(snoozed or 0),
        "lane_areas": [{"id": a["id"], "name": a["name"]} for a in areas],
        "lanes": lane_rows,
    }
    _snapshot_cache[cache_key] = (version, now, snapshot)
    return snapshot


def validate_readonly_sql(sql: str) -> str:
    """Normalize and validate a single read-only SELECT statement."""
    cleaned = (sql or "").strip()
    if not cleaned:
        raise ValueError("empty_sql")
    if ";" in cleaned.rstrip(";"):
        raise ValueError("multiple_statements_not_allowed")
    cleaned = cleaned.rstrip(";").strip()
    if not re.match(r"^SELECT\b", cleaned, re.IGNORECASE):
        raise ValueError("only_select_queries_allowed")
    if _FORBIDDEN_SQL.search(cleaned):
        raise ValueError("forbidden_sql_keyword")
    if not re.search(r"\bLIMIT\b", cleaned, re.IGNORECASE):
        cleaned = f"{cleaned} LIMIT {_MAX_ROWS}"
    return cleaned


def execute_readonly_sql(db_path: str | Path, sql: str) -> Dict[str, Any]:
    """Run a validated SELECT and return column names plus row dicts."""
    query = validate_readonly_sql(sql)
    db_file = Path(db_path)
    with connect_sqlite(db_file) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(query)
        columns = [d[0] for d in (cur.description or [])]
        rows = [dict(row) for row in cur.fetchmany(_MAX_ROWS)]
    return {
        "sql": query,
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": len(rows) >= _MAX_ROWS,
    }


def format_query_result(result: Dict[str, Any]) -> str:
    """Serialize query output for LLM consumption."""
    payload = {
        "sql": result.get("sql"),
        "columns": result.get("columns"),
        "row_count": result.get("row_count"),
        "truncated": result.get("truncated"),
        "user_timezone": local_timezone_name(),
        "rows": localize_datetime_fields(result.get("rows") or []),
    }
    return json.dumps(payload, indent=2, default=str)


def format_arrivals_context(arrivals: Dict[str, Any]) -> str:
    """Serialize arrival data for LLM consumption."""
    return json.dumps(
        localize_datetime_fields(arrivals),
        indent=2,
        default=str,
    )
