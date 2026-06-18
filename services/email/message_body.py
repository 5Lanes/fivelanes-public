"""Gmail MIME decoding, images, and calendar detection."""
from __future__ import annotations

import base64
import html
import re
from typing import Any, List, Optional, Tuple

_HTML_STRIP_RE = re.compile(r"<[^>]+>", re.DOTALL)
_SCRIPT_STYLE_RE = re.compile(
    r"(?is)<(script|style)[^>]*>.*?</(script|style)>", re.DOTALL
)
BODY_IMG_TAG_RE = re.compile(r"<img\b", re.IGNORECASE)
BODY_DATA_IMAGE_RE = re.compile(r"data:image/", re.IGNORECASE)
# Gmail plain-text bodies use ``[image: filename.png]`` for inline images.
BODY_GMAIL_IMAGE_PLACEHOLDER_RE = re.compile(
    r"\[image:\s*[^\]]+\]", re.IGNORECASE
)


def html_to_plain(html_str: str) -> str:
    """Rough HTML → text for LLM input when no text/plain part exists."""
    if not html_str:
        return ""
    t = _SCRIPT_STYLE_RE.sub(" ", html_str)
    t = _HTML_STRIP_RE.sub(" ", t)
    t = html.unescape(t)
    t = re.sub(r"[ \t\r\f\v]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n\n", t)
    return t.strip()


def _decode_body(payload: dict) -> str:
    """Extract plain-text body from Gmail message payload (single-level; prefer message_body_text)."""
    if not payload:
        return ""
    data = payload.get("body", {}).get("data")
    if data:
        try:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        except Exception:
            return ""
    for part in payload.get("parts", []):
        if part.get("mimeType") == "text/plain":
            b = part.get("body", {}).get("data")
            if b:
                try:
                    return base64.urlsafe_b64decode(b).decode("utf-8", errors="replace")
                except Exception:
                    pass
    return ""


def message_body_text(service: Any, msg_id: str, payload: dict) -> str:
    """Prefer text/plain; recurse multipart; fallback to stripped text/html."""
    if not payload:
        return ""

    def decode_part(part: dict) -> str:
        return _decode_part_body(part, service, msg_id)

    def walk_plain(parts: Optional[list]) -> Optional[str]:
        for part in parts or []:
            mime = (part.get("mimeType") or "").lower()
            if mime.startswith("multipart/"):
                got = walk_plain(part.get("parts"))
                if got:
                    return got
            elif mime == "text/plain":
                raw = decode_part(part).strip()
                if raw:
                    return raw
        return None

    def walk_html(parts: Optional[list]) -> Optional[str]:
        for part in parts or []:
            mime = (part.get("mimeType") or "").lower()
            if mime.startswith("multipart/"):
                got = walk_html(part.get("parts"))
                if got:
                    return got
            elif mime == "text/html":
                raw = decode_part(part)
                if raw.strip():
                    return raw.strip()
        return None

    mime_root = (payload.get("mimeType") or "").lower()
    if mime_root == "text/plain":
        raw = decode_part(payload).strip()
        if raw:
            return raw
    if mime_root == "text/html":
        raw = decode_part(payload).strip()
        if raw:
            return html_to_plain(raw)

    plain = walk_plain(payload.get("parts"))
    if plain:
        return plain

    html_blob = walk_html(payload.get("parts"))
    if html_blob:
        return html_to_plain(html_blob)

    data = payload.get("body", {}).get("data")
    if data:
        try:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace").strip()
        except Exception:
            pass
    return ""


def _decode_part_body(part: dict, service, msg_id: str) -> str:
    """Decode a single part's body (inline data or attachment). Returns decoded text or ''."""
    body = part.get("body") or {}
    data = body.get("data")
    if data:
        try:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        except Exception:
            return ""
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
                return base64.urlsafe_b64decode(raw).decode("utf-8", errors="replace")
        except Exception:
            pass
    return ""


def _extract_attachments_text(service, msg_id: str, payload: dict, max_chars: int = 500_000) -> str:
    """
    Recursively extract text from attachment-like parts (e.g. transcript files).
    Skips the main body text/plain; collects text from other text parts and text-like attachments.
    """
    if not payload:
        return ""
    parts = payload.get("parts") or []
    texts: List[str] = []
    total = 0

    def walk(part_list: list) -> None:
        nonlocal total
        for part in part_list:
            if total >= max_chars:
                return
            mime = (part.get("mimeType") or "").lower()
            filename = (part.get("filename") or "").lower()
            # Skip inline main body (handled by _decode_body)
            if mime == "text/plain" and not filename and not part.get("body", {}).get("attachmentId"):
                continue
            # Nested multipart
            if mime.startswith("multipart/"):
                walk(part.get("parts") or [])
                continue
            # Text parts or common transcript extensions (e.g. Gemini meeting notes)
            if mime.startswith("text/") or any(filename.endswith(ext) for ext in (".txt", ".md", ".transcript", ".log")):
                raw = _decode_part_body(part, service, msg_id)
                if raw and raw.strip():
                    texts.append(raw.strip())
                    total += len(raw)

    walk(parts)
    return "\n\n".join(texts) if texts else ""


def get_header(headers: List[dict], name: str) -> str:
    for h in headers or []:
        if (h.get("name") or "").lower() == name.lower():
            return (h.get("value") or "").strip()
    return ""

def _is_calendar_content_type(content_type: str) -> bool:
    """True when Content-Type indicates a calendar invite/update payload."""
    ct = (content_type or "").lower()
    if "text/calendar" in ct:
        return True
    return False


def strip_image_markers_from_body(text: str) -> str:
    """Remove image markers/placeholders; return remaining text (may be empty)."""
    t = (text or "").strip()
    t = BODY_IMG_TAG_RE.sub("", t)
    t = BODY_DATA_IMAGE_RE.sub("", t)
    t = BODY_GMAIL_IMAGE_PLACEHOLDER_RE.sub("", t)
    return t.strip()


def body_text_contains_image(body: str) -> bool:
    """True when decoded body text references or embeds an image."""
    if not (body or "").strip():
        return False
    if BODY_IMG_TAG_RE.search(body):
        return True
    if BODY_DATA_IMAGE_RE.search(body):
        return True
    if BODY_GMAIL_IMAGE_PLACEHOLDER_RE.search(body):
        return True
    return False


def mime_image_part_is_embedded_inline(part: dict, *, in_related: bool = False) -> bool:
    """
    True for images that belong to the message body (not a separate file attachment).

    Gmail often marks HTML-embedded images as ``Content-Disposition: attachment`` while
    still giving them a ``Content-ID`` inside ``multipart/related``.
    """
    mime = (part.get("mimeType") or "").lower()
    if not mime.startswith("image/"):
        return False
    headers = part.get("headers") or []
    cid = (get_header(headers, "Content-ID") or "").strip()
    if cid:
        return True
    if in_related:
        return True
    disposition = (get_header(headers, "Content-Disposition") or "").lower()
    return "attachment" not in disposition


def payload_contains_inline_image(payload: dict) -> bool:
    """True when Gmail MIME payload includes inline/embedded image parts."""
    if not payload:
        return False

    stack: List[Tuple[dict, bool]] = [(payload, False)]
    while stack:
        part, in_related = stack.pop()
        mime = (part.get("mimeType") or "").lower()
        child_in_related = in_related or mime == "multipart/related"
        if mime_image_part_is_embedded_inline(part, in_related=child_in_related):
            return True
        for child in part.get("parts") or []:
            stack.append((child, child_in_related))
    return False


def message_body_has_image(body: str, payload: Optional[dict] = None) -> bool:
    """True when the email body contains an image (text markers or inline MIME parts)."""
    if body_text_contains_image(body):
        return True
    if payload and payload_contains_inline_image(payload):
        return True
    return False


def _has_calendar_payload(payload: dict) -> bool:
    """Detect calendar invite markers in Gmail payload/parts."""
    if not payload:
        return False

    stack = [payload]
    while stack:
        part = stack.pop()
        mime = (part.get("mimeType") or "").lower()
        filename = (part.get("filename") or "").lower()
        headers = part.get("headers") or []
        content_type_header = get_header(headers, "Content-Type")

        if mime == "text/calendar":
            return True
        if filename.endswith(".ics"):
            return True
        if _is_calendar_content_type(content_type_header):
            return True

        children = part.get("parts") or []
        if children:
            stack.extend(children)
    return False


def message_timeline_type(message: dict) -> str:
    """Classify Gmail message for timeline storage."""
    payload = (message or {}).get("payload") or {}
    return "meeting_invite" if _has_calendar_payload(payload) else "email"


def gmail_message_is_draft(msg: dict) -> bool:
    """True when the Gmail API message resource is in the Drafts folder (system label DRAFT)."""
    lids = msg.get("labelIds")
    if not isinstance(lids, list):
        return False
    return "DRAFT" in lids
