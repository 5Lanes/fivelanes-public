"""
Unified email LLM pipeline: segment messages, summarize threads, persist outputs.

Backend (Claude or Ollama) is selected via ``FIVELANES_BACKEND``.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from utils.runtime_paths import data_path, infra_root

from services.email.forwarding import primary_email_from_sender
from services.email.inbox_delivery import (
    timeline_row_needs_image_description,
    timeline_row_process_body,
)
from services.email.segmentation import (
    guard_segmentation_content,
    segmentation_content_from_quoted_tail_only,
)
from services.image_description import (
    fetch_prior_cleaned_content,
    process_timeline_message_segmentation,
    should_reprocess_image_only_row,
)
from services.llm_service import get_llm_backend
from services.pipeline.summary import compute_summary_fingerprint, resolve_thread_summary
from services.prompts import parse_emails
from utils.database import (
    load_processed_cleaned_for_thread,
    load_processed_thread_source_pairs,
    save_claude_run_outputs,
    save_thread_summary_cache,
)


def _run_stamp_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _parse_iso(dt: str) -> datetime:
    if not dt:
        return datetime.min
    try:
        return datetime.fromisoformat(dt.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min


_SegmentationCache = Dict[str, Tuple[Dict[str, Any], str]]


def _segment_body_deduped(
    process_body: str,
    cache: _SegmentationCache,
    *,
    llm=None,
) -> Tuple[Dict[str, Any], str]:
    """Segment email body, deduplicating identical bodies within one run."""
    backend = llm or get_llm_backend()
    digest = hashlib.sha256(process_body.encode("utf-8")).hexdigest()
    resubmit = lambda head: backend.submit_segmentation(parse_emails([head])[0])
    if digest in cache:
        seg, err = cache[digest]
        seg = copy.deepcopy(seg)
        if isinstance(seg, dict) and not err:
            seg = guard_segmentation_content(
                process_body, seg, resubmit_segmentation=resubmit
            )
        return seg, err
    prompt = parse_emails([process_body])[0]
    try:
        seg = backend.submit_segmentation(prompt)
        err = ""
    except Exception as exc:
        seg, err = {}, str(exc)
    if isinstance(seg, dict) and not err:
        seg = guard_segmentation_content(
            process_body,
            seg,
            resubmit_segmentation=resubmit,
        )
    if isinstance(seg, dict) and not err:
        if "content" not in seg:
            raw_preview = str(seg.get("raw_text") or seg).strip().replace("\n", " ")
            if len(raw_preview) > 180:
                raw_preview = f"{raw_preview[:180]}..."
            err = f"Segmentation response missing expected key (content). raw_preview={raw_preview}"
            seg = {}
    stored = copy.deepcopy(seg) if isinstance(seg, dict) else {}
    cache[digest] = (stored, err)
    return stored, err


def load_timeline_entries_by_thread(
    db_path: str,
    *,
    lookback_days: int = 14,
) -> Dict[str, List[Dict[str, Any]]]:
    lookback_bound = (
        datetime.now(timezone.utc) - timedelta(days=lookback_days)
    ).isoformat()

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT source_id, datetime, sender, recipients, summary, body,
                   COALESCE(thread_id, '') AS thread_id,
                   COALESCE(body_has_image, 0) AS body_has_image,
                   COALESCE(fetch_oauth_account_id, '') AS fetch_oauth_account_id
            FROM timeline_entries
            WHERE type IN ('email', 'meeting_invite')
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


def _summary_rows_for_ui(per_message: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in per_message:
        ts = row.get("thread_summary")
        base = {k: v for k, v in row.items() if k != "thread_summary"}
        if isinstance(ts, dict):
            out.append({**ts, **base})
        else:
            out.append(dict(base))
    return out


def _load_previous_pipeline_outputs(out_dir: Path) -> Optional[Dict[str, Any]]:
    latest = out_dir / "latest.json"
    if not latest.is_file():
        return None
    try:
        meta = json.loads(latest.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    stamp = meta.get("run_stamp")
    files = meta.get("files")
    if not stamp or not isinstance(files, dict):
        return None
    cleaned_path = out_dir / str(files.get("cleaned_messages") or "")
    summary_path = out_dir / str(files.get("per_message_summary") or "")
    if not cleaned_path.is_file() or not summary_path.is_file():
        return None
    try:
        cleaned_prev = json.loads(cleaned_path.read_text(encoding="utf-8"))
        per_message_prev = json.loads(summary_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(cleaned_prev, list) or not isinstance(per_message_prev, list):
        return None
    return {
        "run_stamp": stamp,
        "cleaned": cleaned_prev,
        "per_message": per_message_prev,
    }


def run_fivelanes_llm_pipeline(
    lookback_days: int = 14,
    db_path: Optional[str] = None,
    *,
    backend: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Segment new messages and summarize affected threads."""
    db = db_path or os.getenv("DATABASE_NAME") or "timeline.db"
    llm = get_llm_backend(backend=backend)
    run_stamp = _run_stamp_utc()
    print(f"Assigned run stamp: {run_stamp} (backend={llm.name})")

    grouped = load_timeline_entries_by_thread(db, lookback_days=lookback_days)
    if not grouped:
        return [], []

    processed_thread_source_pairs = load_processed_thread_source_pairs(db)
    seg_cache: _SegmentationCache = {}
    cleaned_all: List[Dict[str, Any]] = []
    per_message: List[Dict[str, Any]] = []

    def _thread_min_dt(tid: str) -> datetime:
        times = [_parse_iso(str(x.get("datetime") or "")) for x in grouped.get(tid, [])]
        return min(times) if times else datetime.min

    thread_keys = sorted(grouped.keys(), key=_thread_min_dt)
    generated_at = datetime.now(timezone.utc).isoformat()

    for thread_id in thread_keys:
        rows = grouped[thread_id]
        rows_sorted = sorted(rows, key=lambda x: _parse_iso(str(x.get("datetime") or "")))
        cleaned_thread: List[Dict[str, Any]] = []
        for row in rows_sorted:
            source_id = str(row.get("source_id") or "").strip()
            pair = (str(thread_id or "").strip(), source_id)
            process_body = timeline_row_process_body(row)
            if source_id and pair in processed_thread_source_pairs:
                prior_cleaned = fetch_prior_cleaned_content(db, thread_id, source_id)
                if not should_reprocess_image_only_row(
                    process_body,
                    prior_cleaned,
                    body_has_image=bool(row.get("body_has_image")),
                    row=row,
                ) and not segmentation_content_from_quoted_tail_only(
                    process_body, prior_cleaned
                ):
                    continue
            if not process_body and not timeline_row_needs_image_description(
                row, process_body
            ):
                continue
            seg, err = process_timeline_message_segmentation(
                row,
                process_body,
                seg_cache,
                lambda body, cache: _segment_body_deduped(body, cache, llm=llm),
            )
            entry = {
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
            cleaned_thread.append(entry)
            if source_id and not err:
                processed_thread_source_pairs.add(pair)

        if not cleaned_thread:
            continue

        cleaned_all.extend(cleaned_thread)
        prior_cleaned = load_processed_cleaned_for_thread(db, thread_id)
        history_by_source: Dict[str, Dict[str, Any]] = {
            str(x.get("source_id") or "").strip(): x for x in prior_cleaned
        }
        for c in cleaned_thread:
            sid = str(c.get("source_id") or "").strip()
            history_by_source[sid or f"__new__{len(history_by_source)}"] = c
        summary_input = sorted(
            history_by_source.values(),
            key=lambda x: _parse_iso(str(x.get("datetime") or "")),
        )
        tsumm, summary_mode = resolve_thread_summary(
            db,
            thread_id,
            summary_input,
            newly_segmented=cleaned_thread,
            backend=llm,
        )
        fp = compute_summary_fingerprint(summary_input, db_path=db, backend=llm.name)
        save_thread_summary_cache(
            db,
            thread_id=thread_id,
            thread_summary=tsumm,
            input_fingerprint=fp,
            summary_mode=summary_mode,
            backend=llm.name,
            generated_at=generated_at,
        )

        for c in cleaned_thread:
            per_message.append(
                {
                    "thread_id": c.get("thread_id", ""),
                    "source_id": str(c.get("source_id") or "").strip(),
                    "thread_summary": tsumm,
                    "cleaned_content": str(c.get("cleaned_content") or "").strip(),
                    "quoted_reply": str(c.get("quoted_reply") or "").strip(),
                    "signature": str(c.get("signature") or "").strip(),
                    "api_error": str(c.get("api_error") or "").strip(),
                    "sender": str(c.get("sender") or "").strip(),
                    "datetime": str(c.get("datetime") or "").strip(),
                    "subject": str(c.get("subject") or "").strip(),
                }
            )

    had_new_segmentation = bool(cleaned_all)

    if not cleaned_all or not per_message:
        prev = _load_previous_pipeline_outputs(data_path("out"))
        if prev:
            prev_cleaned = prev.get("cleaned")
            prev_per_message = prev.get("per_message")
            if isinstance(prev_cleaned, list) and isinstance(prev_per_message, list):
                cleaned_all = prev_cleaned
                per_message = prev_per_message

    out_dir = data_path("out")
    out_dir.mkdir(parents=True, exist_ok=True)
    json.dump(
        cleaned_all,
        open(out_dir / f"cleaned_messages_{run_stamp}.json", "w"),
        indent=2,
        ensure_ascii=False,
    )
    out_summary = out_dir / f"summary_{run_stamp}.json"
    json.dump(per_message, open(out_summary, "w"), indent=2, ensure_ascii=False)

    summary_ui = _summary_rows_for_ui(per_message)
    bundle = {
        "cleaned": cleaned_all,
        "summary": summary_ui,
        "run_stamp": run_stamp,
        "generated_at": generated_at,
    }
    out_bundle = out_dir / "fivelanes_summary.json"
    stamped_bundle = out_dir / f"fivelanes_bundle_{run_stamp}.json"
    with out_bundle.open("w", encoding="utf-8") as f:
        json.dump(bundle, f, indent=2, ensure_ascii=False)
    with stamped_bundle.open("w", encoding="utf-8") as f:
        json.dump(bundle, f, indent=2, ensure_ascii=False)

    latest_path = out_dir / "latest.json"
    latest_payload = {
        "run_stamp": run_stamp,
        "generated_at": generated_at,
        "files": {
            "bundle": stamped_bundle.name,
            "cleaned_messages": f"cleaned_messages_{run_stamp}.json",
            "per_message_summary": f"summary_{run_stamp}.json",
        },
    }
    with latest_path.open("w", encoding="utf-8") as f:
        json.dump(latest_payload, f, indent=2, ensure_ascii=False)

    if had_new_segmentation:
        save_claude_run_outputs(
            db,
            run_stamp=run_stamp,
            generated_at=generated_at,
            cleaned=cleaned_all,
            per_message=per_message,
        )

    return cleaned_all, per_message
