"""Re-run thread summaries for active inbox threads (no re-segmentation)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent


def _run_stamp_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


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


def _row_to_cleaned(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "thread_id": row.get("thread_id") or "",
        "source_id": row.get("source_id") or "",
        "datetime": row.get("datetime") or "",
        "sender": row.get("sender") or "",
        "recipients": row.get("recipients") or "",
        "subject": row.get("subject") or "",
        "raw_text": row.get("raw_text") or "",
        "forwarded_from": row.get("forwarded_from") or "",
        "cleaned_content": row.get("cleaned_content") or "",
        "quoted_reply": row.get("quoted_reply") or "",
        "signature": row.get("signature") or "",
        "api_error": row.get("api_error") or "",
    }


def _row_to_per_message(row: Dict[str, Any]) -> Dict[str, Any]:
    try:
        ts = json.loads(row.get("thread_summary_json") or "{}")
    except json.JSONDecodeError:
        ts = {}
    if not isinstance(ts, dict):
        ts = {}
    return {
        "thread_id": row.get("thread_id") or "",
        "source_id": row.get("source_id") or "",
        "thread_summary": ts,
        "cleaned_content": row.get("cleaned_content") or "",
        "quoted_reply": row.get("quoted_reply") or "",
        "signature": row.get("signature") or "",
        "api_error": row.get("api_error") or "",
        "sender": row.get("sender") or "",
        "datetime": row.get("datetime") or "",
        "subject": row.get("subject") or "",
    }


def write_fivelanes_bundle_from_db(
    db_path: str,
    *,
    run_stamp: str,
    generated_at: str,
) -> Path:
    from utils.database import load_latest_claude_output_snapshot_rows

    rows = load_latest_claude_output_snapshot_rows(db_path)
    cleaned = [_row_to_cleaned(r) for r in rows]
    per_message = [_row_to_per_message(r) for r in rows]
    bundle = {
        "cleaned": cleaned,
        "summary": _summary_rows_for_ui(per_message),
        "run_stamp": run_stamp,
        "generated_at": generated_at,
    }
    out_dir = _ROOT / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    bundle_path = out_dir / "fivelanes_summary.json"
    stamped_path = out_dir / f"fivelanes_bundle_{run_stamp}.json"
    for path in (bundle_path, stamped_path):
        with path.open("w", encoding="utf-8") as f:
            json.dump(bundle, f, indent=2, ensure_ascii=False)
    latest_path = out_dir / "latest.json"
    latest_payload = {
        "run_stamp": run_stamp,
        "generated_at": generated_at,
        "files": {
            "bundle": stamped_path.name,
            "cleaned_messages": f"cleaned_messages_{run_stamp}.json",
            "per_message_summary": f"summary_{run_stamp}.json",
        },
    }
    with latest_path.open("w", encoding="utf-8") as f:
        json.dump(latest_payload, f, indent=2, ensure_ascii=False)
    json.dump(
        cleaned,
        open(out_dir / f"cleaned_messages_{run_stamp}.json", "w"),
        indent=2,
        ensure_ascii=False,
    )
    json.dump(
        per_message,
        open(out_dir / f"summary_{run_stamp}.json", "w"),
        indent=2,
        ensure_ascii=False,
    )
    return bundle_path


def force_resummary_active_threads(
    *,
    db_path: str | None = None,
    dry_run: bool = False,
    thread_id: str | None = None,
) -> int:
    """Re-summarize threads shown as Active in the dashboard."""
    from services.llm_service import get_llm_backend
    from services.pipeline.summary import (
        compute_summary_fingerprint,
        summarize_thread,
    )
    from utils.api_error_detection import thread_summary_is_valid
    from utils.database import (
        apply_thread_resummary_to_db,
        list_active_thread_ids_for_resummary,
        load_processed_cleaned_for_thread,
        save_thread_summary_cache,
    )
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
    run_stamp = _run_stamp_utc()

    for i, tid in enumerate(thread_ids, start=1):
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
        tsumm = summarize_thread(cleaned, mode="full", db_path=db, backend=llm)
        if not thread_summary_is_valid(tsumm, cleaned=cleaned):
            log.error(
                "Skip persist %s: invalid summary (%s)",
                tid,
                str(tsumm.get("api_error") or tsumm.keys()),
            )
            continue
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
            updated += 1

    if updated:
        write_fivelanes_bundle_from_db(
            db,
            run_stamp=run_stamp,
            generated_at=generated_at,
        )
    if updated:
        log.info("Updated %d thread(s); bundle written to out/fivelanes_summary.json", updated)
    else:
        log.info("Updated 0 thread(s)")
    return updated
