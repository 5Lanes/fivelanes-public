"""Fingerprint helpers for LLM output caching."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Sequence, Set, Tuple

from services.prompts import prompt_version


def cleaned_fingerprint(rows: List[Dict[str, Any]]) -> Set[Tuple[str, str]]:
    """Set of (source_id, cleaned_content) pairs for change detection."""
    out: Set[Tuple[str, str]] = set()
    for row in rows:
        sid = str(row.get("source_id") or "").strip()
        if not sid:
            continue
        content = str(row.get("cleaned_content") or "").strip()
        out.add((sid, content))
    return out


def calendar_context_hash(
    calendar_events_block: str,
    calendar_timezone: str,
) -> str:
    payload = f"{calendar_timezone}\n{calendar_events_block}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def summary_input_fingerprint(
    cleaned_rows: List[Dict[str, Any]],
    *,
    calendar_events_block: str = "",
    calendar_timezone: str = "",
    backend: str = "",
    version: str | None = None,
) -> str:
    """Stable hash of summary inputs for cache invalidation."""
    fp = cleaned_fingerprint(cleaned_rows)
    parts = sorted(f"{sid}:{content}" for sid, content in fp)
    cal_hash = calendar_context_hash(calendar_events_block, calendar_timezone)
    payload = json.dumps(
        {
            "messages": parts,
            "calendar": cal_hash,
            "prompt_version": version or prompt_version(),
            "backend": (backend or "").strip().lower(),
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def messages_cache_keys(messages: Sequence[Dict[str, Any]], *, max_messages: int = 3) -> List[str]:
    """Stable keys for on-demand LLM cache when ``source_id`` may be absent."""
    tail = list(messages)[-max_messages:]
    keys: List[str] = []
    for m in tail:
        sid = str(m.get("source_id") or "").strip()
        if sid:
            keys.append(sid)
            continue
        payload = "|".join(
            [
                str(m.get("datetime") or m.get("timestamp") or ""),
                str(m.get("sender") or m.get("from") or ""),
                str(m.get("content") or ""),
            ]
        )
        keys.append(hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16])
    return keys


def email_reply_fingerprint(
    *,
    thread_id: str,
    response_intent: str,
    source_ids: List[str],
    backend: str = "",
    version: str | None = None,
) -> str:
    payload = json.dumps(
        {
            "thread_id": (thread_id or "").strip(),
            "response_intent": (response_intent or "").strip(),
            "source_ids": [s.strip() for s in source_ids if s.strip()],
            "prompt_version": version or prompt_version(),
            "backend": (backend or "").strip().lower(),
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def lane_summary_fingerprint(
    *,
    lane_id: int,
    thread_ids: List[str],
    summary_datetimes: List[str],
    backend: str = "",
    version: str | None = None,
) -> str:
    payload = json.dumps(
        {
            "lane_id": int(lane_id),
            "thread_ids": [s.strip() for s in thread_ids if s.strip()],
            "summary_datetimes": [s.strip() for s in summary_datetimes if s.strip()],
            "prompt_version": version or prompt_version(),
            "backend": (backend or "").strip().lower(),
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def meeting_prep_fingerprint(
    *,
    dedupe_key: str,
    thread_id: str,
    source_ids: List[str],
    event_fields: Dict[str, str],
    backend: str = "",
    version: str | None = None,
) -> str:
    payload = json.dumps(
        {
            "dedupe_key": (dedupe_key or "").strip(),
            "thread_id": (thread_id or "").strip(),
            "source_ids": [s.strip() for s in source_ids if s.strip()],
            "event": {k: (v or "").strip() for k, v in sorted(event_fields.items())},
            "prompt_version": version or prompt_version(),
            "backend": (backend or "").strip().lower(),
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
