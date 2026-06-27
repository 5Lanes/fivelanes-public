import email.utils
import json
import logging
import os
import socket
import threading
import urllib.parse
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent
try:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

from utils.runtime_paths import data_path, database_path, env_file, infra_root, load_env

load_env()

from utils.backend_config import apply_backend, get_backend, set_backend

apply_backend(os.getenv("FIVELANES_BACKEND") or "llama")

# Bind address/port for dashboard_server.py (0.0.0.0 = reachable on the LAN).
DASHBOARD_HOST = (os.getenv("DASHBOARD_HOST") or "0.0.0.0").strip() or "0.0.0.0"
DASHBOARD_PORT = int((os.getenv("DASHBOARD_PORT") or "8000").strip() or "8000")

from services.llm_service import get_llm_backend
from services.pipeline.fingerprint import (
    email_reply_fingerprint,
    lane_summary_fingerprint,
    meeting_prep_fingerprint,
    messages_cache_keys,
)
from services.prompts import (
    EMAIL_REPLY_MAX_MESSAGES,
    format_email_reply_prompt,
    format_lane_summary_prompt,
    format_meeting_prep_prompt,
)
from utils.database import (
    _meeting_dedupe_key,
    add_thread_to_lane,
    aggregate_thread_chronological_anchor,
    build_summaries_bundle,
    build_thread_draft_payload,
    create_lane,
    create_thread_plan,
    delete_lane,
    delete_thread_plan,
    update_thread_plan,
    fetch_meetings_rows,
    load_lane_summary,
    load_lane_thread_summaries,
    load_meeting_prep,
    load_thread_draft_reply,
    normalize_lane_summary_payload,
    normalize_meeting_prep_payload,
    remove_thread_from_lane,
    save_lane_summary,
    save_meeting_prep,
    save_thread_draft_reply,
)
from services.thread_snooze import remove_thread_tracking, set_thread_snooze
from utils.logging import configure_logging
from services.slack import (
    SLACK_DMS_DIR,
    fetch_tracked_conversation_keys as fetch_tracked_slack_keys,
    list_conversation_catalog as list_slack_catalog,
    pull_slack_dms,
    set_tracked_conversation_keys as set_tracked_slack_keys,
)
from services.slack.summarize import summarize_tracked_slack_threads
from services.texts import (
    CONVERSATIONS_DIR,
    fetch_tracked_conversation_keys,
    list_conversation_catalog,
    set_tracked_conversation_keys,
)
from services.texts.summarize import summarize_tracked_text_threads
from utils.run_fivelanes_scheduler import (
    pipeline_run_in_progress,
    run_fivelanes_cycle,
    scheduler_loop,
)


DB_PATH = database_path()

_pipeline_lock = threading.Lock()
_pipeline_state: Dict[str, Any] = {
    "running": False,
    "error": None,
    "started_at": None,
    "finished_at": None,
}

_lane_summary_lock = threading.Lock()
_lane_summary_jobs: Dict[int, Dict[str, Any]] = {}


def _lane_summary_has_content(payload: Dict[str, Any]) -> bool:
    if str(payload.get("summary") or "").strip():
        return True
    if str(payload.get("tone_overview") or "").strip():
        return True
    for key in ("highlights", "current_priorities", "waiting_on_others"):
        val = payload.get(key)
        if isinstance(val, list) and any(str(x).strip() for x in val):
            return True
    return False


def _finalize_lane_summary_from_llm(result: Dict[str, Any]) -> tuple[Dict[str, Any], Optional[str]]:
    api_error = str(result.get("api_error") or "").strip()
    summary = normalize_lane_summary_payload(result) if isinstance(result, dict) else {}
    if not _lane_summary_has_content(summary):
        raw = str(result.get("raw_text") or "").strip()
        if raw:
            summary["summary"] = raw
        elif api_error:
            return {}, api_error
        else:
            return {}, "Lane summary model returned no usable content"
    return summary, None


def _lane_summary_job_snapshot(lane_id: int) -> Optional[Dict[str, Any]]:
    with _lane_summary_lock:
        job = _lane_summary_jobs.get(int(lane_id))
        return dict(job) if isinstance(job, dict) else None


def _set_lane_summary_job(lane_id: int, **fields: Any) -> None:
    with _lane_summary_lock:
        job = dict(_lane_summary_jobs.get(int(lane_id)) or {})
        job.update(fields)
        _lane_summary_jobs[int(lane_id)] = job


def _lane_summary_http_payload(
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
    lane_id: int,
    lane_name: str,
    summaries: List[Dict[str, Any]],
    input_fingerprint: str,
) -> None:
    _set_lane_summary_job(
        lane_id,
        status="running",
        error=None,
        started_at=_utc_now_iso(),
        finished_at=None,
    )
    try:
        llm = get_llm_backend(env_path=str(env_file()))
        prompt = format_lane_summary_prompt(lane_name, summaries, db_path=DB_PATH)
        result = llm.submit_lane_summary(prompt)
        summary, err = _finalize_lane_summary_from_llm(result if isinstance(result, dict) else {})
        if err:
            raise RuntimeError(err)
        summary["input_fingerprint"] = input_fingerprint
        updated_at = save_lane_summary(DB_PATH, lane_id=lane_id, summary=summary)
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


def _start_lane_summary_job(
    *,
    lane_id: int,
    lane_name: str,
    summaries: List[Dict[str, Any]],
    input_fingerprint: str,
    force: bool,
) -> Dict[str, Any]:
    with _lane_summary_lock:
        job = _lane_summary_jobs.get(int(lane_id))
        if job and str(job.get("status") or "") == "running":
            return {"ok": True, "pending": True, "lane_id": lane_id, "lane_name": lane_name}

    if not force:
        cached = load_lane_summary(DB_PATH, lane_id=lane_id)
        if (
            cached
            and str(cached.get("input_fingerprint") or "") == input_fingerprint
            and _lane_summary_has_content(cached)
        ):
            return _lane_summary_http_payload(
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
            "lane_id": lane_id,
            "lane_name": lane_name,
            "summaries": summaries,
            "input_fingerprint": input_fingerprint,
        },
        name=f"lane-summary-{lane_id}",
        daemon=True,
    ).start()
    return {"ok": True, "pending": True, "lane_id": lane_id, "lane_name": lane_name}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _start_pipeline_run() -> tuple[bool, Optional[str]]:
    with _pipeline_lock:
        if _pipeline_state["running"]:
            return False, "already_running"
        _pipeline_state["running"] = True
        _pipeline_state["error"] = None
        _pipeline_state["started_at"] = _utc_now_iso()
        _pipeline_state["finished_at"] = None

    def _worker() -> None:
        try:
            apply_backend(get_backend())
            run_fivelanes_cycle(trigger="manual", blocking=True)
        except Exception as exc:
            log.exception("Manual fivelanes run failed")
            with _pipeline_lock:
                _pipeline_state["error"] = str(exc)
        finally:
            with _pipeline_lock:
                _pipeline_state["running"] = False
                _pipeline_state["finished_at"] = _utc_now_iso()

    threading.Thread(
        target=_worker,
        name="fivelanes-manual-run",
        daemon=True,
    ).start()
    return True, None


def _pipeline_status_payload() -> Dict[str, Any]:
    from utils.pipeline_run_log import load_last_pipeline_run

    with _pipeline_lock:
        manual = dict(_pipeline_state)
    last_run = load_last_pipeline_run()
    running = pipeline_run_in_progress()
    payload: Dict[str, Any] = {
        "ok": True,
        "running": running,
        "error": manual.get("error"),
        "started_at": manual.get("started_at"),
        "finished_at": manual.get("finished_at"),
        "backend": get_backend(),
    }
    if running and last_run and str(last_run.get("status") or "") == "running":
        payload["started_at"] = last_run.get("started_at") or payload["started_at"]
        payload["error"] = last_run.get("error") or payload["error"]
    payload["last_run"] = last_run
    return payload


def _db_cache_etag(db_path: str) -> Tuple[str, str]:
    st = Path(db_path).stat()
    etag = f'"{int(st.st_mtime_ns)}-{st.st_size}"'
    last_modified = email.utils.formatdate(st.st_mtime, usegmt=True)
    return etag, last_modified


class DashboardHandler(SimpleHTTPRequestHandler):
    def translate_path(self, path: str) -> str:
        clean = path.split("?", 1)[0]
        if clean.startswith("/out/"):
            rel = clean[len("/out/") :].lstrip("/")
            return str(data_path("out", rel))
        return super().translate_path(path)

    def _send_cache_headers(self, etag: str, last_modified: str) -> None:
        self.send_header("ETag", etag)
        self.send_header("Last-Modified", last_modified)
        self.send_header("Cache-Control", "private, must-revalidate")

    def _not_modified(self, etag: str, last_modified: str) -> None:
        self.send_response(HTTPStatus.NOT_MODIFIED)
        self._send_cache_headers(etag, last_modified)
        self.end_headers()

    def _json_response(
        self,
        status: int,
        payload: Dict[str, Any],
        *,
        etag: Optional[str] = None,
        last_modified: Optional[str] = None,
    ) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if etag and last_modified:
            self._send_cache_headers(etag, last_modified)
        self.end_headers()
        self.wfile.write(body)

    def _get_summaries_bundle(self) -> None:
        db_file = Path(DB_PATH)
        if not db_file.is_file():
            self._json_response(
                HTTPStatus.NOT_FOUND, {"ok": False, "error": "database_not_found"}
            )
            return
        try:
            etag, last_modified = _db_cache_etag(DB_PATH)
        except OSError:
            self._json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": "database_stat_failed"},
            )
            return
        inm = (self.headers.get("If-None-Match") or "").strip()
        if inm == etag:
            self._not_modified(etag, last_modified)
            return
        try:
            bundle = build_summaries_bundle(DB_PATH)
        except Exception as exc:
            log.exception("summaries bundle build failed")
            self._json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": str(exc)},
            )
            return
        if not bundle.get("cleaned") and not bundle.get("summary"):
            self._json_response(
                HTTPStatus.NOT_FOUND,
                {"ok": False, "error": "no_summary_rows"},
                etag=etag,
                last_modified=last_modified,
            )
            return
        self._json_response(
            HTTPStatus.OK, bundle, etag=etag, last_modified=last_modified
        )

    def _post_texts_summarize(self, body: Dict[str, Any]) -> None:
        if not Path(DB_PATH).is_file():
            self._json_response(
                HTTPStatus.NOT_FOUND, {"ok": False, "error": "database_not_found"}
            )
            return
        raw = body.get("conversation_keys")
        force = bool(body.get("force"))
        try:
            result = summarize_tracked_text_threads(
                DB_PATH,
                conversation_keys=raw if isinstance(raw, list) else None,
                force=force,
            )
        except Exception as exc:
            log.exception("texts summarize failed")
            self._json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": str(exc)},
            )
            return
        self._json_response(HTTPStatus.OK, result)

    def _get_texts_catalog(self) -> None:
        catalog = list_conversation_catalog()
        tracked = fetch_tracked_conversation_keys(DB_PATH) if Path(DB_PATH).is_file() else []
        self._json_response(
            HTTPStatus.OK,
            {
                "ok": True,
                "conversations_dir": str(CONVERSATIONS_DIR),
                "catalog": catalog,
                "tracked": tracked,
            },
        )

    def _post_texts_track(self, body: Dict[str, Any]) -> None:
        if not Path(DB_PATH).is_file():
            self._json_response(
                HTTPStatus.NOT_FOUND, {"ok": False, "error": "database_not_found"}
            )
            return
        raw = body.get("conversation_keys")
        if raw is None:
            raw = body.get("tracked")
        if not isinstance(raw, list):
            self._json_response(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "missing_conversation_keys"},
            )
            return
        try:
            result = set_tracked_conversation_keys(DB_PATH, raw)
        except Exception as exc:
            log.exception("texts track failed")
            self._json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": str(exc)},
            )
            return

        keys = result.get("tracked") if isinstance(result.get("tracked"), list) else raw

        def _summarize_worker() -> None:
            try:
                summarize_tracked_text_threads(
                    DB_PATH, conversation_keys=keys, force=True
                )
            except Exception:
                log.exception("Background text summarization failed")

        threading.Thread(
            target=_summarize_worker,
            name="texts-summarize",
            daemon=True,
        ).start()
        result["summarize"] = "started"
        self._json_response(HTTPStatus.OK, result)

    def _post_slack_pull(self) -> None:
        try:
            result = pull_slack_dms()
        except Exception as exc:
            log.exception("slack pull failed")
            self._json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": str(exc)},
            )
            return
        self._json_response(HTTPStatus.OK, result)

    def _post_slack_summarize(self, body: Dict[str, Any]) -> None:
        if not Path(DB_PATH).is_file():
            self._json_response(
                HTTPStatus.NOT_FOUND, {"ok": False, "error": "database_not_found"}
            )
            return
        raw = body.get("conversation_keys")
        force = bool(body.get("force"))
        try:
            result = summarize_tracked_slack_threads(
                DB_PATH,
                conversation_keys=raw if isinstance(raw, list) else None,
                force=force,
            )
        except Exception as exc:
            log.exception("slack summarize failed")
            self._json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": str(exc)},
            )
            return
        self._json_response(HTTPStatus.OK, result)

    def _get_slack_catalog(self) -> None:
        catalog = list_slack_catalog()
        tracked = fetch_tracked_slack_keys(DB_PATH) if Path(DB_PATH).is_file() else []
        self._json_response(
            HTTPStatus.OK,
            {
                "ok": True,
                "slack_dms_dir": str(SLACK_DMS_DIR),
                "catalog": catalog,
                "tracked": tracked,
            },
        )

    def _post_slack_track(self, body: Dict[str, Any]) -> None:
        if not Path(DB_PATH).is_file():
            self._json_response(
                HTTPStatus.NOT_FOUND, {"ok": False, "error": "database_not_found"}
            )
            return
        raw = body.get("conversation_keys")
        if raw is None:
            raw = body.get("tracked")
        if not isinstance(raw, list):
            self._json_response(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "missing_conversation_keys"},
            )
            return
        try:
            result = set_tracked_slack_keys(DB_PATH, raw)
        except Exception as exc:
            log.exception("slack track failed")
            self._json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": str(exc)},
            )
            return

        keys = result.get("tracked") if isinstance(result.get("tracked"), list) else raw

        def _summarize_worker() -> None:
            try:
                summarize_tracked_slack_threads(
                    DB_PATH, conversation_keys=keys, force=True
                )
            except Exception:
                log.exception("Background Slack summarization failed")

        threading.Thread(
            target=_summarize_worker,
            name="slack-summarize",
            daemon=True,
        ).start()
        result["summarize"] = "started"
        self._json_response(HTTPStatus.OK, result)

    def _get_meetings(self) -> None:
        db_file = Path(DB_PATH)
        if not db_file.is_file():
            self._json_response(
                HTTPStatus.NOT_FOUND, {"ok": False, "error": "database_not_found"}
            )
            return
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        days: Optional[int] = None
        days_raw = (qs.get("days") or [None])[0]
        if days_raw is not None:
            try:
                days = max(1, min(90, int(str(days_raw).strip())))
            except (TypeError, ValueError):
                days = None
        try:
            etag, last_modified = _db_cache_etag(DB_PATH)
        except OSError:
            self._json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": "database_stat_failed"},
            )
            return
        inm = (self.headers.get("If-None-Match") or "").strip()
        if inm == etag:
            self._not_modified(etag, last_modified)
            return
        try:
            rows = fetch_meetings_rows(DB_PATH, days=days)
        except Exception as exc:
            log.exception("meetings fetch failed")
            self._json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": str(exc)},
            )
            return
        exported_at = ""
        timezone = ""
        meetings: List[Dict[str, Any]] = []
        for row in rows:
            if not exported_at and row.get("exported_at"):
                exported_at = str(row["exported_at"])
            if not timezone and row.get("timezone"):
                timezone = str(row["timezone"])
            meetings.append(
                {
                    "summary": row.get("summary") or "",
                    "start_iso": row.get("start_iso") or "",
                    "end_iso": row.get("end_iso") or "",
                    "location": row.get("location") or "",
                    "html_link": row.get("html_link") or "",
                    "attendees": row.get("attendees") or [],
                }
            )
        self._json_response(
            HTTPStatus.OK,
            {
                "ok": True,
                "meetings": meetings,
                "exported_at": exported_at,
                "timezone": timezone,
            },
            etag=etag,
            last_modified=last_modified,
        )

    def _get_timeline_db(self) -> None:
        db_file = Path(DB_PATH)
        if not db_file.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "timeline.db not found")
            return
        try:
            etag, last_modified = _db_cache_etag(DB_PATH)
        except OSError:
            self.send_error(
                HTTPStatus.INTERNAL_SERVER_ERROR, "Could not read timeline.db"
            )
            return
        inm = (self.headers.get("If-None-Match") or "").strip()
        if inm == etag:
            self._not_modified(etag, last_modified)
            return
        try:
            data = db_file.read_bytes()
        except OSError:
            self.send_error(
                HTTPStatus.INTERNAL_SERVER_ERROR, "Could not read timeline.db"
            )
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/x-sqlite3")
        self.send_header("Content-Length", str(len(data)))
        self._send_cache_headers(etag, last_modified)
        self.end_headers()
        self.wfile.write(data)

    def _read_post_json(self) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        length_raw = self.headers.get("Content-Length") or "0"
        try:
            length = int(length_raw)
        except ValueError:
            return None, "invalid_content_length"
        raw = self.rfile.read(max(0, length))
        try:
            return json.loads(raw.decode("utf-8") or "{}"), None
        except json.JSONDecodeError:
            return None, "invalid_json"

    def _post_snooze(self, body: Dict[str, Any]) -> None:
        thread_id = str(body.get("thread_id") or body.get("inbox_thread_id") or "").strip()
        snoozed = int(body.get("snoozed") or 0)
        if not thread_id:
            self._json_response(
                HTTPStatus.BAD_REQUEST, {"ok": False, "error": "missing_thread_id"}
            )
            return
        if not set_thread_snooze(DB_PATH, thread_id, snoozed):
            self._json_response(
                HTTPStatus.NOT_FOUND,
                {"ok": False, "error": "thread_not_found", "inbox_thread_id": thread_id},
            )
            return
        self._json_response(HTTPStatus.OK, {"ok": True, "inbox_thread_id": thread_id})

    def _post_remove_tracking(self, body: Dict[str, Any]) -> None:
        thread_id = str(body.get("thread_id") or body.get("inbox_thread_id") or "").strip()
        if not thread_id:
            self._json_response(
                HTTPStatus.BAD_REQUEST, {"ok": False, "error": "missing_thread_id"}
            )
            return
        if not remove_thread_tracking(DB_PATH, thread_id):
            self._json_response(
                HTTPStatus.NOT_FOUND,
                {"ok": False, "error": "thread_not_found", "inbox_thread_id": thread_id},
            )
            return
        self._json_response(
            HTTPStatus.OK,
            {"ok": True, "inbox_thread_id": thread_id, "snoozed": 2},
        )

    def _post_thread_summary(self, body: Dict[str, Any]) -> None:
        thread_id = str(body.get("thread_id") or body.get("inbox_thread_id") or "").strip()
        if not thread_id:
            self._json_response(
                HTTPStatus.BAD_REQUEST, {"ok": False, "error": "missing_thread_id"}
            )
            return
        if not Path(DB_PATH).is_file():
            self._json_response(
                HTTPStatus.NOT_FOUND, {"ok": False, "error": "database_not_found"}
            )
            return
        try:
            from utils.resummary_active_threads import resummary_single_thread

            result = resummary_single_thread(db_path=DB_PATH, thread_id=thread_id)
        except RuntimeError as exc:
            self._json_response(
                HTTPStatus.BAD_GATEWAY, {"ok": False, "error": str(exc)}
            )
            return
        except Exception as exc:
            log.exception("thread summary refresh failed")
            self._json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)}
            )
            return
        if not result.get("ok"):
            err = str(result.get("error") or "thread_summary_failed")
            status = HTTPStatus.BAD_REQUEST
            if err == "no_cleaned_messages":
                status = HTTPStatus.BAD_REQUEST
            self._json_response(status, result)
            return
        self._json_response(HTTPStatus.OK, result)

    def _post_lane_create(self, body: Dict[str, Any]) -> None:
        name = str(body.get("name") or "").strip()
        if not name:
            self._json_response(
                HTTPStatus.BAD_REQUEST, {"ok": False, "error": "missing_lane_name"}
            )
            return
        try:
            lane = create_lane(DB_PATH, name=name)
        except ValueError as exc:
            self._json_response(
                HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)}
            )
            return
        except Exception as exc:
            log.exception("lane create failed")
            self._json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)}
            )
            return
        self._json_response(HTTPStatus.OK, {"ok": True, "lane": lane})

    def _post_lane_add_thread(self, body: Dict[str, Any]) -> None:
        try:
            lane_id = int(body.get("lane_id") or 0)
        except (TypeError, ValueError):
            lane_id = 0
        thread_id = str(body.get("thread_id") or body.get("inbox_thread_id") or "").strip()
        if lane_id <= 0:
            self._json_response(
                HTTPStatus.BAD_REQUEST, {"ok": False, "error": "missing_lane_id"}
            )
            return
        if not thread_id:
            self._json_response(
                HTTPStatus.BAD_REQUEST, {"ok": False, "error": "missing_thread_id"}
            )
            return
        ok = add_thread_to_lane(DB_PATH, lane_id=lane_id, inbox_thread_id=thread_id)
        if not ok:
            self._json_response(
                HTTPStatus.NOT_FOUND, {"ok": False, "error": "lane_not_found"}
            )
            return
        self._json_response(
            HTTPStatus.OK,
            {"ok": True, "lane_id": lane_id, "inbox_thread_id": thread_id},
        )

    def _post_lane_remove_thread(self, body: Dict[str, Any]) -> None:
        try:
            lane_id = int(body.get("lane_id") or 0)
        except (TypeError, ValueError):
            lane_id = 0
        thread_id = str(body.get("thread_id") or body.get("inbox_thread_id") or "").strip()
        if lane_id <= 0:
            self._json_response(
                HTTPStatus.BAD_REQUEST, {"ok": False, "error": "missing_lane_id"}
            )
            return
        if not thread_id:
            self._json_response(
                HTTPStatus.BAD_REQUEST, {"ok": False, "error": "missing_thread_id"}
            )
            return
        ok = remove_thread_from_lane(
            DB_PATH, lane_id=lane_id, inbox_thread_id=thread_id
        )
        if not ok:
            self._json_response(
                HTTPStatus.NOT_FOUND, {"ok": False, "error": "lane_not_found"}
            )
            return
        self._json_response(
            HTTPStatus.OK,
            {"ok": True, "lane_id": lane_id, "inbox_thread_id": thread_id},
        )

    def _post_lane_delete(self, body: Dict[str, Any]) -> None:
        try:
            lane_id = int(body.get("lane_id") or 0)
        except (TypeError, ValueError):
            lane_id = 0
        if lane_id <= 0:
            self._json_response(
                HTTPStatus.BAD_REQUEST, {"ok": False, "error": "missing_lane_id"}
            )
            return
        ok = delete_lane(DB_PATH, lane_id=lane_id)
        if not ok:
            self._json_response(
                HTTPStatus.NOT_FOUND, {"ok": False, "error": "lane_not_found"}
            )
            return
        self._json_response(HTTPStatus.OK, {"ok": True, "lane_id": lane_id})

    def _get_lane_summary(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        try:
            lane_id = int(str((qs.get("lane_id") or ["0"])[0]).strip())
        except (TypeError, ValueError):
            lane_id = 0
        if lane_id <= 0:
            self._json_response(
                HTTPStatus.BAD_REQUEST, {"ok": False, "error": "missing_lane_id"}
            )
            return

        lane, _summaries = load_lane_thread_summaries(DB_PATH, lane_id=lane_id)
        if not lane:
            self._json_response(
                HTTPStatus.NOT_FOUND, {"ok": False, "error": "lane_not_found"}
            )
            return
        lid = int(lane.get("id") or 0)
        name = str(lane.get("name") or "").strip()

        job = _lane_summary_job_snapshot(lid)
        if job and str(job.get("status") or "") == "running":
            self._json_response(
                HTTPStatus.OK,
                {"ok": True, "pending": True, "lane_id": lid, "lane_name": name},
            )
            return
        if job and str(job.get("status") or "") == "error":
            self._json_response(
                HTTPStatus.OK,
                {
                    "ok": False,
                    "lane_id": lid,
                    "lane_name": name,
                    "error": str(job.get("error") or "lane_summary_failed"),
                },
            )
            return

        cached = load_lane_summary(DB_PATH, lane_id=lid)
        if cached and _lane_summary_has_content(cached):
            self._json_response(
                HTTPStatus.OK,
                _lane_summary_http_payload(
                    lane_id=lid,
                    lane_name=name,
                    summary=cached,
                    cached=True,
                    updated_at=str(cached.get("updated_at") or ""),
                ),
            )
            return

        self._json_response(
            HTTPStatus.OK,
            {"ok": True, "pending": False, "lane_id": lid, "lane_name": name},
        )

    def _post_lane_summary(self, body: Dict[str, Any]) -> None:
        try:
            lane_id = int(body.get("lane_id") or 0)
        except (TypeError, ValueError):
            lane_id = 0
        lane_name = str(body.get("lane_name") or "").strip()
        force = bool(body.get("force"))

        lane, summaries = load_lane_thread_summaries(
            DB_PATH,
            lane_name=lane_name or None,
            lane_id=lane_id if lane_id > 0 else None,
        )
        if not lane:
            self._json_response(
                HTTPStatus.NOT_FOUND, {"ok": False, "error": "lane_not_found"}
            )
            return
        lid = int(lane.get("id") or 0)
        name = str(lane.get("name") or "").strip()
        if not summaries:
            self._json_response(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "no_thread_summaries", "lane_id": lid},
            )
            return

        llm = get_llm_backend(env_path=str(env_file()))
        thread_ids = [str(s.get("thread_id") or "").strip() for s in summaries]
        summary_datetimes = [
            aggregate_thread_chronological_anchor(DB_PATH, s) for s in summaries
        ]
        fp = lane_summary_fingerprint(
            lane_id=lid,
            thread_ids=thread_ids,
            summary_datetimes=summary_datetimes,
            backend=llm.name,
        )
        try:
            out = _start_lane_summary_job(
                lane_id=lid,
                lane_name=name,
                summaries=summaries,
                input_fingerprint=fp,
                force=force,
            )
        except ValueError as exc:
            self._json_response(
                HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc) or "bad_request"}
            )
            return
        except Exception as exc:
            log.exception("lane summary start failed")
            self._json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)}
            )
            return
        self._json_response(HTTPStatus.OK, out)

    def _post_plan_create(self, body: Dict[str, Any]) -> None:
        thread_id = str(body.get("thread_id") or body.get("inbox_thread_id") or "").strip()
        action = str(body.get("action") or "").strip()
        step_type = str(body.get("step_type") or "follow up needed").strip()
        by_when = str(body.get("by_when") or "").strip()
        if not thread_id:
            self._json_response(
                HTTPStatus.BAD_REQUEST, {"ok": False, "error": "missing_thread_id"}
            )
            return
        if not action:
            self._json_response(
                HTTPStatus.BAD_REQUEST, {"ok": False, "error": "missing_plan_action"}
            )
            return
        try:
            plan = create_thread_plan(
                DB_PATH,
                inbox_thread_id=thread_id,
                action=action,
                step_type=step_type,
                by_when=by_when,
            )
        except ValueError as exc:
            self._json_response(
                HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)}
            )
            return
        except Exception as exc:
            log.exception("plan create failed")
            self._json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)}
            )
            return
        self._json_response(HTTPStatus.OK, {"ok": True, "plan": plan})

    def _post_plan_update(self, body: Dict[str, Any]) -> None:
        try:
            plan_id = int(body.get("plan_id") or body.get("id") or 0)
        except (TypeError, ValueError):
            plan_id = 0
        if plan_id <= 0:
            self._json_response(
                HTTPStatus.BAD_REQUEST, {"ok": False, "error": "missing_plan_id"}
            )
            return
        thread_id = body.get("thread_id")
        if thread_id is None:
            thread_id = body.get("inbox_thread_id")
        thread_id = str(thread_id).strip() if thread_id is not None else None
        action = body.get("action")
        action = str(action).strip() if action is not None else None
        step_type = body.get("step_type")
        step_type = str(step_type).strip() if step_type is not None else None
        by_when = body.get("by_when")
        by_when = str(by_when).strip() if by_when is not None else None
        if action is not None and not action:
            self._json_response(
                HTTPStatus.BAD_REQUEST, {"ok": False, "error": "missing_plan_action"}
            )
            return
        if thread_id is not None and not thread_id:
            self._json_response(
                HTTPStatus.BAD_REQUEST, {"ok": False, "error": "missing_thread_id"}
            )
            return
        try:
            plan = update_thread_plan(
                DB_PATH,
                plan_id=plan_id,
                inbox_thread_id=thread_id,
                action=action,
                step_type=step_type,
                by_when=by_when,
            )
        except ValueError as exc:
            self._json_response(
                HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)}
            )
            return
        except Exception as exc:
            log.exception("plan update failed")
            self._json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)}
            )
            return
        if not plan:
            self._json_response(
                HTTPStatus.NOT_FOUND, {"ok": False, "error": "plan_not_found"}
            )
            return
        self._json_response(HTTPStatus.OK, {"ok": True, "plan": plan})

    def _post_plan_delete(self, body: Dict[str, Any]) -> None:
        try:
            plan_id = int(body.get("plan_id") or body.get("id") or 0)
        except (TypeError, ValueError):
            plan_id = 0
        if plan_id <= 0:
            self._json_response(
                HTTPStatus.BAD_REQUEST, {"ok": False, "error": "missing_plan_id"}
            )
            return
        ok = delete_thread_plan(DB_PATH, plan_id=plan_id)
        if not ok:
            self._json_response(
                HTTPStatus.NOT_FOUND, {"ok": False, "error": "plan_not_found"}
            )
            return
        self._json_response(HTTPStatus.OK, {"ok": True, "plan_id": plan_id})

    def _post_email_reply(self, body: Dict[str, Any]) -> None:
        thread_id = str(body.get("thread_id") or body.get("inbox_thread_id") or "").strip()
        if not thread_id:
            self._json_response(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "missing_thread_id"},
            )
            return
        intent = str(body.get("response_intent") or "").strip()
        if not intent:
            self._json_response(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "missing_response_intent"},
            )
            return
        raw_messages = body.get("messages")
        if not isinstance(raw_messages, list):
            self._json_response(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "invalid_messages"},
            )
            return
        messages: List[Dict[str, str]] = []
        for item in raw_messages:
            if not isinstance(item, dict):
                continue
            messages.append(
                {
                    "datetime": str(item.get("datetime") or item.get("timestamp") or ""),
                    "sender": str(item.get("sender") or item.get("from") or ""),
                    "recipients": str(item.get("recipients") or ""),
                    "subject": str(item.get("subject") or ""),
                    "content": str(item.get("content") or ""),
                }
            )
        thread_subject = str(body.get("thread_subject") or "").strip()
        force = bool(body.get("force"))
        llm = get_llm_backend(env_path=str(env_file()))
        msg_keys = messages_cache_keys(messages, max_messages=EMAIL_REPLY_MAX_MESSAGES)
        fp = email_reply_fingerprint(
            thread_id=thread_id,
            response_intent=intent,
            source_ids=msg_keys,
            backend=llm.name,
        )
        if not force:
            cached = load_thread_draft_reply(DB_PATH, thread_id=thread_id)
            if cached and str(cached.get("input_fingerprint") or "") == fp:
                out: Dict[str, Any] = {
                    "ok": True,
                    "thread_id": thread_id,
                    "cached": True,
                    "draft_updated_at": cached.get("saved_at") or cached.get("updated_at"),
                }
                out.update({k: v for k, v in cached.items() if k != "input_fingerprint"})
                self._json_response(HTTPStatus.OK, out)
                return
        try:
            prompt = format_email_reply_prompt(
                messages,
                intent,
                thread_subject=thread_subject,
            )
            result = llm.submit_email_reply(prompt)
        except ValueError as exc:
            self._json_response(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": str(exc) or "bad_request"},
            )
            return
        except RuntimeError as exc:
            self._json_response(
                HTTPStatus.BAD_GATEWAY,
                {"ok": False, "error": str(exc)},
            )
            return
        except Exception as exc:
            self._json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": str(exc)},
            )
            return
        reply = result if isinstance(result, dict) else {}
        try:
            draft = build_thread_draft_payload(response_intent=intent, result=reply)
            draft["input_fingerprint"] = fp
            updated_at = save_thread_draft_reply(
                DB_PATH, thread_id=thread_id, draft=draft
            )
        except ValueError as exc:
            self._json_response(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": str(exc) or "bad_request"},
            )
            return
        except Exception as exc:
            self._json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": str(exc)},
            )
            return
        out: Dict[str, Any] = {
            "ok": True,
            "thread_id": thread_id,
            "draft_updated_at": updated_at,
        }
        out.update(reply)
        out.update(draft)
        self._json_response(HTTPStatus.OK, out)

    def _post_meeting_prep(self, body: Dict[str, Any]) -> None:
        thread_id = str(body.get("thread_id") or "").strip()
        if not thread_id:
            self._json_response(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "missing_thread_id"},
            )
            return
        raw_messages = body.get("messages")
        if not isinstance(raw_messages, list):
            self._json_response(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "invalid_messages"},
            )
            return
        messages: List[Dict[str, str]] = []
        for item in raw_messages:
            if not isinstance(item, dict):
                continue
            messages.append(
                {
                    "datetime": str(item.get("datetime") or item.get("timestamp") or ""),
                    "sender": str(item.get("sender") or item.get("from") or ""),
                    "recipients": str(item.get("recipients") or ""),
                    "subject": str(item.get("subject") or ""),
                    "content": str(item.get("content") or ""),
                }
            )
        if not messages:
            self._json_response(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "empty_messages"},
            )
            return
        force = bool(body.get("force"))
        llm = get_llm_backend(env_path=str(env_file()))
        dedupe_key = _meeting_dedupe_key(
            str(body.get("meeting_title") or body.get("summary") or "(No title)").strip()
            or "(No title)",
            str(body.get("meeting_start") or body.get("start_iso") or "").strip(),
            str(body.get("meeting_end") or body.get("end_iso") or "").strip(),
        )
        event_fields = {
            "title": str(body.get("meeting_title") or body.get("summary") or ""),
            "start": str(body.get("meeting_start") or body.get("start_iso") or ""),
            "end": str(body.get("meeting_end") or body.get("end_iso") or ""),
            "location": str(body.get("meeting_location") or body.get("location") or ""),
            "attendees": str(body.get("meeting_attendees") or ""),
        }
        msg_keys = messages_cache_keys(messages, max_messages=10)
        fp = meeting_prep_fingerprint(
            dedupe_key=dedupe_key,
            thread_id=thread_id,
            source_ids=msg_keys,
            event_fields=event_fields,
            backend=llm.name,
        )
        if not force:
            cached = load_meeting_prep(DB_PATH, dedupe_key=dedupe_key, thread_id=thread_id)
            if cached and str(cached.get("input_fingerprint") or "") == fp:
                out: Dict[str, Any] = {
                    "ok": True,
                    "thread_id": thread_id,
                    "dedupe_key": dedupe_key,
                    "cached": True,
                    "prep_updated_at": cached.get("updated_at"),
                }
                out.update({k: v for k, v in cached.items() if k != "input_fingerprint"})
                self._json_response(HTTPStatus.OK, out)
                return
        try:
            prompt = format_meeting_prep_prompt(
                messages,
                meeting_title=event_fields["title"],
                meeting_start=event_fields["start"],
                meeting_end=event_fields["end"],
                meeting_location=event_fields["location"],
                meeting_attendees=event_fields["attendees"],
                thread_label=str(body.get("thread_label") or ""),
            )
            result = llm.submit_meeting_prep(prompt)
        except ValueError as exc:
            self._json_response(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": str(exc) or "bad_request"},
            )
            return
        except RuntimeError as exc:
            self._json_response(
                HTTPStatus.BAD_GATEWAY,
                {"ok": False, "error": str(exc)},
            )
            return
        except Exception as exc:
            self._json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": str(exc)},
            )
            return
        prep = normalize_meeting_prep_payload(result) if isinstance(result, dict) else {}
        prep["input_fingerprint"] = fp
        try:
            updated_at = save_meeting_prep(
                DB_PATH,
                dedupe_key=dedupe_key,
                thread_id=thread_id,
                prep=prep,
            )
        except ValueError as exc:
            self._json_response(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": str(exc) or "bad_request"},
            )
            return
        except Exception as exc:
            self._json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": str(exc)},
            )
            return
        out: Dict[str, Any] = {
            "ok": True,
            "thread_id": thread_id,
            "dedupe_key": dedupe_key,
            "prep_updated_at": updated_at,
        }
        out.update(prep)
        self._json_response(HTTPStatus.OK, out)

    def _post_save_thread_draft(self, body: Dict[str, Any]) -> None:
        thread_id = str(body.get("thread_id") or body.get("inbox_thread_id") or "").strip()
        if not thread_id:
            self._json_response(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "missing_thread_id"},
            )
            return
        intent = str(body.get("response_intent") or "").strip()
        markdown = str(body.get("markdown") or "").strip()
        oq = body.get("open_questions")
        if not isinstance(oq, list):
            oq = []
        try:
            draft = build_thread_draft_payload(
                response_intent=intent,
                result={
                    "reply_body": body.get("reply_body"),
                    "rationale": body.get("rationale"),
                    "open_questions": oq,
                    "raw_text": body.get("raw_text"),
                },
                markdown=markdown or None,
            )
            updated_at = save_thread_draft_reply(
                DB_PATH, thread_id=thread_id, draft=draft
            )
        except ValueError as exc:
            self._json_response(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": str(exc) or "bad_request"},
            )
            return
        except Exception as exc:
            self._json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": str(exc)},
            )
            return
        self._json_response(
            HTTPStatus.OK,
            {"ok": True, "thread_id": thread_id, "updated_at": updated_at},
        )

    def _get_config(self) -> None:
        from utils.features import features_config_payload
        from utils.owner_config import public_config_payload

        payload = {"ok": True, "backend": get_backend()}
        payload.update(public_config_payload())
        payload.update(features_config_payload())
        self._json_response(HTTPStatus.OK, payload)

    def _feature_gate_response(self, method: str, path: str) -> bool:
        """Return True if the request was blocked due to a missing feature."""
        from utils.features import required_feature_for_route

        feature_id = required_feature_for_route(method, path)
        if not feature_id:
            return False
        from utils.features import is_enabled

        if is_enabled(feature_id):
            return False
        self._json_response(
            HTTPStatus.FORBIDDEN,
            {"ok": False, "error": "feature_unavailable", "feature": feature_id},
        )
        return True

    def _get_pipeline_status(self) -> None:
        self._json_response(HTTPStatus.OK, _pipeline_status_payload())

    def _post_config_backend(self, body: Dict[str, Any]) -> None:
        backend = str(body.get("backend") or "").strip().lower()
        if not backend:
            self._json_response(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "missing_backend"},
            )
            return
        try:
            set_backend(backend)
        except ValueError as exc:
            self._json_response(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": str(exc) or "invalid_backend"},
            )
            return
        except OSError as exc:
            self._json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": str(exc)},
            )
            return
        self._json_response(HTTPStatus.OK, {"ok": True, "backend": get_backend()})

    def _post_pipeline_run(self) -> None:
        started, err = _start_pipeline_run()
        if not started:
            payload = _pipeline_status_payload()
            payload["ok"] = False
            payload["error"] = err or "already_running"
            self._json_response(HTTPStatus.CONFLICT, payload)
            return
        payload = _pipeline_status_payload()
        payload["status"] = "running"
        self._json_response(HTTPStatus.ACCEPTED, payload)

    def _request_path(self) -> str:
        return urllib.parse.urlparse(self.path).path.rstrip("/") or "/"

    def _serve_favicon(self) -> None:
        favicon_path = PROJECT_ROOT / "square5.jpg"
        if not favicon_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = favicon_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(body)

    def _serve_app_shell(self) -> None:
        index_path = PROJECT_ROOT / "frontend" / "index.html"
        if not index_path.is_file():
            self._json_response(
                HTTPStatus.NOT_FOUND,
                {"ok": False, "error": "frontend_index_missing"},
            )
            return
        body = index_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self._request_path()
        if self._feature_gate_response("GET", path):
            return
        if path == "/api/summaries/bundle":
            self._get_summaries_bundle()
            return
        if path == "/api/meetings":
            self._get_meetings()
            return
        if path == "/api/lanes/summary":
            self._get_lane_summary()
            return
        if path == "/api/config":
            self._get_config()
            return
        if path == "/api/pipeline/status":
            self._get_pipeline_status()
            return
        if path == "/api/texts/catalog":
            self._get_texts_catalog()
            return
        if path == "/api/slack/catalog":
            self._get_slack_catalog()
            return
        if path == "/timeline.db":
            self._get_timeline_db()
            return
        if path in ("/favicon.ico", "/square5.jpg"):
            self._serve_favicon()
            return
        if path in (
            "/",
            "/dashboard",
            "/threads",
            "/meetings",
            "/lanes",
            "/plans",
            "/texts-setup",
            "/slack-setup",
        ):
            self._serve_app_shell()
            return
        if path == "/summaries.html":
            self.send_response(HTTPStatus.MOVED_PERMANENTLY)
            self.send_header("Location", "/threads")
            self.end_headers()
            return
        if path == "/people":
            self.send_response(HTTPStatus.MOVED_PERMANENTLY)
            self.send_header("Location", "/lanes")
            self.end_headers()
            return
        super().do_GET()

    def do_POST(self) -> None:
        body, err = self._read_post_json()
        if err:
            self._json_response(HTTPStatus.BAD_REQUEST, {"ok": False, "error": err})
            return
        assert body is not None
        path = self._request_path()
        if self._feature_gate_response("POST", path):
            return
        if path == "/api/thread-tracking/snooze":
            self._post_snooze(body)
        elif path == "/api/thread-tracking/remove":
            self._post_remove_tracking(body)
        elif path == "/api/thread-tracking/draft-reply":
            self._post_save_thread_draft(body)
        elif path == "/api/threads/summary":
            self._post_thread_summary(body)
        elif path == "/api/claude/email-reply":
            self._post_email_reply(body)
        elif path == "/api/meeting-prep":
            self._post_meeting_prep(body)
        elif path == "/api/lanes/create":
            self._post_lane_create(body)
        elif path == "/api/lanes/add-thread":
            self._post_lane_add_thread(body)
        elif path == "/api/lanes/remove-thread":
            self._post_lane_remove_thread(body)
        elif path == "/api/lanes/delete":
            self._post_lane_delete(body)
        elif path == "/api/lanes/summary":
            self._post_lane_summary(body)
        elif path == "/api/plans/create":
            self._post_plan_create(body)
        elif path == "/api/plans/update":
            self._post_plan_update(body)
        elif path == "/api/plans/delete":
            self._post_plan_delete(body)
        elif path == "/api/config/backend":
            self._post_config_backend(body)
        elif path == "/api/pipeline/run":
            self._post_pipeline_run()
        elif path == "/api/texts/track":
            self._post_texts_track(body)
        elif path == "/api/texts/summarize":
            self._post_texts_summarize(body)
        elif path == "/api/slack/pull":
            self._post_slack_pull()
        elif path == "/api/slack/track":
            self._post_slack_track(body)
        elif path == "/api/slack/summarize":
            self._post_slack_summarize(body)
        else:
            log.warning("POST %s not handled (raw path=%r)", path, self.path)
            self._json_response(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})


def _lan_ipv4_addresses() -> List[str]:
    """Non-loopback IPv4 addresses (best effort)."""
    addrs: List[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            addrs.append(s.getsockname()[0])
    except OSError:
        pass
    seen = set(addrs)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in seen:
                seen.add(ip)
                addrs.append(ip)
    except OSError:
        pass
    return addrs


def _print_dashboard_urls(host: str, port: int) -> None:
    base = f"http://127.0.0.1:{port}"
    print(f"Serving dashboard + API on {host}:{port}")
    print(f"  Local:   {base}/threads")
    print(f"           {base}/dashboard")
    print(f"           {base}/meetings")
    if host in ("0.0.0.0", ""):
        for ip in _lan_ipv4_addresses():
            lan = f"http://{ip}:{port}"
            print(f"  Network: {lan}/threads")
    elif host not in ("127.0.0.1", "localhost"):
        print(f"  http://{host}:{port}/threads")


def _scheduler_thread_main() -> None:
    try:
        scheduler_loop()
    except Exception:
        log.exception("Fivelanes scheduler thread exited due to an error")
        raise
    finally:
        log.warning("Fivelanes scheduler thread stopped")


def main() -> None:
    os.chdir(infra_root())
    log_path = configure_logging()
    log.info("Dashboard starting (pid=%d, log=%s)", os.getpid(), log_path)
    server = ThreadingHTTPServer((DASHBOARD_HOST, DASHBOARD_PORT), DashboardHandler)
    _print_dashboard_urls(DASHBOARD_HOST, DASHBOARD_PORT)
    scheduler = threading.Thread(
        target=_scheduler_thread_main,
        name="fivelanes-scheduler",
        daemon=True,
    )
    scheduler.start()
    log.info("Fivelanes scheduler thread started (daemon=%s)", scheduler.daemon)
    server.serve_forever()


if __name__ == "__main__":
    main()
