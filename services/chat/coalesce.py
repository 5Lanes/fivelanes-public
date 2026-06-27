"""Merge rapid chat messages into conversation turns for summarization."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

DEFAULT_COALESCE_GAP_MINUTES = 10


def _parse_row_datetime(row: Dict[str, Any]) -> datetime | None:
    raw = str(row.get("datetime") or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def coalesce_chat_turns(
    cleaned: List[Dict[str, Any]],
    *,
    gap_minutes: int = DEFAULT_COALESCE_GAP_MINUTES,
) -> List[Dict[str, Any]]:
    """
    Merge consecutive messages from the same sender into one turn.

    Raw per-message rows are kept for storage and display; use this output only as
    LLM summary input so short back-and-forth is summarized as a conversation.
    """
    if not cleaned:
        return []

    ordered = sorted(cleaned, key=lambda r: str(r.get("datetime") or ""))
    gap = timedelta(minutes=max(1, gap_minutes))
    turns: List[Dict[str, Any]] = []
    current: Dict[str, Any] | None = None
    bodies: List[str] = []
    source_ids: List[str] = []

    def flush() -> None:
        nonlocal current, bodies, source_ids
        if current is None:
            return
        combined = "\n".join(part for part in bodies if part.strip())
        if not combined.strip():
            combined = "(attachment)" if bodies else ""
        if not combined.strip():
            current = None
            bodies = []
            source_ids = []
            return
        turn = dict(current)
        turn["cleaned_content"] = combined
        turn["source_id"] = source_ids[0]
        turns.append(turn)
        current = None
        bodies = []
        source_ids = []

    for row in ordered:
        sender = str(row.get("sender") or "").strip()
        body = str(row.get("cleaned_content") or "").strip()
        sid = str(row.get("source_id") or "").strip()
        dt = _parse_row_datetime(row)
        if not sid:
            continue

        if current is None:
            current = {k: v for k, v in row.items() if not str(k).startswith("_")}
            bodies = [body] if body else []
            source_ids = [sid]
            continue

        prev_dt = _parse_row_datetime(current)
        same_sender = sender == str(current.get("sender") or "").strip()
        within_gap = bool(prev_dt and dt and (dt - prev_dt) <= gap)
        if same_sender and within_gap:
            if body:
                bodies.append(body)
            elif not bodies:
                bodies.append("(attachment)")
            source_ids.append(sid)
            continue

        flush()
        current = {k: v for k, v in row.items() if not str(k).startswith("_")}
        bodies = [body] if body else []
        source_ids = [sid]

    flush()
    return turns
