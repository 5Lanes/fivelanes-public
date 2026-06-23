"""
Unified email LLM pipeline: segment messages, summarize threads, persist outputs.

Backend (Claude or Ollama) is selected via ``FIVELANES_BACKEND``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from services.pipeline.process import (
    load_timeline_entries_by_thread,
    run_threads_llm_pipeline,
    segment_body_deduped,
)

# Backward-compatible aliases for callers that import private helpers.
_segment_body_deduped = segment_body_deduped


def run_fivelanes_llm_pipeline(
    lookback_days: int = 14,
    db_path: Optional[str] = None,
    *,
    backend: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Segment new messages and summarize affected threads."""
    cleaned, per_message = run_threads_llm_pipeline(
        lookback_days=lookback_days,
        db_path=db_path,
        backend=backend,
    )
    if cleaned:
        from services.llm_service import get_llm_backend

        llm = get_llm_backend(backend=backend)
        print(f"Thread LLM pipeline finished (backend={llm.name}, messages={len(cleaned)})")
    return cleaned, per_message
