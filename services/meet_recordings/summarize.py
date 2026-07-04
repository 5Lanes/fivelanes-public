"""Summarize tracked Meet recording notes (conversation-summary tab text)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from services.meet_recordings.tracking import (
    fetch_tracked_document_keys,
    load_imported_note,
    meet_inbox_thread_id,
)
from utils.thread_summary_normalize import finalize_thread_summary

log = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _run_stamp_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _cleaned_row_from_note(
    note: Dict[str, Any], *, thread_id: str
) -> Optional[Dict[str, Any]]:
    key = str(note.get("id") or "").strip()
    body = str(note.get("body") or "").strip()
    if not key or not body:
        return None
    title = str(note.get("label") or note.get("name") or "").strip()
    content = body
    if title and not content.lower().startswith("meeting:"):
        content = f"Meeting: {title}\n\n{content}".strip()
    return {
        "thread_id": thread_id,
        "source_id": f"docs:{key}",
        "datetime": note.get("datetime") or "",
        "sender": note.get("owner_email") or "",
        "recipients": "",
        "subject": note.get("name") or title or "(Meet recording)",
        "raw_text": content,
        "forwarded_from": "",
        "cleaned_content": content,
        "quoted_reply": "",
        "signature": "",
        "api_error": "",
    }


def _latest_thread_summary(db_path: str, thread_id: str) -> Dict[str, Any]:
    from utils.database import _parse_thread_summary_json

    tid = (thread_id or "").strip()
    if not tid:
        return {}
    try:
        import sqlite3

        from utils.database import _ensure_claude_outputs_schema, connect_sqlite

        with connect_sqlite(db_path) as conn:
            _ensure_claude_outputs_schema(conn)
            row = conn.execute(
                """
                SELECT thread_summary_json
                FROM claude_message_outputs
                WHERE COALESCE(thread_id, '') = ?
                  AND COALESCE(TRIM(api_error), '') = ''
                ORDER BY datetime DESC, generated_at DESC, id DESC
                LIMIT 1
                """,
                (tid,),
            ).fetchone()
    except Exception:
        return {}
    if not row:
        return {}
    return _parse_thread_summary_json(row[0])


def summarize_one_meet_recording(
    db_path: str,
    document_key: str,
    *,
    force: bool = False,
    run_stamp: Optional[str] = None,
) -> Dict[str, Any]:
    from utils.api_error_detection import thread_summary_is_valid
    from utils.database import apply_thread_resummary_to_db, save_claude_run_outputs

    key = (document_key or "").strip()
    if not key:
        return {"ok": False, "error": "missing_document_key"}

    note = load_imported_note(key)
    if not note:
        return {"ok": False, "error": "not_imported", "document_key": key}

    thread_id = meet_inbox_thread_id(key)
    cleaned = _cleaned_row_from_note(note, thread_id=thread_id)
    if not cleaned:
        return {"ok": False, "error": "empty_summary", "document_key": key}

    prior = _latest_thread_summary(db_path, thread_id)
    if prior and not force and thread_summary_is_valid(prior, cleaned=[cleaned]):
        return {
            "ok": True,
            "skipped": True,
            "document_key": key,
            "thread_id": thread_id,
        }

    from services.llm_service import get_llm_backend
    from services.pipeline.summary import summarize_thread

    display_label = str(note.get("label") or note.get("name") or key)
    tsumm = finalize_thread_summary(
        summarize_thread([cleaned], mode="full", backend=get_llm_backend()),
        [cleaned],
        display_label=display_label,
        channel="meet_recording",
    )

    stamp = run_stamp or _run_stamp_utc()
    generated_at = _utc_now_iso()
    per_message = [
        {
            "thread_id": thread_id,
            "source_id": cleaned["source_id"],
            "thread_summary": tsumm,
            "cleaned_content": cleaned["cleaned_content"],
            "quoted_reply": "",
            "signature": "",
            "api_error": "",
            "sender": cleaned["sender"],
            "datetime": cleaned["datetime"],
            "subject": cleaned["subject"],
        }
    ]
    save_claude_run_outputs(
        db_path,
        run_stamp=stamp,
        generated_at=generated_at,
        cleaned=[cleaned],
        per_message=per_message,
    )
    apply_thread_resummary_to_db(
        db_path,
        thread_id=thread_id,
        thread_summary=tsumm,
        generated_at=generated_at,
    )

    return {
        "ok": True,
        "document_key": key,
        "thread_id": thread_id,
        "summary_valid": thread_summary_is_valid(tsumm, cleaned=[cleaned]),
        "summary_error": str(tsumm.get("api_error") or ""),
    }


def summarize_tracked_meet_recordings(
    db_path: str,
    *,
    document_keys: Optional[List[str]] = None,
    force: bool = False,
) -> Dict[str, Any]:
    keys = (
        [k.strip() for k in document_keys if str(k).strip()]
        if document_keys is not None
        else fetch_tracked_document_keys(db_path)
    )
    if not keys:
        return {"ok": True, "summarized": 0, "skipped": 0, "errors": []}

    run_stamp = _run_stamp_utc()
    summarized = 0
    skipped = 0
    errors: List[Dict[str, Any]] = []

    for key in keys:
        try:
            result = summarize_one_meet_recording(
                db_path, key, force=force, run_stamp=run_stamp
            )
        except Exception as exc:
            log.exception("Meet recording summary failed for %s", key)
            errors.append({"document_key": key, "error": str(exc)})
            continue
        if not result.get("ok"):
            errors.append(
                {
                    "document_key": key,
                    "error": result.get("error") or "summarize_failed",
                }
            )
            continue
        if result.get("skipped"):
            skipped += 1
        else:
            summarized += 1

    return {
        "ok": True,
        "summarized": summarized,
        "skipped": skipped,
        "errors": errors,
    }
