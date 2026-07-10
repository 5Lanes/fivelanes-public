"""Global slot pool: bound the number of in-flight LLM inference requests.

Supports a high-priority lane so interactive requests (the "Ask AIFred" chat) don't sit
behind a long queue of background pipeline calls (segmentation, summaries, email replies,
meeting prep, lane summaries) that grabbed slots first. Low-priority callers wait behind
any currently-waiting high-priority caller; high-priority callers only wait for a free slot.

Capacity is configurable via ``OLLAMA_MAX_CONCURRENCY`` (default 3) so it can be tuned to
match how many concurrent requests the Ollama server (``OLLAMA_NUM_PARALLEL`` on the GPU
host) is actually configured to serve. Raising this without also raising
``OLLAMA_NUM_PARALLEL`` server-side just moves the queueing into Ollama instead of here.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from typing import Iterator

_cond = threading.Condition()
_used = 0
_high_waiting = 0


def _resolve_capacity() -> int:
    raw = (os.getenv("OLLAMA_MAX_CONCURRENCY") or "").strip()
    try:
        return max(1, int(raw)) if raw else 3
    except ValueError:
        return 3


_capacity = _resolve_capacity()


def inference_capacity() -> int:
    """Configured number of concurrent LLM inference slots (``OLLAMA_MAX_CONCURRENCY``)."""
    return _capacity


@contextmanager
def llm_inference_slot(*, high_priority: bool = False) -> Iterator[None]:
    """Acquire one of the process-wide LLM slots for one complete inference call.

    ``high_priority=True`` lets the caller cut ahead of any low-priority callers still
    waiting (but not ahead of calls already in flight).
    """
    global _used, _high_waiting
    with _cond:
        if high_priority:
            _high_waiting += 1
        try:
            while _used >= _capacity or (not high_priority and _high_waiting > 0):
                _cond.wait()
            _used += 1
        finally:
            if high_priority:
                _high_waiting -= 1
    try:
        yield
    finally:
        with _cond:
            _used -= 1
            _cond.notify_all()
