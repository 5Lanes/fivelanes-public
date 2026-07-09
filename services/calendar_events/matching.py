"""
Match calendar events to existing tracked threads by overlapping participant emails,
falling back to topic/date proximity when attendees alone leave more than one candidate.

Ports ``frontend/src/thread_meeting_match.ts`` to Python so the match can be computed and
persisted server-side (linking the calendar thread into the matched thread's conversation)
instead of only being recomputed client-side at render time.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set

from utils.owner_config import is_likely_own_email

log = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", re.IGNORECASE)
_WORD_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)
_STOPWORDS = {"the", "a", "an", "and", "or", "with", "for", "to", "of", "re", "fwd"}


def normalize_email(email: str) -> str:
    e = (email or "").strip().lower()
    at = e.find("@")
    if at < 1:
        return e
    local = e[:at].split("+")[0]
    return f"{local}@{e[at + 1:]}"


def extract_emails_from_text(raw: str) -> List[str]:
    text = (raw or "").strip()
    if not text:
        return []
    out: List[str] = []
    seen: Set[str] = set()
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, dict):
        for key in ("to", "cc", "bcc"):
            for e in extract_emails_from_text(str(parsed.get(key) or "")):
                if e not in seen:
                    seen.add(e)
                    out.append(e)
        return out
    for m in _EMAIL_RE.findall(text):
        e = normalize_email(m)
        if "@" in e and e not in seen:
            seen.add(e)
            out.append(e)
    return out


def external_emails(emails: Iterable[str]) -> Set[str]:
    out: Set[str] = set()
    for raw in emails:
        e = normalize_email(raw)
        if "@" in e and not is_likely_own_email(e):
            out.add(e)
    return out


@dataclass
class ThreadMatchContext:
    thread_id: str
    label: str
    snoozed: int
    latest_iso: str
    contact_emails: List[str] = field(default_factory=list)


def _topic_tokens(text: str) -> Set[str]:
    return {
        t.lower()
        for t in _WORD_RE.findall(text or "")
        if len(t) > 2 and t.lower() not in _STOPWORDS
    }


def _topic_overlap(a: str, b: str) -> int:
    return len(_topic_tokens(a) & _topic_tokens(b))


def build_thread_match_contexts(db_path: str) -> List[ThreadMatchContext]:
    """Contact-email contexts for every non-calendar tracked thread."""
    from services.calendar_events.tracking import CALENDAR_THREAD_PREFIX
    from utils.database import connect_sqlite, fetch_thread_tracking_rows, load_cached_thread_summary

    out: List[ThreadMatchContext] = []
    with connect_sqlite(db_path) as conn:
        for row in fetch_thread_tracking_rows(db_path):
            thread_id = str(row.get("inbox_thread_id") or "").strip()
            if not thread_id or thread_id.startswith(CALENDAR_THREAD_PREFIX):
                continue
            emails: Set[str] = set()
            latest_iso = ""
            cur = conn.execute(
                "SELECT sender, recipients, datetime, summary FROM timeline_entries WHERE thread_id = ?",
                (thread_id,),
            )
            label = ""
            for sender, recipients, dt, summary in cur.fetchall():
                for e in extract_emails_from_text(str(sender or "")):
                    emails.add(e)
                for e in extract_emails_from_text(str(recipients or "")):
                    emails.add(e)
                dt = str(dt or "")
                if dt and dt > latest_iso:
                    latest_iso = dt
                if summary and not label:
                    label = str(summary)
            cached = load_cached_thread_summary(db_path, thread_id)
            if cached:
                tsumm = cached.get("thread_summary") or {}
                parties = tsumm.get("parties") if isinstance(tsumm, dict) else None
                if isinstance(parties, dict):
                    for key in ("active_speakers", "audience"):
                        for item in parties.get(key) or []:
                            for e in extract_emails_from_text(str(item)):
                                emails.add(e)
                gen_at = str(cached.get("generated_at") or "")
                if gen_at and gen_at > latest_iso:
                    latest_iso = gen_at
            out.append(
                ThreadMatchContext(
                    thread_id=thread_id,
                    label=label,
                    snoozed=int(row.get("snoozed") or 0),
                    latest_iso=latest_iso,
                    contact_emails=sorted(emails),
                )
            )
    return out


def find_matching_conversation_thread(
    attendees: List[str],
    contexts: List[ThreadMatchContext],
    *,
    meeting_summary: str = "",
    meeting_start_iso: str = "",
) -> Optional[ThreadMatchContext]:
    """
    Attendee-overlap match (mirrors ``findMatchingThread``). When more than one thread ties
    on overlap, break the tie by topic-word overlap between the event title and the thread's
    subject, then by proximity of the event start time to the thread's latest activity.
    """
    meeting_external = external_emails(attendees)
    if not meeting_external or not contexts:
        return None

    scored: List[tuple[int, ThreadMatchContext]] = []
    for ctx in contexts:
        if ctx.snoozed == 2:
            continue
        thread_external = external_emails(ctx.contact_emails)
        overlap = len(meeting_external & thread_external)
        if overlap == 0:
            continue
        scored.append((overlap, ctx))
    if not scored:
        return None

    best_overlap = max(overlap for overlap, _ in scored)
    candidates = [ctx for overlap, ctx in scored if overlap == best_overlap]
    if len(candidates) == 1:
        return candidates[0]

    if meeting_summary:
        topic_scores = [(_topic_overlap(meeting_summary, c.label), c) for c in candidates]
        best_topic = max(score for score, _ in topic_scores)
        if best_topic > 0:
            candidates = [c for score, c in topic_scores if score == best_topic]
            if len(candidates) == 1:
                return candidates[0]

    if meeting_start_iso:
        # Prefer the candidate whose latest activity is closest to the meeting start time.
        candidates = sorted(
            candidates, key=lambda ctx: _iso_distance_seconds(ctx.latest_iso, meeting_start_iso)
        )

    best: Optional[ThreadMatchContext] = None
    best_snooze = 2
    for ctx in candidates:
        better = (
            best is None
            or ctx.snoozed < best_snooze
            or (ctx.snoozed == best_snooze and ctx.latest_iso > (best.latest_iso if best else ""))
        )
        if better:
            best = ctx
            best_snooze = ctx.snoozed
    return best


def _iso_distance_seconds(a: str, b: str) -> float:
    from datetime import datetime

    if not a or not b:
        return float("inf")
    try:
        da = datetime.fromisoformat(a.replace("Z", "+00:00"))
        db_ = datetime.fromisoformat(b.replace("Z", "+00:00"))
    except ValueError:
        return float("inf")
    return abs((da - db_).total_seconds())


def link_calendar_threads(db_path: str) -> Dict[str, Any]:
    """
    For every tracked calendar-event thread, find its best-matching non-calendar thread by
    attendee overlap (topic/date tie-break) and link both into the same conversation.
    """
    from services.calendar_events.tracking import (
        calendar_inbox_thread_id,
        fetch_tracked_calendar_dedupe_keys,
    )
    from utils.database import fetch_meetings_rows, link_thread_to_matching_thread

    dedupe_keys = set(fetch_tracked_calendar_dedupe_keys(db_path))
    if not dedupe_keys:
        return {"ok": True, "linked": 0, "unmatched": 0}

    contexts = build_thread_match_contexts(db_path)
    linked = 0
    unmatched = 0
    for meeting in fetch_meetings_rows(db_path):
        key = str(meeting.get("dedupe_key") or "").strip()
        if key not in dedupe_keys:
            continue
        match = find_matching_conversation_thread(
            meeting.get("attendees") or [],
            contexts,
            meeting_summary=str(meeting.get("summary") or ""),
            meeting_start_iso=str(meeting.get("start_iso") or ""),
        )
        if not match:
            unmatched += 1
            continue
        link_thread_to_matching_thread(
            db_path,
            inbox_thread_id=calendar_inbox_thread_id(key),
            matched_inbox_thread_id=match.thread_id,
        )
        linked += 1

    return {"ok": True, "linked": linked, "unmatched": unmatched}
