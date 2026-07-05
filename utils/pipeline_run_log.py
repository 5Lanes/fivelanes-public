"""Persist the last full fivelanes pipeline run (scheduler, dashboard, or CLI)."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from utils.runtime_paths import data_path

_lock = threading.Lock()


def _log_path() -> Path:
    return data_path("out", "last_pipeline_run.json")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _write(payload: Dict[str, Any]) -> None:
    path = _log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")
    tmp.replace(path)


def record_pipeline_run_start(*, trigger: str, backend: str) -> str:
    started_at = _utc_now_iso()
    last_completed_at: Optional[str] = None
    prev = load_last_pipeline_run()
    if prev:
        if str(prev.get("status") or "") == "finished" and prev.get("ok") is True:
            last_completed_at = prev.get("finished_at")
        elif prev.get("last_completed_at"):
            last_completed_at = prev.get("last_completed_at")
    with _lock:
        _write(
            {
                "status": "running",
                "started_at": started_at,
                "finished_at": None,
                "ok": None,
                "error": None,
                "trigger": trigger,
                "backend": backend,
                "last_completed_at": last_completed_at,
            }
        )
    return started_at


def record_pipeline_run_finish(
    *,
    started_at: str,
    trigger: str,
    backend: str,
    ok: bool,
    error: Optional[str] = None,
) -> None:
    finished_at = _utc_now_iso()
    prev = load_last_pipeline_run()
    last_completed_at: Optional[str] = finished_at if ok else None
    if not ok and prev:
        last_completed_at = prev.get("last_completed_at")
    with _lock:
        _write(
            {
                "status": "finished",
                "started_at": started_at,
                "finished_at": finished_at,
                "ok": ok,
                "error": error,
                "trigger": trigger,
                "backend": backend,
                "last_completed_at": last_completed_at,
            }
        )


def load_last_pipeline_run() -> Optional[Dict[str, Any]]:
    path = _log_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def reconcile_stale_pipeline_run(*, in_progress: bool) -> bool:
    """
    Mark a prior ``running`` log entry finished when no cycle holds the run lock.

    Returns True when a stale entry was reconciled.
    """
    if in_progress:
        return False
    last = load_last_pipeline_run()
    if not last or str(last.get("status") or "") != "running":
        return False
    record_pipeline_run_finish(
        started_at=str(last.get("started_at") or ""),
        trigger=str(last.get("trigger") or "unknown"),
        backend=str(last.get("backend") or ""),
        ok=False,
        error="interrupted (process restarted while run was in progress)",
    )
    return True
