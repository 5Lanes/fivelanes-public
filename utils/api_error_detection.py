"""Detect API / pipeline error text masquerading as summary or segmentation output."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Pattern

_API_ERROR_PATTERNS: List[Pattern[str]] = [
    re.compile(r"\b(Claude|Ollama) API error\b", re.IGNORECASE),
    re.compile(r"\brate_limit_error\b", re.IGNORECASE),
    re.compile(r"\bModel returned prose instead of JSON\b", re.IGNORECASE),
    re.compile(r"\bOllama response was not valid JSON\b", re.IGNORECASE),
    re.compile(r"\bSegmentation response missing expected key\b", re.IGNORECASE),
    re.compile(r"\b(?:API|api) request failed\b", re.IGNORECASE),
    re.compile(r"\bOllama request failed\b", re.IGNORECASE),
]

# Failures that a blind re-run will not fix without code/data changes.
_NON_RETRYABLE_SEGMENTATION_ERRORS: List[Pattern[str]] = [
    re.compile(r"Message has no inline images in Gmail payload", re.IGNORECASE),
]


def text_looks_like_api_error(text: str) -> bool:
    """True when ``text`` is empty or matches known LLM / pipeline failure strings."""
    value = (text or "").strip()
    if not value:
        return False
    return any(p.search(value) for p in _API_ERROR_PATTERNS)


def segmentation_error_is_retryable(api_error: str) -> bool:
    err = (api_error or "").strip()
    if not err:
        return False
    if any(p.search(err) for p in _NON_RETRYABLE_SEGMENTATION_ERRORS):
        return False
    return text_looks_like_api_error(err) or "Segmentation response missing" in err


_VERBATIM_MIN_CHARS = 40


def _normalize_compare_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


_ATTRIBUTION_VERB_RE = re.compile(
    r"\b(?:confirmed|replied|stated|asked|emailed|sent|wrote|noted|responded|advised)\b",
    re.IGNORECASE,
)


def _has_attribution_framing(longer: str, shorter: str) -> bool:
    """True when *longer* wraps *shorter* with sender/context, not a raw paste."""
    if longer == shorter:
        return False
    idx = longer.find(shorter)
    if idx < 0:
        return False
    prefix = longer[:idx].strip(" ,.;:-")
    if len(prefix) >= 12 and _ATTRIBUTION_VERB_RE.search(prefix):
        return True
    if re.search(r"\b(?:on |jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b", prefix):
        return True
    return False


def _texts_match_verbatim(a: str, b: str) -> bool:
    """True when ``a`` and ``b`` are the same message text (exact or pasted substring)."""
    left = _normalize_compare_text(a)
    right = _normalize_compare_text(b)
    if not left or not right:
        return False
    if left == right:
        return True
    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    if len(shorter) < _VERBATIM_MIN_CHARS:
        return False
    if shorter in longer:
        if _has_attribution_framing(longer, shorter):
            return False
        return True
    overlap = min(len(left), len(right))
    if overlap >= _VERBATIM_MIN_CHARS and left[:overlap] == right[:overlap]:
        ratio = overlap / max(len(shorter), 1)
        if ratio >= 0.9:
            if _has_attribution_framing(longer, shorter):
                return False
            return True
    return False


def _message_bodies_from_cleaned(cleaned: List[Dict[str, Any]]) -> List[str]:
    bodies: List[str] = []
    for row in cleaned:
        for key in ("cleaned_content", "raw_text"):
            body = str(row.get(key) or "").strip()
            if body and body != "(attachment)":
                bodies.append(body)
    return bodies


def update_looks_like_verbatim_email(update: str, cleaned: List[Dict[str, Any]]) -> bool:
    """True when ``update`` is copied from a message body instead of synthesized."""
    line = str(update or "").strip()
    if not line:
        return False
    for body in _message_bodies_from_cleaned(cleaned):
        if _texts_match_verbatim(line, body):
            return True
    return False


def summary_updates_look_like_verbatim_email(
    summary: Dict[str, Any],
    cleaned: List[Dict[str, Any]],
) -> bool:
    """True when ``latest_updates`` are mostly pasted email text, not synthesized bullets."""
    updates = summary.get("latest_updates")
    if not isinstance(updates, list):
        return False
    lines = [str(item).strip() for item in updates if str(item).strip()]
    if not lines:
        return False
    verbatim = [line for line in lines if update_looks_like_verbatim_email(line, cleaned)]
    if not verbatim:
        return False
    if len(lines) == 1:
        return True
    threshold = (len(lines) + 1) // 2
    return len(verbatim) >= threshold


def summary_body_looks_like_api_error(summary: Dict[str, Any]) -> bool:
    """
    True when a thread summary dict has no usable structured body and looks like a failed LLM call.

    Checks ``api_error``, ``raw_text``, and lone ``latest_updates`` entries.
    """
    if not isinstance(summary, dict):
        return False
    if str(summary.get("api_error") or "").strip():
        return True
    raw = str(summary.get("raw_text") or "").strip()
    if raw and text_looks_like_api_error(raw):
        return True
    updates = summary.get("latest_updates")
    if isinstance(updates, list) and len(updates) == 1:
        if text_looks_like_api_error(str(updates[0] or "")):
            return True
    has_structured = bool(str(summary.get("suggested_thread_label") or "").strip())
    if isinstance(updates, list) and updates:
        has_structured = True
    if raw and not has_structured:
        return True
    return False


def thread_summary_is_valid(
    summary: Dict[str, Any],
    *,
    cleaned: List[Dict[str, Any]] | None = None,
) -> bool:
    """
    True when a summary dict matches the thread-summary schema enough to persist.

    Rejects wrong shapes (e.g. ``{"response": "..."}``) that omit ``latest_updates``,
    and summaries whose ``latest_updates`` paste email bodies verbatim.
    """
    if not isinstance(summary, dict):
        return False
    if summary_body_looks_like_api_error(summary):
        return False
    updates = summary.get("latest_updates")
    if isinstance(updates, list) and any(str(item).strip() for item in updates):
        if cleaned and summary_updates_look_like_verbatim_email(summary, cleaned):
            return False
        return True
    if str(summary.get("suggested_thread_label") or "").strip():
        return True
    return False
