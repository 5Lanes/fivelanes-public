"""
Re-run segmentation and/or thread summaries when stored output looks like an API error.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from utils.api_error_detection import (
    segmentation_error_is_retryable,
    thread_summary_is_valid,
)
from utils.database import connect_sqlite

log = logging.getLogger(__name__)


def list_latest_failed_segmentation_pairs(db_path: str) -> List[Dict[str, Any]]:
    try:
        with connect_sqlite(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                WITH ranked AS (
                    SELECT thread_id, source_id, api_error,
                           ROW_NUMBER() OVER (
                               PARTITION BY COALESCE(thread_id, ''), COALESCE(source_id, '')
                               ORDER BY generated_at DESC, id DESC
                           ) AS rn
                    FROM message_outputs
                    WHERE COALESCE(TRIM(source_id), '') != ''
                )
                SELECT thread_id, source_id, api_error
                FROM ranked
                WHERE rn = 1 AND COALESCE(TRIM(api_error), '') != ''
                """
            ).fetchall()
    except sqlite3.Error:
        return []
    out: List[Dict[str, Any]] = []
    for row in rows:
        err = str(row["api_error"] or "").strip()
        if not segmentation_error_is_retryable(err):
            continue
        out.append(
            {
                "thread_id": str(row["thread_id"] or "").strip(),
                "source_id": str(row["source_id"] or "").strip(),
                "api_error": err,
            }
        )
    return out


def list_thread_ids_with_recovered_segmentation(db_path: str) -> List[str]:
    """
    Thread ids where a message has both a retryable failed segmentation row and a
    later successful row (segmentation was fixed but summary may still be stale).
    """
    try:
        with connect_sqlite(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT DISTINCT COALESCE(thread_id, '') AS thread_id, source_id, api_error
                FROM message_outputs
                WHERE COALESCE(TRIM(source_id), '') != ''
                  AND COALESCE(TRIM(thread_id), '') != ''
                  AND COALESCE(TRIM(api_error), '') != ''
                """
            ).fetchall()
    except sqlite3.Error:
        return []

    candidates: Set[str] = set()
    for row in rows:
        err = str(row["api_error"] or "").strip()
        if not segmentation_error_is_retryable(err):
            continue
        tid = str(row["thread_id"] or "").strip()
        sid = str(row["source_id"] or "").strip()
        if not tid or not sid:
            continue
        try:
            with connect_sqlite(db_path) as conn:
                ok = conn.execute(
                    """
                    SELECT 1
                    FROM message_outputs
                    WHERE COALESCE(thread_id, '') = ?
                      AND source_id = ?
                      AND COALESCE(TRIM(api_error), '') = ''
                      AND COALESCE(TRIM(cleaned_content), '') != ''
                    LIMIT 1
                    """,
                    (tid, sid),
                ).fetchone()
        except sqlite3.Error:
            continue
        if ok:
            candidates.add(tid)
    return sorted(candidates)


def list_thread_ids_with_bad_summary(db_path: str) -> List[str]:
    try:
        with connect_sqlite(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                WITH ranked AS (
                    SELECT thread_id, thread_summary_json,
                           ROW_NUMBER() OVER (
                               PARTITION BY COALESCE(thread_id, '')
                               ORDER BY generated_at DESC, id DESC
                           ) AS rn
                    FROM message_outputs
                    WHERE COALESCE(TRIM(thread_id), '') != ''
                      AND COALESCE(TRIM(api_error), '') = ''
                )
                SELECT thread_id, thread_summary_json
                FROM ranked
                WHERE rn = 1
                """
            ).fetchall()
    except sqlite3.Error:
        return []
    bad: List[str] = []
    for row in rows:
        tid = str(row["thread_id"] or "").strip()
        if not tid:
            continue
        try:
            import json

            summary = json.loads(row["thread_summary_json"] or "{}")
        except json.JSONDecodeError:
            summary = {}
        from utils.database import load_processed_cleaned_for_thread

        cleaned = load_processed_cleaned_for_thread(db_path, tid)
        if not thread_summary_is_valid(summary if isinstance(summary, dict) else {}, cleaned=cleaned):
            bad.append(tid)
    return bad


def retry_failed_pipeline_outputs(
    *,
    db_path: str | None = None,
    dry_run: bool = False,
    thread_id: str | None = None,
) -> Tuple[int, int]:
    """Re-segment failed messages and re-summarize affected threads."""
    from services.llm_service import get_llm_backend
    from services.pipeline.process import (
        merge_cleaned_for_thread,
        per_message_rows,
        persist_thread_summary,
        run_stamp_utc,
        segment_body_deduped,
        segment_timeline_row,
        load_timeline_entry_by_source_id,
    )
    from services.pipeline.summary import summarize_thread
    from utils.database import save_message_outputs
    from utils.runtime_paths import database_path

    db = db_path or database_path()
    llm = get_llm_backend()
    failed_pairs = list_latest_failed_segmentation_pairs(db)
    if thread_id:
        tid_filter = thread_id.strip()
        failed_pairs = [p for p in failed_pairs if p.get("thread_id") == tid_filter]

    bad_summary_threads = set(list_thread_ids_with_bad_summary(db))
    recovered_threads = set(list_thread_ids_with_recovered_segmentation(db))
    if thread_id:
        tid_filter = thread_id.strip()
        bad_summary_threads = {t for t in bad_summary_threads if t == tid_filter}
        recovered_threads = {t for t in recovered_threads if t == tid_filter}

    log.info(
        "Retry scan: %d segmentation failure(s), %d error-shaped summary thread(s), "
        "%d recovered-segmentation thread(s), backend=%s",
        len(failed_pairs),
        len(bad_summary_threads),
        len(recovered_threads),
        llm.name,
    )
    if dry_run:
        for p in failed_pairs[:20]:
            log.info(
                "  would re-segment %s / %s: %s",
                p["thread_id"],
                p["source_id"],
                p["api_error"][:80],
            )
        for tid in sorted(bad_summary_threads | recovered_threads)[:20]:
            log.info("  would refresh summary for thread %s", tid)
        return 0, 0

    seg_cache: Dict[str, Tuple[Dict[str, Any], str]] = {}
    cleaned_new: List[Dict[str, Any]] = []
    threads_segmentation_fixed: Set[str] = set()

    for pair in failed_pairs:
        source_id = pair["source_id"]
        thread_id_val = pair["thread_id"]
        row = load_timeline_entry_by_source_id(db, source_id)
        if not row:
            log.warning("Skip %s: no timeline_entries row", source_id)
            continue
        from services.email.inbox_delivery import timeline_row_process_body

        process_body = timeline_row_process_body(row)
        if not process_body:
            log.warning("Skip %s: empty process body", source_id)
            continue
        entry = segment_timeline_row(
            row,
            thread_id=thread_id_val or str(row.get("thread_id") or "").strip(),
            seg_cache=seg_cache,
            segment_fn=lambda body, cache: segment_body_deduped(body, cache, llm=llm),
        )
        cleaned_new.append(entry)
        if thread_id_val and not str(entry.get("api_error") or "").strip():
            threads_segmentation_fixed.add(thread_id_val)
        err = str(entry.get("api_error") or "").strip()
        if err:
            log.warning("Re-segment %s still failed: %s", source_id, err[:120])
        else:
            log.info("Re-segment %s OK", source_id)

    threads_needing_summary = (
        threads_segmentation_fixed | bad_summary_threads | recovered_threads
    )

    run_stamp = run_stamp_utc()
    generated_at = datetime.now(timezone.utc).isoformat()
    per_message: List[Dict[str, Any]] = []
    threads_resummarized = 0
    summary_by_thread: Dict[str, Dict[str, Any]] = {}

    for tid in sorted(threads_needing_summary):
        cleaned = merge_cleaned_for_thread(db, tid, cleaned_new)
        if not cleaned:
            log.warning("Skip summary for %s: no successful cleaned messages", tid)
            continue
        log.info("Refreshing thread summary for %s (%d message(s))", tid, len(cleaned))
        tsumm = summarize_thread(cleaned, mode="full", db_path=db, backend=llm)
        if not thread_summary_is_valid(tsumm, cleaned=cleaned):
            log.warning(
                "Thread %s summary still looks like an error after re-run: %s",
                tid,
                str(tsumm.get("api_error") or tsumm.get("raw_text") or "")[:120],
            )
        persist_thread_summary(
            db,
            tid,
            cleaned,
            tsumm,
            "full",
            backend=llm.name,
            generated_at=generated_at,
            apply_to_outputs=True,
        )
        if thread_summary_is_valid(tsumm, cleaned=cleaned):
            threads_resummarized += 1
        summary_by_thread[tid] = tsumm

    for entry in cleaned_new:
        if str(entry.get("api_error") or "").strip():
            continue
        tid = str(entry.get("thread_id") or "").strip()
        tsumm = summary_by_thread.get(tid, {})
        per_message.extend(per_message_rows([entry], tsumm))

    if cleaned_new:
        save_message_outputs(
            db,
            run_stamp=run_stamp,
            generated_at=generated_at,
            cleaned=cleaned_new,
            per_message=per_message or [],
        )

    seg_ok = sum(1 for c in cleaned_new if not str(c.get("api_error") or "").strip())
    log.info(
        "Retry finished: %d/%d segmentation OK, %d thread(s) re-summarized",
        seg_ok,
        len(cleaned_new),
        threads_resummarized,
    )
    return len(cleaned_new), threads_resummarized
