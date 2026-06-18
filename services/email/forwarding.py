"""Forward unwrap, inner RFC ids, embedded-forward parsing."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from email import message_from_string
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any, Dict, List, Optional, Tuple

from services.email.config import SOURCE_ACCOUNT
from services.email.message_body import html_to_plain, message_body_has_image
from services.email.recipients import extract_emails_lower

log = logging.getLogger(__name__)

_FORWARD_BLOCK_START = re.compile(
    r"(?m)^[\s]*(?:[-_=]{2,}[\s]*Forwarded message[\s]*[-_=]{2,}|Begin forwarded message:?)[\s]*$",
    re.IGNORECASE,
)
_FORWARD_BLOCK_LOOSE = re.compile(
    r"(?mi)^[\s]*(?:[-_=]{2,}\s*)?Forwarded message(?:\s*[-_=]{2,})?\s*$",
)
_FROM_HEADER_LINE = re.compile(r"(?m)^From:\s*(.+)$")
_MESSAGE_ID_HEADER_LINE = re.compile(
    r"(?im)^Message-ID:\s*(<[^>\s]+@[^>\s]+>|[^\s<>]+@[^\s<>]+)",
)
_IN_REPLY_TO_LINE = re.compile(
    r"(?im)^In-Reply-To:\s*(<[^>]+>)",
)


def _addressed_to_source_inbox(recipients: str, body: str, inbox_lower: str) -> bool:
    """True if ``inbox_lower`` appears as a recipient (metadata or early To/Bcc: lines)."""
    if not inbox_lower:
        return False
    if inbox_lower in (recipients or "").lower():
        return True
    for line in (body or "").splitlines()[:50]:
        s = line.strip().lower()
        if (s.startswith("to:") or s.startswith("bcc:")) and inbox_lower in s:
            return True
    return False


def strip_forwarded_to_source_address(
    body: str,
    *,
    recipients: Optional[str] = None,
) -> Tuple[str, Optional[str]]:
    """
    If this message is addressed to ``SOURCE_ACCOUNT`` (``.env``) and the body looks like
    a forward, return text starting at the first ``From:`` inside the forwarded block and
    the parsed source email. Otherwise return ``(body, None)``.

    Unwrapping runs only for mail to the Fivelanes inbox so arbitrary forwards in other
    threads are left intact.
    """
    if not (body or "").strip():
        return body or "", None
    inbox = (SOURCE_ACCOUNT or "").strip().lower()
    if not inbox:
        return body, None
    if not _addressed_to_source_inbox(recipients or "", body, inbox):
        return body, None
    plain = _body_text_for_rfc_extract(body)
    found = _find_embedded_forward_slice(plain)
    if not found:
        return body, None
    start, header_blob, body_tail = found
    from_m = re.search(r"(?im)^From:\s*(.+)$", header_blob)
    if not from_m:
        return body, None
    addrs = getaddresses([from_m.group(1).strip()])
    source_email: Optional[str] = None
    for _, addr in addrs:
        if addr and "@" in addr:
            source_email = addr.strip().lower()
            break
    sliced = plain[start:].strip()
    if body_tail:
        sliced = f"{header_blob}\n\n{body_tail}".strip()
    else:
        sliced = header_blob.strip()
    return sliced, source_email


def primary_email_from_sender(sender: str) -> str:
    """First address from a ``From`` header string (e.g. the ``sender`` column in ``fivelanes``)."""
    for _, addr in getaddresses([sender or ""]):
        if addr and "@" in addr:
            return addr.strip().lower()
    return ""


def body_without_forward_to_source(
    raw_body: str, recipients: Optional[str] = None
) -> str:
    """Body after removing a forward-to-``SOURCE_ACCOUNT`` envelope in the body, if applicable."""
    text, _ = strip_forwarded_to_source_address(raw_body, recipients=recipients)
    return (text or "").strip()




def forward_marker_match(body: str):
    """Optional Gmail forward delimiter line (not used for delivery classification)."""
    b = body or ""
    m = _FORWARD_BLOCK_START.search(b)
    if m:
        return m
    return _FORWARD_BLOCK_LOOSE.search(b)


def _body_text_for_rfc_extract(body: str) -> str:
    """Normalize CRLF and strip HTML when the forward lives in an HTML part."""
    b = body or ""
    bl = b.lower()
    if "<html" in bl or "<div" in bl or "<table" in bl:
        if "forward" in bl or "from:" in bl:
            b = html_to_plain(b)
    return b.replace("\r\n", "\n").replace("\r", "\n")


def _headers_look_like_embedded_message(header_blob: str) -> bool:
    """True when a header block looks like a forwarded email, not prose."""
    if not (header_blob or "").strip():
        return False
    from_m = re.search(r"(?im)^From:\s*(.+)$", header_blob)
    if not from_m or "@" not in from_m.group(1):
        return False
    lower = header_blob.lower()
    secondary = sum(
        1
        for h in ("date:", "subject:", "to:", "message-id:", "cc:")
        if h in lower
    )
    return secondary >= 1


def _collect_rfc_header_block_at(text: str) -> Optional[Tuple[str, str]]:
    """RFC822 header lines + body tail starting at a ``From:`` line."""
    lines = (text or "").splitlines()
    hdr_lines: List[str] = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        if not ln.strip():
            break
        if ln.startswith((" ", "\t")) and hdr_lines:
            hdr_lines[-1] = hdr_lines[-1] + " " + ln.strip()
            i += 1
            continue
        if not hdr_lines and not ln.lstrip().lower().startswith("from:"):
            return None
        hdr_lines.append(ln)
        i += 1
    if not hdr_lines:
        return None
    header_blob = "\n".join(hdr_lines)
    body_tail = "\n".join(lines[i + 1 :])
    return header_blob, body_tail


def _find_embedded_forward_slice(plain: str) -> Optional[Tuple[int, str, str]]:
    """
    Return ``(start_index, header_blob, body_tail)`` for the first embedded forwarded
    message in plain text (RFC headers in the body, with or without a Gmail delimiter).
    """
    text = _body_text_for_rfc_extract(plain or "")
    if not text.strip():
        return None
    search_from = 0
    marker = forward_marker_match(text)
    if marker:
        search_from = marker.end()
    positions: List[int] = [search_from]
    positions.extend(m.start() for m in re.finditer(r"(?m)^From:\s*.+$", text, re.I))
    for pos in sorted(set(positions)):
        rest = text[pos:].lstrip("\n\r")
        if not rest.lower().startswith("from:"):
            continue
        split = _collect_rfc_header_block_at(rest)
        if not split:
            continue
        header_blob, body_tail = split
        if not _headers_look_like_embedded_message(header_blob):
            continue
        start = pos + len(text[pos:]) - len(rest)
        return start, header_blob, body_tail
    return None


def body_contains_embedded_forwarded_thread(body: str) -> bool:
    """True when the body embeds another email thread (RFC header block), not merely a forward marker line."""
    return _find_embedded_forward_slice(body or "") is not None


def _message_id_from_embedded_forward(body: str) -> Optional[str]:
    """Scan embedded forward headers/body for RFC ids Gmail omits from the tight header blob."""
    found = _find_embedded_forward_slice(body or "")
    if not found:
        return None
    _start, header_blob, body_tail = found
    tail = (header_blob + "\n" + body_tail)[:20000]
    # Prefer explicit Message-ID (may appear below From/Date/Subject).
    mm = _MESSAGE_ID_HEADER_LINE.search(tail)
    if mm:
        return (mm.group(1) or "").strip().strip("<>")
    ir = _IN_REPLY_TO_LINE.search(tail)
    if ir:
        return (ir.group(1) or "").strip().strip("<>")
    # References: <...@...> ... (first id often identifies the thread root in the source mailbox)
    ref_m = re.search(r"(?im)^References:\s*(\S.*)$", tail)
    if ref_m:
        refs = re.findall(r"<([^>\s]+@[^>\s]+)>", ref_m.group(1))
        if refs:
            return refs[0].strip()
    return None


def extract_inner_rfc_message_id(body: str) -> Optional[str]:
    """
    RFC Message-ID for the **inner** forwarded message (no angle brackets), for
    ``rfc822msgid:`` search in the originator's mailbox.
    """
    plain = _body_text_for_rfc_extract(body or "")
    inner = _parse_first_forward_inner_from_plain_body(plain)
    mid = ""
    if inner:
        mid = (inner.get("message_id") or "").strip()
    if mid:
        return mid.strip("<>")
    return _message_id_from_embedded_forward(plain)


def _angle_bracket_ids(header_value: str) -> List[str]:
    """All ``<...@...>`` tokens from a References / In-Reply-To / Message-ID header."""
    if not (header_value or "").strip():
        return []
    return [x.strip() for x in re.findall(r"<([^>\n]+)>", header_value)]


def extract_envelope_rfc_message_id(m: Dict[str, Any]) -> str:
    """
    RFC id from Gmail MIME headers on **this** message (no brackets).

    Bodies often omit Message-ID; Gmail still exposes ``In-Reply-To``, ``References``,
    and ``Message-ID`` on the envelope. Prefer parent pointers, then this message's id.
    """
    irt = m.get("header_in_reply_to") or ""
    ids = _angle_bracket_ids(irt)
    if ids:
        return ids[0].strip("<>")
    refs = m.get("header_references") or ""
    ref_ids = _angle_bracket_ids(refs)
    if ref_ids:
        # Last id is often the immediate parent; first is thread root — try last first.
        return ref_ids[-1].strip("<>")
    mid = m.get("header_message_id") or ""
    mids = _angle_bracket_ids(mid)
    if mids:
        return mids[0].strip("<>")
    # Rare: bare token without brackets
    mid_stripped = (mid or "").strip()
    if "@" in mid_stripped and "<" not in mid_stripped:
        return mid_stripped
    return ""

def _coerce_inner_forward_date(date_header: str, fallback_iso: str) -> str:
    """Parse Date from an embedded forward header; fall back to envelope datetime."""
    d = (date_header or "").strip()
    if not d:
        return fallback_iso
    try:
        dt = parsedate_to_datetime(d)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except Exception:
        pass
    m = re.search(
        r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2}),\s+(\d{4})\s+at\s+"
        r"(\d{1,2}):(\d{2})(?::(\d{2}))?(?:\s*([AP]M))?",
        d,
        re.I,
    )
    if not m:
        return fallback_iso
    months = {
        k.lower(): i
        for i, k in enumerate(
            (
                "Jan",
                "Feb",
                "Mar",
                "Apr",
                "May",
                "Jun",
                "Jul",
                "Aug",
                "Sep",
                "Oct",
                "Nov",
                "Dec",
            ),
            start=1,
        )
    }
    mon = months.get(m.group(1).lower())
    if not mon:
        return fallback_iso
    day = int(m.group(2))
    year = int(m.group(3))
    hh = int(m.group(4))
    minute = int(m.group(5))
    ap = (m.group(7) or "").upper()
    if ap == "PM" and hh != 12:
        hh += 12
    if ap == "AM" and hh == 12:
        hh = 0
    try:
        dt = datetime(year, mon, day, hh, minute, tzinfo=timezone.utc)
        return dt.isoformat()
    except ValueError:
        return fallback_iso


def _parse_first_forward_inner_from_plain_body(body: str) -> Optional[Dict[str, str]]:
    """
    Gmail often stores the prior conversation only inside ``text/plain`` (not as separate
    thread messages). Parse the first embedded forward: inner headers + body.
    """
    plain = _body_text_for_rfc_extract(body or "")
    found = _find_embedded_forward_slice(plain)
    if not found:
        return None
    _start, header_blob, body_tail = found
    try:
        msg = message_from_string(header_blob + "\n\n")
    except Exception:
        return None
    inner_body = body_tail
    nested = forward_marker_match(inner_body)
    if nested:
        inner_body = inner_body[: nested.start()].rstrip()
    mid = (msg.get("Message-Id") or msg.get("Message-ID") or "").strip()
    return {
        "from": (msg.get("from") or "").strip(),
        "to": (msg.get("to") or "").strip(),
        "cc": (msg.get("cc") or "").strip(),
        "bcc": (msg.get("bcc") or "").strip(),
        "date": (msg.get("date") or "").strip(),
        "subject": (msg.get("subject") or "").strip(),
        "body": (inner_body or "").strip(),
        "message_id": mid,
    }


