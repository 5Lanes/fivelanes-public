"""Global lock: only one in-flight LLM inference request at a time."""

from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

_LLM_INFERENCE_LOCK = threading.Lock()

# #region agent log
_DEBUG_LOG_PATH = Path(__file__).resolve().parent.parent / ".cursor" / "debug-3d391d.log"


def _agent_debug_log(*, location: str, message: str, data: dict[str, Any], hypothesis_id: str) -> None:
    try:
        payload = {
            "sessionId": "3d391d",
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": data,
            "timestamp": int(time.time() * 1000),
            "runId": "llm-serial",
        }
        with open(_DEBUG_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, default=str) + "\n")
    except Exception:
        pass


# #endregion


@contextmanager
def llm_inference_slot(*, backend: str = "", kind: str = "", model: str = "") -> Iterator[None]:
    """Acquire the process-wide LLM slot for one complete inference call."""
    # #region agent log
    _agent_debug_log(
        location="llm_inference_lock.py:llm_inference_slot",
        message="llm lock wait",
        data={"backend": backend, "kind": kind, "model": model},
        hypothesis_id="LLM",
    )
    # #endregion
    with _LLM_INFERENCE_LOCK:
        # #region agent log
        _agent_debug_log(
            location="llm_inference_lock.py:llm_inference_slot",
            message="llm lock acquired",
            data={"backend": backend, "kind": kind, "model": model},
            hypothesis_id="LLM",
        )
        # #endregion
        yield
