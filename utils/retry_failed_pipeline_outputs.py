"""
Re-run segmentation and/or thread summaries when stored output looks like an API error.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from utils.api_error_detection import (
    segmentation_error_is_retryable,
    thread_summary_is_valid,
)

log = logging.getLogger(__name__)


def _run_stamp_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _segment_body_fn():
    backend = _pipeline_backend()
    if backend == "claude":
        from routes.claude_routes import _segment_body_deduped

        return _segment_body_deduped
    if backend == "llama":
        from routes.llama_routes import _segment_body_deduped

        return _segment_body_deduped
    raise ValueError(f"Invalid FIVELANES_BACKEND: {backend}")


def _summarize_cleaned(cleaned: List[Dict[str, Any]]) -> Dict[str, Any]:
    from utils.resummary_active_threads import _summarize_cleaned

    return _summarize_cleaned(cleaned)


def _merged_cleaned_for_thread(
    db_path: str,
    thread_id: str,
    cleaned_new: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Successful cleaned rows for a thread, including not-yet-persisted retry segmentation."""
    from utils.database import load_processed_cleaned_for_thread

    tid = (thread_id or "").strip()
    by_source: Dict[str, Dict[str, Any]] = {
        str(row.get("source_id") or "").strip(): row
        for row in load_processed_cleaned_for_thread(db_path, tid)
        if str(row.get("source_id") or "").strip()
    }
    for row in cleaned_new:
        if str(row.get("thread_id") or "").strip() != tid:
            continue
        if str(row.get("api_error") or "").strip():
            continue
        sid = str(row.get("source_id") or "").strip()
        if not sid:
            continue
        by_source[sid] = {
            "thread_id": tid,
            "source_id": sid,
            "datetime": str(row.get("datetime") or ""),
            "sender": str(row.get("sender") or ""),
            "recipients": str(row.get("recipients") or ""),
            "subject": str(row.get("subject") or ""),
            "raw_text": str(row.get("raw_text") or ""),
            "forwarded_from": str(row.get("forwarded_from") or ""),
            "cleaned_content": str(row.get("cleaned_content") or ""),
            "quoted_reply": str(row.get("quoted_reply") or ""),
            "signature": str(row.get("signature") or ""),
            "api_error": "",
        }
    out = list(by_source.values())
    out.sort(key=lambda x: str(x.get("datetime") or ""))
    return out


def load_timeline_entry_by_source_id(db_path: str, source_id: str) -> Optional[Dict[str, Any]]:
    sid = (source_id or "").strip()
    if not sid:
        return None
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT source_id, datetime, sender, recipients, summary, body,
                       COALESCE(thread_id, '') AS thread_id,
                       COALESCE(body_has_image, 0) AS body_has_image,
                       COALESCE(fetch_oauth_account_id, '') AS fetch_oauth_account_id
                FROM timeline_entries
                WHERE source_id = ?
                LIMIT 1
                """,
                (sid,),
            ).fetchone()
    except sqlite3.Error:
        return None
    return dict(row) if row else None


def list_latest_failed_segmentation_pairs(db_path: str) -> List[Dict[str, Any]]:
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                WITH ranked AS (
                    SELECT thread_id, source_id, api_error,
                           ROW_NUMBER() OVER (
                               PARTITION BY COALESCE(thread_id, ''), COALESCE(source_id, '')
                               ORDER BY generated_at DESC, id DESC
                           ) AS rn
                    FROM claude_message_outputs
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
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT DISTINCT COALESCE(thread_id, '') AS thread_id, source_id, api_error
                FROM claude_message_outputs
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
            with sqlite3.connect(db_path) as conn:
                ok = conn.execute(
                    """
                    SELECT 1
                    FROM claude_message_outputs
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
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                WITH ranked AS (
                    SELECT thread_id, thread_summary_json,
                           ROW_NUMBER() OVER (
                               PARTITION BY COALESCE(thread_id, '')
                               ORDER BY generated_at DESC, id DESC
                           ) AS rn
                    FROM claude_message_outputs
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
    db = db_path or os.getenv("DATABASE_NAME") or "timeline.db"
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
        _pipeline_backend(),
    )
    if dry_run:
        for p in failed_pairs[:20]:
            log.info("  would re-segment %s / %s: %s", p["thread_id"], p["source_id"], p["api_error"][:80])
        for tid in sorted(bad_summary_threads | recovered_threads)[:20]:
            log.info("  would refresh summary for thread %s", tid)
        return 0, 0

    from routes.llm_routes import _segment_body_deduped
    from services.email.forwarding import primary_email_from_sender
    from services.email.inbox_delivery import timeline_row_process_body
    from services.image_description import process_timeline_message_segmentation
    from utils.database import apply_thread_resummary_to_db, save_claude_run_outputs
    from utils.resummary_active_threads import write_fivelanes_bundle_from_db

    llm = get_llm_backend()
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
        process_body = timeline_row_process_body(row)
        if not process_body:
            log.warning("Skip %s: empty process body", source_id)
            continue
        seg, err = process_timeline_message_segmentation(
            row,
            process_body,
            seg_cache,
            lambda body, cache: _segment_body_deduped(body, cache, llm=llm),
        )
        entry = {
            "thread_id": thread_id_val or str(row.get("thread_id") or "").strip(),
            "source_id": source_id,
            "datetime": row.get("datetime", ""),
            "sender": row.get("sender", ""),
            "recipients": row.get("recipients", ""),
            "subject": row.get("summary", ""),
            "raw_text": process_body,
            "forwarded_from": primary_email_from_sender(str(row.get("sender") or "")),
            "cleaned_content": str(seg.get("content") or "").strip(),
            "quoted_reply": str(seg.get("quoted_reply") or "").strip(),
            "signature": str(seg.get("signature") or "").strip(),
            "api_error": err,
        }
        cleaned_new.append(entry)
        if thread_id_val and not err:
            threads_segmentation_fixed.add(thread_id_val)
        if err:
            log.warning("Re-segment %s still failed: %s", source_id, err[:120])
        else:
            log.info("Re-segment %s OK", source_id)

    threads_needing_summary = (
        threads_segmentation_fixed | bad_summary_threads | recovered_threads
    )

    run_stamp = _run_stamp_utc()
    generated_at = datetime.now(timezone.utc).isoformat()
    per_message: List[Dict[str, Any]] = []
    threads_resummarized = 0
    summary_by_thread: Dict[str, Dict[str, Any]] = {}

    for tid in sorted(threads_needing_summary):
        cleaned = _merged_cleaned_for_thread(db, tid, cleaned_new)
        if not cleaned:
            log.warning("Skip summary for %s: no successful cleaned messages", tid)
            continue
        log.info("Refreshing thread summary for %s (%d message(s))", tid, len(cleaned))
        tsumm = _summarize_cleaned(cleaned)
        if not thread_summary_is_valid(tsumm, cleaned=cleaned):
            log.warning(
                "Thread %s summary still looks like an error after re-run: %s",
                tid,
                str(tsumm.get("api_error") or tsumm.get("raw_text") or "")[:120],
            )
        fp = compute_summary_fingerprint(cleaned, db_path=db, backend=llm.name)
        save_thread_summary_cache(
            db,
            thread_id=tid,
            thread_summary=tsumm,
            input_fingerprint=fp,
            summary_mode="full",
            backend=llm.name,
            generated_at=generated_at,
        )
        n = apply_thread_resummary_to_db(
            db,
            thread_id=tid,
            thread_summary=tsumm,
            generated_at=generated_at,
        )
        if n:
            threads_resummarized += 1
        summary_by_thread[tid] = tsumm

    for entry in cleaned_new:
        if str(entry.get("api_error") or "").strip():
            continue
        tid = str(entry.get("thread_id") or "").strip()
        tsumm = summary_by_thread.get(tid, {})
        per_message.append(
            {
                "thread_id": tid,
                "source_id": str(entry.get("source_id") or "").strip(),
                "thread_summary": tsumm,
                "cleaned_content": str(entry.get("cleaned_content") or "").strip(),
                "quoted_reply": str(entry.get("quoted_reply") or "").strip(),
                "signature": str(entry.get("signature") or "").strip(),
                "api_error": "",
                "sender": str(entry.get("sender") or "").strip(),
                "datetime": str(entry.get("datetime") or "").strip(),
                "subject": str(entry.get("subject") or "").strip(),
            }
        )

    if cleaned_new:
        save_claude_run_outputs(
            db,
            run_stamp=run_stamp,
            generated_at=generated_at,
            cleaned=cleaned_new,
            per_message=per_message or [],
        )

    if threads_resummarized or cleaned_new:
        write_fivelanes_bundle_from_db(
            db,
            run_stamp=run_stamp,
            generated_at=generated_at,
        )

    seg_ok = sum(1 for c in cleaned_new if not str(c.get("api_error") or "").strip())
    log.info(
        "Retry finished: %d/%d segmentation OK, %d thread(s) re-summarized",
        seg_ok,
        len(cleaned_new),
        threads_resummarized,
    )
    return len(cleaned_new), threads_resummarized
