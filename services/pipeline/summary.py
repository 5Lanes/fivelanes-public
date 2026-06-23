"""Shared thread summary logic for email and utility pipelines."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, Sequence

from services.llm_service import LlmBackend, get_llm_backend
from services.pipeline.fingerprint import cleaned_fingerprint, summary_input_fingerprint
from services.prompts import (
    format_incremental_thread_summary_prompt,
    format_thread_summary_prompt,
)
from services.scheduling_availability_step import calendar_context_for_summary_prompt
from utils.api_error_detection import thread_summary_is_valid
from utils.thread_summary_normalize import finalize_thread_summary


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


def _calendar_context(db_path: str | None) -> tuple[str, str]:
    return calendar_context_for_summary_prompt(db_path=db_path)


def compute_summary_fingerprint(
    cleaned: List[Dict[str, Any]],
    *,
    db_path: str | None = None,
    backend: str = "",
) -> str:
    cal_block, cal_tz = _calendar_context(db_path)
    return summary_input_fingerprint(
        cleaned,
        calendar_events_block=cal_block,
        calendar_timezone=cal_tz,
        backend=backend,
    )


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

    cal_block, cal_tz = _calendar_context(db_path)
    expected_fp = summary_input_fingerprint(
        merged_cleaned,
        calendar_events_block=cal_block,
        calendar_timezone=cal_tz,
        backend=backend,
    )
    cached = load_cached_thread_summary(db_path, thread_id)
    if not cached:
        return True
    if str(cached.get("input_fingerprint") or "") != expected_fp:
        return True
    summary = cached.get("thread_summary") if isinstance(cached.get("thread_summary"), dict) else {}
    return not thread_summary_is_valid(summary, cleaned=merged_cleaned)


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
    llm = backend or get_llm_backend()
    blocks = build_summary_blocks(cleaned)

    if mode == "incremental" and prior_summary and new_cleaned:
        new_blocks = build_summary_blocks(new_cleaned)
        prompt = format_incremental_thread_summary_prompt(
            prior_summary,
            new_blocks,
            db_path=db_path,
        )
        try:
            summary = llm.submit_incremental_summary(prompt)
            finalized = finalize_thread_summary(summary, cleaned)
            if thread_summary_is_valid(finalized, cleaned=cleaned):
                return finalized
        except Exception as exc:
            summary = {"api_error": str(exc)}
            finalized = finalize_thread_summary(summary, cleaned)
            if thread_summary_is_valid(finalized, cleaned=cleaned):
                return finalized

    prompt = format_thread_summary_prompt(blocks, db_path=db_path)
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
