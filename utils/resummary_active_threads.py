"""Re-run thread summaries for active inbox threads (no re-segmentation)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict

log = logging.getLogger(__name__)


def force_resummary_active_threads(
    *,
    db_path: str | None = None,
    dry_run: bool = False,
    thread_id: str | None = None,
) -> int:
    """Re-summarize threads shown as Active in the dashboard."""
    from services.llm_service import get_llm_backend
    from services.pipeline.process import force_resummarize_thread
    from utils.database import list_active_thread_ids_for_resummary
    from utils.runtime_paths import database_path

    db = db_path or database_path()
    llm = get_llm_backend()
    thread_ids = list_active_thread_ids_for_resummary(db)
    if thread_id:
        tid = thread_id.strip()
        thread_ids = [tid] if tid else []
    log.info(
        "Force resummary: %d active thread(s), backend=%s, db=%s",
        len(thread_ids),
        llm.name,
        db,
    )
    if dry_run:
        return 0

    updated = 0
    generated_at = datetime.now(timezone.utc).isoformat()

    for i, tid in enumerate(thread_ids, start=1):
        from utils.database import load_processed_cleaned_for_thread

        cleaned = load_processed_cleaned_for_thread(db, tid)
        if not cleaned:
            log.warning("[%d/%d] Skip %s: no cleaned messages", i, len(thread_ids), tid)
            continue
        log.info(
            "[%d/%d] Full resummary for thread %s (%d messages)",
            i,
            len(thread_ids),
            tid,
            len(cleaned),
        )
        if force_resummarize_thread(
            db,
            tid,
            llm=llm,
            generated_at=generated_at,
            apply_to_outputs=True,
        ):
            updated += 1

    if updated:
        log.info("Updated %d thread(s)", updated)
    else:
        log.info("Updated 0 thread(s)")
    return updated


def resummary_single_thread(
    *,
    db_path: str | None = None,
    thread_id: str,
) -> Dict[str, Any]:
    """Re-run a full thread summary for one thread (dashboard refresh)."""
    from services.texts.tracking import TEXT_THREAD_PREFIX, parse_text_inbox_thread_id
    from utils.api_error_detection import thread_summary_is_valid
    from utils.database import load_cached_thread_summary, load_processed_cleaned_for_thread
    from utils.runtime_paths import database_path

    tid = (thread_id or "").strip()
    if not tid:
        return {"ok": False, "error": "missing_thread_id"}

    db = db_path or database_path()

    if tid.startswith(TEXT_THREAD_PREFIX):
        key = parse_text_inbox_thread_id(tid)
        if not key:
            return {"ok": False, "error": "invalid_text_thread_id", "thread_id": tid}
        from services.texts.summarize import _latest_thread_summary, summarize_one_text_thread

        result = summarize_one_text_thread(db, key, force=True)
        if not result.get("ok"):
            return result
        tsumm = _latest_thread_summary(db, tid)
        cleaned = load_processed_cleaned_for_thread(db, tid)
        if not thread_summary_is_valid(tsumm, cleaned=cleaned):
            return {
                "ok": False,
                "error": "invalid_summary",
                "thread_id": tid,
                "api_error": str(tsumm.get("api_error") or ""),
            }
        cached = load_cached_thread_summary(db, tid)
        return {
            "ok": True,
            "thread_id": tid,
            "thread_summary": tsumm,
            "summary_updated_at": (cached or {}).get("generated_at"),
            "skipped": bool(result.get("skipped")),
        }

    from services.slack.tracking import SLACK_THREAD_PREFIX, parse_slack_inbox_thread_id

    if tid.startswith(SLACK_THREAD_PREFIX):
        key = parse_slack_inbox_thread_id(tid)
        if not key:
            return {"ok": False, "error": "invalid_slack_thread_id", "thread_id": tid}
        from services.slack.summarize import _latest_thread_summary, summarize_one_slack_thread

        result = summarize_one_slack_thread(db, key, force=True)
        if not result.get("ok"):
            return result
        tsumm = _latest_thread_summary(db, tid)
        cleaned = load_processed_cleaned_for_thread(db, tid)
        if not thread_summary_is_valid(tsumm, cleaned=cleaned):
            return {
                "ok": False,
                "error": "invalid_summary",
                "thread_id": tid,
                "api_error": str(tsumm.get("api_error") or ""),
            }
        cached = load_cached_thread_summary(db, tid)
        return {
            "ok": True,
            "thread_id": tid,
            "thread_summary": tsumm,
            "summary_updated_at": (cached or {}).get("generated_at"),
            "skipped": bool(result.get("skipped")),
        }

    from services.linkedin.tracking import LINKEDIN_THREAD_PREFIX, parse_linkedin_inbox_thread_id

    if tid.startswith(LINKEDIN_THREAD_PREFIX):
        key = parse_linkedin_inbox_thread_id(tid)
        if not key:
            return {"ok": False, "error": "invalid_linkedin_thread_id", "thread_id": tid}
        from services.linkedin.summarize import _latest_thread_summary, summarize_one_linkedin_thread

        result = summarize_one_linkedin_thread(db, key, force=True)
        if not result.get("ok"):
            return result
        tsumm = _latest_thread_summary(db, tid)
        cleaned = load_processed_cleaned_for_thread(db, tid)
        if not thread_summary_is_valid(tsumm, cleaned=cleaned):
            return {
                "ok": False,
                "error": "invalid_summary",
                "thread_id": tid,
                "api_error": str(tsumm.get("api_error") or ""),
            }
        cached = load_cached_thread_summary(db, tid)
        return {
            "ok": True,
            "thread_id": tid,
            "thread_summary": tsumm,
            "summary_updated_at": (cached or {}).get("generated_at"),
            "skipped": bool(result.get("skipped")),
        }

    cleaned = load_processed_cleaned_for_thread(db, tid)
    if not cleaned:
        return {"ok": False, "error": "no_cleaned_messages", "thread_id": tid}

    rows_updated = force_resummary_active_threads(db_path=db, thread_id=tid)
    cached = load_cached_thread_summary(db, tid)
    tsumm = (cached or {}).get("thread_summary") if cached else {}
    if not isinstance(tsumm, dict):
        tsumm = {}
    if not thread_summary_is_valid(tsumm, cleaned=cleaned):
        return {
            "ok": False,
            "error": "invalid_summary",
            "thread_id": tid,
            "api_error": str(tsumm.get("api_error") or ""),
        }
    if not rows_updated:
        return {"ok": False, "error": "no_rows_updated", "thread_id": tid}

    return {
        "ok": True,
        "thread_id": tid,
        "thread_summary": tsumm,
        "summary_updated_at": (cached or {}).get("generated_at"),
        "rows_updated": rows_updated,
    }
