"""Global lock: only one in-flight LLM inference request at a time."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

_LLM_INFERENCE_LOCK = threading.Lock()


@contextmanager
def llm_inference_slot() -> Iterator[None]:
    """Acquire the process-wide LLM slot for one complete inference call."""
    with _LLM_INFERENCE_LOCK:
        yield
