"""Build timeline/Gmail API message dicts from full responses."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional, Tuple

from googleapiclient.errors import HttpError

from services.email.forwarding import primary_email_from_sender
from services.email.message_body import (
    get_header,
    gmail_message_is_draft,
    message_body_has_image,
    message_body_text,
    message_timeline_type,
)
from services.email.recipients import extract_emails_lower, recipients_contain_address

log = logging.getLogger(__name__)

def get_account_email(service: Any) -> Optional[str]:
    """Return the authenticated account's email from Gmail profile, or None."""
    try:
        profile = service.users().getProfile(userId="me").execute()
        account_email = (profile.get("emailAddress") or "").strip().lower() or None
        return account_email
    except Exception:
        return None


def row_from_full_message(
    service: Any,
    thread_id: str,
    account_id: str,
    account_email: Optional[str],
    full_msg: dict,
    use_account_prefix: bool,
    include_body: bool,
) -> dict:
    """Build the same dict shape as ``_pull_from_account`` from a ``messages.get`` full response."""
    msg_id = full_msg.get("id") or ""
    payload = full_msg.get("payload") or {}
    headers = payload.get("headers", [])
    from_addr = get_header(headers, "From")
    to_addr = get_header(headers, "To")
    cc_addr = get_header(headers, "Cc")
    bcc_addr = get_header(headers, "Bcc")
    subject = get_header(headers, "Subject")
    date_str = get_header(headers, "Date")
    ts = datetime.now(timezone.utc).isoformat()
    if date_str:
        try:
            from email.utils import parsedate_to_datetime

            dt = parsedate_to_datetime(date_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            ts = dt.isoformat()
        except Exception:
            pass
    idate = full_msg.get("internalDate")
    if idate is not None:
        try:
            ts = datetime.fromtimestamp(int(idate) / 1000.0, tz=timezone.utc).isoformat()
        except (TypeError, ValueError, OSError):
            pass
    stored_id = f"{account_id}:{msg_id}" if use_account_prefix else msg_id
    from_emails = extract_emails_lower(from_addr)
    direction = "sent" if (account_email and account_email in from_emails) else "received"
    body_text = ""
    if include_body:
        body_text = message_body_text(service, msg_id, payload)
    message_type = message_timeline_type(full_msg)
    return {
        "id": stored_id,
        "type": message_type,
        "message_id": msg_id,
        "thread_id": full_msg.get("threadId", ""),
        "datetime": ts,
        "sender": from_addr or "",
        "recipients": {
            "to": to_addr or "",
            "cc": cc_addr or "",
            "bcc": bcc_addr or "",
        },
        "from": from_addr or "",
        "to": to_addr or "",
        "cc": cc_addr or "",
        "bcc": bcc_addr or "",
        "subject": subject or "(No subject)",
        "timestamp": ts,
        "direction": direction,
        "body": body_text,
        "body_has_image": message_body_has_image(body_text, payload),
        "header_message_id": get_header(headers, "Message-ID"),
        "header_in_reply_to": get_header(headers, "In-Reply-To"),
        "header_references": get_header(headers, "References"),
    }


def api_message_row_to_timeline_entry(
    m: dict,
    *,
    fetch_oauth_account_id: str = "",
    inbox_delivery_kind: str = "",
) -> Dict[str, Any]:
    """Map a Gmail API row (e.g. from ``_row_from_full_message``) to ``timeline_entries`` columns."""
    t = m.get("type") or "email"
    if t not in ("email", "meeting_invite"):
        t = "email"
    rec = m.get("recipients") or {}
    if isinstance(rec, dict):
        recipients_str = json.dumps(rec, ensure_ascii=False)
    else:
        recipients_str = str(rec)
    kind = (
        inbox_delivery_kind
        or str(m.get("inbox_delivery_kind") or "").strip()
    )
    out: Dict[str, Any] = {
        "source_id": m.get("message_id") or "",
        "type": t,
        "datetime": m.get("datetime") or m.get("timestamp") or "",
        "sender": m.get("sender") or m.get("from") or "",
        "recipients": recipients_str,
        "participants": "",
        "summary": (m.get("subject") or "").strip() or "(No subject)",
        "body": (m.get("body") or "").strip(),
        "body_has_image": 1 if m.get("body_has_image") else 0,
        "thread_id": (m.get("thread_id") or "").strip(),
        "fetch_oauth_account_id": fetch_oauth_account_id or "",
    }
    if kind:
        out["inbox_delivery_kind"] = kind
    return out


def to_field_from_thread_message_ref(service: Any, ref: dict) -> str:
    """To header from a thread ``messages[]`` entry, or a metadata fetch if headers are missing."""
    payload = ref.get("payload") or {}
    headers = payload.get("headers") or []
    if not headers and ref.get("id"):
        try:
            meta = (
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=ref["id"],
                    format="metadata",
                    metadataHeaders=["To"],
                )
                .execute()
            )
            headers = (meta.get("payload") or {}).get("headers") or []
        except HttpError:
            return ""
    return get_header(headers, "To")


def recipient_headers_from_thread_message_ref(
    service: Any, ref: dict
) -> Tuple[str, str, str]:
    """To / Cc / Bcc from a thread ``messages[]`` entry (metadata fetch if headers missing)."""
    payload = ref.get("payload") or {}
    headers = payload.get("headers") or []
    if not headers and ref.get("id"):
        try:
            meta = (
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=ref["id"],
                    format="metadata",
                    metadataHeaders=["From", "To", "Cc", "Bcc", "Date", "Subject", "Content-Type"],
                )
                .execute()
            )
            headers = (meta.get("payload") or {}).get("headers") or []
        except HttpError:
            return "", "", ""
    return (
        get_header(headers, "To"),
        get_header(headers, "Cc"),
        get_header(headers, "Bcc"),
    )


def forwarder_email_from_inbox_delivery_messages(
    service: Any,
    sorted_refs: List[dict],
    inbox_lower: str,
    *,
    fallback: str,
) -> str:
    """
    Among Gmail thread messages that **deliver** to the Fivelanes inbox (To/Cc/Bcc),
    return the **From** address on the **newest** such message — that is who forwarded or
    sent mail into ``SOURCE_ACCOUNT``, not the inner ``From:`` of the embedded forward
    (those appear on non-delivery messages we keep for timeline body).
    """
    if not sorted_refs or not inbox_lower:
        return fallback

    def internal_date_ms(x: dict) -> int:
        v = x.get("internalDate")
        try:
            return int(v) if v is not None else 0
        except (TypeError, ValueError):
            return 0

    deliveries: List[dict] = []
    for ref in sorted_refs:
        to_, cc_, bb_ = _recipient_headers_from_thread_message_ref(service, ref)
        if recipients_contain_address(to_, cc_, bb_, inbox_lower):
            deliveries.append(ref)
    if not deliveries:
        return fallback
    shell = max(deliveries, key=_internal_date_ms)
    hdrs = (shell.get("payload") or {}).get("headers") or []
    if not hdrs and shell.get("id"):
        try:
            meta = (
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=shell["id"],
                    format="metadata",
                    metadataHeaders=["From", "To", "Cc", "Bcc"],
                )
                .execute()
            )
            hdrs = (meta.get("payload") or {}).get("headers") or []
        except HttpError:
            pass
    from_hdr = get_header(hdrs, "From")
    fe = primary_email_from_sender(from_hdr)
    return fe or fallback


def internal_date_ms(ref: dict) -> int:
    v = ref.get("internalDate")
    try:
        return int(v) if v is not None else 0
    except (TypeError, ValueError):
        return 0


def row_from_thread_ref(
    service: Any,
    tid: str,
    account_id: str,
    account_email: Optional[str],
    ref: dict,
    use_account_prefix: bool,
    include_body: bool,
) -> Optional[dict]:
    target_id = (ref.get("id") or "").strip()
    if not target_id:
        return None
    try:
        full_target = service.users().messages().get(
            userId="me", id=target_id, format="full"
        ).execute()
    except HttpError:
        return None
    return _row_from_full_message(
        service,
        tid,
        account_id,
        account_email,
        full_target,
        use_account_prefix,
        include_body,
    )


_get_account_email = get_account_email
_row_from_full_message = row_from_full_message
_api_message_row_to_timeline_entry = api_message_row_to_timeline_entry
_to_field_from_thread_message_ref = to_field_from_thread_message_ref
_recipient_headers_from_thread_message_ref = recipient_headers_from_thread_message_ref
_forwarder_email_from_inbox_delivery_messages = forwarder_email_from_inbox_delivery_messages
_internal_date_ms = internal_date_ms
_row_from_thread_ref = row_from_thread_ref
