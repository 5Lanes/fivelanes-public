"""Run LLM thread summaries for tracked Slack DMs and persist to SQLite."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from services.slack.format import (
    cleaned_rows_for_conversation,
    conversation_label,
    conversation_service,
    load_messages_for_key,
    tracked_thread_cleaned_rows,
)
from services.slack.tracking import (
    fetch_tracked_conversation_keys,
    slack_inbox_thread_id,
)
from services.thread_snooze import maybe_unsnooze_slack_thread
from utils.thread_summary_normalize import finalize_thread_summary

log = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _run_stamp_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _message_fingerprint(rows: List[Dict[str, Any]]) -> Set[Tuple[str, str]]:
    out: Set[Tuple[str, str]] = set()
    for row in rows:
        sid = str(row.get("source_id") or "").strip()
        if not sid:
            continue
        out.add((sid, str(row.get("datetime") or "")))
    return out


def _latest_thread_summary(db_path: str, thread_id: str) -> Dict[str, Any]:
    from utils.database import _parse_thread_summary_json

    tid = (thread_id or "").strip()
    if not tid:
        return {}
    try:
        import sqlite3
        from pathlib import Path

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


def thread_needs_summary(
    db_path: str,
    thread_id: str,
    file_cleaned: List[Dict[str, Any]],
    *,
    force: bool = False,
) -> bool:
    if force:
        return bool(file_cleaned)
    if not file_cleaned:
        return False
    from utils.api_error_detection import thread_summary_is_valid
    from utils.database import load_processed_cleaned_for_thread

    db_cleaned = load_processed_cleaned_for_thread(db_path, thread_id)
    file_fp = _message_fingerprint(file_cleaned)
    db_fp = _message_fingerprint(db_cleaned)
    if file_fp - db_fp:
        return True
    if not db_cleaned and file_cleaned:
        return True
    summary = _latest_thread_summary(db_path, thread_id)
    return not thread_summary_is_valid(summary, cleaned=file_cleaned)


def summarize_one_slack_thread(
    db_path: str,
    conversation_key: str,
    *,
    force: bool = False,
    run_stamp: Optional[str] = None,
) -> Dict[str, Any]:
    from utils.api_error_detection import thread_summary_is_valid
    from utils.database import apply_thread_resummary_to_db, save_claude_run_outputs

    key = (conversation_key or "").strip()
    if not key:
        return {"ok": False, "error": "missing_conversation_key"}

    thread_id = slack_inbox_thread_id(key)
    messages = load_messages_for_key(key)
    if not messages:
        return {"ok": False, "error": "no_messages", "conversation_key": key}

    file_cleaned = cleaned_rows_for_conversation(key, thread_id, messages)
    if not file_cleaned:
        return {"ok": False, "error": "no_message_ids", "conversation_key": key}

    maybe_unsnooze_slack_thread(db_path, key)

    if not thread_needs_summary(db_path, thread_id, file_cleaned, force=force):
        return {"ok": True, "skipped": True, "conversation_key": key, "thread_id": thread_id}

    merged_cleaned, new_cleaned = tracked_thread_cleaned_rows(db_path, key, thread_id)
    if not merged_cleaned:
        return {"ok": False, "error": "no_messages", "conversation_key": key}

    label = conversation_label(messages, key)
    service = conversation_service(messages)
    display_label = f"{label} · {service}" if service else label

    from services.chat.coalesce import coalesce_chat_turns
    from services.llm_service import get_llm_backend
    from services.pipeline.summary import summarize_chat_thread

    turns = coalesce_chat_turns(merged_cleaned)
    tsumm = finalize_thread_summary(
        summarize_chat_thread(turns, backend=get_llm_backend()),
        merged_cleaned,
        display_label=display_label,
        channel="slack",
    )

    stamp = run_stamp or _run_stamp_utc()
    generated_at = _utc_now_iso()

    if new_cleaned:
        per_message: List[Dict[str, Any]] = []
        for row in new_cleaned:
            per_message.append(
                {
                    "thread_id": thread_id,
                    "source_id": row["source_id"],
                    "thread_summary": tsumm,
                    "cleaned_content": row["cleaned_content"],
                    "quoted_reply": "",
                    "signature": "",
                    "api_error": "",
                    "sender": row["sender"],
                    "datetime": row["datetime"],
                    "subject": row["subject"],
                }
            )
        save_claude_run_outputs(
            db_path,
            run_stamp=stamp,
            generated_at=generated_at,
            cleaned=new_cleaned,
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
        "conversation_key": key,
        "thread_id": thread_id,
        "messages": len(merged_cleaned),
        "new_messages": len(new_cleaned),
        "summary_valid": thread_summary_is_valid(tsumm, cleaned=merged_cleaned),
        "summary_error": str(tsumm.get("api_error") or ""),
    }


def summarize_tracked_slack_threads(
    db_path: str,
    *,
    conversation_keys: Optional[List[str]] = None,
    force: bool = False,
) -> Dict[str, Any]:
    keys = (
        [k.strip() for k in conversation_keys if str(k).strip()]
        if conversation_keys is not None
        else fetch_tracked_conversation_keys(db_path)
    )
    if not keys:
        return {"ok": True, "summarized": 0, "skipped": 0, "errors": []}

    run_stamp = _run_stamp_utc()
    summarized = 0
    skipped = 0
    errors: List[Dict[str, Any]] = []

    for key in keys:
        try:
            result = summarize_one_slack_thread(
                db_path, key, force=force, run_stamp=run_stamp
            )
        except Exception as exc:
            log.exception("Slack summary failed for %s", key)
            errors.append({"conversation_key": key, "error": str(exc)})
            continue
        if result.get("skipped"):
            skipped += 1
        elif result.get("ok"):
            summarized += 1
        else:
            errors.append(result)

    return {
        "ok": not errors or summarized > 0,
        "summarized": summarized,
        "skipped": skipped,
        "errors": errors,
        "run_stamp": run_stamp,
    }
