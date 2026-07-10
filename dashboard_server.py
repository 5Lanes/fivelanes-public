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
    format_meeting_prep_prompt,
)
from utils.database import (
    _meeting_dedupe_key,
    ensure_database_schema,
    add_thread_to_lane,
    aggregate_thread_chronological_anchor,
    archive_lane,
    build_summaries_bundle,
    build_thread_draft_payload,
    create_lane,
    create_lane_area,
    create_thread_plan,
    delete_lane,
    delete_thread_plan,
    update_thread_plan,
    load_all_lane_areas,
    assign_lane_to_area,
    update_lane_area,
    fetch_meetings_rows,
    load_lane_summary,
    load_lane_thread_summaries,
    load_meeting_prep,
    load_thread_draft_reply,
    normalize_meeting_prep_payload,
    remove_thread_from_lane,
    save_meeting_prep,
    save_thread_draft_reply,
)
from services.thread_snooze import remove_thread_tracking, set_thread_snooze
from utils.lane_summary_jobs import (
    lane_summary_has_content as _lane_summary_has_content,
    lane_summary_http_payload as _lane_summary_http_payload,
    lane_summary_job_snapshot as _lane_summary_job_snapshot,
    start_lane_summary_job as _start_lane_summary_job,
)
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
from services.linkedin import (
    LINKEDIN_MESSAGES_DIR,
    fetch_tracked_conversation_keys as fetch_tracked_linkedin_keys,
    list_conversation_catalog as list_linkedin_catalog,
    pull_linkedin_messages,
    set_tracked_conversation_keys as set_tracked_linkedin_keys,
    write_selections_for_conversation_keys,
)
from services.linkedin.summarize import summarize_tracked_linkedin_threads
from services.meet_recordings import (
    MEET_RECORDINGS_DIR,
    fetch_tracked_document_keys as fetch_tracked_meet_keys,
    list_document_catalog as list_meet_catalog,
    pull_meet_recording_catalog,
    set_tracked_document_keys as set_tracked_meet_keys,
    summarize_tracked_meet_recordings,
)
from services.calendar_events import (
    fetch_tracked_calendar_dedupe_keys,
    list_meeting_catalog,
    set_tracked_meeting_keys,
    summarize_tracked_calendar_event_threads,
)
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

# One lock per premium channel so a track/pull-triggered background summarize and a
# manual "Generate summaries" click can never run concurrently and double-bill the LLM.
_CHANNEL_SUMMARIZE_LOCKS: Dict[str, threading.Lock] = {
    "texts": threading.Lock(),
    "slack": threading.Lock(),
    "linkedin": threading.Lock(),
    "meet_recordings": threading.Lock(),
    "calendar_events": threading.Lock(),
}


def _run_channel_summarize(channel: str, fn):
    """Run ``fn`` (a zero-arg summarize call) serialized against other summarize calls
    for the same channel. Blocks rather than skipping, since callers may pass
    ``force=True`` and expect the run to actually happen."""
    lock = _CHANNEL_SUMMARIZE_LOCKS[channel]
    with lock:
        return fn()

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


PIPELINE_STALL_THRESHOLD_SEC = int((os.getenv("FIVELANES_STALL_THRESHOLD_SEC") or "900").strip() or "900")


def _seconds_since_iso(value: Any) -> Optional[float]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds()


def _warm_gai_chat_cache() -> None:
    """Refresh the GAI chat schema/snapshot cache so the next chat turn is served from cache."""
    try:
        from services.gai.db_context import warm_chat_context_cache

        warm_chat_context_cache(DB_PATH)
    except Exception:
        log.exception("Failed to warm GAI chat context cache")


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
            _warm_gai_chat_cache()

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
        payload["stage"] = last_run.get("stage")
        payload["detail"] = last_run.get("detail")
        heartbeat = last_run.get("progress_at") or last_run.get("started_at")
        idle_sec = _seconds_since_iso(heartbeat)
        payload["idle_sec"] = idle_sec
        payload["stalled"] = bool(idle_sec is not None and idle_sec >= PIPELINE_STALL_THRESHOLD_SEC)
    payload["last_run"] = last_run
    return payload


def _db_cache_etag(db_path: str) -> Tuple[str, str]:
    from utils.database import message_outputs_revision

    st = Path(db_path).stat()
    content_rev = message_outputs_revision(db_path)
    etag = f'"{int(st.st_mtime_ns)}-{st.st_size}-{content_rev}-{_summaries_bundle_epoch}"'
    last_modified = email.utils.formatdate(st.st_mtime, usegmt=True)
    return etag, last_modified


_summaries_bundle_cache: Optional[Dict[str, Any]] = None
_summaries_bundle_epoch: int = 0


def _get_cached_summaries_bundle(etag: str) -> Optional[Dict[str, Any]]:
    cached = _summaries_bundle_cache
    if (
        cached
        and cached.get("etag") == etag
        and cached.get("epoch") == _summaries_bundle_epoch
    ):
        bundle = cached.get("bundle")
        if isinstance(bundle, dict):
            return bundle
    return None


def _store_summaries_bundle_cache(
    etag: str, bundle: Dict[str, Any], *, build_epoch: int
) -> None:
    global _summaries_bundle_cache
    if build_epoch != _summaries_bundle_epoch:
        return
    _summaries_bundle_cache = {
        "etag": etag,
        "bundle": bundle,
        "epoch": build_epoch,
    }


def _invalidate_summaries_bundle_cache() -> None:
    global _summaries_bundle_cache, _summaries_bundle_epoch
    _summaries_bundle_cache = None
    _summaries_bundle_epoch += 1


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

    def _write_chunked_bytes(self, data: bytes) -> None:
        if not data:
            return
        self.wfile.write(f"{len(data):x}\r\n".encode("ascii"))
        self.wfile.write(data)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def _ndjson_stream_response(
        self,
        status: int,
        events: Any,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            for event in events:
                line = json.dumps(event, default=str) + "\n"
                self._write_chunked_bytes(line.encode("utf-8"))
        finally:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

    def _get_summaries_bundle(self) -> None:
        db_file = Path(DB_PATH)
        if not db_file.is_file():
            self._json_response(
                HTTPStatus.NOT_FOUND, {"ok": False, "error": "database_not_found"}
            )
            return
        from services.thread_snooze import (
            refresh_linkedin_threads_auto_unsnooze,
            refresh_slack_threads_auto_unsnooze,
            refresh_text_threads_auto_unsnooze,
        )

        cleared = (
            refresh_text_threads_auto_unsnooze(DB_PATH)
            + refresh_slack_threads_auto_unsnooze(DB_PATH)
            + refresh_linkedin_threads_auto_unsnooze(DB_PATH)
        )
        try:
            etag, last_modified = _db_cache_etag(DB_PATH)
        except OSError:
            self._json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": "database_stat_failed"},
            )
            return
        inm = (self.headers.get("If-None-Match") or "").strip()
        if inm == etag and cleared == 0:
            self._not_modified(etag, last_modified)
            return
        bundle = _get_cached_summaries_bundle(etag)
        if bundle is None or cleared > 0:
            build_epoch = _summaries_bundle_epoch
            try:
                bundle = build_summaries_bundle(DB_PATH)
            except Exception as exc:
                log.exception("summaries bundle build failed")
                self._json_response(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"ok": False, "error": str(exc)},
                )
                return
            _store_summaries_bundle_cache(etag, bundle, build_epoch=build_epoch)
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
            result = _run_channel_summarize(
                "texts",
                lambda: summarize_tracked_text_threads(
                    DB_PATH,
                    conversation_keys=raw if isinstance(raw, list) else None,
                    force=force,
                ),
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
                _run_channel_summarize(
                    "texts",
                    lambda: summarize_tracked_text_threads(
                        DB_PATH, conversation_keys=keys, force=True
                    ),
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

    def _post_meet_recordings_pull(self) -> None:
        try:
            result = pull_meet_recording_catalog()
        except Exception as exc:
            log.exception("meet recordings pull failed")
            self._json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": str(exc)},
            )
            return
        self._json_response(HTTPStatus.OK, result)

    def _get_meet_recordings_catalog(self) -> None:
        catalog = list_meet_catalog()
        tracked = fetch_tracked_meet_keys(DB_PATH) if Path(DB_PATH).is_file() else []
        self._json_response(
            HTTPStatus.OK,
            {
                "ok": True,
                "meet_recordings_dir": str(MEET_RECORDINGS_DIR),
                "catalog": catalog,
                "tracked": tracked,
            },
        )

    def _post_meet_recordings_track(self, body: Dict[str, Any]) -> None:
        if not Path(DB_PATH).is_file():
            self._json_response(
                HTTPStatus.NOT_FOUND, {"ok": False, "error": "database_not_found"}
            )
            return
        raw = body.get("document_keys")
        if raw is None:
            raw = body.get("tracked")
        if not isinstance(raw, list):
            self._json_response(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "missing_document_keys"},
            )
            return
        try:
            result = set_tracked_meet_keys(DB_PATH, raw)
        except Exception as exc:
            log.exception("meet recordings track failed")
            self._json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": str(exc)},
            )
            return

        keys = result.get("tracked") if isinstance(result.get("tracked"), list) else raw

        def _summarize_worker() -> None:
            try:
                _run_channel_summarize(
                    "meet_recordings",
                    lambda: summarize_tracked_meet_recordings(
                        DB_PATH, document_keys=keys, force=True
                    ),
                )
            except Exception:
                log.exception("Background meet recording summarization failed")

        threading.Thread(
            target=_summarize_worker,
            name="meet-recordings-summarize",
            daemon=True,
        ).start()
        self._json_response(HTTPStatus.OK, result)

    def _post_meet_recordings_summarize(self, body: Dict[str, Any]) -> None:
        if not Path(DB_PATH).is_file():
            self._json_response(
                HTTPStatus.NOT_FOUND, {"ok": False, "error": "database_not_found"}
            )
            return
        raw = body.get("document_keys")
        force = bool(body.get("force"))
        try:
            result = _run_channel_summarize(
                "meet_recordings",
                lambda: summarize_tracked_meet_recordings(
                    DB_PATH,
                    document_keys=raw if isinstance(raw, list) else None,
                    force=force,
                ),
            )
        except Exception as exc:
            log.exception("meet recordings summarize failed")
            self._json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": str(exc)},
            )
            return
        self._json_response(HTTPStatus.OK, result)

    def _get_calendar_catalog(self) -> None:
        if not Path(DB_PATH).is_file():
            self._json_response(
                HTTPStatus.NOT_FOUND, {"ok": False, "error": "database_not_found"}
            )
            return
        catalog = list_meeting_catalog(DB_PATH)
        tracked = fetch_tracked_calendar_dedupe_keys(DB_PATH)
        self._json_response(
            HTTPStatus.OK,
            {
                "ok": True,
                "catalog": catalog,
                "tracked": tracked,
            },
        )

    def _post_calendar_track(self, body: Dict[str, Any]) -> None:
        if not Path(DB_PATH).is_file():
            self._json_response(
                HTTPStatus.NOT_FOUND, {"ok": False, "error": "database_not_found"}
            )
            return
        raw = body.get("dedupe_keys")
        if raw is None:
            raw = body.get("tracked")
        if not isinstance(raw, list):
            self._json_response(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "missing_dedupe_keys"},
            )
            return
        try:
            result = set_tracked_meeting_keys(DB_PATH, raw)
        except Exception as exc:
            log.exception("calendar track failed")
            self._json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": str(exc)},
            )
            return

        keys = result.get("tracked") if isinstance(result.get("tracked"), list) else raw

        def _summarize_worker() -> None:
            try:
                _run_channel_summarize(
                    "calendar_events",
                    lambda: summarize_tracked_calendar_event_threads(
                        DB_PATH, dedupe_keys=keys, force=True
                    ),
                )
            except Exception:
                log.exception("Background calendar summarization failed")

        threading.Thread(
            target=_summarize_worker,
            name="calendar-summarize",
            daemon=True,
        ).start()
        self._json_response(HTTPStatus.OK, result)

    def _post_calendar_summarize(self, body: Dict[str, Any]) -> None:
        if not Path(DB_PATH).is_file():
            self._json_response(
                HTTPStatus.NOT_FOUND, {"ok": False, "error": "database_not_found"}
            )
            return
        raw = body.get("dedupe_keys")
        force = bool(body.get("force"))
        try:
            result = _run_channel_summarize(
                "calendar_events",
                lambda: summarize_tracked_calendar_event_threads(
                    DB_PATH,
                    dedupe_keys=raw if isinstance(raw, list) else None,
                    force=force,
                ),
            )
        except Exception as exc:
            log.exception("calendar summarize failed")
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
            result = _run_channel_summarize(
                "slack",
                lambda: summarize_tracked_slack_threads(
                    DB_PATH,
                    conversation_keys=raw if isinstance(raw, list) else None,
                    force=force,
                ),
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
                _run_channel_summarize(
                    "slack",
                    lambda: summarize_tracked_slack_threads(
                        DB_PATH, conversation_keys=keys, force=True
                    ),
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

    def _post_linkedin_pull(self, body: Dict[str, Any]) -> None:
        raw = body.get("conversation_keys")
        if raw is None:
            raw = body.get("tracked")
        keys = raw if isinstance(raw, list) else None
        if isinstance(keys, list) and not keys:
            keys = None
        try:
            result = pull_linkedin_messages(DB_PATH, conversation_keys=keys)
        except Exception as exc:
            log.exception("linkedin pull failed")
            self._json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": str(exc)},
            )
            return

        if result.get("skipped"):
            self._json_response(HTTPStatus.OK, result)
            return

        keys = result.get("selections") or keys
        pulled_keys = fetch_tracked_linkedin_keys(DB_PATH) if Path(DB_PATH).is_file() else []

        def _summarize_worker() -> None:
            try:
                _run_channel_summarize(
                    "linkedin",
                    lambda: summarize_tracked_linkedin_threads(
                        DB_PATH, conversation_keys=pulled_keys or None
                    ),
                )
            except Exception:
                log.exception("Background LinkedIn summarization after pull failed")

        threading.Thread(
            target=_summarize_worker,
            name="linkedin-summarize-after-pull",
            daemon=True,
        ).start()
        result["summarize"] = "started"
        self._json_response(HTTPStatus.OK, result)

    def _post_linkedin_summarize(self, body: Dict[str, Any]) -> None:
        if not Path(DB_PATH).is_file():
            self._json_response(
                HTTPStatus.NOT_FOUND, {"ok": False, "error": "database_not_found"}
            )
            return
        raw = body.get("conversation_keys")
        force = bool(body.get("force"))
        try:
            result = _run_channel_summarize(
                "linkedin",
                lambda: summarize_tracked_linkedin_threads(
                    DB_PATH,
                    conversation_keys=raw if isinstance(raw, list) else None,
                    force=force,
                ),
            )
        except Exception as exc:
            log.exception("linkedin summarize failed")
            self._json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": str(exc)},
            )
            return
        self._json_response(HTTPStatus.OK, result)

    def _get_linkedin_catalog(self) -> None:
        catalog = list_linkedin_catalog()
        tracked = fetch_tracked_linkedin_keys(DB_PATH) if Path(DB_PATH).is_file() else []
        self._json_response(
            HTTPStatus.OK,
            {
                "ok": True,
                "linkedin_messages_dir": str(LINKEDIN_MESSAGES_DIR),
                "catalog": catalog,
                "tracked": tracked,
            },
        )

    def _post_linkedin_track(self, body: Dict[str, Any]) -> None:
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
            result = set_tracked_linkedin_keys(DB_PATH, raw)
        except Exception as exc:
            log.exception("linkedin track failed")
            self._json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": str(exc)},
            )
            return

        keys = result.get("tracked") if isinstance(result.get("tracked"), list) else raw
        try:
            write_selections_for_conversation_keys(keys)
        except Exception:
            log.exception("Failed to persist LinkedIn pull selections")

        def _summarize_worker() -> None:
            try:
                _run_channel_summarize(
                    "linkedin",
                    lambda: summarize_tracked_linkedin_threads(
                        DB_PATH, conversation_keys=keys, force=True
                    ),
                )
            except Exception:
                log.exception("Background LinkedIn summarization failed")

        threading.Thread(
            target=_summarize_worker,
            name="linkedin-summarize",
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
        area_id_raw = body.get("area_id")
        area_id: Optional[int] = None
        if area_id_raw is not None and str(area_id_raw).strip() != "":
            try:
                area_id = int(area_id_raw)
            except (TypeError, ValueError):
                area_id = None
        try:
            lane = create_lane(DB_PATH, name=name, area_id=area_id)
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

    def _get_lane_areas(self) -> None:
        areas = load_all_lane_areas(DB_PATH)
        self._json_response(HTTPStatus.OK, {"ok": True, "lane_areas": areas})

    def _post_lane_area_create(self, body: Dict[str, Any]) -> None:
        name = str(body.get("name") or "").strip()
        if not name:
            self._json_response(
                HTTPStatus.BAD_REQUEST, {"ok": False, "error": "missing_lane_area_name"}
            )
            return
        try:
            color_index = int(body.get("color_index") or 0)
        except (TypeError, ValueError):
            color_index = 0
        try:
            area = create_lane_area(DB_PATH, name=name, color_index=color_index)
        except ValueError as exc:
            self._json_response(
                HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)}
            )
            return
        self._json_response(HTTPStatus.OK, {"ok": True, "lane_area": area})

    def _post_lane_area_update(self, body: Dict[str, Any]) -> None:
        try:
            area_id = int(body.get("area_id") or body.get("id") or 0)
        except (TypeError, ValueError):
            area_id = 0
        if area_id <= 0:
            self._json_response(
                HTTPStatus.BAD_REQUEST, {"ok": False, "error": "missing_area_id"}
            )
            return
        name = body.get("name")
        color_index = body.get("color_index")
        sort_order = body.get("sort_order")
        try:
            if color_index is not None:
                color_index = int(color_index)
        except (TypeError, ValueError):
            color_index = None
        try:
            if sort_order is not None:
                sort_order = int(sort_order)
        except (TypeError, ValueError):
            sort_order = None
        try:
            ok = update_lane_area(
                DB_PATH,
                area_id=area_id,
                name=str(name).strip() if name is not None else None,
                color_index=color_index,
                sort_order=sort_order,
            )
        except ValueError as exc:
            self._json_response(
                HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)}
            )
            return
        if not ok:
            self._json_response(
                HTTPStatus.NOT_FOUND, {"ok": False, "error": "lane_area_not_found"}
            )
            return
        self._json_response(HTTPStatus.OK, {"ok": True, "area_id": area_id})

    def _post_lane_assign_area(self, body: Dict[str, Any]) -> None:
        try:
            lane_id = int(body.get("lane_id") or 0)
        except (TypeError, ValueError):
            lane_id = 0
        area_raw = body.get("area_id")
        area_id: Optional[int] = None
        if area_raw is not None and str(area_raw).strip() != "":
            try:
                area_id = int(area_raw)
            except (TypeError, ValueError):
                area_id = None
        if lane_id <= 0:
            self._json_response(
                HTTPStatus.BAD_REQUEST, {"ok": False, "error": "missing_lane_id"}
            )
            return
        ok = assign_lane_to_area(DB_PATH, lane_id=lane_id, area_id=area_id)
        if not ok:
            self._json_response(
                HTTPStatus.NOT_FOUND, {"ok": False, "error": "lane_or_area_not_found"}
            )
            return
        self._json_response(
            HTTPStatus.OK, {"ok": True, "lane_id": lane_id, "area_id": area_id}
        )

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
        _invalidate_summaries_bundle_cache()
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
        _invalidate_summaries_bundle_cache()
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

    def _post_lane_archive(self, body: Dict[str, Any]) -> None:
        try:
            lane_id = int(body.get("lane_id") or 0)
        except (TypeError, ValueError):
            lane_id = 0
        archived = body.get("archived", True)
        if isinstance(archived, str):
            archived = archived.strip().lower() not in ("0", "false", "no")
        else:
            archived = bool(archived)
        if lane_id <= 0:
            self._json_response(
                HTTPStatus.BAD_REQUEST, {"ok": False, "error": "missing_lane_id"}
            )
            return
        ok = archive_lane(DB_PATH, lane_id=lane_id, archived=archived)
        if not ok:
            self._json_response(
                HTTPStatus.NOT_FOUND, {"ok": False, "error": "lane_not_found"}
            )
            return
        self._json_response(
            HTTPStatus.OK, {"ok": True, "lane_id": lane_id, "archived": archived}
        )

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
        if job and str(job.get("status") or "") == "done":
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
                db_path=DB_PATH,
                lane_id=lid,
                lane_name=name,
                thread_ids=thread_ids,
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
        _invalidate_summaries_bundle_cache()
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
        _invalidate_summaries_bundle_cache()
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
        _invalidate_summaries_bundle_cache()
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

    def _post_gai_chat(self, body: Dict[str, Any]) -> None:
        message = str(body.get("message") or "").strip()
        if not message:
            self._json_response(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "missing_message"},
            )
            return
        raw_history = body.get("history")
        if raw_history is not None and not isinstance(raw_history, list):
            self._json_response(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "invalid_history"},
            )
            return
        history: List[Dict[str, str]] = []
        for item in raw_history or []:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip()
            content = str(item.get("content") or "").strip()
            if role in ("user", "assistant") and content:
                history.append({"role": role, "content": content})
        session_context = body.get("session")
        if session_context is not None and not isinstance(session_context, dict):
            self._json_response(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "invalid_session"},
            )
            return
        stream = body.get("stream") is True
        try:
            if stream:
                from services.gai.chat import iter_gai_chat_events

                self._ndjson_stream_response(
                    HTTPStatus.OK,
                    iter_gai_chat_events(
                        DB_PATH,
                        message,
                        history=history,
                        session_context=session_context or {},
                    ),
                )
                return

            from services.gai.chat import answer_question

            result = answer_question(
                DB_PATH,
                message,
                history=history,
                session_context=session_context or {},
            )
        except RuntimeError as exc:
            self._json_response(
                HTTPStatus.BAD_GATEWAY,
                {"ok": False, "error": str(exc)},
            )
            return
        except Exception as exc:
            log.exception("GAI chat failed")
            self._json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": str(exc)},
            )
            return
        if not result.get("ok"):
            self._json_response(HTTPStatus.BAD_REQUEST, result)
            return
        self._json_response(HTTPStatus.OK, result)

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
        from utils.email_capture_config import get_email_capture_mode
        from utils.features import features_config_payload
        from utils.owner_config import public_config_payload
        from utils.lookback_config import get_lookback_days
        from utils.scheduler_config import get_schedule_config

        payload = {"ok": True, "backend": get_backend()}
        payload.update(public_config_payload())
        payload.update(features_config_payload())
        payload["schedule"] = get_schedule_config().to_dict()
        payload["lookback_days"] = get_lookback_days()
        payload["email_capture"] = get_email_capture_mode()
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

    def _post_config_schedule(self, body: Dict[str, Any]) -> None:
        from utils.scheduler_config import set_schedule_config

        try:
            config = set_schedule_config(body)
        except ValueError as exc:
            self._json_response(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": str(exc) or "invalid_schedule"},
            )
            return
        except OSError as exc:
            self._json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": str(exc)},
            )
            return
        self._json_response(
            HTTPStatus.OK,
            {"ok": True, "schedule": config.to_dict()},
        )

    def _post_config_lookback_days(self, body: Dict[str, Any]) -> None:
        from utils.lookback_config import set_lookback_days

        raw = body.get("lookback_days")
        if raw is None:
            self._json_response(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "missing_lookback_days"},
            )
            return
        try:
            days = set_lookback_days(raw)
        except ValueError as exc:
            self._json_response(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": str(exc) or "invalid_lookback_days"},
            )
            return
        except OSError as exc:
            self._json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": str(exc)},
            )
            return
        self._json_response(
            HTTPStatus.OK,
            {"ok": True, "lookback_days": days},
        )

    def _post_config_email_capture(self, body: Dict[str, Any]) -> None:
        from utils.email_capture_config import set_email_capture_mode

        mode = str(body.get("email_capture") or body.get("mode") or "").strip().lower()
        if not mode:
            self._json_response(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "missing_email_capture"},
            )
            return
        try:
            applied = set_email_capture_mode(mode)
        except ValueError as exc:
            self._json_response(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": str(exc) or "invalid_email_capture"},
            )
            return
        except OSError as exc:
            self._json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": str(exc)},
            )
            return
        self._json_response(
            HTTPStatus.OK,
            {"ok": True, "email_capture": applied},
        )

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
        if path == "/api/lane-areas":
            self._get_lane_areas()
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
        if path == "/api/linkedin/catalog":
            self._get_linkedin_catalog()
            return
        if path == "/api/meet-recordings/catalog":
            self._get_meet_recordings_catalog()
            return
        if path == "/api/calendar/catalog":
            self._get_calendar_catalog()
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
            "/sources",
            "/threads",
            "/meetings",
            "/lanes",
            "/plans",
            "/texts-setup",
            "/slack-setup",
            "/linkedin-setup",
            "/meet-recordings-setup",
            "/calendar-setup",
        ):
            self._serve_app_shell()
            return
        if path == "/summaries.html":
            self.send_response(HTTPStatus.MOVED_PERMANENTLY)
            self.send_header("Location", "/dashboard")
            self.end_headers()
            return
        if path == "/people":
            self.send_response(HTTPStatus.MOVED_PERMANENTLY)
            self.send_header("Location", "/dashboard#lanes")
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
        elif path == "/api/gai/chat":
            self._post_gai_chat(body)
        elif path == "/api/lanes/create":
            self._post_lane_create(body)
        elif path == "/api/lanes/add-thread":
            self._post_lane_add_thread(body)
        elif path == "/api/lanes/remove-thread":
            self._post_lane_remove_thread(body)
        elif path == "/api/lanes/delete":
            self._post_lane_delete(body)
        elif path == "/api/lanes/archive":
            self._post_lane_archive(body)
        elif path == "/api/lanes/summary":
            self._post_lane_summary(body)
        elif path == "/api/lane-areas":
            self._post_lane_area_create(body)
        elif path == "/api/lane-areas/update":
            self._post_lane_area_update(body)
        elif path == "/api/lanes/assign-area":
            self._post_lane_assign_area(body)
        elif path == "/api/plans/create":
            self._post_plan_create(body)
        elif path == "/api/plans/update":
            self._post_plan_update(body)
        elif path == "/api/plans/delete":
            self._post_plan_delete(body)
        elif path == "/api/config/backend":
            self._post_config_backend(body)
        elif path == "/api/config/schedule":
            self._post_config_schedule(body)
        elif path == "/api/config/lookback-days":
            self._post_config_lookback_days(body)
        elif path == "/api/config/email-capture":
            self._post_config_email_capture(body)
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
        elif path == "/api/linkedin/track":
            self._post_linkedin_track(body)
        elif path == "/api/linkedin/pull":
            self._post_linkedin_pull(body)
        elif path == "/api/linkedin/summarize":
            self._post_linkedin_summarize(body)
        elif path == "/api/meet-recordings/pull":
            self._post_meet_recordings_pull()
        elif path == "/api/meet-recordings/track":
            self._post_meet_recordings_track(body)
        elif path == "/api/meet-recordings/summarize":
            self._post_meet_recordings_summarize(body)
        elif path == "/api/calendar/track":
            self._post_calendar_track(body)
        elif path == "/api/calendar/summarize":
            self._post_calendar_summarize(body)
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
    ensure_database_schema(DB_PATH)
    threading.Thread(
        target=_warm_gai_chat_cache,
        name="fivelanes-gai-chat-cache-warmup",
        daemon=True,
    ).start()
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
