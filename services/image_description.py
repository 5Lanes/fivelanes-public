"""
Vision path for image-only Gmail messages: fetch inline images and produce segmentation-shaped
``content`` for the Fivelanes pipeline (instead of Llama/Claude text segmentation).
"""

from __future__ import annotations

import base64
import logging
import os
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

from googleapiclient.errors import HttpError

from services.claude_service import (
    claude_supported_image_media_type,
    describe_image_with_claude,
)
from utils.database import connect_sqlite
from services.email.config import SOURCE_OAUTH_ACCOUNT_ID
from services.email.inbox_delivery import (
    PLACEHOLDER_SUBJECTS,
    body_is_empty_except_image,
    timeline_row_needs_image_description,
)
from services.email.message_body import (
    BODY_GMAIL_IMAGE_PLACEHOLDER_RE,
    mime_image_part_is_embedded_inline,
    payload_contains_inline_image,
    strip_image_markers_from_body,
)
from services.gmail_client import get_gmail_services_for_account_id
from services.image_ocr import (
    extract_text_from_image_bytes,
    image_ocr_enabled,
    ocr_text_is_usable,
)
from services.llama_service import describe_image_with_llava

FIVELANES_BACKEND = (os.getenv("FIVELANES_BACKEND") or "llama").strip().lower()


def cleaned_content_is_image_stub(process_body: str, cleaned_content: str) -> bool:
    """True when ``cleaned_content`` is still a filename/placeholder, not a vision description."""
    if not body_is_empty_except_image(process_body):
        return False
    cleaned = (cleaned_content or "").strip()
    body = (process_body or "").strip()
    if not cleaned:
        return True
    if cleaned == body:
        return True
    for match in BODY_GMAIL_IMAGE_PLACEHOLDER_RE.finditer(body):
        placeholder = match.group(0)
        inner = placeholder[7:-1].strip() if placeholder.lower().startswith("[image:") else ""
        if cleaned == placeholder or (inner and cleaned == inner):
            return True
    stripped = strip_image_markers_from_body(body)
    if cleaned == stripped:
        return True
    # Short token-like values (e.g. ``image.png``) are not real descriptions.
    if len(cleaned) < 120 and "\n" not in cleaned and cleaned.count(" ") < 4:
        return True
    return False


def fetch_prior_cleaned_content(
    db_path: str, thread_id: str, source_id: str
) -> str:
    """Latest ``cleaned_content`` for a message from ``claude_message_outputs``."""
    if not source_id:
        return ""
    try:
        with connect_sqlite(db_path) as conn:
            row = conn.execute(
                """
                SELECT cleaned_content
                FROM claude_message_outputs
                WHERE source_id = ? AND COALESCE(thread_id, '') = ?
                ORDER BY generated_at DESC
                LIMIT 1
                """,
                (source_id, thread_id or ""),
            ).fetchone()
    except sqlite3.Error:
        return ""
    return str(row[0] or "").strip() if row else ""


def should_reprocess_image_only_row(
    process_body: str,
    cleaned_content: str,
    *,
    body_has_image: bool = False,
    row: Optional[Dict[str, Any]] = None,
) -> bool:
    """Re-run vision when the body is image-only but ``cleaned_content`` lacks a description."""
    if row is not None and timeline_row_needs_image_description(row, process_body):
        cleaned = (cleaned_content or "").strip().lower()
        if not cleaned or cleaned in PLACEHOLDER_SUBJECTS:
            return True
        return cleaned_content_is_image_stub(process_body, cleaned_content)
    if not body_is_empty_except_image(process_body, body_has_image=body_has_image):
        return False
    return cleaned_content_is_image_stub(process_body, cleaned_content)


def _decode_part_bytes(part: dict, service: Any, msg_id: str) -> bytes:
    body = part.get("body") or {}
    data = body.get("data")
    if data:
        try:
            return base64.urlsafe_b64decode(data)
        except Exception:
            return b""
    attachment_id = body.get("attachmentId")
    if attachment_id and service and msg_id:
        try:
            att = (
                service.users()
                .messages()
                .attachments()
                .get(userId="me", messageId=msg_id, id=attachment_id)
                .execute()
            )
            raw = att.get("data")
            if raw:
                return base64.urlsafe_b64decode(raw)
        except Exception:
            pass
    return b""


def _collect_inline_images(
    payload: dict, service: Any, msg_id: str
) -> List[Tuple[str, bytes]]:
    """``(mime_type, raw_bytes)`` for each inline/embedded image part."""
    if not payload:
        return []
    out: List[Tuple[str, bytes]] = []
    stack: List[Tuple[dict, bool]] = [(payload, False)]
    while stack:
        part, in_related = stack.pop()
        mime = (part.get("mimeType") or "").lower()
        child_in_related = in_related or mime == "multipart/related"
        if mime_image_part_is_embedded_inline(part, in_related=child_in_related):
            raw = _decode_part_bytes(part, service, msg_id)
            if raw:
                out.append((mime, raw))
        for child in part.get("parts") or []:
            stack.append((child, child_in_related))
    return out


def _fetch_full_message(
    service: Any, message_id: str
) -> Optional[dict]:
    if not message_id:
        return None
    try:
        return (
            service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )
    except HttpError:
        return None


def _describe_one_image(
    mime: str, raw: bytes, *, context: str
) -> Tuple[str, str]:
    if image_ocr_enabled():
        ocr_text, ocr_err = extract_text_from_image_bytes(raw)
        if ocr_text and ocr_text_is_usable(ocr_text):
            log.info("image_ocr: transcript %d chars (vision skipped)", len(ocr_text))
            return ocr_text, ""
        if ocr_err and ocr_err not in ("OCR disabled", "empty image"):
            log.debug("image_ocr: %s; falling back to vision", ocr_err)

    media_type = claude_supported_image_media_type(mime)
    b64 = base64.standard_b64encode(raw).decode("ascii")
    if FIVELANES_BACKEND == "claude":
        if not media_type:
            return "", f"Unsupported image type for Claude: {mime}"
        result = describe_image_with_claude(
            media_type=media_type,
            base64_data=b64,
            context=context,
        )
    else:
        result = describe_image_with_llava(base64_data=b64, context=context)
    text = str(result.get("description") or "").strip()
    err = "" if text else "Vision model returned empty description"
    return text, err


def _subject_context_for_image_row(row: Dict[str, Any], subject: str) -> str:
    subj = (subject or "").strip()
    if subj.lower() in PLACEHOLDER_SUBJECTS:
        return ""
    kind = str(row.get("inbox_delivery_kind") or "").strip()
    if kind == "cc_bcc_only":
        kind = "cc_bcc"
    if kind in ("direct_to", "cc_bcc"):
        return subj
    return ""


def _prefix_subject_on_image_content(content: str, subject: str) -> str:
    subj = (subject or "").strip()
    body = (content or "").strip()
    if not subj or subj.lower() in PLACEHOLDER_SUBJECTS:
        return body
    if body.lower().startswith("subject:"):
        return body
    return f"Subject: {subj}\n\n{body}".strip()


def describe_image_only_timeline_row(
    row: Dict[str, Any],
    *,
    subject: str = "",
) -> Tuple[Dict[str, Any], str]:
    """
    Fetch inline images for a timeline row and return segmentation-shaped output
    (``content`` / ``quoted_reply`` / ``signature``).
    """
    source_id = str(row.get("source_id") or "").strip()
    account_id = (
        str(row.get("fetch_oauth_account_id") or "").strip()
        or SOURCE_OAUTH_ACCOUNT_ID
    )
    pairs = get_gmail_services_for_account_id(account_id)
    if not pairs:
        return {}, "No Gmail credentials for image fetch"
    _aid, service = pairs[0]

    full = _fetch_full_message(service, source_id)
    if not full:
        err = f"Could not fetch Gmail message {source_id!r}"
        log.warning("image_description %s account=%s: %s", source_id, account_id, err)
        return {}, err
    payload = full.get("payload") or {}
    images = _collect_inline_images(payload, service, source_id)
    if not images:
        has_inline = payload_contains_inline_image(payload)
        err = (
            "No decodable inline images in Gmail payload"
            if has_inline
            else "Message has no inline images in Gmail payload"
        )
        log.warning(
            "image_description %s account=%s: %s (has_inline_marker=%s)",
            source_id,
            account_id,
            err,
            has_inline,
        )
        return {}, err

    context = _subject_context_for_image_row(row, subject)
    parts: List[str] = []
    errors: List[str] = []
    for mime, raw in images:
        text, err = _describe_one_image(mime, raw, context=context)
        if text:
            parts.append(text)
        elif err:
            errors.append(err)

    content = "\n\n".join(parts).strip()
    if not content:
        err = "; ".join(errors) or "Image description failed"
        log.warning("image_description %s account=%s: %s", source_id, account_id, err)
        return {}, err
    kind = str(row.get("inbox_delivery_kind") or "").strip()
    if kind == "cc_bcc_only":
        kind = "cc_bcc"
    if kind in ("direct_to", "cc_bcc"):
        content = _prefix_subject_on_image_content(content, subject)
    log.info(
        "image_description %s account=%s: described %d image(s), %d chars",
        source_id,
        account_id,
        len(parts),
        len(content),
    )
    return {"content": content, "quoted_reply": "", "signature": ""}, ""


def process_timeline_message_segmentation(
    row: Dict[str, Any],
    process_body: str,
    seg_cache: Dict[str, Tuple[Dict[str, Any], str]],
    segment_fn,
) -> Tuple[Dict[str, Any], str]:
    """
    Route image-only bodies to vision; otherwise call ``segment_fn(process_body, seg_cache)``.
    """
    if timeline_row_needs_image_description(row, process_body):
        subj = str(row.get("summary") or "").strip()
        return describe_image_only_timeline_row(row, subject=subj)
    if not (process_body or "").strip():
        return {}, ""
    return segment_fn(process_body, seg_cache)
