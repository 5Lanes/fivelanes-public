"""Shared thread summary logic for email and utility pipelines."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Literal, Optional, Sequence

log = logging.getLogger(__name__)

from services.llm_service import LlmBackend, get_llm_backend
from services.pipeline.fingerprint import cleaned_fingerprint, summary_input_fingerprint
from services.prompts import (
    format_incremental_thread_summary_prompt,
    format_scheduling_ask_prompt,
    format_thread_summary_prompt,
)
from services.scheduling_availability_step import check_proposed_windows_availability
from utils.api_error_detection import thread_summary_is_valid
from utils.thread_summary_normalize import finalize_thread_summary


_CALENDAR_META_LINE_PREFIXES = ("When:", "Location:", "Attendees:", "Link:")
_CALENDAR_ONLY_MARKER = "No agenda, notes, or other message content is available for this event."


def _looks_like_bare_calendar_metadata(content: str) -> bool:
    """True for content built by ``_cleaned_row_from_meeting`` (calendar-event tracking):
    a "Meeting: <title>" header followed only by When:/Location:/Attendees:/Link: lines,
    with no agenda, notes, or transcript. Meet-recording transcripts share the "Meeting: "
    header but always have real prose after it, so they don't match this."""
    text = (content or "").strip()
    if not text.startswith("Meeting: "):
        return False
    _, _, body = text.partition("\n\n")
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    if not lines:
        return True
    return all(ln.startswith(_CALENDAR_META_LINE_PREFIXES) for ln in lines)


def _deterministic_calendar_only_summary(cleaned: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    Build a thread summary directly from structured fields, skipping the LLM entirely, for
    a thread whose only message is bare calendar metadata (no agenda, notes, or written
    content of any kind).

    Verified across repeated regenerations: even with explicit prompt rules forbidding it,
    the local model unreliably invents unanswered requests, deadlines, RSVP next_steps, and
    even copies unrelated calendar-availability data into other fields for this kind of thin
    content — instruction-following alone isn't reliable enough here. Since there is nothing
    in this content beyond title/time/attendees, a deterministic summary is also strictly
    more accurate than anything an LLM could add.
    """
    if len(cleaned) != 1:
        return None
    row = cleaned[0]
    if not _looks_like_bare_calendar_metadata(str(row.get("cleaned_content") or "")):
        return None
    title = str(row.get("subject") or "").strip() or "(No title)"
    when = str(row.get("datetime") or "").strip()
    attendees = str(row.get("recipients") or row.get("sender") or "").strip()
    update = f'"{title}" is on the calendar for {when}.' if when else f'"{title}" is on the calendar.'
    if attendees:
        update += f" Attendees: {attendees}."
    update += f" {_CALENDAR_ONLY_MARKER}"
    return {
        "latest_updates": [update],
        "next_steps": [],
        "last_sender": "",
        "tone": "informational",
        "parties": {"active_speakers": [], "audience": []},
        "suggested_thread_label": title,
    }


def _thread_summary_is_calendar_only(summary: Dict[str, Any]) -> bool:
    """True when a thread summary was produced by ``_deterministic_calendar_only_summary``
    (single ``latest_updates`` entry carrying the calendar-only marker)."""
    updates = summary.get("latest_updates")
    if not isinstance(updates, list) or len(updates) != 1:
        return False
    return _CALENDAR_ONLY_MARKER in str(updates[0])


def deterministic_calendar_only_lane_summary(
    thread_summaries: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """
    Build a lane roll-up directly from structured fields, skipping the LLM entirely, when
    every thread in the lane is calendar-only (see ``_deterministic_calendar_only_summary``).

    Verified: even with explicit "never invent a meeting/reschedule/outcome" prompt rules,
    the local model fabricates a full narrative (invented dates, invented reschedules,
    invented conflicts) when asked to write a flowing briefing from lanes this thin — the
    lane-level prompt rule alone did not reliably stop it. A lane with zero real message
    content anywhere has nothing genuine to synthesize, so state the plain facts instead.
    """
    if not thread_summaries or not all(_thread_summary_is_calendar_only(s) for s in thread_summaries):
        return None
    ordered = sorted(thread_summaries, key=lambda s: str(s.get("datetime") or ""))
    highlights: List[str] = []
    for s in ordered:
        label = str(s.get("suggested_thread_label") or "(unknown)").strip()
        dt = str(s.get("datetime") or "").strip()
        highlights.append(f"{label} — on the calendar for {dt}" if dt else label)
    summary_text = (
        "This lane only has calendar entries on it, with no messages or notes attached yet: "
        + "; ".join(highlights)
        + "."
    )
    return {
        "summary": summary_text,
        "highlights": highlights,
        "current_priorities": [],
        "waiting_on_others": [],
        "tone_overview": "calendar only",
    }


def build_summary_blocks(cleaned: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Build message blocks for summary prompts (chronological, oldest first)."""
    ordered = sorted(
        list(cleaned),
        key=lambda m: str(m.get("datetime") or ""),
    )
    return [
        {
            "datetime": m.get("datetime", ""),
            "sender": (m.get("sender") or m.get("forwarded_from") or "").strip(),
            "recipients": m.get("recipients", ""),
            "subject": m.get("subject", ""),
            "content": m.get("cleaned_content", ""),
        }
        for m in ordered
    ]


def compute_summary_fingerprint(
    cleaned: List[Dict[str, Any]],
    *,
    db_path: str | None = None,
    backend: str = "",
) -> str:
    return summary_input_fingerprint(cleaned, backend=backend)


def thread_needs_summary(
    db_path: str,
    thread_id: str,
    merged_cleaned: List[Dict[str, Any]],
    *,
    force: bool = False,
    backend: str = "",
) -> bool:
    """Return True when a thread summary LLM call is needed."""
    if force:
        return bool(merged_cleaned)
    if not merged_cleaned:
        return False

    from utils.database import load_cached_thread_summary, load_processed_cleaned_for_thread

    db_cleaned = load_processed_cleaned_for_thread(db_path, thread_id)
    merged_fp = cleaned_fingerprint(merged_cleaned)
    db_fp = cleaned_fingerprint(db_cleaned)
    if merged_fp - db_fp:
        return True
    if not db_cleaned and merged_cleaned:
        return True

    expected_fp = summary_input_fingerprint(merged_cleaned, backend=backend)
    cached = load_cached_thread_summary(db_path, thread_id)
    if not cached:
        return True
    if str(cached.get("input_fingerprint") or "") != expected_fp:
        return True
    summary = cached.get("thread_summary") if isinstance(cached.get("thread_summary"), dict) else {}
    if not thread_summary_is_valid(summary, cleaned=merged_cleaned):
        return True
    from utils.summary_timeliness import summary_is_temporally_stale

    return summary_is_temporally_stale(summary)


def new_messages_since_db(
    merged_cleaned: List[Dict[str, Any]],
    db_cleaned: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Return cleaned rows present in merged but not in DB (by source_id + content)."""
    db_fp = cleaned_fingerprint(db_cleaned)
    out: List[Dict[str, Any]] = []
    for row in merged_cleaned:
        sid = str(row.get("source_id") or "").strip()
        content = str(row.get("cleaned_content") or "").strip()
        if not sid:
            continue
        if (sid, content) not in db_fp:
            out.append(row)
    out.sort(key=lambda x: str(x.get("datetime") or ""))
    return out


def _last_message_is_from_owner(sender: str) -> bool:
    import re

    from utils.owner_config import is_likely_own_email

    emails = re.findall(r"[\w.+-]+@[\w.-]+\.[\w-]+", sender or "")
    return bool(emails) and all(is_likely_own_email(e) for e in emails)


def _apply_scheduling_ask_step(
    finalized: Dict[str, Any],
    blocks: List[Dict[str, Any]],
    llm: LlmBackend,
) -> Dict[str, Any]:
    """
    Run the dedicated scheduling-ask-detection prompt on the last message and, only if it
    finds a real ask, merge in a next_steps availability check (computed deterministically,
    not by a model) and any counterparty-offered windows.

    Kept as a separate step from the main summary call on purpose: a small model asked to
    produce a whole narrative, next_steps, tone, and a scheduling check all at once has
    repeatedly been observed to invent a next_step and borrow a stray date from context it
    was only shown for an unrelated reason. A narrow, single-purpose call has much less
    room to do that, and the availability arithmetic itself is exact — better done in code.
    """
    if not blocks or _last_message_is_from_owner(blocks[-1].get("sender") or ""):
        return finalized
    try:
        prompt = format_scheduling_ask_prompt(blocks)
        result = llm.submit_scheduling_ask(prompt)
    except Exception:
        log.exception("Scheduling-ask detection failed; skipping")
        return finalized
    if not isinstance(result, dict) or not result.get("is_scheduling_ask"):
        return finalized

    proposed = result.get("proposed_windows")
    if isinstance(proposed, list) and proposed:
        avail_text = check_proposed_windows_availability(
            [w for w in proposed if isinstance(w, dict)]
        )
        if avail_text:
            next_steps = finalized.get("next_steps")
            next_steps = list(next_steps) if isinstance(next_steps, list) else []
            next_steps.append(
                {
                    "type": "response required",
                    "action": f"Calendar for proposed time: {avail_text}",
                    "by_when": "",
                }
            )
            finalized["next_steps"] = next_steps

    counter = result.get("counterparty_offered_windows")
    if isinstance(counter, list) and counter:
        finalized["counterparty_availability"] = [
            {
                "date": str(w.get("date") or ""),
                "start": str(w.get("start") or ""),
                "end": str(w.get("end") or ""),
                "label": str(w.get("label") or ""),
            }
            for w in counter
            if isinstance(w, dict)
        ]
    return finalized


def summarize_thread(
    cleaned: List[Dict[str, Any]],
    *,
    mode: Literal["incremental", "full"] = "incremental",
    prior_summary: Optional[Dict[str, Any]] = None,
    new_cleaned: Optional[List[Dict[str, Any]]] = None,
    db_path: str | None = None,
    backend: LlmBackend | None = None,
) -> Dict[str, Any]:
    """
    Run thread summary LLM call.

    ``incremental`` merges prior summary with new message blocks; falls back to full on failure.
    """
    deterministic = _deterministic_calendar_only_summary(cleaned)
    if deterministic is not None:
        return finalize_thread_summary(deterministic, cleaned)

    llm = backend or get_llm_backend()
    blocks = build_summary_blocks(cleaned)
    true_last_sender = blocks[-1]["sender"] if blocks else ""

    def _finalize(summary: Dict[str, Any]) -> Dict[str, Any]:
        finalized = finalize_thread_summary(summary, cleaned)
        # last_sender is a plain lookup on the already-sorted message blocks, not a
        # judgment call — computing it in code removes an entire class of misattribution
        # (the model sometimes names the second-to-last sender, especially on long
        # multi-quote threads) rather than trusting the model's own reading of "who sent
        # the last message" alongside everything else it's asked to produce in one call.
        if true_last_sender:
            finalized["last_sender"] = true_last_sender
        return finalized

    if mode == "incremental" and prior_summary and new_cleaned:
        new_blocks = build_summary_blocks(new_cleaned)
        prompt = format_incremental_thread_summary_prompt(prior_summary, new_blocks)
        try:
            summary = llm.submit_incremental_summary(prompt)
            finalized = _finalize(summary)
            if thread_summary_is_valid(finalized, cleaned=cleaned):
                return _apply_scheduling_ask_step(finalized, blocks, llm)
        except Exception as exc:
            summary = {"api_error": str(exc)}
            finalized = _finalize(summary)
            if thread_summary_is_valid(finalized, cleaned=cleaned):
                return _apply_scheduling_ask_step(finalized, blocks, llm)

    prompt = format_thread_summary_prompt(blocks)
    try:
        summary = llm.submit_summary(prompt)
    except Exception as exc:
        summary = {"api_error": str(exc)}
    return _apply_scheduling_ask_step(_finalize(summary), blocks, llm)


def summarize_chat_thread(
    cleaned: List[Dict[str, Any]],
    *,
    db_path: str | None = None,
    backend: LlmBackend | None = None,
) -> Dict[str, Any]:
    """Full summary for a chat thread (Slack/SMS) using turn-based prompts."""
    from services.prompts import format_chat_thread_summary_prompt

    llm = backend or get_llm_backend()
    blocks = build_summary_blocks(cleaned)
    prompt = format_chat_thread_summary_prompt(blocks)
    try:
        summary = llm.submit_summary(prompt)
    except Exception as exc:
        summary = {"api_error": str(exc)}
    return finalize_thread_summary(summary, cleaned)


def resolve_thread_summary(
    db_path: str,
    thread_id: str,
    merged_cleaned: List[Dict[str, Any]],
    *,
    newly_segmented: List[Dict[str, Any]],
    force_full: bool = False,
    backend: LlmBackend | None = None,
) -> tuple[Dict[str, Any], str]:
    """
    Decide skip/incremental/full and return (summary, mode).

    mode is one of: ``cached``, ``incremental``, ``full``, ``skipped``.
    """
    llm = backend or get_llm_backend()
    active_backend = llm.name

    from utils.database import load_cached_thread_summary, load_processed_cleaned_for_thread

    cal_block, cal_tz = _calendar_context(db_path)
    fp = summary_input_fingerprint(
        merged_cleaned,
        calendar_events_block=cal_block,
        calendar_timezone=cal_tz,
        backend=active_backend,
    )

    if not thread_needs_summary(db_path, thread_id, merged_cleaned, backend=active_backend):
        cached = load_cached_thread_summary(db_path, thread_id)
        if cached and isinstance(cached.get("thread_summary"), dict):
            return cached["thread_summary"], "cached"

    prior_summary: Dict[str, Any] = {}
    cached = load_cached_thread_summary(db_path, thread_id)
    if cached and isinstance(cached.get("thread_summary"), dict):
        prior_summary = cached["thread_summary"]

    db_cleaned = load_processed_cleaned_for_thread(db_path, thread_id)
    new_rows = new_messages_since_db(merged_cleaned, db_cleaned)
    if not new_rows and newly_segmented:
        new_rows = newly_segmented

    use_full = force_full or not thread_summary_is_valid(prior_summary, cleaned=merged_cleaned)
    if use_full:
        summary = summarize_thread(
            merged_cleaned,
            mode="full",
            db_path=db_path,
            backend=llm,
        )
        return summary, "full"

    summary = summarize_thread(
        merged_cleaned,
        mode="incremental",
        prior_summary=prior_summary,
        new_cleaned=new_rows or newly_segmented,
        db_path=db_path,
        backend=llm,
    )
    if not thread_summary_is_valid(summary, cleaned=merged_cleaned):
        summary = summarize_thread(
            merged_cleaned,
            mode="full",
            db_path=db_path,
            backend=llm,
        )
        return summary, "full"
    return summary, "incremental"
