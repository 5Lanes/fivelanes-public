"""Gmail fetch for messages delivered to/cc/bcc the Fivelanes inbox."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from googleapiclient.errors import HttpError

from services.email.config import SOURCE_ACCOUNT, SOURCE_OAUTH_ACCOUNT_ID
from services.email.gmail_message import get_account_email
from services.email.message_body import (
    get_header,
    gmail_message_is_draft,
    message_body_has_image,
    message_body_text,
    message_timeline_type,
)
from services.email.recipients import extract_emails_lower, matches_contact_emails, recipients_contain_address
from services.gmail_client import get_gmail_services_for_account_id

log = logging.getLogger(__name__)


def pull_from_account(
    account_id: str,
    service: Any,
    emails: set,
    max_results: int,
    use_account_prefix: bool,
    include_body: bool,
    *,
    contact_query: Optional[str] = None,
    thread_query: Optional[str] = None,
    inbox_forward_lower: Optional[str] = None,
) -> List[dict]:
    """Fetch messages from one Gmail account."""
    if thread_query:
        q = thread_query
    elif contact_query:
        q = contact_query
    else:
        raise ValueError("Either contact_query or thread_query must be provided")
    account_email = get_account_email(service)
    list_kw: Dict[str, Any] = {
        "userId": "me",
        "maxResults": min(max_results, 500),
        "q": q,
    }
    try:
        log.info("Querying Gmail for %s", q)
        response = service.users().messages().list(**list_kw).execute()
    except HttpError as e:
        log.error("Gmail list error for %s: %s", account_id, e)
        raise
    messages = response.get("messages", [])
    log.info("Account %s: API returned %d message refs", account_id, len(messages))

    result = []
    for msg_ref in messages[:max_results]:
        msg_id = msg_ref["id"]
        try:
            metadata_msg = (
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=msg_id,
                    format="metadata",
                    metadataHeaders=["From", "To", "Cc", "Bcc", "Date", "Subject", "Content-Type"],
                )
                .execute()
            )
        except HttpError:
            continue

        if gmail_message_is_draft(metadata_msg):
            continue

        payload = metadata_msg.get("payload", {})
        headers = payload.get("headers", [])
        from_addr = get_header(headers, "From")
        to_addr = get_header(headers, "To")
        cc_addr = get_header(headers, "Cc")
        bcc_addr = get_header(headers, "Bcc")
        subject = get_header(headers, "Subject")
        date_str = get_header(headers, "Date")
        if inbox_forward_lower:
            if not recipients_contain_address(
                to_addr, cc_addr, bcc_addr, inbox_forward_lower
            ):
                continue
        elif thread_query:
            pass
        elif not matches_contact_emails(from_addr, to_addr, cc_addr, bcc_addr, emails):
            continue

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

        stored_id = f"{account_id}:{msg_id}" if use_account_prefix else msg_id
        from_emails = extract_emails_lower(from_addr)
        direction = "sent" if (account_email and account_email in from_emails) else "received"

        body_text = ""
        full_msg = metadata_msg
        if include_body:
            try:
                full_msg = (
                    service.users()
                    .messages()
                    .get(userId="me", id=msg_id, format="full")
                    .execute()
                )
            except HttpError:
                full_msg = metadata_msg
            body_text = message_body_text(service, msg_id, full_msg.get("payload", {}))
        message_type = message_timeline_type(full_msg if include_body else metadata_msg)

        pay = full_msg.get("payload") or {}
        hdrs = pay.get("headers") or []
        row = {
            "id": stored_id,
            "type": message_type,
            "message_id": msg_id,
            "thread_id": full_msg.get("threadId", metadata_msg.get("threadId", "")),
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
            "body_has_image": message_body_has_image(body_text, pay),
            "header_message_id": get_header(hdrs, "Message-ID"),
            "header_in_reply_to": get_header(hdrs, "In-Reply-To"),
            "header_references": get_header(hdrs, "References"),
        }
        result.append(row)
    return result


def pull_fivelanes_inbox_messages(
    max_results: int,
    *,
    lookback_days: int,
    after_date: Optional[str] = None,
    include_body: bool = True,
    source_account: Optional[str] = None,
) -> List[dict]:
    """
    Messages delivered to/cc/bcc the Fivelanes inbox (Gmail search + recipient check).

    Returns raw message dicts; routing and seed rewriting happen in ``inbox_process``.
    """
    pairs = get_gmail_services_for_account_id(SOURCE_OAUTH_ACCOUNT_ID)
    if not pairs:
        log.warning(
            "No Gmail OAuth for SOURCE_OAUTH_ACCOUNT_ID=%r (check credentials/tokens.json)",
            SOURCE_OAUTH_ACCOUNT_ID,
        )
        return []

    oauth_account_id, service = pairs[0]
    inbox_lower = (source_account or SOURCE_ACCOUNT or "").strip().lower()
    if not inbox_lower:
        log.warning(
            "No inbox address configured (pass source_account= or set SOURCE_ACCOUNT in .env)"
        )
        return []

    log.info(
        "Gmail: messages to/cc/bcc %s (OAuth key %s)", inbox_lower, oauth_account_id
    )

    date_prefix = ""
    if lookback_days < 0:
        raise ValueError("lookback_days must be >= 0")

    if after_date and len(after_date) >= 10:
        y, m, d = after_date[:4], int(after_date[5:7]), int(after_date[8:10])
        date_prefix = f"after:{y}/{m}/{d} "
    elif lookback_days > 0:
        d = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).date()
        date_prefix = f"after:{d.year}/{d.month:02d}/{d.day:02d} "

    q = f"{date_prefix}(to:{inbox_lower} OR cc:{inbox_lower} OR bcc:{inbox_lower})".strip()
    all_results: List[dict] = []
    try:
        batch = pull_from_account(
            oauth_account_id,
            service,
            set(),
            max_results,
            False,
            include_body,
            thread_query=q,
            inbox_forward_lower=inbox_lower,
        )
        all_results.extend(batch)
    except HttpError as e:
        log.error("Gmail API error for %s: %s", oauth_account_id, e)

    all_results.sort(key=lambda m: m.get("timestamp", ""), reverse=True)
    total = len(all_results)
    out = all_results[:max_results]
    if total > max_results:
        log.info(
            "Returning %d newest of %d inbox messages (max_results=%d)",
            len(out),
            total,
            max_results,
        )
    else:
        log.info("Matched %d inbox messages", total)
    return out
