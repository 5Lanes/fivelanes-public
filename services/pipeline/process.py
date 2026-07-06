"""Thread LLM pipeline: segment timeline messages, summarize, and persist."""

from __future__ import annotations

import copy
import hashlib
import logging
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from services.email.forwarding import primary_email_from_sender
from services.email.inbox_delivery import (
    timeline_row_needs_image_description,
    timeline_row_process_body,
)
from services.email.segmentation import (
    guard_segmentation_content,
    segmentation_content_from_quoted_tail_only,
    segmentation_content_not_from_reply_head,
    strip_quoted_thread_tail,
)
from services.image_description import (
    process_timeline_message_segmentation,
    should_reprocess_image_only_row,
)
from services.llm_service import LlmBackend, get_llm_backend
from services.pipeline.summary import (
    compute_summary_fingerprint,
    resolve_thread_summary,
    summarize_thread,
)
from services.prompts import parse_emails
from utils.api_error_detection import thread_summary_is_valid
from utils.database import (
    _ensure_timeline_schema,
    apply_thread_resummary_to_db,
    connect_sqlite,
    load_prior_cleaned_content_by_pair,
    load_processed_cleaned_for_thread,
    load_processed_thread_source_pairs,
    save_claude_run_outputs,
    save_thread_summary_cache,
)
from utils.runtime_paths import database_path

log = logging.getLogger(__name__)

SegmentationCache = Dict[str, Tuple[Dict[str, Any], str]]
SegmentFn = Callable[[str, SegmentationCache], Tuple[Dict[str, Any], str]]


def run_stamp_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def parse_iso(dt: str) -> datetime:
    if not dt:
        return datetime.min
    try:
        return datetime.fromisoformat(dt.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min


def segment_body_deduped(
    process_body: str,
    cache: SegmentationCache,
    *,
    llm: LlmBackend | None = None,
    full_body: Optional[str] = None,
) -> Tuple[Dict[str, Any], str]:
    """Segment email body, deduplicating identical bodies within one run."""
    backend = llm or get_llm_backend()
    digest = hashlib.sha256(process_body.encode("utf-8")).hexdigest()
    if digest in cache:
        seg, err = cache[digest]
        return copy.deepcopy(seg) if isinstance(seg, dict) else seg, err
    prompt = parse_emails([process_body])[0]
    try:
        seg = backend.submit_segmentation(prompt)
        err = ""
    except Exception as exc:
        seg, err = {}, str(exc)
    if isinstance(seg, dict) and not err:
        if "content" not in seg:
            raw_preview = str(seg.get("raw_text") or seg).strip().replace("\n", " ")
            if len(raw_preview) > 180:
                raw_preview = f"{raw_preview[:180]}..."
            err = (
                "Segmentation response missing expected key (content). "
                f"raw_preview={raw_preview}"
            )
            seg = {}
        else:
            seg = guard_segmentation_content(
                full_body or process_body,
                seg,
                resubmit_segmentation=lambda head: backend.submit_segmentation(
                    parse_emails([head])[0]
                ),
            )
    stored = copy.deepcopy(seg) if isinstance(seg, dict) else {}
    cache[digest] = (stored, err)
    return stored, err


def load_timeline_entries_by_thread(
    db_path: str,
    *,
    lookback_days: int | None = None,
) -> Dict[str, List[Dict[str, Any]]]:
    from utils.lookback_config import get_lookback_days

    days = get_lookback_days() if lookback_days is None else lookback_days
    lookback_bound = (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).isoformat()

    with connect_sqlite(db_path, row_factory=sqlite3.Row) as conn:
        _ensure_timeline_schema(conn)
        conn.commit()
        rows = conn.execute(
            """
            SELECT source_id, type, datetime, sender, recipients, summary, body,
                   COALESCE(thread_id, '') AS thread_id,
                   COALESCE(body_has_image, 0) AS body_has_image,
                   COALESCE(fetch_oauth_account_id, '') AS fetch_oauth_account_id
            FROM timeline_entries
            WHERE (
                type IN ('email', 'meeting_invite')
                OR (type = 'meeting' AND COALESCE(TRIM(body), '') != '')
            )
              AND datetime >= ?
            ORDER BY datetime ASC
            """,
            (lookback_bound,),
        ).fetchall()

    by_tid: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        d = dict(r)
        tid = (d.get("thread_id") or "").strip()
        if not tid:
            tid = f"_orphan_{d.get('source_id') or 'unknown'}"
        by_tid[tid].append(d)
    return dict(by_tid)


def load_timeline_entry_by_source_id(
    db_path: str,
    source_id: str,
) -> Optional[Dict[str, Any]]:
    sid = (source_id or "").strip()
    if not sid:
        return None
    try:
        with connect_sqlite(db_path, row_factory=sqlite3.Row) as conn:
            row = conn.execute(
                """
                SELECT source_id, type, datetime, sender, recipients, summary, body,
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


def row_needs_segmentation(
    row: Dict[str, Any],
    process_body: str,
    *,
    thread_id: str,
    processed_pairs: Set[Tuple[str, str]],
    prior_cleaned_by_pair: Dict[Tuple[str, str], str],
) -> bool:
    source_id = str(row.get("source_id") or "").strip()
    pair = (str(thread_id or "").strip(), source_id)
    if source_id and pair in processed_pairs:
        prior_cleaned = prior_cleaned_by_pair.get(pair, "")
        if not should_reprocess_image_only_row(
            process_body,
            prior_cleaned,
            body_has_image=bool(row.get("body_has_image")),
            row=row,
        ) and not segmentation_content_from_quoted_tail_only(
            process_body, prior_cleaned
        ) and not segmentation_content_not_from_reply_head(
            process_body, prior_cleaned
        ):
            return False
    if not process_body and not timeline_row_needs_image_description(row, process_body):
        return False
    return True


def cleaned_entry_from_timeline_row(
    *,
    thread_id: str,
    row: Dict[str, Any],
    process_body: str,
    seg: Dict[str, Any],
    err: str,
) -> Dict[str, Any]:
    source_id = str(row.get("source_id") or "").strip()
    return {
        "thread_id": thread_id,
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


def segment_timeline_row(
    row: Dict[str, Any],
    *,
    thread_id: str,
    seg_cache: SegmentationCache,
    segment_fn: SegmentFn,
) -> Dict[str, Any]:
    process_body = timeline_row_process_body(row)
    # Meet recording notes already store the conversation-summary tab text.
    if str(row.get("type") or "").strip() == "meeting":
        seg = {
            "content": process_body,
            "quoted_reply": "",
            "signature": "",
        }
        return cleaned_entry_from_timeline_row(
            thread_id=thread_id,
            row=row,
            process_body=process_body,
            seg=seg,
            err="",
        )
    seg_body = strip_quoted_thread_tail(process_body) or process_body
    seg, err = process_timeline_message_segmentation(
        row,
        seg_body,
        seg_cache,
        lambda body, cache: segment_body_deduped(
            body, cache, full_body=process_body
        ),
    )
    return cleaned_entry_from_timeline_row(
        thread_id=thread_id,
        row=row,
        process_body=process_body,
        seg=seg,
        err=err,
    )


def segment_new_timeline_rows(
    thread_id: str,
    rows: List[Dict[str, Any]],
    *,
    processed_pairs: Set[Tuple[str, str]],
    prior_cleaned_by_pair: Dict[Tuple[str, str], str],
    seg_cache: SegmentationCache,
    segment_fn: SegmentFn,
) -> List[Dict[str, Any]]:
    """Segment only timeline rows that need fresh LLM output."""
    rows_sorted = sorted(rows, key=lambda x: parse_iso(str(x.get("datetime") or "")))
    cleaned_thread: List[Dict[str, Any]] = []
    for row in rows_sorted:
        process_body = timeline_row_process_body(row)
        if not row_needs_segmentation(
            row,
            process_body,
            thread_id=thread_id,
            processed_pairs=processed_pairs,
            prior_cleaned_by_pair=prior_cleaned_by_pair,
        ):
            continue
        cleaned_thread.append(
            segment_timeline_row(
                row,
                thread_id=thread_id,
                seg_cache=seg_cache,
                segment_fn=segment_fn,
            )
        )
    return cleaned_thread


def merge_cleaned_for_thread(
    db_path: str,
    thread_id: str,
    cleaned_new: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Successful cleaned rows for a thread, including not-yet-persisted segmentation."""
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


def build_summary_input(
    db_path: str,
    thread_id: str,
    newly_segmented: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    prior_cleaned = load_processed_cleaned_for_thread(db_path, thread_id)
    history_by_source: Dict[str, Dict[str, Any]] = {
        str(x.get("source_id") or "").strip(): x for x in prior_cleaned
    }
    for c in newly_segmented:
        sid = str(c.get("source_id") or "").strip()
        history_by_source[sid or f"__new__{len(history_by_source)}"] = c
    return sorted(
        history_by_source.values(),
        key=lambda x: parse_iso(str(x.get("datetime") or "")),
    )


def per_message_rows(
    cleaned_thread: List[Dict[str, Any]],
    thread_summary: Dict[str, Any],
) -> List[Dict[str, Any]]:
    return [
        {
            "thread_id": c.get("thread_id", ""),
            "source_id": str(c.get("source_id") or "").strip(),
            "thread_summary": thread_summary,
            "cleaned_content": str(c.get("cleaned_content") or "").strip(),
            "quoted_reply": str(c.get("quoted_reply") or "").strip(),
            "signature": str(c.get("signature") or "").strip(),
            "api_error": str(c.get("api_error") or "").strip(),
            "sender": str(c.get("sender") or "").strip(),
            "datetime": str(c.get("datetime") or "").strip(),
            "subject": str(c.get("subject") or "").strip(),
        }
        for c in cleaned_thread
    ]


def persist_thread_summary(
    db_path: str,
    thread_id: str,
    summary_input: List[Dict[str, Any]],
    thread_summary: Dict[str, Any],
    summary_mode: str,
    *,
    backend: str,
    generated_at: str,
    apply_to_outputs: bool = False,
) -> int:
    """Write summary cache and optionally update ``claude_message_outputs`` rows."""
    fp = compute_summary_fingerprint(summary_input, db_path=db_path, backend=backend)
    save_thread_summary_cache(
        db_path,
        thread_id=thread_id,
        thread_summary=thread_summary,
        input_fingerprint=fp,
        summary_mode=summary_mode,
        backend=backend,
        generated_at=generated_at,
    )
    if not apply_to_outputs:
        return 0
    return apply_thread_resummary_to_db(
        db_path,
        thread_id=thread_id,
        thread_summary=thread_summary,
        generated_at=generated_at,
    )


def process_thread_llm(
    db_path: str,
    thread_id: str,
    timeline_rows: List[Dict[str, Any]],
    *,
    processed_pairs: Set[Tuple[str, str]],
    prior_cleaned_by_pair: Dict[Tuple[str, str], str],
    seg_cache: SegmentationCache,
    llm: LlmBackend,
    generated_at: str,
    force_full: bool = False,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Segment new messages and summarize one thread.

    Returns ``(newly_segmented, per_message_rows)``; both empty when nothing changed.
    """
    cleaned_thread = segment_new_timeline_rows(
        thread_id,
        timeline_rows,
        processed_pairs=processed_pairs,
        prior_cleaned_by_pair=prior_cleaned_by_pair,
        seg_cache=seg_cache,
        segment_fn=lambda body, cache: segment_body_deduped(body, cache, llm=llm),
    )
    if not cleaned_thread:
        return [], []

    for c in cleaned_thread:
        sid = str(c.get("source_id") or "").strip()
        if sid and not str(c.get("api_error") or "").strip():
            processed_pairs.add((str(thread_id or "").strip(), sid))

    summary_input = build_summary_input(db_path, thread_id, cleaned_thread)
    tsumm, summary_mode = resolve_thread_summary(
        db_path,
        thread_id,
        summary_input,
        newly_segmented=cleaned_thread,
        force_full=force_full,
        backend=llm,
    )
    persist_thread_summary(
        db_path,
        thread_id,
        summary_input,
        tsumm,
        summary_mode,
        backend=llm.name,
        generated_at=generated_at,
    )
    return cleaned_thread, per_message_rows(cleaned_thread, tsumm)


def run_threads_llm_pipeline(
    lookback_days: int | None = None,
    db_path: str | None = None,
    *,
    backend: str | None = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Segment new timeline messages and summarize affected threads."""
    from utils.lookback_config import get_lookback_days

    days = get_lookback_days() if lookback_days is None else lookback_days
    db = db_path or database_path()
    llm = get_llm_backend(backend=backend)
    run_stamp = run_stamp_utc()
    log.info("Thread LLM pipeline run_stamp=%s backend=%s", run_stamp, llm.name)

    grouped = load_timeline_entries_by_thread(db, lookback_days=days)
    if not grouped:
        return [], []

    processed_pairs = load_processed_thread_source_pairs(db)
    prior_cleaned_by_pair = load_prior_cleaned_content_by_pair(db)
    seg_cache: SegmentationCache = {}
    cleaned_all: List[Dict[str, Any]] = []
    per_message: List[Dict[str, Any]] = []
    generated_at = datetime.now(timezone.utc).isoformat()

    thread_keys = sorted(
        grouped.keys(),
        key=lambda tid: min(
            (parse_iso(str(x.get("datetime") or "")) for x in grouped.get(tid, [])),
            default=datetime.min,
        ),
    )

    # #region agent log
    try:
        import json as _json, time as _time
        with open("/home/luisaherrmann/Code/fivelanes-public/.cursor/debug-2e4b92.log", "a", encoding="utf-8") as _df:
            _df.write(_json.dumps({"sessionId": "2e4b92", "hypothesisId": "B", "location": "process.py:run_threads_llm_pipeline", "message": "thread_loop_start", "data": {"thread_count": len(thread_keys), "lookback_days": days, "run_stamp": run_stamp}, "timestamp": int(_time.time() * 1000)}) + "\n")
    except Exception:
        pass
    # #endregion

    for thread_id in thread_keys:
        # #region agent log
        _thread_t0 = datetime.now(timezone.utc)
        _timeline_count = len(grouped.get(thread_id) or [])
        # #endregion
        cleaned_thread, thread_per_message = process_thread_llm(
            db,
            thread_id,
            grouped[thread_id],
            processed_pairs=processed_pairs,
            prior_cleaned_by_pair=prior_cleaned_by_pair,
            seg_cache=seg_cache,
            llm=llm,
            generated_at=generated_at,
        )
        if cleaned_thread:
            # #region agent log
            try:
                import json as _json, time as _time
                _elapsed_ms = int((datetime.now(timezone.utc) - _thread_t0).total_seconds() * 1000)
                with open("/home/luisaherrmann/Code/fivelanes-public/.cursor/debug-2e4b92.log", "a", encoding="utf-8") as _df:
                    _df.write(_json.dumps({"sessionId": "2e4b92", "hypothesisId": "B", "location": "process.py:run_threads_llm_pipeline", "message": "thread_processed", "data": {"thread_id": thread_id[:80], "new_messages": len(cleaned_thread), "elapsed_ms": _elapsed_ms}, "timestamp": int(_time.time() * 1000)}) + "\n")
            except Exception:
                pass
            # #endregion
            cleaned_all.extend(cleaned_thread)
        else:
            # #region agent log
            try:
                import json as _json
                import time as _time

                _unprocessed = sum(
                    1
                    for row in (grouped.get(thread_id) or [])
                    if (thread_id, str(row.get("source_id") or "").strip()) not in processed_pairs
                )
                if _unprocessed:
                    with open(
                        "/home/luisaherrmann/Code/fivelanes-public/.cursor/debug-0ddb00.log",
                        "a",
                        encoding="utf-8",
                    ) as _df:
                        _df.write(
                            _json.dumps(
                                {
                                    "sessionId": "0ddb00",
                                    "hypothesisId": "B",
                                    "location": "process.py:run_threads_llm_pipeline",
                                    "message": "thread_skipped_no_output",
                                    "data": {
                                        "thread_id": thread_id[:80],
                                        "timeline_rows": _timeline_count,
                                        "unprocessed_pairs": _unprocessed,
                                    },
                                    "timestamp": int(_time.time() * 1000),
                                }
                            )
                            + "\n"
                        )
            except Exception:
                pass
            # #endregion
        if cleaned_thread:
            per_message.extend(thread_per_message)
            save_claude_run_outputs(
                db,
                run_stamp=run_stamp,
                generated_at=generated_at,
                cleaned=cleaned_thread,
                per_message=thread_per_message,
                replace_run_stamp=False,
            )

    return cleaned_all, per_message


def force_resummarize_thread(
    db_path: str,
    thread_id: str,
    *,
    llm: LlmBackend | None = None,
    generated_at: str | None = None,
    apply_to_outputs: bool = True,
) -> bool:
    """Full thread resummary from stored cleaned bodies (no re-segmentation)."""
    active_llm = llm or get_llm_backend()
    tid = (thread_id or "").strip()
    cleaned = load_processed_cleaned_for_thread(db_path, tid)
    if not cleaned:
        return False

    tsumm = summarize_thread(cleaned, mode="full", db_path=db_path, backend=active_llm)
    if not thread_summary_is_valid(tsumm, cleaned=cleaned):
        log.error(
            "Skip persist %s: invalid summary (%s)",
            tid,
            str(tsumm.get("api_error") or tsumm.keys()),
        )
        return False

    stamp = generated_at or datetime.now(timezone.utc).isoformat()
    rows = persist_thread_summary(
        db_path,
        tid,
        cleaned,
        tsumm,
        "full",
        backend=active_llm.name,
        generated_at=stamp,
        apply_to_outputs=apply_to_outputs,
    )
    return bool(rows) if apply_to_outputs else True
