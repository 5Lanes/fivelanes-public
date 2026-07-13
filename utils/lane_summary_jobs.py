"""Background lane (track) summary jobs and auto-refresh when child threads change."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

_lane_summary_lock = threading.Lock()
_lane_summary_jobs: Dict[int, Dict[str, Any]] = {}
# Only one lane summary worker runs at a time (thread refresh + lane rollup).
_lane_summary_worker_lock = threading.Lock()
_lane_summary_refresh_depth = threading.local()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _in_lane_summary_refresh() -> bool:
    return bool(getattr(_lane_summary_refresh_depth, "active", False))


def _set_lane_summary_refresh_active(active: bool) -> None:
    _lane_summary_refresh_depth.active = active


def lane_summary_has_content(payload: Dict[str, Any]) -> bool:
    if str(payload.get("summary") or "").strip():
        return True
    if str(payload.get("tone_overview") or "").strip():
        return True
    for key in ("highlights", "current_priorities", "waiting_on_others"):
        val = payload.get(key)
        if isinstance(val, list) and any(str(x).strip() for x in val):
            return True
    return False


def _lane_summary_is_fresh(payload: Dict[str, Any]) -> bool:
    from utils.summary_timeliness import lane_summary_is_stale

    return lane_summary_has_content(payload) and not lane_summary_is_stale(payload)


def _finalize_lane_summary_from_llm(
    result: Dict[str, Any], *, summaries: List[Dict[str, Any]] | None = None
) -> tuple[Dict[str, Any], Optional[str]]:
    from utils.database import normalize_lane_summary_payload

    api_error = str(result.get("api_error") or "").strip()
    summary = normalize_lane_summary_payload(result) if isinstance(result, dict) else {}
    if not lane_summary_has_content(summary):
        raw = str(result.get("raw_text") or "").strip()
        if raw:
            summary["summary"] = raw
        elif api_error:
            return {}, api_error
        return {}, "Lane summary model returned no usable content"
    from utils.summary_timeliness import drop_ungrounded_dates, reframe_summary_temporal_fields

    summary = reframe_summary_temporal_fields(summary)
    summary = drop_ungrounded_dates(summary, summaries or [])
    return summary, None


def lane_summary_job_snapshot(lane_id: int) -> Optional[Dict[str, Any]]:
    with _lane_summary_lock:
        job = _lane_summary_jobs.get(int(lane_id))
        return dict(job) if isinstance(job, dict) else None


def _set_lane_summary_job(lane_id: int, **fields: Any) -> None:
    with _lane_summary_lock:
        job = dict(_lane_summary_jobs.get(int(lane_id)) or {})
        job.update(fields)
        _lane_summary_jobs[int(lane_id)] = job


def _refresh_lane_thread_summaries(
    *,
    db_path: str,
    thread_ids: List[str],
    llm: Any,
) -> None:
    """Refresh stale lane thread summaries one at a time (never in parallel)."""
    from services.pipeline.process import force_resummarize_thread
    from services.pipeline.summary import thread_needs_summary
    from utils.database import load_processed_cleaned_for_thread

    generated_at = _utc_now_iso()
    backend = llm.name
    for tid in thread_ids:
        tid = tid.strip()
        if not tid:
            continue
        cleaned = load_processed_cleaned_for_thread(db_path, tid)
        if not cleaned:
            continue
        if not thread_needs_summary(db_path, tid, cleaned, force=False, backend=backend):
            continue
        force_resummarize_thread(
            db_path,
            tid,
            llm=llm,
            generated_at=generated_at,
            apply_to_outputs=True,
        )


def lane_summary_http_payload(
    *,
    lane_id: int,
    lane_name: str,
    summary: Dict[str, Any],
    cached: bool,
    updated_at: str,
) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "ok": True,
        "lane_id": lane_id,
        "lane_name": lane_name,
        "cached": cached,
        "summary_updated_at": updated_at,
        "pending": False,
    }
    out.update({k: v for k, v in summary.items() if k != "input_fingerprint"})
    return out


def _run_lane_summary_worker(
    *,
    db_path: str,
    lane_id: int,
    lane_name: str,
    thread_ids: List[str],
    summaries: List[Dict[str, Any]],
    input_fingerprint: str,
) -> None:
    from services.llm_service import get_llm_backend
    from services.prompts import format_lane_summary_prompt, summary_as_of_date
    from utils.database import load_lane_thread_summaries, save_lane_summary
    from utils.runtime_paths import env_file

    _set_lane_summary_job(
        lane_id,
        status="running",
        error=None,
        started_at=_utc_now_iso(),
        finished_at=None,
    )
    with _lane_summary_worker_lock:
        _set_lane_summary_refresh_active(True)
        try:
            llm = get_llm_backend(env_path=str(env_file()))
            _refresh_lane_thread_summaries(
                db_path=db_path,
                thread_ids=thread_ids,
                llm=llm,
            )
            _lane, summaries = load_lane_thread_summaries(db_path, lane_id=lane_id)
            if not summaries:
                raise RuntimeError("no_thread_summaries")
            from services.pipeline.summary import deterministic_calendar_only_lane_summary

            deterministic = deterministic_calendar_only_lane_summary(summaries)
            if deterministic is not None:
                summary, err = deterministic, None
            else:
                prompt = format_lane_summary_prompt(lane_name, summaries, db_path=db_path)
                result = llm.submit_lane_summary(prompt)
                summary, err = _finalize_lane_summary_from_llm(
                    result if isinstance(result, dict) else {}, summaries=summaries
                )
            if err:
                raise RuntimeError(err)
            summary["input_fingerprint"] = input_fingerprint
            summary["summary_as_of_date"] = summary_as_of_date()
            updated_at = save_lane_summary(db_path, lane_id=lane_id, summary=summary)
            _set_lane_summary_job(
                lane_id,
                status="done",
                error=None,
                finished_at=_utc_now_iso(),
                summary_updated_at=updated_at,
            )
            log.info("Lane summary finished for lane_id=%s (%s)", lane_id, lane_name)
        except Exception as exc:
            log.exception("Lane summary failed for lane_id=%s", lane_id)
            _set_lane_summary_job(
                lane_id,
                status="error",
                error=str(exc) or "lane_summary_failed",
                finished_at=_utc_now_iso(),
            )
        finally:
            _set_lane_summary_refresh_active(False)


def start_lane_summary_job(
    *,
    db_path: str,
    lane_id: int,
    lane_name: str,
    thread_ids: List[str],
    summaries: List[Dict[str, Any]],
    input_fingerprint: str,
    force: bool,
) -> Dict[str, Any]:
    from utils.database import load_lane_summary

    with _lane_summary_lock:
        job = _lane_summary_jobs.get(int(lane_id))
        if job and str(job.get("status") or "") == "running":
            return {"ok": True, "pending": True, "lane_id": lane_id, "lane_name": lane_name}

    if not force:
        cached = load_lane_summary(db_path, lane_id=lane_id)
        if (
            cached
            and str(cached.get("input_fingerprint") or "") == input_fingerprint
            and _lane_summary_is_fresh(cached)
        ):
            return lane_summary_http_payload(
                lane_id=lane_id,
                lane_name=lane_name,
                summary=cached,
                cached=True,
                updated_at=str(cached.get("updated_at") or ""),
            )

    _set_lane_summary_job(
        lane_id,
        status="running",
        error=None,
        started_at=_utc_now_iso(),
        finished_at=None,
    )
    threading.Thread(
        target=_run_lane_summary_worker,
        kwargs={
            "db_path": db_path,
            "lane_id": lane_id,
            "lane_name": lane_name,
            "thread_ids": thread_ids,
            "summaries": summaries,
            "input_fingerprint": input_fingerprint,
        },
        name=f"lane-summary-{lane_id}",
        daemon=True,
    ).start()
    return {"ok": True, "pending": True, "lane_id": lane_id, "lane_name": lane_name}


def schedule_lane_summaries_for_thread(db_path: str, thread_id: str, *, force: bool = False) -> None:
    """Enqueue lane roll-up refreshes for every track that contains ``thread_id``."""
    if _in_lane_summary_refresh():
        return
    tid = (thread_id or "").strip()
    if not tid:
        return
    from services.llm_service import get_llm_backend
    from services.pipeline.fingerprint import lane_summary_fingerprint
    from utils.database import aggregate_thread_chronological_anchor, lane_ids_for_thread, load_lane_thread_summaries
    from utils.runtime_paths import env_file

    lane_ids = lane_ids_for_thread(db_path, tid)
    if not lane_ids:
        return

    llm = get_llm_backend(env_path=str(env_file()))
    for lane_id in lane_ids:
        lane, summaries = load_lane_thread_summaries(db_path, lane_id=lane_id)
        if not lane or not summaries:
            continue
        name = str(lane.get("name") or "").strip()
        thread_ids = [str(s.get("thread_id") or "").strip() for s in summaries]
        summary_datetimes = [
            aggregate_thread_chronological_anchor(db_path, s) for s in summaries
        ]
        fp = lane_summary_fingerprint(
            lane_id=lane_id,
            thread_ids=thread_ids,
            summary_datetimes=summary_datetimes,
            backend=llm.name,
        )
        try:
            start_lane_summary_job(
                db_path=db_path,
                lane_id=lane_id,
                lane_name=name,
                thread_ids=thread_ids,
                summaries=summaries,
                input_fingerprint=fp,
                force=force,
            )
        except Exception:
            log.exception("Failed to schedule lane summary for lane_id=%s", lane_id)
