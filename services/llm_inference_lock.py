"""Global lock: only one in-flight LLM inference request at a time.

Supports a high-priority lane so interactive requests (the "Ask AIFred" chat) don't sit
behind a long queue of background pipeline calls (segmentation, summaries, email replies,
meeting prep, lane summaries) that grabbed the lock first. Low-priority callers wait behind
any currently-waiting high-priority caller; high-priority callers only wait for whichever
call currently holds the slot.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

_cond = threading.Condition()
_busy = False
_high_waiting = 0


@contextmanager
def llm_inference_slot(*, high_priority: bool = False) -> Iterator[None]:
    """Acquire the process-wide LLM slot for one complete inference call.

    ``high_priority=True`` lets the caller cut ahead of any low-priority callers still
    waiting (but not ahead of a call already in flight).
    """
    global _busy, _high_waiting
    with _cond:
        if high_priority:
            _high_waiting += 1
        try:
            while _busy or (not high_priority and _high_waiting > 0):
                _cond.wait()
            _busy = True
        finally:
            if high_priority:
                _high_waiting -= 1
    try:
        yield
    finally:
        with _cond:
            _busy = False
            _cond.notify_all()
