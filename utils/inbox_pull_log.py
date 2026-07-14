"""Persist the last inbox-only pull (no LLM step) so repeat pulls only fetch net-new mail."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from utils.runtime_paths import data_path

_lock = threading.Lock()


def _log_path() -> Path:
    return data_path("out", "last_inbox_pull.json")


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


def record_inbox_pull_finish(
    *, started_at: str, ok: bool, error: Optional[str] = None, trigger: Optional[str] = None
) -> None:
    _write(
        {
            "started_at": started_at,
            "finished_at": _utc_now_iso(),
            "ok": ok,
            "error": error,
            "trigger": trigger,
        }
    )


def load_last_inbox_pull() -> Optional[Dict[str, Any]]:
    path = _log_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def last_successful_email_pull_at() -> Optional[str]:
    """Newest finish time between the last full pipeline run and last inbox-only pull.

    Used as the ``after_date`` cutoff for the next email pull (full cycle or
    inbox-only), so it only fetches mail since whichever ran more recently instead of
    rescanning the whole lookback window. Returns ``None`` when neither has ever run
    successfully (first pull falls back to the full lookback window).
    """
    from utils.pipeline_run_log import load_last_pipeline_run

    candidates: list[str] = []
    full = load_last_pipeline_run()
    if full:
        # ``last_completed_at`` (not ``finished_at``/``ok``) survives the "running"
        # transition record_pipeline_run_start() writes right before fl.main() runs,
        # so this is the only field that reliably reflects the *previous* completion
        # when this helper is called from inside a full-cycle run in progress.
        last_completed_at = full.get("last_completed_at")
        if not last_completed_at and full.get("ok") is True:
            last_completed_at = full.get("finished_at")
        if last_completed_at:
            candidates.append(str(last_completed_at))
    inbox_only = load_last_inbox_pull()
    if inbox_only and inbox_only.get("ok") is True and inbox_only.get("finished_at"):
        candidates.append(str(inbox_only["finished_at"]))
    return max(candidates) if candidates else None
