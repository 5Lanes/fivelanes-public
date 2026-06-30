"""
SQLite storage.

**``thread_tracking``** — one row per Fivelanes inbox thread: ``inbox_thread_id`` (snooze/remove/dashboard
key), ``gmail_inbox_thread_id`` (Cc/Bcc: real inbox Gmail ``threadId`` when ``inbox_thread_id`` is
``rfc:…``), ``source_email`` (envelope **From** = which of your addresses delivered to the inbox),
``snoozed`` (0 active / 1 snoozed / 2 removed), ``has_plan``, ``inner_rfc_message_id``,
``resolved_oauth_account_id``, ``resolution_error``, timestamps. Snooze and removal always use
``inbox_thread_id``; they are independent of which mailbox supplied ``timeline_entries.source_id``.

**``timeline_entries``** — one row per message in a resolved conversation. ``thread_id`` matches
``thread_tracking.inbox_thread_id`` (inbox tracking key for UI/pipeline grouping).
``source_id`` is the Gmail message id from the mailbox thread where the conversation lives
(resolved via RFC Message-ID on the forwarder's OAuth account), **not** from Fivelanes inbox
forward/cc shell copies. ``fetch_oauth_account_id`` records which token was used to fetch the body.
See README § "Thread identity: inbox tracking vs timeline messages".

**``meetings``** — calendar events from ``out/availability_calendar_latest.json``
(``calendar_events_index``), refreshed on each availability export.

**``meeting_preps``** — LLM meeting prep briefs keyed by ``(dedupe_key, thread_id)``.

**``claude_message_outputs``** — segmented/cleaned message rows and thread summaries.

**``thread_summaries``** — cached thread summary fingerprints for skip/incremental resummary.

**``thread_draft_replies``** — saved LLM draft replies keyed by ``thread_id``.

**``lanes``** / **``lane_threads``** — user-defined lanes and inbox thread membership.

**``lane_summaries``** — LLM roll-up briefs for all threads assigned to a lane.

**``thread_plans``** — user-defined next steps tied to an inbox thread (action, type, optional deadline).

**``dismissed_todo_plans``** — todo email (thread, action) pairs the user deleted; inbox sync
will not recreate them.

Text threads use on-disk ``conversations/*.json`` plus ``thread_tracking`` rows with
``inbox_thread_id`` ``text:<key>`` (no separate text tables).
"""

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

# Wait up to 60s when another connection holds the database (pipeline + dashboard overlap).
_SQLITE_BUSY_TIMEOUT_MS = 60_000


def connect_sqlite(
    db_path: str | Path,
    *,
    row_factory: Any = None,
    timeout: float | None = None,
) -> sqlite3.Connection:
    """Open SQLite with WAL mode and a long busy timeout for concurrent dashboard + pipeline access."""
    db_file = Path(db_path)
    conn = sqlite3.connect(
        db_file,
        timeout=timeout if timeout is not None else _SQLITE_BUSY_TIMEOUT_MS / 1000.0,
    )
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
    if row_factory is not None:
        conn.row_factory = row_factory
    return conn


def ensure_database_schema(db_path: str) -> None:
    """Ensure active application tables exist."""
    db_file = Path(db_path)
    with connect_sqlite(db_file) as conn:
        _ensure_timeline_schema(conn)
        _ensure_thread_tracking_schema(conn)
        _ensure_meetings_schema(conn)
        _ensure_meeting_preps_schema(conn)
        _ensure_lanes_schema(conn)
        _ensure_lane_summaries_schema(conn)
        _ensure_thread_plans_schema(conn)
        _ensure_dismissed_todo_plans_schema(conn)
        _ensure_claude_outputs_schema(conn)
        _ensure_thread_summaries_schema(conn)
        _ensure_thread_draft_replies_schema(conn)
        conn.commit()


def _normalize_field(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _dedupe_rows(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Deduplicate rows by ``source_id`` while preserving order."""
    seen_ids: set[str] = set()
    deduped: List[Dict[str, Any]] = []

    for row in rows:
        normalized = {
            "source_id": _normalize_field(row.get("source_id")),
            "type": _normalize_field(row.get("type")),
            "datetime": _normalize_field(row.get("datetime")),
            "sender": _normalize_field(row.get("sender")),
            "recipients": _normalize_field(row.get("recipients")),
            "participants": _normalize_field(row.get("participants")),
            "summary": _normalize_field(row.get("summary")),
            "body": _normalize_field(row.get("body")),
            "thread_id": _normalize_field(row.get("thread_id")),
            "fetch_oauth_account_id": _normalize_field(row.get("fetch_oauth_account_id")),
            "body_has_image": 1 if row.get("body_has_image") else 0,
        }
        source_id = normalized["source_id"]
        if not source_id:
            continue
        if source_id in seen_ids:
            continue
        seen_ids.add(source_id)
        deduped.append(normalized)

    return deduped

def _dedupe_thread_tracking_rows(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Deduplicate rows by ``inbox_thread_id`` while preserving order."""
    by_thread: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []

    for row in rows:
        inbox_tid = _normalize_field(
            row.get("inbox_thread_id") or row.get("thread_id")
        )
        try:
            _sn = int(row.get("snoozed") or 0)
        except (TypeError, ValueError):
            _sn = 0
        if _sn not in (0, 1, 2):
            _sn = 0
        normalized = {
            "inbox_thread_id": inbox_tid,
            "gmail_inbox_thread_id": _normalize_field(row.get("gmail_inbox_thread_id")),
            "source_email": _normalize_field(row.get("source_email")),
            "snoozed": _sn,
            "inner_rfc_message_id": _normalize_field(row.get("inner_rfc_message_id")),
            "resolved_oauth_account_id": _normalize_field(
                row.get("resolved_oauth_account_id")
            ),
            "resolution_error": _normalize_field(row.get("resolution_error")),
            "inbox_delivery_kind": _normalize_field(row.get("inbox_delivery_kind")),
            "created_at": _normalize_field(row.get("created_at")),
            "updated_at": _normalize_field(row.get("updated_at")),
        }
        if not normalized["inbox_thread_id"]:
            continue
        if not normalized["source_email"]:
            continue
        tid = normalized["inbox_thread_id"]
        if tid not in by_thread:
            by_thread[tid] = normalized
            order.append(tid)
        elif normalized["updated_at"] > by_thread[tid]["updated_at"]:
            prev = by_thread[tid]
            if not normalized["inbox_delivery_kind"]:
                normalized["inbox_delivery_kind"] = prev.get("inbox_delivery_kind", "")
            if not normalized["resolved_oauth_account_id"]:
                normalized["resolved_oauth_account_id"] = prev.get(
                    "resolved_oauth_account_id", ""
                )
            by_thread[tid] = normalized

    return [by_thread[tid] for tid in order]

def _ensure_timeline_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS timeline_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id TEXT,
            type TEXT NOT NULL CHECK (type IN ('email', 'meeting_invite', 'meeting')),
            datetime TEXT,
            sender TEXT,
            recipients TEXT,
            participants TEXT,
            summary TEXT,
            body TEXT
        )
        """
    )
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(timeline_entries)").fetchall()
    }
    if "source_id" not in columns:
        conn.execute("ALTER TABLE timeline_entries ADD COLUMN source_id TEXT")
    if "body" not in columns:
        conn.execute("ALTER TABLE timeline_entries ADD COLUMN body TEXT")
    if "thread_id" not in columns:
        conn.execute("ALTER TABLE timeline_entries ADD COLUMN thread_id TEXT")
    if "fetch_oauth_account_id" not in columns:
        conn.execute(
            "ALTER TABLE timeline_entries ADD COLUMN fetch_oauth_account_id TEXT"
        )
    if "body_has_image" not in columns:
        conn.execute(
            "ALTER TABLE timeline_entries ADD COLUMN body_has_image INTEGER NOT NULL DEFAULT 0"
        )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_timeline_entries_source_id "
        "ON timeline_entries(source_id)"
    )


def _meeting_dedupe_key(summary: str, start_iso: str, end_iso: str) -> str:
    return f"{summary}|{start_iso}|{end_iso}"


def _normalize_attendee_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    out: List[str] = []
    seen: set[str] = set()
    for item in value:
        e = (item if isinstance(item, str) else str(item)).strip().lower()
        if e and "@" in e and e not in seen:
            seen.add(e)
            out.append(e)
    return sorted(out)


def _dedupe_meeting_rows(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse rows sharing ``dedupe_key``; union attendee lists."""
    by_key: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for row in rows:
        key = _normalize_field(row.get("dedupe_key"))
        if not key:
            continue
        if key not in by_key:
            by_key[key] = dict(row)
            order.append(key)
            continue
        kept = by_key[key]
        merged = sorted(
            set(_normalize_attendee_list(kept.get("attendees")))
            | set(_normalize_attendee_list(row.get("attendees")))
        )
        kept["attendees"] = merged
        if not kept.get("location") and row.get("location"):
            kept["location"] = row["location"]
        if not kept.get("html_link") and row.get("html_link"):
            kept["html_link"] = row["html_link"]
    return [by_key[k] for k in order]


def _ensure_meetings_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS meetings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dedupe_key TEXT NOT NULL,
            summary TEXT NOT NULL,
            start_iso TEXT NOT NULL,
            end_iso TEXT,
            location TEXT,
            html_link TEXT,
            kind TEXT,
            calendar_summary TEXT,
            account_id TEXT,
            week_local TEXT,
            attendees_json TEXT NOT NULL DEFAULT '[]',
            exported_at TEXT,
            timezone TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_meetings_dedupe_key ON meetings(dedupe_key)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_meetings_start_iso ON meetings(start_iso)"
    )


def meetings_rows_from_availability_doc(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Parse ``calendar_events_index`` from an availability export document.

    Returns normalized rows ready for ``replace_meetings``.
    """
    meta = doc.get("meta") if isinstance(doc.get("meta"), dict) else {}
    exported_at = _normalize_field(meta.get("generated_at"))
    tz = _normalize_field(meta.get("timezone"))
    index = doc.get("calendar_events_index")
    if not isinstance(index, list):
        return []

    now = datetime.now(timezone.utc).isoformat()
    raw: List[Dict[str, Any]] = []
    for ev in index:
        if not isinstance(ev, dict):
            continue
        summary = _normalize_field(ev.get("summary")) or "(No title)"
        start_iso = _normalize_field(ev.get("start_iso"))
        end_iso = _normalize_field(ev.get("end_iso"))
        if not start_iso:
            continue
        attendees = _normalize_attendee_list(ev.get("attendees"))
        raw.append(
            {
                "dedupe_key": _meeting_dedupe_key(summary, start_iso, end_iso),
                "summary": summary,
                "start_iso": start_iso,
                "end_iso": end_iso,
                "location": _normalize_field(ev.get("location")),
                "html_link": _normalize_field(ev.get("html_link") or ev.get("htmlLink")),
                "kind": _normalize_field(ev.get("kind")),
                "calendar_summary": _normalize_field(ev.get("calendar_summary")),
                "account_id": _normalize_field(ev.get("account_id")),
                "week_local": _normalize_field(ev.get("week_local")),
                "attendees": attendees,
                "exported_at": exported_at,
                "timezone": tz,
                "updated_at": now,
            }
        )
    deduped = _dedupe_meeting_rows(raw)
    for row in deduped:
        row["attendees_json"] = json.dumps(row.pop("attendees"), ensure_ascii=False)
    return deduped


def replace_meetings(db_path: str, rows: List[Dict[str, Any]]) -> int:
    """
    Replace all rows in ``meetings`` with ``rows`` (full snapshot per export).

    Returns the number of inserted rows.
    """
    db_file = Path(db_path)
    with connect_sqlite(db_file) as conn:
        _ensure_meetings_schema(conn)
        conn.execute("DELETE FROM meetings")
        if rows:
            conn.executemany(
                """
                INSERT INTO meetings (
                    dedupe_key, summary, start_iso, end_iso, location, html_link,
                    kind, calendar_summary, account_id, week_local, attendees_json,
                    exported_at, timezone, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        row["dedupe_key"],
                        row["summary"],
                        row["start_iso"],
                        row["end_iso"],
                        row.get("location", ""),
                        row.get("html_link", ""),
                        row.get("kind", ""),
                        row.get("calendar_summary", ""),
                        row.get("account_id", ""),
                        row.get("week_local", ""),
                        row.get("attendees_json", "[]"),
                        row.get("exported_at", ""),
                        row.get("timezone", ""),
                        row["updated_at"],
                    )
                    for row in rows
                ],
            )
        conn.commit()
    return len(rows)


def replace_meetings_from_availability_doc(db_path: str, doc: Dict[str, Any]) -> int:
    """Persist ``calendar_events_index`` from an availability JSON document."""
    return replace_meetings(db_path, meetings_rows_from_availability_doc(doc))


_MEETING_PREP_FIELDS = (
    "prep_summary",
    "talking_points",
    "open_loops",
    "suggested_opener",
    "open_questions",
)


def normalize_meeting_prep_payload(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Extract storable meeting-prep fields from an API/LLM response."""
    out: Dict[str, Any] = {}
    for key in _MEETING_PREP_FIELDS:
        val = raw.get(key)
        if key in ("talking_points", "open_loops", "open_questions"):
            if isinstance(val, list):
                out[key] = [str(x).strip() for x in val if str(x).strip()]
            else:
                out[key] = []
        else:
            out[key] = _normalize_field(val)
    return out


def _ensure_meeting_preps_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS meeting_preps (
            dedupe_key TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            prep_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (dedupe_key, thread_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_meeting_preps_thread_id ON meeting_preps(thread_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_meeting_preps_updated_at ON meeting_preps(updated_at)"
    )


def save_meeting_prep(
    db_path: str,
    *,
    dedupe_key: str,
    thread_id: str,
    prep: Dict[str, Any],
) -> str:
    """
    Persist meeting prep for one calendar event + inbox thread pair.

    Returns ``updated_at`` (UTC ISO).
    """
    key = _normalize_field(dedupe_key)
    tid = _normalize_field(thread_id)
    if not key or not tid:
        raise ValueError("dedupe_key and thread_id are required")
    updated_at = datetime.now(timezone.utc).isoformat()
    payload = normalize_meeting_prep_payload(prep if isinstance(prep, dict) else {})
    db_file = Path(db_path)
    with connect_sqlite(db_file) as conn:
        _ensure_meeting_preps_schema(conn)
        conn.execute(
            """
            INSERT INTO meeting_preps (dedupe_key, thread_id, prep_json, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(dedupe_key, thread_id) DO UPDATE SET
                prep_json = excluded.prep_json,
                updated_at = excluded.updated_at
            """,
            (key, tid, json.dumps(payload, ensure_ascii=False), updated_at),
        )
        conn.commit()
    return updated_at


def load_meeting_prep(
    db_path: str, *, dedupe_key: str, thread_id: str
) -> Optional[Dict[str, Any]]:
    """Return parsed prep JSON for one meeting/thread pair, or ``None``."""
    key = _normalize_field(dedupe_key)
    tid = _normalize_field(thread_id)
    if not key or not tid:
        return None
    db_file = Path(db_path)
    with connect_sqlite(db_file) as conn:
        _ensure_meeting_preps_schema(conn)
        row = conn.execute(
            "SELECT prep_json FROM meeting_preps WHERE dedupe_key = ? AND thread_id = ?",
            (key, tid),
        ).fetchone()
    if not row or not row[0]:
        return None
    try:
        loaded = json.loads(row[0])
        return normalize_meeting_prep_payload(loaded) if isinstance(loaded, dict) else None
    except json.JSONDecodeError:
        return None


def load_all_meeting_preps(db_path: str) -> Dict[str, Dict[str, Any]]:
    """
    Return all saved preps keyed by ``"{dedupe_key}|{thread_id}"`` (dashboard cache shape).
    """
    db_file = Path(db_path)
    out: Dict[str, Dict[str, Any]] = {}
    with connect_sqlite(db_file) as conn:
        _ensure_meeting_preps_schema(conn)
        rows = conn.execute(
            "SELECT dedupe_key, thread_id, prep_json FROM meeting_preps ORDER BY updated_at DESC"
        ).fetchall()
    for dedupe_key, thread_id, prep_json in rows:
        dk = _normalize_field(dedupe_key)
        tid = _normalize_field(thread_id)
        if not dk or not tid or not prep_json:
            continue
        cache_key = f"{dk}|{tid}"
        if cache_key in out:
            continue
        try:
            loaded = json.loads(prep_json)
            if isinstance(loaded, dict):
                out[cache_key] = normalize_meeting_prep_payload(loaded)
        except json.JSONDecodeError:
            continue
    return out


def _ensure_lanes_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS lanes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS lane_threads (
            lane_id INTEGER NOT NULL,
            inbox_thread_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (lane_id, inbox_thread_id),
            FOREIGN KEY (lane_id) REFERENCES lanes(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_lane_threads_inbox_thread_id "
        "ON lane_threads(inbox_thread_id)"
    )


def create_lane(db_path: str, *, name: str) -> Dict[str, Any]:
    """Insert a lane and return its row."""
    label = _normalize_field(name)
    if not label:
        raise ValueError("missing_lane_name")
    now = datetime.now(timezone.utc).isoformat()
    db_file = Path(db_path)
    with connect_sqlite(db_file) as conn:
        _ensure_lanes_schema(conn)
        cur = conn.execute(
            "INSERT INTO lanes (name, created_at, updated_at) VALUES (?, ?, ?)",
            (label, now, now),
        )
        lane_id = int(cur.lastrowid or 0)
        conn.commit()
    return {"id": lane_id, "name": label, "created_at": now, "updated_at": now}


def add_thread_to_lane(db_path: str, *, lane_id: int, inbox_thread_id: str) -> bool:
    """Add a thread to a lane. Returns False if lane missing."""
    tid = _normalize_field(inbox_thread_id)
    if not tid or lane_id <= 0:
        return False
    now = datetime.now(timezone.utc).isoformat()
    db_file = Path(db_path)
    with connect_sqlite(db_file) as conn:
        _ensure_lanes_schema(conn)
        row = conn.execute("SELECT id FROM lanes WHERE id = ?", (lane_id,)).fetchone()
        if not row:
            return False
        conn.execute(
            """
            INSERT INTO lane_threads (lane_id, inbox_thread_id, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(lane_id, inbox_thread_id) DO NOTHING
            """,
            (lane_id, tid, now),
        )
        conn.execute(
            "UPDATE lanes SET updated_at = ? WHERE id = ?",
            (now, lane_id),
        )
        conn.commit()
    return True


def remove_thread_from_lane(db_path: str, *, lane_id: int, inbox_thread_id: str) -> bool:
    """Remove a thread from a lane. Returns False if lane missing."""
    tid = _normalize_field(inbox_thread_id)
    if not tid or lane_id <= 0:
        return False
    now = datetime.now(timezone.utc).isoformat()
    db_file = Path(db_path)
    with connect_sqlite(db_file) as conn:
        _ensure_lanes_schema(conn)
        row = conn.execute("SELECT id FROM lanes WHERE id = ?", (lane_id,)).fetchone()
        if not row:
            return False
        conn.execute(
            "DELETE FROM lane_threads WHERE lane_id = ? AND inbox_thread_id = ?",
            (lane_id, tid),
        )
        conn.execute(
            "UPDATE lanes SET updated_at = ? WHERE id = ?",
            (now, lane_id),
        )
        conn.commit()
    return True


def delete_lane(db_path: str, *, lane_id: int) -> bool:
    """Delete a lane and its thread memberships and summary. Returns False if lane missing."""
    if lane_id <= 0:
        return False
    db_file = Path(db_path)
    with connect_sqlite(db_file) as conn:
        _ensure_lanes_schema(conn)
        _ensure_lane_summaries_schema(conn)
        row = conn.execute("SELECT id FROM lanes WHERE id = ?", (lane_id,)).fetchone()
        if not row:
            return False
        conn.execute("DELETE FROM lane_threads WHERE lane_id = ?", (lane_id,))
        conn.execute("DELETE FROM lane_summaries WHERE lane_id = ?", (lane_id,))
        conn.execute("DELETE FROM lanes WHERE id = ?", (lane_id,))
        conn.commit()
    return True


def load_all_lanes(db_path: str) -> List[Dict[str, Any]]:
    """Return all lanes ordered by name."""
    db_file = Path(db_path)
    with connect_sqlite(db_file) as conn:
        _ensure_lanes_schema(conn)
        rows = conn.execute(
            "SELECT id, name, created_at, updated_at FROM lanes ORDER BY name COLLATE NOCASE"
        ).fetchall()
    return [
        {
            "id": int(r[0]),
            "name": _normalize_field(r[1]),
            "created_at": _normalize_field(r[2]),
            "updated_at": _normalize_field(r[3]),
        }
        for r in rows
    ]


def load_lane_thread_memberships(db_path: str) -> Dict[str, List[str]]:
    """Return ``lane_id`` → ordered inbox thread ids."""
    db_file = Path(db_path)
    out: Dict[str, List[str]] = {}
    with connect_sqlite(db_file) as conn:
        _ensure_lanes_schema(conn)
        rows = conn.execute(
            """
            SELECT lane_id, inbox_thread_id
            FROM lane_threads
            ORDER BY lane_id, created_at
            """
        ).fetchall()
    for lane_id, thread_id in rows:
        key = str(int(lane_id))
        tid = _normalize_field(thread_id)
        if not tid:
            continue
        bucket = out.setdefault(key, [])
        if tid not in bucket:
            bucket.append(tid)
    return out


def load_lane_thread_summaries(
    db_path: str,
    *,
    lane_name: str | None = None,
    lane_id: int | None = None,
) -> tuple[Dict[str, Any] | None, List[Dict[str, Any]]]:
    """
    Return ``(lane_row, summaries)`` for a lane.

    ``summaries`` are dashboard-shaped thread summary dicts for threads tagged to the lane,
    sorted by ``datetime`` ascending (oldest first). Threads without a summary are omitted.
    """
    lanes = load_all_lanes(db_path)
    lane: Dict[str, Any] | None = None
    if lane_id is not None:
        lane = next((l for l in lanes if int(l.get("id") or 0) == int(lane_id)), None)
    elif lane_name:
        key = (lane_name or "").strip().casefold()
        lane = next((l for l in lanes if (l.get("name") or "").strip().casefold() == key), None)
    if not lane:
        return None, []

    memberships = load_lane_thread_memberships(db_path)
    thread_ids = memberships.get(str(int(lane["id"])), [])
    if not thread_ids:
        return lane, []

    bundle = build_summaries_bundle(db_path)
    by_tid: Dict[str, Dict[str, Any]] = {}
    for row in bundle.get("summary") or []:
        tid = _normalize_field(row.get("thread_id"))
        if tid and tid not in by_tid:
            by_tid[tid] = row

    summaries: List[Dict[str, Any]] = []
    for tid in thread_ids:
        row = by_tid.get(tid)
        if row:
            summaries.append(row)
    summaries.sort(key=lambda s: _parse_iso_datetime(aggregate_thread_chronological_anchor(db_path, s)))
    return lane, summaries


_AGGREGATE_SUMMARY_FIELDS = (
    "summary",
    "highlights",
    "current_priorities",
    "waiting_on_others",
    "tone_overview",
)


def normalize_lane_summary_payload(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Extract storable lane-summary fields from an API/LLM response."""
    out: Dict[str, Any] = {}
    for key in _AGGREGATE_SUMMARY_FIELDS:
        val = raw.get(key)
        if key in ("highlights", "current_priorities", "waiting_on_others"):
            if isinstance(val, list):
                out[key] = [str(x).strip() for x in val if str(x).strip()]
            else:
                out[key] = []
        else:
            out[key] = _normalize_field(val)
    from utils.summary_timeliness import reframe_summary_temporal_fields

    return reframe_summary_temporal_fields(out)


def _ensure_lane_summaries_schema(conn: sqlite3.Connection) -> None:
    _ensure_lanes_schema(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS lane_summaries (
            lane_id INTEGER PRIMARY KEY,
            summary_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (lane_id) REFERENCES lanes(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_lane_summaries_updated_at "
        "ON lane_summaries(updated_at)"
    )


def save_lane_summary(
    db_path: str,
    *,
    lane_id: int,
    summary: Dict[str, Any],
) -> str:
    """
    Persist a roll-up summary for one lane.

    Returns ``updated_at`` (UTC ISO).
    """
    if lane_id <= 0:
        raise ValueError("lane_id is required")
    updated_at = datetime.now(timezone.utc).isoformat()
    raw = summary if isinstance(summary, dict) else {}
    payload = normalize_lane_summary_payload(raw)
    fp = _normalize_field(raw.get("input_fingerprint"))
    if fp:
        payload["input_fingerprint"] = fp
    as_of = _normalize_field(raw.get("summary_as_of_date"))
    if as_of:
        payload["summary_as_of_date"] = as_of
    db_file = Path(db_path)
    with connect_sqlite(db_file) as conn:
        _ensure_lane_summaries_schema(conn)
        row = conn.execute("SELECT id FROM lanes WHERE id = ?", (lane_id,)).fetchone()
        if not row:
            raise ValueError("lane_not_found")
        conn.execute(
            """
            INSERT INTO lane_summaries (lane_id, summary_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(lane_id) DO UPDATE SET
                summary_json = excluded.summary_json,
                updated_at = excluded.updated_at
            """,
            (lane_id, json.dumps(payload, ensure_ascii=False), updated_at),
        )
        conn.execute(
            "UPDATE lanes SET updated_at = ? WHERE id = ?",
            (updated_at, lane_id),
        )
        conn.commit()
    return updated_at


def load_lane_summary(db_path: str, *, lane_id: int) -> Optional[Dict[str, Any]]:
    """Return parsed lane summary JSON, or ``None``."""
    if lane_id <= 0:
        return None
    db_file = Path(db_path)
    with connect_sqlite(db_file) as conn:
        _ensure_lane_summaries_schema(conn)
        row = conn.execute(
            "SELECT summary_json, updated_at FROM lane_summaries WHERE lane_id = ?",
            (lane_id,),
        ).fetchone()
    if not row or not row[0]:
        return None
    try:
        loaded = json.loads(row[0])
        out = normalize_lane_summary_payload(loaded) if isinstance(loaded, dict) else {}
        out["updated_at"] = _normalize_field(row[1])
        if isinstance(loaded, dict) and loaded.get("input_fingerprint"):
            out["input_fingerprint"] = _normalize_field(loaded.get("input_fingerprint"))
        return out
    except json.JSONDecodeError:
        return None


def load_all_lane_summaries(db_path: str) -> Dict[str, Dict[str, Any]]:
    """Return ``lane_id`` → summary payload (includes ``updated_at``)."""
    db_file = Path(db_path)
    out: Dict[str, Dict[str, Any]] = {}
    with connect_sqlite(db_file) as conn:
        _ensure_lane_summaries_schema(conn)
        rows = conn.execute(
            "SELECT lane_id, summary_json, updated_at FROM lane_summaries ORDER BY updated_at DESC"
        ).fetchall()
    for lane_id, summary_json, updated_at in rows:
        from utils.summary_timeliness import lane_summary_is_stale

        key = str(int(lane_id))
        loaded: Dict[str, Any] = {}
        try:
            parsed = json.loads(summary_json or "{}")
            loaded = parsed if isinstance(parsed, dict) else {}
            payload = normalize_lane_summary_payload(loaded)
        except json.JSONDecodeError:
            payload = {}
        payload["updated_at"] = _normalize_field(updated_at)
        if loaded.get("input_fingerprint"):
            payload["input_fingerprint"] = _normalize_field(loaded.get("input_fingerprint"))
        if loaded.get("summary_as_of_date"):
            payload["summary_as_of_date"] = _normalize_field(loaded.get("summary_as_of_date"))
        stale = lane_summary_is_stale(payload)
        if stale:
            payload = {"updated_at": payload["updated_at"]}
        out[key] = payload
    return out


TODO_PLAN_THREAD_PREFIX = "todo:"


def todo_plan_thread_id(gmail_inbox_thread_id: str) -> str:
    """Synthetic plan thread id — not a tracked inbox/source thread."""
    tid = _normalize_field(gmail_inbox_thread_id)
    if not tid:
        return ""
    if tid.startswith(TODO_PLAN_THREAD_PREFIX):
        return tid
    return f"{TODO_PLAN_THREAD_PREFIX}{tid}"


def is_todo_plan_thread_id(inbox_thread_id: str) -> bool:
    return _normalize_field(inbox_thread_id).startswith(TODO_PLAN_THREAD_PREFIX)


def gmail_inbox_thread_id_from_todo_plan(inbox_thread_id: str) -> str:
    tid = _normalize_field(inbox_thread_id)
    if tid.startswith(TODO_PLAN_THREAD_PREFIX):
        return tid[len(TODO_PLAN_THREAD_PREFIX) :]
    return ""


def _migrate_legacy_todo_plan_thread_ids(conn: sqlite3.Connection) -> None:
    """Point todo-origin plans at synthetic ``todo:`` ids, not inbox Gmail thread ids."""
    _ensure_thread_tracking_schema(conn)
    rows = conn.execute(
        """
        SELECT p.id, p.inbox_thread_id
        FROM thread_plans p
        WHERE p.inbox_thread_id NOT LIKE ?
          AND p.inbox_thread_id NOT LIKE 'text:%'
          AND p.inbox_thread_id IN (
            SELECT inbox_thread_id FROM thread_tracking
            WHERE inbox_delivery_kind = 'todo_plan'
          )
        """,
        (f"{TODO_PLAN_THREAD_PREFIX}%",),
    ).fetchall()
    for plan_id, tid in rows:
        conn.execute(
            "UPDATE thread_plans SET inbox_thread_id = ? WHERE id = ?",
            (todo_plan_thread_id(_normalize_field(tid)), int(plan_id)),
        )


def _ensure_thread_plans_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS thread_plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inbox_thread_id TEXT NOT NULL,
            action TEXT NOT NULL,
            step_type TEXT NOT NULL DEFAULT 'follow up needed',
            by_when TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_thread_plans_inbox_thread_id "
        "ON thread_plans(inbox_thread_id)"
    )
    _migrate_legacy_todo_plan_thread_ids(conn)


def create_todo_thread_plan(
    db_path: str,
    *,
    gmail_inbox_thread_id: str,
    action: str,
    step_type: str = "follow up needed",
    by_when: str = "",
) -> Dict[str, Any]:
    """Create a plan from a Todo: inbox email (not linked to a tracked thread)."""
    return create_thread_plan(
        db_path,
        inbox_thread_id=todo_plan_thread_id(gmail_inbox_thread_id),
        action=action,
        step_type=step_type,
        by_when=by_when,
    )


def _ensure_dismissed_todo_plans_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dismissed_todo_plans (
            inbox_thread_id TEXT NOT NULL,
            action TEXT NOT NULL,
            dismissed_at TEXT NOT NULL,
            PRIMARY KEY (inbox_thread_id, action)
        )
        """
    )


def _record_dismissed_todo_plan(
    conn: sqlite3.Connection,
    inbox_thread_id: str,
    action: str,
    *,
    dismissed_at: Optional[str] = None,
) -> None:
    tid = _normalize_field(inbox_thread_id)
    label = _normalize_field(action)
    if not tid or not label:
        return
    _ensure_dismissed_todo_plans_schema(conn)
    when = dismissed_at or datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT OR REPLACE INTO dismissed_todo_plans
            (inbox_thread_id, action, dismissed_at)
        VALUES (?, ?, ?)
        """,
        (tid, label, when),
    )


def dismiss_todo_plan(db_path: str, *, inbox_thread_id: str, action: str) -> None:
    """Record a todo plan the user removed so inbox sync does not recreate it."""
    tid = _normalize_field(inbox_thread_id)
    label = _normalize_field(action)
    if not tid or not label:
        return
    db_file = Path(db_path)
    with connect_sqlite(db_file) as conn:
        _record_dismissed_todo_plan(conn, tid, label)
        conn.commit()


def todo_plan_is_dismissed(
    db_path: str, inbox_thread_id: str, action: str
) -> bool:
    """True when the user deleted this todo (thread, action) and it must not be recreated."""
    tid = _normalize_field(inbox_thread_id)
    label = _normalize_field(action)
    if not tid or not label:
        return False
    db_file = Path(db_path)
    with connect_sqlite(db_file) as conn:
        _ensure_dismissed_todo_plans_schema(conn)
        row = conn.execute(
            """
            SELECT 1 FROM dismissed_todo_plans
            WHERE inbox_thread_id = ? AND action = ?
            LIMIT 1
            """,
            (tid, label),
        ).fetchone()
    return row is not None


def _sync_thread_tracking_has_plan(conn: sqlite3.Connection, inbox_thread_id: str) -> None:
    """Set ``has_plan`` on ``thread_tracking`` from ``thread_plans`` row count."""
    tid = _normalize_field(inbox_thread_id)
    if not tid or is_todo_plan_thread_id(tid):
        return
    _ensure_thread_plans_schema(conn)
    _ensure_thread_tracking_schema(conn)
    row = conn.execute(
        "SELECT COUNT(*) FROM thread_plans WHERE inbox_thread_id = ?",
        (tid,),
    ).fetchone()
    count = int(row[0] or 0) if row else 0
    has_plan = 1 if count > 0 else 0
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE thread_tracking SET has_plan = ?, updated_at = ? "
        "WHERE inbox_thread_id = ?",
        (has_plan, now, tid),
    )


def plan_exists_for_thread_action(
    db_path: str, inbox_thread_id: str, action: str
) -> bool:
    """True when a plan with the same thread and action already exists."""
    tid = _normalize_field(inbox_thread_id)
    label = _normalize_field(action)
    if not tid or not label:
        return False
    db_file = Path(db_path)
    with connect_sqlite(db_file) as conn:
        _ensure_thread_plans_schema(conn)
        row = conn.execute(
            """
            SELECT 1 FROM thread_plans
            WHERE inbox_thread_id = ? AND action = ?
            LIMIT 1
            """,
            (tid, label),
        ).fetchone()
    return row is not None


def create_thread_plan(
    db_path: str,
    *,
    inbox_thread_id: str,
    action: str,
    step_type: str = "follow up needed",
    by_when: str = "",
) -> Dict[str, Any]:
    """Insert a plan row and return it."""
    tid = _normalize_field(inbox_thread_id)
    label = _normalize_field(action)
    if not tid:
        raise ValueError("missing_thread_id")
    if not label:
        raise ValueError("missing_plan_action")
    st = _normalize_field(step_type) or "follow up needed"
    when = _normalize_field(by_when)
    now = datetime.now(timezone.utc).isoformat()
    db_file = Path(db_path)
    with connect_sqlite(db_file) as conn:
        _ensure_thread_plans_schema(conn)
        _ensure_thread_tracking_schema(conn)
        cur = conn.execute(
            """
            INSERT INTO thread_plans
                (inbox_thread_id, action, step_type, by_when, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (tid, label, st, when, now, now),
        )
        plan_id = int(cur.lastrowid or 0)
        _sync_thread_tracking_has_plan(conn, tid)
        conn.commit()
    return {
        "id": plan_id,
        "inbox_thread_id": tid,
        "action": label,
        "step_type": st,
        "by_when": when,
        "created_at": now,
        "updated_at": now,
    }


def update_thread_plan(
    db_path: str,
    *,
    plan_id: int,
    inbox_thread_id: Optional[str] = None,
    action: Optional[str] = None,
    step_type: Optional[str] = None,
    by_when: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Update a plan row. Returns the updated row, or None if missing."""
    if plan_id <= 0:
        return None
    db_file = Path(db_path)
    with connect_sqlite(db_file) as conn:
        _ensure_thread_plans_schema(conn)
        _ensure_thread_tracking_schema(conn)
        row = conn.execute(
            """
            SELECT id, inbox_thread_id, action, step_type, by_when, created_at, updated_at
            FROM thread_plans WHERE id = ?
            """,
            (plan_id,),
        ).fetchone()
        if not row:
            return None
        old_tid = _normalize_field(row[1])
        tid = _normalize_field(inbox_thread_id) if inbox_thread_id is not None else old_tid
        label = _normalize_field(action) if action is not None else _normalize_field(row[2])
        st = (
            _normalize_field(step_type) or "follow up needed"
            if step_type is not None
            else _normalize_field(row[3]) or "follow up needed"
        )
        when = _normalize_field(by_when) if by_when is not None else _normalize_field(row[4])
        if not tid:
            raise ValueError("missing_thread_id")
        if not label:
            raise ValueError("missing_plan_action")
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            UPDATE thread_plans
            SET inbox_thread_id = ?, action = ?, step_type = ?, by_when = ?, updated_at = ?
            WHERE id = ?
            """,
            (tid, label, st, when, now, plan_id),
        )
        conn.commit()
        if old_tid and old_tid != tid:
            _sync_thread_tracking_has_plan(conn, old_tid)
        _sync_thread_tracking_has_plan(conn, tid)
    return {
        "id": plan_id,
        "inbox_thread_id": tid,
        "action": label,
        "step_type": st,
        "by_when": when,
        "created_at": str(row[5]),
        "updated_at": now,
    }


def delete_thread_plan(db_path: str, *, plan_id: int) -> bool:
    """Delete a plan by id. Returns False if missing."""
    if plan_id <= 0:
        return False
    db_file = Path(db_path)
    with connect_sqlite(db_file) as conn:
        _ensure_thread_plans_schema(conn)
        _ensure_thread_tracking_schema(conn)
        row = conn.execute(
            "SELECT inbox_thread_id, action FROM thread_plans WHERE id = ?",
            (plan_id,),
        ).fetchone()
        tid = _normalize_field(row[0]) if row else ""
        action = _normalize_field(row[1]) if row else ""
        cur = conn.execute("DELETE FROM thread_plans WHERE id = ?", (plan_id,))
        deleted = int(cur.rowcount or 0) > 0
        if deleted and tid:
            if is_todo_plan_thread_id(tid):
                gmail_tid = gmail_inbox_thread_id_from_todo_plan(tid)
                if action and gmail_tid:
                    _record_dismissed_todo_plan(conn, gmail_tid, action)
            else:
                _sync_thread_tracking_has_plan(conn, tid)
        conn.commit()
        return deleted


def untrack_todo_plan_inbox_thread(db_path: str, *, inbox_thread_id: str) -> bool:
    """
    Consume a Todo: inbox thread: drop timeline/summary rows and mark tracking removed.

    ``thread_plans`` rows are kept — Todo emails should exist only as plans.
    """
    tid = _normalize_field(inbox_thread_id)
    if not tid or tid.startswith("text:") or tid.startswith("slack:") or tid.startswith("linkedin:"):
        return False
    now = datetime.now(timezone.utc).isoformat()
    db_file = Path(db_path)
    with connect_sqlite(db_file) as conn:
        _ensure_thread_tracking_schema(conn)
        _ensure_timeline_schema(conn)
        _ensure_claude_outputs_schema(conn)
        _ensure_thread_summaries_schema(conn)
        _ensure_thread_draft_replies_schema(conn)
        _ensure_lanes_schema(conn)
        conn.execute(
            "DELETE FROM timeline_entries WHERE COALESCE(thread_id, '') = ?", (tid,)
        )
        conn.execute(
            "DELETE FROM claude_message_outputs WHERE COALESCE(thread_id, '') = ?",
            (tid,),
        )
        conn.execute("DELETE FROM thread_summaries WHERE thread_id = ?", (tid,))
        conn.execute("DELETE FROM thread_draft_replies WHERE thread_id = ?", (tid,))
        conn.execute("DELETE FROM lane_threads WHERE inbox_thread_id = ?", (tid,))
        conn.execute(
            """
            INSERT INTO thread_tracking (
                inbox_thread_id, source_email, snoozed, inner_rfc_message_id,
                resolved_oauth_account_id, resolution_error, inbox_delivery_kind,
                created_at, updated_at
            )
            VALUES (?, '', 2, '', '', '', 'todo_plan', ?, ?)
            ON CONFLICT(inbox_thread_id) DO UPDATE SET
                snoozed = 2,
                inbox_delivery_kind = COALESCE(
                    NULLIF(excluded.inbox_delivery_kind, ''),
                    thread_tracking.inbox_delivery_kind
                ),
                updated_at = excluded.updated_at
            """,
            (tid, now, now),
        )
        _sync_thread_tracking_has_plan(conn, tid)
        conn.commit()
        return True


def load_thread_subjects(db_path: str, thread_id: str) -> List[str]:
    """Distinct non-empty subjects from ``claude_message_outputs`` for one thread."""
    tid = _normalize_field(thread_id)
    if not tid:
        return []
    db_file = Path(db_path)
    with connect_sqlite(db_file) as conn:
        _ensure_claude_outputs_schema(conn)
        rows = conn.execute(
            """
            SELECT DISTINCT subject FROM claude_message_outputs
            WHERE COALESCE(thread_id, '') = ? AND TRIM(COALESCE(subject, '')) != ''
            """,
            (tid,),
        ).fetchall()
    return [_normalize_field(r[0]) for r in rows if _normalize_field(r[0])]


def load_all_thread_plans(db_path: str) -> List[Dict[str, Any]]:
    """Return all user plans, newest first."""
    db_file = Path(db_path)
    with connect_sqlite(db_file) as conn:
        _ensure_thread_plans_schema(conn)
        rows = conn.execute(
            """
            SELECT id, inbox_thread_id, action, step_type, by_when, created_at, updated_at
            FROM thread_plans
            ORDER BY updated_at DESC, id DESC
            """
        ).fetchall()
    return [
        {
            "id": int(r[0]),
            "inbox_thread_id": _normalize_field(r[1]),
            "action": _normalize_field(r[2]),
            "step_type": _normalize_field(r[3]) or "follow up needed",
            "by_when": _normalize_field(r[4]),
            "created_at": _normalize_field(r[5]),
            "updated_at": _normalize_field(r[6]),
        }
        for r in rows
    ]


def _parse_meeting_iso_utc(iso: str) -> Optional[datetime]:
    """Parse a calendar ISO timestamp to UTC (returns ``None`` when invalid/empty)."""
    if not (iso or "").strip():
        return None
    try:
        parsed = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _meeting_bounds_utc(start_iso: str, end_iso: str) -> Optional[Tuple[datetime, datetime]]:
    start = _parse_meeting_iso_utc(start_iso)
    if start is None:
        return None
    end = _parse_meeting_iso_utc(end_iso)
    if end is None or end <= start:
        end = start + timedelta(hours=1)
    return start, end


def fetch_meetings_rows(db_path: str, *, days: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    Return meeting rows (attendees parsed from JSON).

    When ``days`` is set, only rows that may still be active or start within the
    next ``days`` calendar days are returned (matches dashboard lookahead filtering).
    """
    db_file = Path(db_path)
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=days) if days is not None and days > 0 else None
    with connect_sqlite(db_file) as conn:
        _ensure_meetings_schema(conn)
        cur = conn.execute(
            """
            SELECT dedupe_key, summary, start_iso, end_iso, location, html_link,
                   kind, calendar_summary, account_id, week_local, attendees_json,
                   exported_at, timezone, updated_at
            FROM meetings
            WHERE start_iso != ''
            ORDER BY start_iso ASC
            """,
        )
        out: List[Dict[str, Any]] = []
        for r in cur.fetchall():
            start_iso = r[2] or ""
            end_iso = r[3] or ""
            if horizon is not None:
                bounds = _meeting_bounds_utc(start_iso, end_iso)
                if bounds is None:
                    continue
                start_u, end_u = bounds
                if start_u >= horizon or end_u < now:
                    continue
            attendees: List[str] = []
            try:
                parsed = json.loads(r[10] or "[]")
                if isinstance(parsed, list):
                    attendees = _normalize_attendee_list(parsed)
            except json.JSONDecodeError:
                pass
            out.append(
                {
                    "dedupe_key": r[0] or "",
                    "summary": r[1] or "",
                    "start_iso": start_iso,
                    "end_iso": end_iso,
                    "location": r[4] or "",
                    "html_link": r[5] or "",
                    "kind": r[6] or "",
                    "calendar_summary": r[7] or "",
                    "account_id": r[8] or "",
                    "week_local": r[9] or "",
                    "attendees": attendees,
                    "exported_at": r[11] or "",
                    "timezone": r[12] or "",
                    "updated_at": r[13] or "",
                }
            )
        return out


def _migrate_thread_tracking_if_needed(conn: sqlite3.Connection) -> None:
    """Upgrade legacy thread_tracking (thread_id/account_id) to new columns."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(thread_tracking)").fetchall()}
    if not cols:
        return
    if "source_email" in cols:
        return

    conn.execute(
        """
        CREATE TABLE _thread_tracking_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inbox_thread_id TEXT NOT NULL,
            source_email TEXT NOT NULL,
            snoozed INTEGER NOT NULL DEFAULT 0,
            inner_rfc_message_id TEXT,
            resolved_oauth_account_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (inbox_thread_id)
        )
        """
    )
    rows = conn.execute("SELECT * FROM thread_tracking").fetchall()
    col_names = [d[0] for d in conn.execute("PRAGMA table_info(thread_tracking)").fetchall()]
    for tup in rows:
        rec = dict(zip(col_names, tup))
        tid = (rec.get("thread_id") or "").strip()
        if not tid:
            continue
        acct = (rec.get("account_id") or "").strip()
        if "@" in acct and "," not in acct:
            src = acct.lower()
        elif "," in acct:
            src = acct.split(",")[0].strip().lower()
        else:
            src = "unknown@local.invalid"
        ca = (rec.get("created_at") or rec.get("updated_at") or "").strip()
        ua = (rec.get("updated_at") or ca).strip()
        if not ca:
            ca = "1970-01-01T00:00:00+00:00"
        if not ua:
            ua = ca
        conn.execute(
            """
            INSERT OR IGNORE INTO _thread_tracking_new (
                inbox_thread_id, source_email, snoozed, inner_rfc_message_id,
                resolved_oauth_account_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (tid, src, 0, None, None, ca or ua, ua or ca),
        )
    conn.execute("DROP TABLE thread_tracking")
    conn.execute("ALTER TABLE _thread_tracking_new RENAME TO thread_tracking")


def _ensure_thread_tracking_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS thread_tracking (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inbox_thread_id TEXT NOT NULL,
            source_email TEXT NOT NULL,
            snoozed INTEGER NOT NULL DEFAULT 0,
            inner_rfc_message_id TEXT,
            resolved_oauth_account_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (inbox_thread_id)
        )
        """
    )
    cols = {row[1] for row in conn.execute("PRAGMA table_info(thread_tracking)").fetchall()}
    if "source_email" not in cols:
        _migrate_thread_tracking_if_needed(conn)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_thread_tracking_inbox_thread_id "
        "ON thread_tracking(inbox_thread_id)"
    )
    cols = {row[1] for row in conn.execute("PRAGMA table_info(thread_tracking)").fetchall()}
    if "snoozed" not in cols:
        conn.execute(
            "ALTER TABLE thread_tracking ADD COLUMN snoozed INTEGER NOT NULL DEFAULT 0"
        )
    if "resolution_error" not in cols:
        conn.execute("ALTER TABLE thread_tracking ADD COLUMN resolution_error TEXT")
    cols = {row[1] for row in conn.execute("PRAGMA table_info(thread_tracking)").fetchall()}
    if "inbox_delivery_kind" not in cols:
        conn.execute(
            "ALTER TABLE thread_tracking ADD COLUMN inbox_delivery_kind TEXT"
        )
    cols = {row[1] for row in conn.execute("PRAGMA table_info(thread_tracking)").fetchall()}
    if "has_plan" not in cols:
        conn.execute(
            "ALTER TABLE thread_tracking ADD COLUMN has_plan INTEGER NOT NULL DEFAULT 0"
        )
        _ensure_thread_plans_schema(conn)
        conn.execute(
            """
            UPDATE thread_tracking SET has_plan = 1
            WHERE inbox_thread_id IN (
                SELECT DISTINCT inbox_thread_id FROM thread_plans
            )
            """
        )
    cols = {row[1] for row in conn.execute("PRAGMA table_info(thread_tracking)").fetchall()}
    if "gmail_inbox_thread_id" not in cols:
        conn.execute(
            "ALTER TABLE thread_tracking ADD COLUMN gmail_inbox_thread_id TEXT"
        )
    cols = [row[1] for row in conn.execute("PRAGMA table_info(thread_tracking)").fetchall()]
    if "participant_email" in cols:
        conn.execute(
            """
            CREATE TABLE _thread_tracking_replace_participant (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                inbox_thread_id TEXT NOT NULL,
                source_email TEXT NOT NULL,
                snoozed INTEGER NOT NULL DEFAULT 0,
                inner_rfc_message_id TEXT,
                resolved_oauth_account_id TEXT,
                resolution_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (inbox_thread_id)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO _thread_tracking_replace_participant (
                id, inbox_thread_id, source_email, snoozed, inner_rfc_message_id,
                resolved_oauth_account_id, resolution_error, created_at, updated_at
            )
            SELECT
                id, inbox_thread_id, source_email,
                COALESCE(snoozed, 0), inner_rfc_message_id,
                resolved_oauth_account_id, COALESCE(resolution_error, ''),
                created_at, updated_at
            FROM thread_tracking
            """
        )
        conn.execute("DROP TABLE thread_tracking")
        conn.execute(
            "ALTER TABLE _thread_tracking_replace_participant RENAME TO thread_tracking"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_thread_tracking_inbox_thread_id "
            "ON thread_tracking(inbox_thread_id)"
        )


def replace_timeline_entries(db_path: str, rows: List[Dict[str, Any]]) -> int:
    """
    Replace ``timeline_entries`` (multi-account / sync tooling).

    Returns the number of inserted rows.
    """
    db_file = Path(db_path)
    deduped_rows = _dedupe_rows(rows)

    with connect_sqlite(db_file) as conn:
        _ensure_timeline_schema(conn)
        conn.execute("DELETE FROM timeline_entries")
        if deduped_rows:
            conn.executemany(
                """
                INSERT INTO timeline_entries (
                    source_id, type, datetime, sender, recipients, participants,
                    summary, body, thread_id, fetch_oauth_account_id, body_has_image
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        row["source_id"],
                        row["type"],
                        row["datetime"],
                        row["sender"],
                        row["recipients"],
                        row["participants"],
                        row["summary"],
                        row["body"],
                        row.get("thread_id", ""),
                        row.get("fetch_oauth_account_id", ""),
                        row.get("body_has_image", 0),
                    )
                    for row in deduped_rows
                ],
            )
        conn.commit()

    return len(deduped_rows)


def upsert_timeline_entries(db_path: str, rows: List[Dict[str, Any]]) -> int:
    """
    Insert or update rows in ``timeline_entries`` by ``source_id`` (does not delete existing rows).

    Returns the number of rows applied (inserts + updates).
    """
    db_file = Path(db_path)
    deduped_rows = _dedupe_rows(rows)

    with connect_sqlite(db_file) as conn:
        _ensure_timeline_schema(conn)
        if deduped_rows:
            conn.executemany(
                """
                INSERT INTO timeline_entries (
                    source_id, type, datetime, sender, recipients, participants,
                    summary, body, thread_id, fetch_oauth_account_id, body_has_image
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    type = excluded.type,
                    datetime = excluded.datetime,
                    sender = excluded.sender,
                    recipients = excluded.recipients,
                    participants = excluded.participants,
                    summary = excluded.summary,
                    body = excluded.body,
                    thread_id = excluded.thread_id,
                    fetch_oauth_account_id = excluded.fetch_oauth_account_id,
                    body_has_image = excluded.body_has_image
                """,
                [
                    (
                        row["source_id"],
                        row["type"],
                        row["datetime"],
                        row["sender"],
                        row["recipients"],
                        row["participants"],
                        row["summary"],
                        row["body"],
                        row.get("thread_id", ""),
                        row.get("fetch_oauth_account_id", ""),
                        row.get("body_has_image", 0),
                    )
                    for row in deduped_rows
                ],
            )
        conn.commit()

    return len(deduped_rows)


def prune_timeline_entries_for_thread(
    db_path: str, thread_id: str, keep_source_ids: set[str]
) -> int:
    """Delete timeline rows for ``thread_id`` whose ``source_id`` is not in ``keep_source_ids``."""
    tid = _normalize_field(thread_id)
    if not tid:
        return 0
    keep = sorted({str(x).strip() for x in keep_source_ids if str(x).strip()})
    db_file = Path(db_path)
    with connect_sqlite(db_file) as conn:
        _ensure_timeline_schema(conn)
        if keep:
            placeholders = ",".join("?" for _ in keep)
            cur = conn.execute(
                f"""
                DELETE FROM timeline_entries
                WHERE thread_id = ? AND COALESCE(TRIM(source_id), '') != ''
                  AND source_id NOT IN ({placeholders})
                """,
                [tid, *keep],
            )
        else:
            cur = conn.execute(
                "DELETE FROM timeline_entries WHERE thread_id = ?",
                (tid,),
            )
        conn.commit()
        return int(cur.rowcount or 0)


def upsert_thread_tracking(db_path: str, rows: List[Dict[str, Any]]) -> int:
    """
    Insert or update rows in ``thread_tracking`` by ``inbox_thread_id``.

    Returns the number of rows applied (inserts + updates).
    """
    db_file = Path(db_path)
    deduped_rows = _dedupe_thread_tracking_rows(rows)

    with connect_sqlite(db_file) as conn:
        _ensure_thread_tracking_schema(conn)
        if deduped_rows:
            conn.executemany(
                """
                INSERT INTO thread_tracking (
                    inbox_thread_id, gmail_inbox_thread_id, source_email, snoozed,
                    inner_rfc_message_id, resolved_oauth_account_id, resolution_error,
                    inbox_delivery_kind, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(inbox_thread_id) DO UPDATE SET
                    gmail_inbox_thread_id = COALESCE(
                        NULLIF(excluded.gmail_inbox_thread_id, ''),
                        thread_tracking.gmail_inbox_thread_id
                    ),
                    source_email = excluded.source_email,
                    -- Preserve persisted snooze/plan state during refresh upserts.
                    -- Explicit changes are applied via API endpoints.
                    snoozed = COALESCE(thread_tracking.snoozed, 0),
                    has_plan = COALESCE(thread_tracking.has_plan, 0),
                    inner_rfc_message_id = COALESCE(
                        NULLIF(excluded.inner_rfc_message_id, ''),
                        thread_tracking.inner_rfc_message_id
                    ),
                    resolved_oauth_account_id = COALESCE(
                        NULLIF(excluded.resolved_oauth_account_id, ''),
                        thread_tracking.resolved_oauth_account_id
                    ),
                    resolution_error = excluded.resolution_error,
                    inbox_delivery_kind = COALESCE(
                        NULLIF(excluded.inbox_delivery_kind, ''),
                        thread_tracking.inbox_delivery_kind
                    ),
                    updated_at = excluded.updated_at
                WHERE thread_tracking.snoozed != 2
                """,
                [
                    (
                        row["inbox_thread_id"],
                        row.get("gmail_inbox_thread_id", ""),
                        row["source_email"],
                        row.get("snoozed", 0),
                        row["inner_rfc_message_id"],
                        row["resolved_oauth_account_id"],
                        row.get("resolution_error", ""),
                        row.get("inbox_delivery_kind", ""),
                        row["created_at"],
                        row["updated_at"],
                    )
                    for row in deduped_rows
                ],
            )
        conn.commit()

    return len(deduped_rows)


def fetch_removed_inbox_thread_ids(db_path: str) -> set[str]:
    """Inbox thread ids with ``snoozed`` = 2 (removed from tracking)."""
    db_file = Path(db_path)
    with connect_sqlite(db_file) as conn:
        _ensure_thread_tracking_schema(conn)
        rows = conn.execute(
            "SELECT inbox_thread_id FROM thread_tracking WHERE snoozed = 2"
        ).fetchall()
    out: set[str] = set()
    for (tid,) in rows:
        t = _normalize_field(tid)
        if t:
            out.add(t)
    return out


def retire_legacy_gmail_forward_tracking(
    db_path: str, gmail_inbox_thread_id: str
) -> bool:
    """
    Drop a legacy ``forward_to`` row keyed by inbox Gmail ``threadId`` once RFC rows exist.

    Does not remap timeline data (avoids dragging incorrectly merged messages onto one RFC).
    """
    tid = _normalize_field(gmail_inbox_thread_id)
    if not tid or tid.startswith(_RFC_THREAD_PREFIX):
        return False
    with connect_sqlite(db_path) as conn:
        _ensure_thread_tracking_schema(conn)
        row = conn.execute(
            """
            SELECT inbox_delivery_kind FROM thread_tracking
            WHERE inbox_thread_id = ?
            """,
            (tid,),
        ).fetchone()
        if not row or _normalize_field(row[0]) != "forward_to":
            return False
        (rfc_count,) = conn.execute(
            """
            SELECT COUNT(*) FROM thread_tracking
            WHERE gmail_inbox_thread_id = ?
              AND inbox_thread_id LIKE ?
            """,
            (tid, f"{_RFC_THREAD_PREFIX}%"),
        ).fetchone()
        if int(rfc_count or 0) < 1:
            return False
        conn.execute("DELETE FROM thread_tracking WHERE inbox_thread_id = ?", (tid,))
        conn.commit()
    return True


def remap_dashboard_thread_id(db_path: str, from_tid: str, to_tid: str) -> None:
    """Point timeline, Claude outputs, lanes, and drafts at one dashboard thread id."""
    src = _normalize_field(from_tid)
    dst = _normalize_field(to_tid)
    if not src or not dst or src == dst:
        return
    removed = fetch_removed_inbox_thread_ids(db_path)
    if src in removed or dst in removed:
        return
    with connect_sqlite(db_path) as conn:
        _ensure_timeline_schema(conn)
        _ensure_claude_outputs_schema(conn)
        _ensure_thread_tracking_schema(conn)
        _ensure_lanes_schema(conn)
        _ensure_thread_plans_schema(conn)
        _ensure_thread_draft_replies_schema(conn)
        conn.execute(
            "UPDATE timeline_entries SET thread_id = ? WHERE thread_id = ?",
            (dst, src),
        )
        conn.execute(
            """
            DELETE FROM claude_message_outputs
            WHERE COALESCE(thread_id, '') = ?
              AND COALESCE(TRIM(source_id), '') != ''
              AND source_id IN (
                  SELECT source_id FROM claude_message_outputs
                  WHERE COALESCE(thread_id, '') = ?
              )
            """,
            (src, dst),
        )
        conn.execute(
            "UPDATE claude_message_outputs SET thread_id = ? WHERE thread_id = ?",
            (dst, src),
        )
        conn.execute(
            "UPDATE lane_threads SET inbox_thread_id = ? WHERE inbox_thread_id = ?",
            (dst, src),
        )
        conn.execute(
            "DELETE FROM lane_threads WHERE inbox_thread_id = ? AND rowid NOT IN ("
            "SELECT MIN(rowid) FROM lane_threads WHERE inbox_thread_id = ? GROUP BY lane_id"
            ")",
            (dst, dst),
        )
        conn.execute(
            "UPDATE thread_draft_replies SET thread_id = ? WHERE thread_id = ?",
            (dst, src),
        )
        conn.execute(
            "UPDATE thread_plans SET inbox_thread_id = ? WHERE inbox_thread_id = ?",
            (dst, src),
        )
        conn.execute(
            "DELETE FROM thread_tracking WHERE inbox_thread_id = ?", (src,)
        )
        conn.commit()


_RFC_THREAD_PREFIX = "rfc:"


def _is_rfc_thread_id(thread_id: str) -> bool:
    return str(thread_id or "").strip().startswith(_RFC_THREAD_PREFIX)


def _cc_bcc_gmail_vs_rfc_pair(
    group: List[Dict[str, Any]],
) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """
    Return ``(gmail_inbox_row, rfc_canonical_row)`` for Cc/Bcc migration leftovers.

    Before RFC-prefixed tracking keys, inbox seeds used the Fivelanes Gmail thread id;
    the canonical row keeps that id in ``gmail_inbox_thread_id``.
    """
    if len(group) != 2:
        return None
    rfc_rows = [r for r in group if _is_rfc_thread_id(r.get("inbox_thread_id"))]
    gmail_rows = [r for r in group if not _is_rfc_thread_id(r.get("inbox_thread_id"))]
    if len(rfc_rows) != 1 or len(gmail_rows) != 1:
        return None
    rfc_row, gmail_row = rfc_rows[0], gmail_rows[0]
    kind = _normalize_field(gmail_row.get("inbox_delivery_kind"))
    if kind not in ("cc_bcc", "cc_bcc_only"):
        return None
    gmail_tid = _normalize_field(gmail_row.get("inbox_thread_id"))
    rfc_gmail = _normalize_field(rfc_row.get("gmail_inbox_thread_id"))
    if not gmail_tid or rfc_gmail != gmail_tid:
        return None
    return gmail_row, rfc_row


def _inbox_seed_vs_discovered_pair(
    group: List[Dict[str, Any]],
) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """
    Return ``(discovered_row, inbox_seed_row)`` for exactly one inbox seed plus one
    discovered source-thread row.

    Seed rows have ``inbox_delivery_kind`` and/or ``resolved_oauth_account_id``;
    discovered rows are the extra ``thread_tracking`` entry created from a source
    Gmail ``threadId`` during expansion.
    """
    if len(group) != 2:
        return None
    a, b = group[0], group[1]

    def _is_seed(row: Dict[str, Any]) -> bool:
        return bool(
            _normalize_field(row.get("inbox_delivery_kind"))
            or _normalize_field(row.get("resolved_oauth_account_id"))
        )

    sa, sb = _is_seed(a), _is_seed(b)
    if sa and not sb:
        return b, a
    if sb and not sa:
        return a, b
    return None


def collapse_thread_tracking_duplicates_by_inner_rfc(db_path: str) -> int:
    """
    Merge ``thread_tracking`` rows that share ``inner_rfc_message_id``.

    Only collapses an exact pair where one row is the inbox seed (has
    ``inbox_delivery_kind``) and the other is a discovered source Gmail ``threadId``.
    """
    rows = fetch_thread_tracking_rows(db_path)
    by_inner: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        inner = _normalize_field(row.get("inner_rfc_message_id"))
        if inner:
            by_inner.setdefault(inner, []).append(row)
    collapsed = 0
    for group in by_inner.values():
        pair = _inbox_seed_vs_discovered_pair(group) or _cc_bcc_gmail_vs_rfc_pair(group)
        if not pair:
            continue
        discovered, seed = pair
        if int(discovered.get("snoozed") or 0) == 2 or int(seed.get("snoozed") or 0) == 2:
            continue
        from_tid = _normalize_field(discovered.get("inbox_thread_id"))
        to_tid = _normalize_field(seed.get("inbox_thread_id"))
        if from_tid and to_tid and from_tid != to_tid:
            remap_dashboard_thread_id(db_path, from_tid, to_tid)
            collapsed += 1
    if collapsed:
        prune_inbox_shell_duplicate_entries(db_path)
    return collapsed


def prune_inbox_shell_duplicate_entries(db_path: str) -> Tuple[int, int]:
    """
    Drop Fivelanes-inbox Bcc/Cc shell copies (``source_id`` = ``gmail_inbox_thread_id``).

    Returns ``(timeline_deleted, claude_outputs_deleted)``.
    """
    shell_ids: set[str] = set()
    for row in fetch_thread_tracking_rows(db_path):
        if not _is_rfc_thread_id(row.get("inbox_thread_id")):
            continue
        gid = _normalize_field(row.get("gmail_inbox_thread_id"))
        if gid:
            shell_ids.add(gid)
    if not shell_ids:
        return 0, 0

    placeholders = ",".join("?" for _ in shell_ids)
    params = sorted(shell_ids)
    with connect_sqlite(db_path) as conn:
        _ensure_timeline_schema(conn)
        _ensure_claude_outputs_schema(conn)
        cur_t = conn.execute(
            f"""
            DELETE FROM timeline_entries
            WHERE source_id IN ({placeholders})
            """,
            params,
        )
        cur_c = conn.execute(
            f"""
            DELETE FROM claude_message_outputs
            WHERE source_id IN ({placeholders})
            """,
            params,
        )
        conn.commit()
        return int(cur_t.rowcount or 0), int(cur_c.rowcount or 0)


def fetch_thread_tracking_rows(db_path: str) -> List[Dict[str, Any]]:
    """Return all thread_tracking rows (for pipeline expand step)."""
    db_file = Path(db_path)
    with connect_sqlite(db_file) as conn:
        _ensure_thread_tracking_schema(conn)
        cur = conn.execute(
            """
            SELECT inbox_thread_id, gmail_inbox_thread_id, source_email, snoozed, has_plan,
                   inner_rfc_message_id, resolved_oauth_account_id, resolution_error,
                   inbox_delivery_kind, created_at, updated_at
            FROM thread_tracking
            ORDER BY updated_at DESC
            """
        )
        out = []
        for r in cur.fetchall():
            out.append(
                {
                    "inbox_thread_id": r[0] or "",
                    "gmail_inbox_thread_id": r[1] or "",
                    "source_email": r[2] or "",
                    "snoozed": int(r[3] or 0),
                    "has_plan": int(r[4] or 0),
                    "inner_rfc_message_id": r[5] or "",
                    "resolved_oauth_account_id": r[6] or "",
                    "resolution_error": r[7] or "",
                    "inbox_delivery_kind": r[8] or "",
                    "created_at": r[9] or "",
                    "updated_at": r[10] or "",
                }
            )
        return out


def set_thread_tracking_snoozed(
    db_path: str, *, inbox_thread_id: str, snoozed: int
) -> bool:
    """Persist snooze flag for one ``thread_tracking`` row by inbox thread id."""
    tid = _normalize_field(inbox_thread_id)
    if not tid:
        return False
    raw = int(snoozed)
    snooze_value = raw if raw in (0, 1, 2) else 0
    db_file = Path(db_path)
    with connect_sqlite(db_file) as conn:
        _ensure_thread_tracking_schema(conn)
        cur = conn.execute(
            "UPDATE thread_tracking SET snoozed = ?, updated_at = ? "
            "WHERE inbox_thread_id = ?",
            (snooze_value, datetime.now(timezone.utc).isoformat(), tid),
        )
        conn.commit()
        return cur.rowcount > 0


def clear_snooze_only_for_threads(
    db_path: str, inbox_thread_ids: Sequence[str]
) -> None:
    """
    Set ``snoozed`` from 1 → 0 for the given inbox thread ids on ``thread_tracking`` and on
    ``claude_message_outputs`` rows keyed by ``thread_id``. Rows with ``snoozed`` = 2 (removed)
    are left unchanged.
    """
    tids: List[str] = []
    seen: set[str] = set()
    for x in inbox_thread_ids:
        t = _normalize_field(x)
        if t and t not in seen:
            seen.add(t)
            tids.append(t)
    if not tids:
        return
    now = datetime.now(timezone.utc).isoformat()
    db_file = Path(db_path)
    ph = ",".join("?" for _ in tids)
    with connect_sqlite(db_file) as conn:
        _ensure_thread_tracking_schema(conn)
        _ensure_claude_outputs_schema(conn)
        conn.execute(
            f"UPDATE thread_tracking SET snoozed = 0, updated_at = ? "
            f"WHERE snoozed = 1 AND inbox_thread_id IN ({ph})",
            [now, *tids],
        )
        conn.execute(
            f"UPDATE claude_message_outputs SET snoozed = 0 "
            f"WHERE snoozed = 1 AND COALESCE(thread_id, '') IN ({ph})",
            tids,
        )
        conn.commit()


def _ensure_claude_outputs_schema(conn: sqlite3.Connection) -> None:
    """Single-row-per-message table for Claude pipeline outputs."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS claude_message_outputs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_stamp TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            thread_id TEXT,
            source_id TEXT,
            datetime TEXT,
            sender TEXT,
            recipients TEXT,
            subject TEXT,
            raw_text TEXT,
            forwarded_from TEXT,
            cleaned_content TEXT,
            quoted_reply TEXT,
            signature TEXT,
            api_error TEXT,
            snoozed INTEGER NOT NULL DEFAULT 0,
            thread_summary_json TEXT NOT NULL,
            aggregate_summary_json TEXT NOT NULL
        )
        """
    )
    cols = {
        row[1] for row in conn.execute("PRAGMA table_info(claude_message_outputs)").fetchall()
    }
    if "snoozed" not in cols:
        conn.execute(
            "ALTER TABLE claude_message_outputs ADD COLUMN snoozed INTEGER NOT NULL DEFAULT 0"
        )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_claude_message_outputs_run_source "
        "ON claude_message_outputs(run_stamp, source_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_claude_message_outputs_run_stamp "
        "ON claude_message_outputs(run_stamp)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_claude_message_outputs_thread_id "
        "ON claude_message_outputs(thread_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_claude_message_outputs_thread_source "
        "ON claude_message_outputs(thread_id, source_id)"
    )
    try:
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_claude_message_outputs_thread_source_success
            ON claude_message_outputs(COALESCE(thread_id, ''), source_id)
            WHERE source_id IS NOT NULL AND source_id != ''
              AND COALESCE(TRIM(api_error), '') = ''
            """
        )
    except sqlite3.OperationalError:
        # Existing duplicate successful rows prevent index creation until deduped.
        pass


_PLACEHOLDER_CLEANED = frozenset({"(no subject)", "image.png"})


def _cleaned_content_is_known_placeholder(content: str) -> bool:
    return (content or "").strip().lower() in _PLACEHOLDER_CLEANED


def _prior_success_row_is_replaceable(
    prior_cleaned: str, *, raw_text: str = ""
) -> bool:
    """True when an existing successful row may be deleted and replaced (vision upgrade)."""
    prior = (prior_cleaned or "").strip()
    if not prior:
        return True
    if _cleaned_content_is_known_placeholder(prior):
        return True
    if raw_text:
        from services.email.segmentation import segmentation_content_from_quoted_tail_only

        if segmentation_content_from_quoted_tail_only(raw_text, prior):
            return True
    # Short filename-like tokens from image-only segmentation, not real body text.
    return len(prior) < 120 and "\n" not in prior and prior.count(" ") < 4


def _claude_outputs_latest_success_content_by_pair(
    conn: sqlite3.Connection,
) -> Dict[Tuple[str, str], str]:
    """Latest ``cleaned_content`` per ``(thread_id, source_id)`` for successful rows."""
    try:
        rows = conn.execute(
            """
            SELECT COALESCE(thread_id, ''), source_id, cleaned_content
            FROM claude_message_outputs
            WHERE source_id IS NOT NULL AND source_id != ''
              AND COALESCE(TRIM(api_error), '') = ''
            ORDER BY generated_at DESC, id DESC
            """
        ).fetchall()
    except sqlite3.Error:
        return {}
    out: Dict[Tuple[str, str], str] = {}
    for r in rows:
        if not r:
            continue
        tid = _normalize_field(r[0])
        sid = _normalize_field(r[1])
        if not sid:
            continue
        pair = (tid, sid)
        if pair not in out:
            out[pair] = _normalize_field(r[2])
    return out


def _claude_outputs_successful_thread_source_pairs(conn: sqlite3.Connection) -> Set[Tuple[str, str]]:
    """``(thread_id, source_id)`` pairs that already have a successful pipeline row."""
    return set(_claude_outputs_latest_success_content_by_pair(conn).keys())


def load_all_processed_cleaned_by_thread(db_path: str) -> Dict[str, List[Dict[str, Any]]]:
    """
    Successful cleaned messages per thread (deduped by ``source_id``), single query.
    """
    try:
        with connect_sqlite(db_path) as conn:
            conn.row_factory = sqlite3.Row
            _ensure_claude_outputs_schema(conn)
            rows = conn.execute(
                """
                WITH ranked AS (
                    SELECT thread_id, source_id, datetime, sender, recipients, subject, raw_text,
                           forwarded_from, cleaned_content, quoted_reply, signature, api_error,
                           ROW_NUMBER() OVER (
                               PARTITION BY COALESCE(thread_id, ''), COALESCE(source_id, '')
                               ORDER BY generated_at DESC, id DESC
                           ) AS rn
                    FROM claude_message_outputs
                    WHERE COALESCE(TRIM(api_error), '') = ''
                )
                SELECT thread_id, source_id, datetime, sender, recipients, subject, raw_text,
                       forwarded_from, cleaned_content, quoted_reply, signature, api_error
                FROM ranked
                WHERE rn = 1
                ORDER BY thread_id, datetime ASC
                """
            ).fetchall()
    except sqlite3.Error:
        return {}
    by_thread: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        tid = _normalize_field(row["thread_id"])
        sid = str(row["source_id"] or "").strip()
        if not tid or not sid:
            continue
        by_thread.setdefault(tid, []).append(
            {
                "thread_id": tid,
                "source_id": sid,
                "datetime": row["datetime"] or "",
                "sender": row["sender"] or "",
                "recipients": row["recipients"] or "",
                "subject": row["subject"] or "",
                "raw_text": row["raw_text"] or "",
                "forwarded_from": row["forwarded_from"] or "",
                "cleaned_content": row["cleaned_content"] or "",
                "quoted_reply": row["quoted_reply"] or "",
                "signature": row["signature"] or "",
                "api_error": "",
            }
        )
    return by_thread


def load_processed_cleaned_for_thread(
    db_path: str, thread_id: str
) -> List[Dict[str, Any]]:
    """
    Successful cleaned messages for a thread (deduped by ``source_id``).

    Used for re-summarization without re-segmenting historical messages.
    """
    tid = _normalize_field(thread_id)
    if not tid:
        return []
    try:
        with connect_sqlite(db_path) as conn:
            conn.row_factory = sqlite3.Row
            _ensure_claude_outputs_schema(conn)
            rows = conn.execute(
                """
                WITH ranked AS (
                    SELECT source_id, datetime, sender, recipients, subject, raw_text,
                           forwarded_from, cleaned_content, quoted_reply, signature, api_error,
                           ROW_NUMBER() OVER (
                               PARTITION BY COALESCE(source_id, '')
                               ORDER BY generated_at DESC, id DESC
                           ) AS rn
                    FROM claude_message_outputs
                    WHERE thread_id = ?
                      AND COALESCE(TRIM(api_error), '') = ''
                )
                SELECT source_id, datetime, sender, recipients, subject, raw_text, forwarded_from,
                       cleaned_content, quoted_reply, signature, api_error
                FROM ranked
                WHERE rn = 1
                ORDER BY datetime ASC
                """,
                (tid,),
            ).fetchall()
    except sqlite3.Error:
        return []
    out: List[Dict[str, Any]] = []
    for row in rows:
        sid = str(row["source_id"] or "").strip()
        if not sid:
            continue
        out.append(
            {
                "thread_id": tid,
                "source_id": sid,
                "datetime": row["datetime"] or "",
                "sender": row["sender"] or "",
                "recipients": row["recipients"] or "",
                "subject": row["subject"] or "",
                "raw_text": row["raw_text"] or "",
                "forwarded_from": row["forwarded_from"] or "",
                "cleaned_content": row["cleaned_content"] or "",
                "quoted_reply": row["quoted_reply"] or "",
                "signature": row["signature"] or "",
                "api_error": "",
            }
        )
    return out


def aggregate_thread_chronological_anchor(db_path: str, summary: Dict[str, Any]) -> str:
    """Earliest email datetime in a thread, used to order threads for lane summaries."""
    tid = _normalize_field(summary.get("thread_id"))
    if tid:
        cleaned = load_processed_cleaned_for_thread(db_path, tid)
        if cleaned:
            return _normalize_field(cleaned[0].get("datetime"))
    return _normalize_field(summary.get("datetime"))


def _parse_iso_datetime(dt: str) -> datetime:
    if not dt:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(dt).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def _merge_bundle_threads(bundle: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """Group bundle cleaned/summary rows by thread_id (same as frontend ``mergeRows``)."""
    by_id: Dict[str, Dict[str, Any]] = {}
    for c in bundle.get("cleaned") or []:
        sid = _normalize_field(c.get("source_id"))
        if sid:
            by_id[sid] = {"cleaned": c, "summary": None}
    for s in bundle.get("summary") or []:
        sid = _normalize_field(s.get("source_id"))
        if not sid:
            continue
        row = by_id.get(sid) or {"cleaned": None, "summary": None}
        row["summary"] = s
        by_id[sid] = row

    by_thread: Dict[str, List[Dict[str, Any]]] = {}
    for sid, row in by_id.items():
        tid = _normalize_field((row.get("cleaned") or {}).get("thread_id")) or _normalize_field(
            (row.get("summary") or {}).get("thread_id")
        )
        key = tid or f"_orphan_{sid}"
        by_thread.setdefault(key, []).append(row)
    return by_thread


def list_active_thread_ids_for_resummary(db_path: str) -> List[str]:
    """
    Thread ids shown as Active in the dashboard.

    Matches frontend ``mergeRows`` + ``partitionThreadsBySnooze`` (newest message
    ``snoozed`` = 0), not every ``thread_tracking`` row with ``snoozed`` = 0.
    """
    try:
        bundle = build_summaries_bundle(db_path)
    except Exception:
        return []

    by_thread = _merge_bundle_threads(bundle)
    active: List[tuple[datetime, str]] = []
    for tid, rows in by_thread.items():
        if tid.startswith("_orphan_"):
            continue
        rows.sort(
            key=lambda r: _normalize_field(
                (r.get("summary") or r.get("cleaned") or {}).get("datetime")
            ),
            reverse=True,
        )
        summary = rows[0].get("summary") or {}
        if int(summary.get("snoozed") or 0) != 0:
            continue
        newest = _parse_iso_datetime(
            _normalize_field(
                (rows[0].get("summary") or rows[0].get("cleaned") or {}).get("datetime")
            )
        )
        active.append((newest, tid))
    active.sort(key=lambda item: item[0], reverse=True)
    return [tid for _, tid in active]


def _ensure_thread_summaries_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS thread_summaries (
            thread_id TEXT NOT NULL PRIMARY KEY,
            summary_mode TEXT NOT NULL,
            input_fingerprint TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            backend TEXT NOT NULL,
            thread_summary_json TEXT NOT NULL,
            generated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_thread_summaries_generated_at "
        "ON thread_summaries(generated_at)"
    )


def save_thread_summary_cache(
    db_path: str,
    *,
    thread_id: str,
    thread_summary: Dict[str, Any],
    input_fingerprint: str,
    summary_mode: str,
    backend: str,
    generated_at: str,
) -> None:
    """Persist cached thread summary metadata for skip/incremental decisions."""
    from services.prompts import prompt_version

    tid = _normalize_field(thread_id)
    if not tid:
        return
    summary_json = json.dumps(
        thread_summary if isinstance(thread_summary, dict) else {},
        ensure_ascii=False,
    )
    with connect_sqlite(db_path) as conn:
        _ensure_thread_summaries_schema(conn)
        conn.execute(
            """
            INSERT INTO thread_summaries (
                thread_id, summary_mode, input_fingerprint, prompt_version,
                backend, thread_summary_json, generated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(thread_id) DO UPDATE SET
                summary_mode = excluded.summary_mode,
                input_fingerprint = excluded.input_fingerprint,
                prompt_version = excluded.prompt_version,
                backend = excluded.backend,
                thread_summary_json = excluded.thread_summary_json,
                generated_at = excluded.generated_at
            """,
            (
                tid,
                _normalize_field(summary_mode) or "full",
                _normalize_field(input_fingerprint),
                prompt_version(),
                _normalize_field(backend),
                summary_json,
                generated_at,
            ),
        )
        conn.commit()


def load_all_thread_summaries_map(db_path: str) -> Dict[str, Dict[str, Any]]:
    """All cached thread summaries keyed by ``thread_id``."""
    out: Dict[str, Dict[str, Any]] = {}
    try:
        with connect_sqlite(db_path) as conn:
            _ensure_thread_summaries_schema(conn)
            rows = conn.execute(
                "SELECT thread_id, thread_summary_json FROM thread_summaries"
            ).fetchall()
    except sqlite3.Error:
        return out
    for tid, json_str in rows:
        key = _normalize_field(tid)
        if key:
            out[key] = _parse_thread_summary_json(json_str)
    return out


def load_cached_thread_summary(db_path: str, thread_id: str) -> Optional[Dict[str, Any]]:
    """Return cached summary row for a thread, or ``None``."""
    tid = _normalize_field(thread_id)
    if not tid:
        return None
    try:
        with connect_sqlite(db_path) as conn:
            _ensure_thread_summaries_schema(conn)
            row = conn.execute(
                """
                SELECT summary_mode, input_fingerprint, prompt_version, backend,
                       thread_summary_json, generated_at
                FROM thread_summaries
                WHERE thread_id = ?
                """,
                (tid,),
            ).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    try:
        summary = json.loads(row[4] or "{}")
    except json.JSONDecodeError:
        summary = {}
    return {
        "summary_mode": row[0],
        "input_fingerprint": row[1],
        "prompt_version": row[2],
        "backend": row[3],
        "thread_summary": summary if isinstance(summary, dict) else {},
        "generated_at": row[5],
    }


def apply_thread_resummary_to_db(
    db_path: str,
    *,
    thread_id: str,
    thread_summary: Dict[str, Any],
    generated_at: str,
) -> int:
    """Update summary JSON on all successful rows for one thread."""
    tid = _normalize_field(thread_id)
    if not tid:
        return 0
    summary_json = json.dumps(
        thread_summary if isinstance(thread_summary, dict) else {},
        ensure_ascii=False,
    )
    with connect_sqlite(db_path) as conn:
        _ensure_claude_outputs_schema(conn)
        cur = conn.execute(
            """
            UPDATE claude_message_outputs
            SET thread_summary_json = ?, generated_at = ?
            WHERE COALESCE(thread_id, '') = ?
              AND COALESCE(TRIM(api_error), '') = ''
            """,
            (summary_json, generated_at, tid),
        )
        conn.commit()
        return int(cur.rowcount or 0)


def load_latest_claude_output_snapshot_rows(db_path: str) -> List[Dict[str, Any]]:
    """One row per (thread_id, source_id): latest ``generated_at`` (dashboard shape)."""
    try:
        with connect_sqlite(db_path) as conn:
            conn.row_factory = sqlite3.Row
            _ensure_claude_outputs_schema(conn)
            rows = conn.execute(
                """
                WITH ranked AS (
                    SELECT *,
                           ROW_NUMBER() OVER (
                               PARTITION BY COALESCE(thread_id, ''), COALESCE(source_id, '')
                               ORDER BY generated_at DESC, id DESC
                           ) AS rn
                    FROM claude_message_outputs
                )
                SELECT run_stamp, generated_at, thread_id, source_id, datetime, sender,
                       recipients, subject, raw_text, forwarded_from, cleaned_content,
                       quoted_reply, signature, api_error, thread_summary_json, snoozed
                FROM ranked
                WHERE rn = 1
                ORDER BY datetime DESC
                """
            ).fetchall()
    except sqlite3.Error:
        return []
    return [dict(r) for r in rows]


def _parse_thread_summary_json(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(str(raw))
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def load_all_thread_draft_replies(db_path: str) -> Dict[str, Dict[str, Any]]:
    """All saved draft replies keyed by ``thread_id`` (dashboard cache shape)."""
    db_file = Path(db_path)
    out: Dict[str, Dict[str, Any]] = {}
    with connect_sqlite(db_file) as conn:
        _ensure_thread_draft_replies_schema(conn)
        rows = conn.execute(
            "SELECT thread_id, draft_json FROM thread_draft_replies"
        ).fetchall()
    for thread_id, draft_json in rows:
        tid = _normalize_field(thread_id)
        if not tid or not draft_json:
            continue
        try:
            loaded = json.loads(draft_json)
            if isinstance(loaded, dict):
                out[tid] = loaded
        except json.JSONDecodeError:
            out[tid] = {"markdown": str(draft_json)}
    return out


def pending_message_counts_by_thread(
    db_path: str,
    *,
    lookback_days: int = 14,
) -> Dict[str, int]:
    """
    Per ``thread_id``, count messages not yet in successful ``claude_message_outputs``.

    Email: ``timeline_entries`` rows within lookback missing a successful output row.
    Text: on-disk conversation messages missing a successful output row.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, lookback_days))
    cutoff_iso = cutoff.isoformat()
    successful: set[tuple[str, str]] = set()
    try:
        with connect_sqlite(db_path) as conn:
            _ensure_claude_outputs_schema(conn)
            for tid, sid in conn.execute(
                """
                SELECT COALESCE(thread_id, ''), source_id
                FROM claude_message_outputs
                WHERE COALESCE(TRIM(source_id), '') != ''
                  AND COALESCE(TRIM(api_error), '') = ''
                """
            ).fetchall():
                pair = (_normalize_field(tid), _normalize_field(sid))
                if pair[1]:
                    successful.add(pair)
    except sqlite3.Error:
        pass

    counts: Dict[str, int] = {}
    try:
        with connect_sqlite(db_path) as conn:
            for tid, sid in conn.execute(
                """
                SELECT COALESCE(thread_id, ''), source_id
                FROM timeline_entries
                WHERE type IN ('email', 'meeting_invite')
                  AND datetime >= ?
                  AND COALESCE(TRIM(source_id), '') != ''
                """,
                (cutoff_iso,),
            ).fetchall():
                thread_id = _normalize_field(tid)
                source_id = _normalize_field(sid)
                if not thread_id or not source_id:
                    continue
                if (thread_id, source_id) in successful:
                    continue
                counts[thread_id] = counts.get(thread_id, 0) + 1
    except sqlite3.Error:
        pass

    try:
        from services.texts.format import load_messages_for_key, message_source_id
        from services.texts.tracking import fetch_tracked_conversation_keys, text_inbox_thread_id

        for key in fetch_tracked_conversation_keys(db_path):
            thread_id = text_inbox_thread_id(key)
            messages = load_messages_for_key(key)
            pending = 0
            for msg in messages:
                sid = message_source_id(msg)
                if not sid:
                    continue
                if (thread_id, sid) not in successful:
                    pending += 1
            if pending:
                counts[thread_id] = pending
    except Exception:
        pass

    return {tid: n for tid, n in counts.items() if n > 0}


def build_summaries_bundle(db_path: str) -> Dict[str, Any]:
    """
    Dashboard summaries payload: latest message rows, snooze overrides, drafts, meeting preps.
    """
    from services.linkedin.tracking import (
        LINKEDIN_THREAD_PREFIX,
        fetch_tracked_conversation_keys as fetch_tracked_linkedin_keys,
        linkedin_inbox_thread_id as _linkedin_inbox_thread_id,
    )
    from services.slack.tracking import (
        SLACK_THREAD_PREFIX,
        fetch_tracked_conversation_keys as fetch_tracked_slack_keys,
        slack_inbox_thread_id as _slack_inbox_thread_id,
    )
    from services.texts.tracking import (
        TEXT_THREAD_PREFIX,
        fetch_tracked_conversation_keys,
        text_inbox_thread_id as _text_inbox_thread_id,
    )
    from services.thread_snooze import (
        refresh_linkedin_threads_auto_unsnooze,
        refresh_slack_threads_auto_unsnooze,
        refresh_text_threads_auto_unsnooze,
        snooze_map,
    )

    refresh_text_threads_auto_unsnooze(db_path)
    refresh_slack_threads_auto_unsnooze(db_path)
    refresh_linkedin_threads_auto_unsnooze(db_path)
    from utils.thread_summary_normalize import finalize_thread_summary

    tracked_text_thread_ids = {
        _text_inbox_thread_id(k) for k in fetch_tracked_conversation_keys(db_path)
    }
    tracked_slack_thread_ids = {
        _slack_inbox_thread_id(k) for k in fetch_tracked_slack_keys(db_path)
    }
    tracked_linkedin_thread_ids = {
        _linkedin_inbox_thread_id(k) for k in fetch_tracked_linkedin_keys(db_path)
    }
    thread_summary_cache = load_all_thread_summaries_map(db_path)
    cleaned_by_thread = load_all_processed_cleaned_by_thread(db_path)
    finalized_by_thread: Dict[str, Dict[str, Any]] = {}

    def finalized_for_thread(tid: str, fallback: Dict[str, Any]) -> Dict[str, Any]:
        if tid in finalized_by_thread:
            return finalized_by_thread[tid]
        base = dict(thread_summary_cache.get(tid) or fallback)
        cleaned_for_thread = cleaned_by_thread.get(tid, [])
        finalized = finalize_thread_summary(base, cleaned_for_thread)
        finalized_by_thread[tid] = finalized
        return finalized

    rows = load_latest_claude_output_snapshot_rows(db_path)
    cleaned: List[Dict[str, Any]] = []
    summary: List[Dict[str, Any]] = []
    latest_run_stamp = ""
    latest_generated_at = ""
    for raw in rows:
        tid = _normalize_field(raw.get("thread_id"))
        if tid.startswith(TEXT_THREAD_PREFIX) and tid not in tracked_text_thread_ids:
            continue
        if tid.startswith(SLACK_THREAD_PREFIX) and tid not in tracked_slack_thread_ids:
            continue
        if tid.startswith(LINKEDIN_THREAD_PREFIX) and tid not in tracked_linkedin_thread_ids:
            continue
        fallback_summary = _parse_thread_summary_json(raw.get("thread_summary_json"))
        thread_summary = finalized_for_thread(tid, fallback_summary) if tid else fallback_summary
        run = _normalize_field(raw.get("run_stamp"))
        generated = _normalize_field(raw.get("generated_at"))
        if not latest_generated_at or generated > latest_generated_at:
            latest_generated_at = generated
            latest_run_stamp = run
        cleaned_row = {
            "thread_id": _normalize_field(raw.get("thread_id")),
            "source_id": _normalize_field(raw.get("source_id")),
            "datetime": _normalize_field(raw.get("datetime")),
            "sender": _normalize_field(raw.get("sender")),
            "recipients": _normalize_field(raw.get("recipients")),
            "subject": _normalize_field(raw.get("subject")),
            "raw_text": _normalize_field(raw.get("raw_text")),
            "forwarded_from": _normalize_field(raw.get("forwarded_from")),
            "cleaned_content": _normalize_field(raw.get("cleaned_content")),
            "quoted_reply": _normalize_field(raw.get("quoted_reply")),
            "signature": _normalize_field(raw.get("signature")),
            "api_error": _normalize_field(raw.get("api_error")),
        }
        cleaned.append(cleaned_row)
        summary_row: Dict[str, Any] = {
            **thread_summary,
            "thread_id": cleaned_row["thread_id"],
            "source_id": cleaned_row["source_id"],
            "datetime": cleaned_row["datetime"],
            "sender": cleaned_row["sender"],
            "subject": cleaned_row["subject"],
            "cleaned_content": cleaned_row["cleaned_content"],
            "quoted_reply": cleaned_row["quoted_reply"],
            "signature": cleaned_row["signature"],
            "summary_api_error": _normalize_field(thread_summary.get("api_error")),
            "snoozed": int(raw.get("snoozed") or 0),
        }
        summary.append(summary_row)

    tt_rows = fetch_thread_tracking_rows(db_path)
    tt_snooze_map = {
        _normalize_field(tid): state
        for tid, state in snooze_map(db_path).items()
        if _normalize_field(tid)
    }
    tt_plan_map = {
        _normalize_field(r.get("inbox_thread_id")): int(r.get("has_plan") or 0)
        for r in tt_rows
        if _normalize_field(r.get("inbox_thread_id"))
    }
    for s in summary:
        tid = _normalize_field(s.get("thread_id"))
        if tid and tid in tt_snooze_map:
            s["snoozed"] = tt_snooze_map[tid]
        if tid and tid in tt_plan_map:
            s["has_plan"] = tt_plan_map[tid]

    bundle = {
        "cleaned": cleaned,
        "summary": summary,
        "run_stamp": latest_run_stamp,
        "generated_at": latest_generated_at,
        "thread_drafts": load_all_thread_draft_replies(db_path),
        "meeting_preps": load_all_meeting_preps(db_path),
        "lanes": load_all_lanes(db_path),
        "lane_threads": load_lane_thread_memberships(db_path),
        "lane_summaries": load_all_lane_summaries(db_path),
        "thread_plans": load_all_thread_plans(db_path),
    }
    from services.email.bundle import append_unsynced_email_threads_to_bundle
    from services.linkedin.bundle import append_unsynced_linkedin_threads_to_bundle
    from services.slack.bundle import append_unsynced_slack_threads_to_bundle
    from services.texts.bundle import append_unsynced_text_threads_to_bundle
    from services.email.config import inbox_lookback_days_from_env

    lookback_days = max(1, inbox_lookback_days_from_env())

    append_unsynced_text_threads_to_bundle(db_path, bundle)
    append_unsynced_slack_threads_to_bundle(db_path, bundle)
    append_unsynced_linkedin_threads_to_bundle(db_path, bundle)
    append_unsynced_email_threads_to_bundle(db_path, bundle, lookback_days=lookback_days)
    bundle["pending_message_counts"] = pending_message_counts_by_thread(
        db_path,
        lookback_days=lookback_days,
    )
    try:
        from services.email.config import SOURCE_ACCOUNT

        bundle["source_account"] = (SOURCE_ACCOUNT or "").strip().lower()
    except Exception:
        bundle["source_account"] = ""
    return bundle


def load_processed_thread_source_pairs(db_path: str) -> Set[Tuple[str, str]]:
    """
    (thread_id, source_id) pairs that already have a successful pipeline row.

    Pairs that only have rows with a non-empty ``api_error`` are omitted so a later run can
    re-segment and insert a new row.
    """
    try:
        with connect_sqlite(db_path) as conn:
            _ensure_claude_outputs_schema(conn)
            return _claude_outputs_successful_thread_source_pairs(conn)
    except sqlite3.Error:
        return set()


def load_prior_cleaned_content_by_pair(db_path: str) -> Dict[Tuple[str, str], str]:
    """Latest successful ``cleaned_content`` per ``(thread_id, source_id)``."""
    try:
        with connect_sqlite(db_path) as conn:
            _ensure_claude_outputs_schema(conn)
            return _claude_outputs_latest_success_content_by_pair(conn)
    except sqlite3.Error:
        return {}


def save_claude_run_outputs(
    db_path: str,
    *,
    run_stamp: str,
    generated_at: str,
    cleaned: List[Dict[str, Any]],
    per_message: List[Dict[str, Any]],
    replace_run_stamp: bool = True,
) -> None:
    """
    Persist newly segmented messages to SQLite (one successful row per message).

    Skips insert when a successful row already exists for ``(thread_id, source_id)``.
    Replaces at most one prior successful row when upgrading placeholders or image stubs.
    Rows with only ``api_error`` set do not block a later successful insert.

    When ``replace_run_stamp`` is False, existing rows for this ``run_stamp`` are kept
    (for incremental per-thread writes within one pipeline run).
    """
    db_file = Path(db_path)
    summary_by_key: Dict[tuple[str, str, str], Dict[str, Any]] = {}
    for row in per_message:
        key = (
            _normalize_field(row.get("thread_id")),
            _normalize_field(row.get("source_id")),
            _normalize_field(row.get("datetime")),
        )
        ts = row.get("thread_summary")
        summary_by_key[key] = ts if isinstance(ts, dict) else {}

    aggregate_json = "{}"
    with connect_sqlite(db_file) as conn:
        _ensure_claude_outputs_schema(conn)
        if replace_run_stamp:
            conn.execute("DELETE FROM claude_message_outputs WHERE run_stamp = ?", (run_stamp,))
        existing_content = _claude_outputs_latest_success_content_by_pair(conn)
        cleaned_to_insert: List[Dict[str, Any]] = []
        for row in cleaned:
            tid = _normalize_field(row.get("thread_id"))
            sid = _normalize_field(row.get("source_id"))
            if not sid:
                continue
            pair = (tid, sid)
            new_err = _normalize_field(row.get("api_error"))
            new_cleaned = _normalize_field(row.get("cleaned_content"))
            if not new_err and pair in existing_content:
                prior_cleaned = existing_content[pair]
                if not _prior_success_row_is_replaceable(
                    prior_cleaned,
                    raw_text=_normalize_field(row.get("raw_text")),
                ):
                    continue
                if new_cleaned == prior_cleaned:
                    continue
                conn.execute(
                    """
                    DELETE FROM claude_message_outputs
                    WHERE COALESCE(thread_id, '') = ? AND source_id = ?
                      AND COALESCE(TRIM(api_error), '') = ''
                    """,
                    (tid, sid),
                )
            elif new_err and pair in existing_content:
                continue
            if not new_err:
                existing_content[pair] = new_cleaned
            cleaned_to_insert.append(row)
        if cleaned_to_insert:
            conn.executemany(
                """
                INSERT OR IGNORE INTO claude_message_outputs (
                    run_stamp, generated_at, thread_id, source_id, datetime, sender,
                    recipients, subject, raw_text, forwarded_from, cleaned_content,
                    quoted_reply, signature, api_error, snoozed, thread_summary_json,
                    aggregate_summary_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_stamp,
                        generated_at,
                        _normalize_field(row.get("thread_id")),
                        _normalize_field(row.get("source_id")),
                        _normalize_field(row.get("datetime")),
                        _normalize_field(row.get("sender")),
                        _normalize_field(row.get("recipients")),
                        _normalize_field(row.get("subject")),
                        _normalize_field(row.get("raw_text")),
                        _normalize_field(row.get("forwarded_from")),
                        _normalize_field(row.get("cleaned_content")),
                        _normalize_field(row.get("quoted_reply")),
                        _normalize_field(row.get("signature")),
                        _normalize_field(row.get("api_error")),
                        int(
                            summary_by_key.get(
                                (
                                    _normalize_field(row.get("thread_id")),
                                    _normalize_field(row.get("source_id")),
                                    _normalize_field(row.get("datetime")),
                                ),
                                {},
                            ).get("snoozed", 0)
                        )
                        if isinstance(
                            summary_by_key.get(
                                (
                                    _normalize_field(row.get("thread_id")),
                                    _normalize_field(row.get("source_id")),
                                    _normalize_field(row.get("datetime")),
                                ),
                                {},
                            ),
                            dict,
                        )
                        else 0,
                        json.dumps(
                            summary_by_key.get(
                                (
                                    _normalize_field(row.get("thread_id")),
                                    _normalize_field(row.get("source_id")),
                                    _normalize_field(row.get("datetime")),
                                ),
                                {},
                            ),
                            ensure_ascii=False,
                        ),
                        aggregate_json,
                    )
                    for row in cleaned_to_insert
                ],
            )
        conn.commit()


def delete_claude_outputs_for_thread(db_path: str, thread_id: str) -> int:
    """Delete all ``claude_message_outputs`` rows for one dashboard ``thread_id``."""
    tid = _normalize_field(thread_id)
    if not tid:
        return 0
    db_file = Path(db_path)
    with connect_sqlite(db_file) as conn:
        _ensure_claude_outputs_schema(conn)
        cur = conn.execute(
            "DELETE FROM claude_message_outputs WHERE COALESCE(thread_id, '') = ?",
            (tid,),
        )
        conn.commit()
        return int(cur.rowcount or 0)


def set_claude_outputs_thread_snoozed(
    db_path: str, *, thread_id: str, snoozed: int
) -> bool:
    """Persist snooze flag for all claude_message_outputs rows by thread_id."""
    tid = _normalize_field(thread_id)
    if not tid:
        return False
    raw = int(snoozed)
    snooze_value = raw if raw in (0, 1, 2) else 0
    db_file = Path(db_path)
    with connect_sqlite(db_file) as conn:
        _ensure_claude_outputs_schema(conn)
        cur = conn.execute(
            "UPDATE claude_message_outputs SET snoozed = ? WHERE COALESCE(thread_id, '') = ?",
            (snooze_value, tid),
        )
        conn.commit()
        return cur.rowcount > 0


def format_draft_reply_markdown(raw: Dict[str, Any]) -> str:
    """Dashboard markdown for a generated reply (matches frontend ``formatDraftReplyMarkdown``)."""
    body = _normalize_field(raw.get("reply_body"))
    rationale = _normalize_field(raw.get("rationale"))
    raw_text = _normalize_field(raw.get("raw_text"))
    oq_raw = raw.get("open_questions")
    oq: List[str] = []
    if isinstance(oq_raw, list):
        oq = [str(x).strip() for x in oq_raw if str(x).strip()]
    lines: List[str] = ["## Draft reply", ""]
    if body:
        lines.extend([body, ""])
    elif raw_text:
        lines.extend(["```", raw_text, "```", ""])
    else:
        lines.extend(["_(No reply body returned.)_", ""])
    lines.extend(["---", ""])
    if rationale:
        lines.extend([f"**Note:** {rationale}", ""])
    if oq:
        lines.extend(["**Double-check before sending:**", ""])
        for q in oq:
            lines.append(f"- {q}")
        lines.append("")
    return "\n".join(lines).strip()


def build_thread_draft_payload(
    *,
    response_intent: str,
    result: Dict[str, Any],
    markdown: Optional[str] = None,
) -> Dict[str, Any]:
    """Storable draft dict from LLM reply JSON and the user's intent."""
    intent = _normalize_field(response_intent)
    if not intent:
        raise ValueError("response_intent is required")
    reply = result if isinstance(result, dict) else {}
    oq_raw = reply.get("open_questions")
    oq: List[str] = []
    if isinstance(oq_raw, list):
        oq = [str(x).strip() for x in oq_raw if str(x).strip()]
    md = (markdown or "").strip() or format_draft_reply_markdown(reply)
    if not md:
        raise ValueError("markdown is required")
    return {
        "response_intent": intent,
        "markdown": md,
        "reply_body": _normalize_field(reply.get("reply_body")),
        "rationale": _normalize_field(reply.get("rationale")),
        "open_questions": oq,
    }


def _ensure_thread_draft_replies_schema(conn: sqlite3.Connection) -> None:
    """One row per Gmail thread id (matches ``claude_message_outputs.thread_id``)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS thread_draft_replies (
            thread_id TEXT NOT NULL PRIMARY KEY,
            draft_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_thread_draft_replies_updated_at "
        "ON thread_draft_replies(updated_at)"
    )


def save_thread_draft_reply(
    db_path: str,
    *,
    thread_id: str,
    draft: Dict[str, Any],
) -> str:
    """
    Persist or replace the saved draft reply for a thread.

    ``draft`` is stored as JSON (e.g. response_intent, markdown, reply_body, …).
    Returns ``updated_at`` (UTC ISO).
    """
    tid = _normalize_field(thread_id)
    if not tid:
        raise ValueError("thread_id is required")
    updated_at = datetime.now(timezone.utc).isoformat()
    payload = dict(draft)
    payload["saved_at"] = payload.get("saved_at") or updated_at
    db_file = Path(db_path)
    with connect_sqlite(db_file) as conn:
        _ensure_thread_draft_replies_schema(conn)
        conn.execute(
            """
            INSERT INTO thread_draft_replies (thread_id, draft_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(thread_id) DO UPDATE SET
                draft_json = excluded.draft_json,
                updated_at = excluded.updated_at
            """,
            (tid, json.dumps(payload, ensure_ascii=False), updated_at),
        )
        conn.commit()
    return updated_at


def load_thread_draft_reply(
    db_path: str, *, thread_id: str
) -> Optional[Dict[str, Any]]:
    """Return parsed draft JSON for ``thread_id``, or ``None`` if missing."""
    tid = _normalize_field(thread_id)
    if not tid:
        return None
    db_file = Path(db_path)
    with connect_sqlite(db_file) as conn:
        _ensure_thread_draft_replies_schema(conn)
        row = conn.execute(
            "SELECT draft_json FROM thread_draft_replies WHERE thread_id = ?",
            (tid,),
        ).fetchone()
    if not row or not row[0]:
        return None
    try:
        loaded = json.loads(row[0])
        return loaded if isinstance(loaded, dict) else None
    except json.JSONDecodeError:
        return None