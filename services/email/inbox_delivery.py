"""Timeline body prep for LLM segmentation (post-pipeline, pre-model)."""
from __future__ import annotations

import json
from typing import Any, Dict

from services.email.forwarding import body_without_forward_to_source
from services.email.message_body import body_text_contains_image, strip_image_markers_from_body

PLACEHOLDER_SUBJECTS = frozenset({"(no subject)"})


def timeline_row_raw_body(row: Dict[str, Any]) -> str:
    """Stored body only — do not substitute subject when body is empty."""
    return str(row.get("body") or "").strip()


def timeline_row_process_body(row: Dict[str, Any]) -> str:
    """Body prepared for segmentation (forward-strip or direct capture with subject)."""
    raw = timeline_row_raw_body(row)
    if not raw:
        return ""
    kind = str(row.get("inbox_delivery_kind") or "").strip()
    if kind == "cc_bcc_only":
        kind = "cc_bcc"
    if kind in ("cc_bcc", "direct_to"):
        text = raw
    else:
        rec = str(row.get("recipients") or "")
        text = body_without_forward_to_source(raw, rec)
    if kind == "direct_to":
        subj = str(row.get("summary") or "").strip()
        if subj and subj.lower() not in PLACEHOLDER_SUBJECTS:
            if text:
                return f"Subject: {subj}\n\n{text}".strip()
            return f"Subject: {subj}"
    return (text or "").strip()


def timeline_row_needs_image_description(
    row: Dict[str, Any], process_body: str
) -> bool:
    """
    True when the message should use vision instead of text segmentation.

    Includes empty stored bodies (image may exist only in Gmail MIME) and
    Gmail ``[image: …]`` placeholders.
    """
    if body_is_empty_except_image(
        process_body, body_has_image=bool(row.get("body_has_image"))
    ):
        return True
    if not timeline_row_raw_body(row) and not (process_body or "").strip():
        return True
    return False


def body_is_empty_except_image(
    process_body: str, *, body_has_image: bool = False
) -> bool:
    """
    True when there is no meaningful text but the message includes an image
    (``body_has_image`` from pull, Gmail ``[image: …]`` placeholders, or HTML img tags).
    """
    text = (process_body or "").strip()
    if not text:
        return bool(body_has_image)
    if not body_text_contains_image(text):
        return False
    return not strip_image_markers_from_body(text)
