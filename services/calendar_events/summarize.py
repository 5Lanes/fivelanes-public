"""Summarize tracked calendar-event threads (single-item, no segmentation)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from services.calendar_events.tracking import (
    calendar_inbox_thread_id,
    fetch_tracked_calendar_dedupe_keys,
)
from utils.thread_summary_normalize import finalize_thread_summary

log = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _run_stamp_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _cleaned_row_from_meeting(
    meeting: Dict[str, Any], *, thread_id: str
) -> Optional[Dict[str, Any]]:
    key = str(meeting.get("dedupe_key") or "").strip()
    title = str(meeting.get("summary") or "").strip() or "(No title)"
    if not key:
        return None
    from services.calendar_events.tracking import _format_event_body

    content = f"Meeting: {title}\n\n{_format_event_body(meeting)}".strip()
    attendees = meeting.get("attendees") or []
    attendees_str = ", ".join(attendees)
    return {
        "thread_id": thread_id,
        "source_id": thread_id,
        "datetime": meeting.get("start_iso") or "",
        "sender": attendees_str,
        "recipients": attendees_str,
        "subject": title,
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
        from utils.database import _ensure_message_outputs_schema, connect_sqlite

        with connect_sqlite(db_path) as conn:
            _ensure_message_outputs_schema(conn)
            row = conn.execute(
                """
                SELECT thread_summary_json
                FROM message_outputs
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


def summarize_one_calendar_event(
    db_path: str,
    dedupe_key: str,
    *,
    force: bool = False,
    run_stamp: Optional[str] = None,
) -> Dict[str, Any]:
    from utils.api_error_detection import thread_summary_is_valid
    from utils.database import apply_thread_resummary_to_db, fetch_meetings_rows, save_message_outputs

    key = (dedupe_key or "").strip()
    if not key:
        return {"ok": False, "error": "missing_dedupe_key"}

    meeting = next(
        (m for m in fetch_meetings_rows(db_path) if str(m.get("dedupe_key") or "") == key),
        None,
    )
    if not meeting:
        return {"ok": False, "error": "not_tracked", "dedupe_key": key}

    thread_id = calendar_inbox_thread_id(key)
    cleaned = _cleaned_row_from_meeting(meeting, thread_id=thread_id)
    if not cleaned:
        return {"ok": False, "error": "empty_event", "dedupe_key": key}

    prior = _latest_thread_summary(db_path, thread_id)
    if prior and not force and thread_summary_is_valid(prior, cleaned=[cleaned]):
        return {"ok": True, "skipped": True, "dedupe_key": key, "thread_id": thread_id}

    from services.llm_service import get_llm_backend
    from services.pipeline.summary import summarize_thread

    display_label = str(meeting.get("summary") or key)
    tsumm = finalize_thread_summary(
        summarize_thread([cleaned], mode="full", backend=get_llm_backend()),
        [cleaned],
        display_label=display_label,
        channel="calendar_event",
    )

    stamp = run_stamp or _run_stamp_utc()
    generated_at = _utc_now_iso()
    summary_err = str(tsumm.get("api_error") or "").strip()
    cleaned_row = {**cleaned, "api_error": summary_err} if summary_err else cleaned
    per_message = [
        {
            "thread_id": thread_id,
            "source_id": cleaned["source_id"],
            "thread_summary": tsumm,
            "cleaned_content": cleaned["cleaned_content"],
            "quoted_reply": "",
            "signature": "",
            "api_error": summary_err,
            "sender": cleaned["sender"],
            "datetime": cleaned["datetime"],
            "subject": cleaned["subject"],
        }
    ]
    save_message_outputs(
        db_path,
        run_stamp=stamp,
        generated_at=generated_at,
        cleaned=[cleaned_row],
        per_message=per_message,
        replace_run_stamp=False,
    )
    apply_thread_resummary_to_db(
        db_path,
        thread_id=thread_id,
        thread_summary=tsumm,
        generated_at=generated_at,
    )

    return {
        "ok": True,
        "dedupe_key": key,
        "thread_id": thread_id,
        "summary_valid": thread_summary_is_valid(tsumm, cleaned=[cleaned]),
        "summary_error": str(tsumm.get("api_error") or ""),
    }


def summarize_tracked_calendar_event_threads(
    db_path: str,
    *,
    dedupe_keys: Optional[List[str]] = None,
    force: bool = False,
) -> Dict[str, Any]:
    keys = (
        [k.strip() for k in dedupe_keys if str(k).strip()]
        if dedupe_keys is not None
        else fetch_tracked_calendar_dedupe_keys(db_path)
    )
    if not keys:
        return {"ok": True, "summarized": 0, "skipped": 0, "errors": []}

    run_stamp = _run_stamp_utc()
    summarized = 0
    skipped = 0
    errors: List[Dict[str, Any]] = []

    for key in keys:
        try:
            result = summarize_one_calendar_event(db_path, key, force=force, run_stamp=run_stamp)
        except Exception as exc:
            log.exception("Calendar event summary failed for %s", key)
            errors.append({"dedupe_key": key, "error": str(exc)})
            continue
        if not result.get("ok"):
            errors.append(
                {"dedupe_key": key, "error": result.get("error") or "summarize_failed"}
            )
            continue
        if result.get("skipped"):
            skipped += 1
        else:
            summarized += 1

    return {"ok": True, "summarized": summarized, "skipped": skipped, "errors": errors}
