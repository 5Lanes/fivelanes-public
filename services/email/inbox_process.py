"""Single orchestrator for inbox message routing, tracking, and thread expansion."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from googleapiclient.errors import HttpError

from services.email.config import DATABASE_NAME, SOURCE_ACCOUNT, SOURCE_OAUTH_ACCOUNT_ID
from services.email.forwarding import (
    extract_envelope_rfc_message_id,
    primary_email_from_sender,
)
from services.email.inbox_route import (
    InboxRoute,
    cc_bcc_fivelanes_thread_id,
    dedupe_timeline_rows_by_source_id,
    gmail_inbox_thread_id_for_tracking,
    inbox_cc_delivery_matches_ref,
    process_todo_plan,
    purge_tracked_todo_only_threads,
    resolve_ref_id,
    route_from_tracking,
    route_inbox_message,
)
from services.email.gmail_message import (
    api_message_row_to_timeline_entry,
    forwarder_email_from_inbox_delivery_messages,
    get_account_email,
    get_header,
    internal_date_ms,
    recipient_headers_from_thread_message_ref,
    row_from_full_message,
    row_from_thread_ref,
)
from services.email.inbox_pull import pull_fivelanes_inbox_messages
from services.email.message_body import gmail_message_is_draft
from services.email.recipients import (
    is_cc_bcc_only_recipient,
    recipients_contain_address,
    to_field_contains_address,
)
from services.email.thread_resolve import (
    bind_timeline_rows_to_inbox_thread,
    collect_thread_expansion_candidates,
    new_sent_source_ids,
    peek_timeline_source_ids_for_thread,
    pick_best_thread_expansion,
    pull_rfc_linked_thread_rows,
    pull_timeline_messages_for_threads,
    resolve_source_mailbox_thread,
    thread_timeline_is_current,
)
from services.gmail_client import (
    get_gmail_services_for_account_id,
    oauth_account_id_for_email,
    profile_email_to_account_id_map,
)
from services.thread_snooze import (
    is_removed,
    is_snoozed,
    maybe_unsnooze_email_thread,
    unseen_source_ids,
)
from utils.database import (
    collapse_thread_tracking_duplicates_by_inner_rfc,
    fetch_removed_inbox_thread_ids,
    fetch_thread_tracking_rows,
    prune_timeline_entries_for_thread,
    retire_legacy_gmail_forward_tracking,
    upsert_thread_tracking,
    upsert_timeline_entries,
)

log = logging.getLogger(__name__)


def _with_seed_meta(row: dict, *, route: InboxRoute, forwarder_email: str) -> dict:
    return {
        **row,
        "forwarder_email": forwarder_email,
        "inbox_delivery_kind": route.value,
        "inbox_route": route.value,
    }


def _trigger_message_id(m: dict) -> str:
    raw = str(m.get("message_id") or m.get("id") or "").strip()
    if ":" in raw:
        raw = raw.split(":", 1)[-1]
    return raw


def _ref_matching_trigger(refs: List[dict], m: dict) -> Optional[dict]:
    if not refs:
        return None
    tid = _trigger_message_id(m)
    if tid:
        for ref in refs:
            if str(ref.get("id") or "").strip() == tid:
                return ref
    return refs[-1]


def rewrite_inbox_seed(
    service: Any,
    account_id: str,
    account_email: Optional[str],
    m: dict,
    route: InboxRoute,
    inbox_lower: str,
    *,
    use_account_prefix: bool = False,
    include_body: bool = True,
) -> dict:
    """Pick the representative message for thread_tracking from an inbox Gmail thread."""
    tid = (m.get("thread_id") or "").strip()
    envelope_fallback = primary_email_from_sender(str(m.get("sender") or ""))

    if not tid or not inbox_lower:
        return _with_seed_meta(m, route=route, forwarder_email=envelope_fallback)

    try:
        thr = (
            service.users()
            .threads()
            .get(
                userId="me",
                id=tid,
                format="metadata",
                metadataHeaders=["From", "To", "Cc", "Bcc", "Date", "Subject", "Content-Type"],
            )
            .execute()
        )
        refs = [r for r in (thr.get("messages") or []) if not gmail_message_is_draft(r)]
    except HttpError:
        return _with_seed_meta(m, route=route, forwarder_email=envelope_fallback)

    if not refs:
        log.warning("No messages in thread %s", tid)
        return _with_seed_meta(m, route=route, forwarder_email=envelope_fallback)

    sorted_refs = sorted(refs, key=internal_date_ms)
    forwarder_email = forwarder_email_from_inbox_delivery_messages(
        service, sorted_refs, inbox_lower, fallback=envelope_fallback
    )

    if route == InboxRoute.DIRECT_TO:
        deliveries: List[dict] = []
        for ref in sorted_refs:
            to_hdr, cc_hdr, bcc_hdr = recipient_headers_from_thread_message_ref(
                service, ref
            )
            if recipients_contain_address(to_hdr, cc_hdr, bcc_hdr, inbox_lower):
                deliveries.append(ref)
        target_ref = _ref_matching_trigger(
            deliveries if deliveries else sorted_refs, m
        ) or sorted_refs[-1]
        row = row_from_thread_ref(
            service,
            tid,
            account_id,
            account_email,
            target_ref,
            use_account_prefix,
            include_body,
        )
        if row:
            return _with_seed_meta(row, route=route, forwarder_email=forwarder_email)
        return _with_seed_meta({**m, "forwarder_email": forwarder_email}, route=route, forwarder_email=forwarder_email)

    if route == InboxRoute.CC_BCC:
        cc_deliveries: List[dict] = []
        for ref in sorted_refs:
            to_hdr, cc_hdr, bcc_hdr = recipient_headers_from_thread_message_ref(
                service, ref
            )
            if is_cc_bcc_only_recipient(to_hdr, cc_hdr, bcc_hdr, inbox_lower):
                cc_deliveries.append(ref)
        if cc_deliveries:
            target_ref = _ref_matching_trigger(cc_deliveries, m) or cc_deliveries[-1]
            row = row_from_thread_ref(
                service,
                tid,
                account_id,
                account_email,
                target_ref,
                use_account_prefix,
                include_body,
            )
            if row:
                return _with_seed_meta(row, route=route, forwarder_email=forwarder_email)

    kept: List[dict] = []
    for ref in sorted_refs:
        to_hdr, cc_hdr, bcc_hdr = recipient_headers_from_thread_message_ref(
            service, ref
        )
        if route == InboxRoute.CC_BCC:
            if to_field_contains_address(to_hdr, inbox_lower):
                continue
        elif recipients_contain_address(to_hdr, cc_hdr, bcc_hdr, inbox_lower):
            continue
        kept.append(ref)

    if not kept:
        log.warning(
            "Timeline inbox thread %s: no message left after removing recipient %s",
            tid,
            inbox_lower,
        )
        return _with_seed_meta({**m, "forwarder_email": forwarder_email}, route=route, forwarder_email=forwarder_email)

    target_ref = _ref_matching_trigger(kept, m) or kept[-1]
    row = row_from_thread_ref(
        service,
        tid,
        account_id,
        account_email,
        target_ref,
        use_account_prefix,
        include_body,
    )
    if not row:
        return _with_seed_meta({**m, "forwarder_email": forwarder_email}, route=route, forwarder_email=forwarder_email)
    return _with_seed_meta(row, route=route, forwarder_email=forwarder_email)


def build_tracking_row(
    m: dict, route: InboxRoute, *, now_iso: str
) -> Optional[Dict[str, Any]]:
    """One thread_tracking row from a rewritten inbox seed message."""
    tid = (m.get("thread_id") or "").strip()
    if not tid:
        return None
    source_email = (m.get("forwarder_email") or "").strip().lower()
    if not source_email:
        log.warning(
            "build_tracking_row: missing forwarder for thread_id=%s route=%s",
            tid,
            route.value,
        )
        return None
    inner_rfc = resolve_ref_id(m, route)
    if not inner_rfc and route == InboxRoute.DIRECT_TO:
        inner_rfc = extract_envelope_rfc_message_id(m) or ""
    row: Dict[str, Any] = {
        "inbox_thread_id": tid,
        "source_email": source_email,
        "snoozed": 0,
        "inner_rfc_message_id": inner_rfc,
        "resolved_oauth_account_id": "",
        "resolution_error": "",
        "inbox_delivery_kind": route.value,
        "created_at": now_iso,
        "updated_at": now_iso,
    }
    if route in (InboxRoute.FORWARD_TO, InboxRoute.CC_BCC):
        fivelanes_tid = cc_bcc_fivelanes_thread_id(row["inner_rfc_message_id"])
        if fivelanes_tid:
            row["gmail_inbox_thread_id"] = tid
            row["inbox_thread_id"] = fivelanes_tid
    return row


def enumerate_inbox_thread_tracking_rows(
    service: Any,
    account_id: str,
    account_email: Optional[str],
    gmail_thread_id: str,
    inbox_lower: str,
    *,
    now_iso: str,
) -> List[Dict[str, Any]]:
    """One ``thread_tracking`` row per distinct inner RFC in an inbox Gmail thread."""
    tid = (gmail_thread_id or "").strip()
    if not tid or not inbox_lower:
        return []
    try:
        thr = (
            service.users()
            .threads()
            .get(userId="me", id=tid, format="full")
            .execute()
        )
    except HttpError as exc:
        log.warning("Inbox thread enumerate failed for %s: %s", tid, exc)
        return []

    refs = [r for r in (thr.get("messages") or []) if not gmail_message_is_draft(r)]
    if not refs:
        return []

    sorted_refs = sorted(refs, key=internal_date_ms)
    forwarder_email = forwarder_email_from_inbox_delivery_messages(
        service, sorted_refs, inbox_lower, fallback=""
    )
    rows_by_key: Dict[str, Dict[str, Any]] = {}
    for ref in sorted_refs:
        row = row_from_thread_ref(
            service,
            tid,
            account_id,
            account_email,
            ref,
            False,
            include_body=True,
        )
        if not row:
            continue
        route = route_inbox_message(row, inbox_lower)
        if route not in (InboxRoute.FORWARD_TO, InboxRoute.CC_BCC):
            continue
        seeded = _with_seed_meta(
            row,
            route=route,
            forwarder_email=forwarder_email,
        )
        track_row = build_tracking_row(seeded, route, now_iso=now_iso)
        if track_row:
            rows_by_key[track_row["inbox_thread_id"]] = track_row
    return list(rows_by_key.values())


def peek_cc_bcc_inbox_source_ids(
    service: Any,
    inbox_thread_id: str,
    inbox_lower: str,
    *,
    match_inner_rfc: str = "",
) -> Optional[set[str]]:
    """Gmail message ids for cc/bcc-only inbox deliveries (metadata-only)."""
    if not inbox_thread_id or not inbox_lower:
        return set()
    meta_headers = ["To", "Cc", "Bcc", "Message-ID", "In-Reply-To", "References"]
    try:
        thr = (
            service.users()
            .threads()
            .get(
                userId="me",
                id=inbox_thread_id,
                format="metadata",
                metadataHeaders=meta_headers,
            )
            .execute()
        )
    except HttpError as exc:
        log.warning(
            "Cc/Bcc inbox peek failed for thread_id=%s: %s", inbox_thread_id, exc
        )
        return None

    want_rfc = (match_inner_rfc or "").strip()
    out: set[str] = set()
    for msg in sorted(thr.get("messages") or [], key=internal_date_ms):
        if gmail_message_is_draft(msg):
            continue
        hdrs = (msg.get("payload") or {}).get("headers") or []
        to_h = get_header(hdrs, "To")
        cc_h = get_header(hdrs, "Cc")
        bcc_h = get_header(hdrs, "Bcc")
        if not is_cc_bcc_only_recipient(to_h, cc_h, bcc_h, inbox_lower):
            continue
        if want_rfc:
            api_row = {
                "header_in_reply_to": get_header(hdrs, "In-Reply-To"),
                "header_references": get_header(hdrs, "References"),
                "header_message_id": get_header(hdrs, "Message-ID"),
            }
            if not inbox_cc_delivery_matches_ref(api_row, want_rfc):
                continue
        mid = str(msg.get("id") or "").strip()
        if mid:
            out.add(mid)
    return out


def pull_messages_where_cc_bcc_only(
    service: Any,
    account_id: str,
    inbox_thread_id: str,
    inbox_lower: str,
    *,
    include_body: bool,
    fetch_oauth_account_id: str,
    match_inner_rfc: str = "",
) -> List[Dict[str, Any]]:
    """Emit cc/bcc-only inbox deliveries that match ``match_inner_rfc`` when set."""
    if not inbox_thread_id or not inbox_lower:
        return []
    try:
        thr = (
            service.users()
            .threads()
            .get(userId="me", id=inbox_thread_id, format="full")
            .execute()
        )
    except HttpError as exc:
        log.warning(
            "Cc/Bcc inbox pull failed for thread_id=%s: %s", inbox_thread_id, exc
        )
        return []

    account_email = get_account_email(service)
    want_rfc = (match_inner_rfc or "").strip()
    matched_msgs: List[Dict[str, Any]] = []
    for full_msg in sorted(thr.get("messages") or [], key=internal_date_ms):
        if gmail_message_is_draft(full_msg):
            continue
        pl = full_msg.get("payload") or {}
        hdrs = pl.get("headers") or []
        to_h = get_header(hdrs, "To")
        cc_h = get_header(hdrs, "Cc")
        bcc_h = get_header(hdrs, "Bcc")
        if not is_cc_bcc_only_recipient(to_h, cc_h, bcc_h, inbox_lower):
            continue
        api_row = row_from_full_message(
            service,
            inbox_thread_id,
            account_id,
            account_email,
            full_msg,
            False,
            include_body,
        )
        if want_rfc and not inbox_cc_delivery_matches_ref(api_row, want_rfc):
            continue
        matched_msgs.append(full_msg)

    if want_rfc and not matched_msgs:
        return []
    if not want_rfc and len(matched_msgs) > 1:
        matched_msgs = [matched_msgs[-1]]

    rows: List[Dict[str, Any]] = []
    for full_msg in matched_msgs:
        api_row = row_from_full_message(
            service,
            inbox_thread_id,
            account_id,
            account_email,
            full_msg,
            False,
            include_body,
        )
        entry = api_message_row_to_timeline_entry(
            api_row,
            fetch_oauth_account_id=fetch_oauth_account_id,
            inbox_delivery_kind=InboxRoute.CC_BCC.value,
        )
        if entry.get("source_id"):
            rows.append(entry)
    return rows


def _envelope_rfc_from_inbox_direct_thread(
    service: Any,
    inbox_thread_id: str,
    inbox_lower: str,
) -> str:
    """Envelope RFC Message-ID for the latest inbox delivery on a direct-to thread."""
    if not inbox_thread_id or not inbox_lower:
        return ""
    try:
        thr = (
            service.users()
            .threads()
            .get(userId="me", id=inbox_thread_id, format="full")
            .execute()
        )
    except HttpError as exc:
        log.warning(
            "Direct inbox RFC lookup failed for thread_id=%s: %s",
            inbox_thread_id,
            exc,
        )
        return ""

    account_email = get_account_email(service)
    deliveries: List[dict] = []
    for full_msg in sorted(thr.get("messages") or [], key=internal_date_ms):
        if gmail_message_is_draft(full_msg):
            continue
        pl = full_msg.get("payload") or {}
        hdrs = pl.get("headers") or []
        to_h = get_header(hdrs, "To")
        cc_h = get_header(hdrs, "Cc")
        bcc_h = get_header(hdrs, "Bcc")
        if recipients_contain_address(to_h, cc_h, bcc_h, inbox_lower):
            deliveries.append(full_msg)
    target = deliveries[-1] if deliveries else None
    if not target:
        return ""
    api_row = row_from_full_message(
        service,
        inbox_thread_id,
        "",
        account_email,
        target,
        False,
        include_body=False,
    )
    return (extract_envelope_rfc_message_id(api_row) or "").strip()


def _pull_and_bind_source_thread(
    *,
    account_id: str,
    service: Any,
    remote_thread_id: str,
    inbox_tid: str,
    route: InboxRoute,
    db_path: Optional[str],
    include_body: bool,
    force_full_refresh: bool,
) -> List[Dict[str, Any]]:
    """Pull messages from the source Gmail thread; bind rows to the inbox tracking key."""
    rows = bind_timeline_rows_to_inbox_thread(
        pull_timeline_messages_for_threads(
            service,
            account_id,
            [remote_thread_id],
            include_body=include_body,
            fetch_oauth_account_id=account_id,
            inbox_shell_skip="none",
            db_path=db_path,
            timeline_thread_id=inbox_tid,
            force_full_refresh=force_full_refresh,
        ),
        inbox_tid,
    )
    for r in rows:
        r["inbox_delivery_kind"] = route.value
    return rows


def _try_expand_from_source_mailbox_thread(
    row: Dict[str, Any],
    *,
    route: InboxRoute,
    inbox_tid: str,
    gmail_inbox_tid: str,
    source_email: str,
    inner_rfc: Optional[str],
    source_service: Any,
    inbox_lower: str,
    db_path: Optional[str],
    include_body: bool,
    force_full_refresh: bool,
) -> Optional[Tuple[List[Dict[str, Any]], str]]:
    """
    Pull timeline rows from the mailbox thread where the conversation lives.

    Inbox forward/cc shell copies use different Gmail message ids; those are ignored.
    """
    envelope_rfc = (inner_rfc or "").strip()
    if not envelope_rfc and route == InboxRoute.DIRECT_TO:
        envelope_rfc = _envelope_rfc_from_inbox_direct_thread(
            source_service, gmail_inbox_tid, inbox_lower
        )
    ctx = resolve_source_mailbox_thread(
        source_email=source_email,
        envelope_rfc=envelope_rfc,
    )
    if not ctx:
        return None
    account_id, service, remote_tid = ctx
    if (
        db_path
        and not force_full_refresh
        and thread_timeline_is_current(
            db_path,
            inbox_tid,
            peek_timeline_source_ids_for_thread(
                service,
                remote_tid,
                inbox_shell_skip="none",
            ),
        )
    ):
        log.info(
            "Thread unchanged (source-thread peek): inbox_thread_id=%s account=%s",
            inbox_tid,
            account_id,
        )
        return [], account_id
    rows = _pull_and_bind_source_thread(
        account_id=account_id,
        service=service,
        remote_thread_id=remote_tid,
        inbox_tid=inbox_tid,
        route=route,
        db_path=db_path,
        include_body=include_body,
        force_full_refresh=force_full_refresh,
    )
    log.info(
        "Thread expand (source mailbox): inbox_thread_id=%s account=%s remote_thread_id=%s messages=%d",
        inbox_tid,
        account_id,
        remote_tid,
        len(rows),
    )
    return rows, account_id


def _envelope_ref_from_inbox_cc_thread(
    service: Any,
    inbox_thread_id: str,
    inbox_lower: str,
) -> str:
    if not inbox_thread_id or not inbox_lower:
        return ""
    try:
        thr = (
            service.users()
            .threads()
            .get(userId="me", id=inbox_thread_id, format="full")
            .execute()
        )
    except HttpError as exc:
        log.warning(
            "Cc/Bcc RFC lookup failed for thread_id=%s: %s", inbox_thread_id, exc
        )
        return ""
    account_email = get_account_email(service)
    for full_msg in reversed(sorted(thr.get("messages") or [], key=internal_date_ms)):
        if gmail_message_is_draft(full_msg):
            continue
        pl = full_msg.get("payload") or {}
        hdrs = pl.get("headers") or []
        to_h = get_header(hdrs, "To")
        cc_h = get_header(hdrs, "Cc")
        bcc_h = get_header(hdrs, "Bcc")
        if not is_cc_bcc_only_recipient(to_h, cc_h, bcc_h, inbox_lower):
            continue
        api_row = row_from_full_message(
            service,
            inbox_thread_id,
            "",
            account_email,
            full_msg,
            False,
            include_body=False,
        )
        mid = (extract_envelope_rfc_message_id(api_row) or "").strip()
        if mid:
            return mid
    return ""


def _pinned_remote_peek(row: Dict[str, Any]) -> Optional[Tuple[str, Any, str, set[str]]]:
    """Pinned-account metadata peek: account id, service, remote thread id, source ids."""
    inner_rfc = (row.get("inner_rfc_message_id") or "").strip()
    pinned = (row.get("resolved_oauth_account_id") or "").strip()
    if not inner_rfc or not pinned:
        return None
    pairs = get_gmail_services_for_account_id(pinned)
    if not pairs:
        return None
    pinned_id, pinned_svc = pairs[0]
    from services.gmail_client import find_thread_id_by_rfc_message_id

    remote_tid = find_thread_id_by_rfc_message_id(pinned_svc, inner_rfc)
    if not remote_tid:
        return None
    peek_ids = peek_timeline_source_ids_for_thread(
        pinned_svc,
        remote_tid,
        inbox_shell_skip="none",
    )
    if peek_ids is None:
        return None
    return pinned_id, pinned_svc, remote_tid, set(peek_ids)


def _try_expand_via_pinned_account(
    row: Dict[str, Any],
    *,
    inner_rfc: str,
    inbox_tid: str,
    gmail_inbox_tid: str,
    route: InboxRoute,
    inbox_lower: str,
    source_service: Any,
    source_oauth_id: str,
    db_path: str,
    include_body: bool,
    force_full_refresh: bool,
) -> Optional[Tuple[List[Dict[str, Any]], Optional[str]]]:
    """
    Resolve inner RFC on the last successful OAuth account only.

    Returns ``None`` to fall back to multi-account candidate collection.
    """
    if force_full_refresh:
        return None
    pinned = (row.get("resolved_oauth_account_id") or "").strip()
    if not pinned:
        return None

    peek_ctx = _pinned_remote_peek(row)
    if peek_ctx is None:
        if pinned:
            log.info(
                "Pinned RFC resolve miss on account=%s inbox_thread_id=%s; full resolve",
                pinned,
                inbox_tid,
            )
        return None
    pinned_id, pinned_svc, remote_tid, peek_ids = peek_ctx

    if thread_timeline_is_current(db_path, inbox_tid, peek_ids):
        log.info(
            "Thread unchanged (pinned metadata peek): inbox_thread_id=%s account=%s",
            inbox_tid,
            pinned_id,
        )
        return [], pinned_id

    remote_rows = _pull_and_bind_source_thread(
        account_id=pinned_id,
        service=pinned_svc,
        remote_thread_id=remote_tid,
        inbox_tid=inbox_tid,
        route=route,
        db_path=db_path,
        include_body=include_body,
        force_full_refresh=True,
    )
    log.info(
        "Thread expand (pinned): inbox_thread_id=%s account=%s messages=%d",
        inbox_tid,
        pinned_id,
        len(remote_rows),
    )
    return remote_rows, pinned_id


def expand_thread(
    row: Dict[str, Any],
    *,
    source_service: Any,
    source_oauth_id: str,
    inbox_lower: str,
    include_body: bool = True,
    db_path: Optional[str] = None,
    force_full_refresh: bool = False,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Expand one tracked thread into timeline_entries rows."""
    inbox_tid = (row.get("inbox_thread_id") or "").strip()
    source_email = (row.get("source_email") or "").strip()
    inner_rfc = (row.get("inner_rfc_message_id") or "").strip() or None
    route = route_from_tracking(row)
    preferred = oauth_account_id_for_email(source_email)

    log.info(
        "Thread expand start: inbox_thread_id=%s source_email=%s route=%s has_inner_rfc=%s",
        inbox_tid,
        source_email,
        route.value,
        bool(inner_rfc),
    )
    if not inbox_tid:
        return [], None

    gmail_inbox_tid = gmail_inbox_thread_id_for_tracking(row) or inbox_tid

    if route == InboxRoute.DIRECT_TO:
        source_pull = _try_expand_from_source_mailbox_thread(
            row,
            route=route,
            inbox_tid=inbox_tid,
            gmail_inbox_tid=gmail_inbox_tid,
            source_email=source_email,
            inner_rfc=inner_rfc,
            source_service=source_service,
            inbox_lower=inbox_lower,
            db_path=db_path,
            include_body=include_body,
            force_full_refresh=force_full_refresh,
        )
        if source_pull is not None:
            return source_pull
        if (
            db_path
            and not force_full_refresh
            and thread_timeline_is_current(
                db_path,
                inbox_tid,
                peek_timeline_source_ids_for_thread(
                    source_service,
                    inbox_tid,
                    inbox_shell_skip="none",
                ),
            )
        ):
            log.info("Thread unchanged (metadata peek): inbox_thread_id=%s", inbox_tid)
            return [], source_oauth_id
        rows = pull_timeline_messages_for_threads(
            source_service,
            source_oauth_id,
            [inbox_tid],
            include_body=include_body,
            fetch_oauth_account_id=source_oauth_id,
            inbox_shell_skip="none",
            db_path=db_path,
            timeline_thread_id=inbox_tid,
            force_full_refresh=force_full_refresh,
        )
        for r in rows:
            r["inbox_delivery_kind"] = route.value
        return rows, source_oauth_id

    effective_inner = inner_rfc
    if not effective_inner and route == InboxRoute.CC_BCC and inbox_lower:
        effective_inner = _envelope_ref_from_inbox_cc_thread(
            source_service, gmail_inbox_tid, inbox_lower
        )
        if effective_inner:
            log.info("Thread expand: cc_bcc using envelope RFC from inbox thread")

    if not effective_inner:
        shell_skip = "skip_to_inbox" if route == InboxRoute.CC_BCC else "skip_all_inbox"
        if (
            db_path
            and not force_full_refresh
            and thread_timeline_is_current(
                db_path,
                inbox_tid,
                peek_timeline_source_ids_for_thread(
                    source_service,
                    gmail_inbox_tid,
                    inbox_shell_skip=shell_skip,
                    inbox_lower=inbox_lower,
                ),
            )
        ):
            log.info("Thread unchanged (metadata peek): inbox_thread_id=%s", inbox_tid)
            return [], source_oauth_id
        rows = pull_timeline_messages_for_threads(
            source_service,
            source_oauth_id,
            [gmail_inbox_tid],
            include_body=include_body,
            fetch_oauth_account_id=source_oauth_id,
            inbox_shell_skip=shell_skip,
            db_path=db_path,
            timeline_thread_id=inbox_tid,
            force_full_refresh=force_full_refresh,
        )
        for r in rows:
            r["inbox_delivery_kind"] = route.value
        return rows, source_oauth_id

    expand_row = (
        row
        if effective_inner == inner_rfc
        else {**row, "inner_rfc_message_id": effective_inner}
    )
    if db_path:
        pinned_result = _try_expand_via_pinned_account(
            expand_row,
            inner_rfc=effective_inner,
            inbox_tid=inbox_tid,
            gmail_inbox_tid=gmail_inbox_tid,
            route=route,
            inbox_lower=inbox_lower,
            source_service=source_service,
            source_oauth_id=source_oauth_id,
            db_path=db_path,
            include_body=include_body,
            force_full_refresh=force_full_refresh,
        )
        if pinned_result is not None:
            return pinned_result

    candidates = collect_thread_expansion_candidates(
        expand_row, include_body=include_body
    )
    best = pick_best_thread_expansion(candidates, preferred)

    if best:
        continuation = pull_rfc_linked_thread_rows(
            best,
            include_body=include_body,
        )
        remote_rows = bind_timeline_rows_to_inbox_thread(
            dedupe_timeline_rows_by_source_id(list(best.rows) + continuation),
            inbox_tid,
        )
        for r in remote_rows:
            r["inbox_delivery_kind"] = route.value

        if route == InboxRoute.CC_BCC:
            log.info(
                "Thread expand cc_bcc: remote=%d (inbox shell copies omitted)",
                len(remote_rows),
            )
            return remote_rows, best.account_id

        return remote_rows, best.account_id

    if route == InboxRoute.CC_BCC:
        log.warning(
            "Thread expand: no remote thread for cc_bcc inbox_thread_id=%s; inbox fallback",
            inbox_tid,
        )
        shell_skip = "skip_to_inbox"
        if (
            db_path
            and not force_full_refresh
            and thread_timeline_is_current(
                db_path,
                inbox_tid,
                peek_timeline_source_ids_for_thread(
                    source_service,
                    gmail_inbox_tid,
                    inbox_shell_skip=shell_skip,
                    inbox_lower=inbox_lower,
                ),
            )
        ):
            log.info("Thread unchanged (metadata peek): inbox_thread_id=%s", inbox_tid)
            return [], source_oauth_id
        rows = pull_timeline_messages_for_threads(
            source_service,
            source_oauth_id,
            [gmail_inbox_tid],
            include_body=include_body,
            fetch_oauth_account_id=source_oauth_id,
            inbox_shell_skip=shell_skip,
            db_path=db_path,
            timeline_thread_id=inbox_tid,
            force_full_refresh=force_full_refresh,
        )
        for r in rows:
            r["inbox_delivery_kind"] = route.value
        return rows, source_oauth_id

    source_pull = _try_expand_from_source_mailbox_thread(
        expand_row,
        route=route,
        inbox_tid=inbox_tid,
        gmail_inbox_tid=gmail_inbox_tid,
        source_email=source_email,
        inner_rfc=effective_inner,
        source_service=source_service,
        inbox_lower=inbox_lower,
        db_path=db_path,
        include_body=include_body,
        force_full_refresh=force_full_refresh,
    )
    if source_pull is not None:
        return source_pull

    shell_skip = "skip_all_inbox"
    if (
        db_path
        and not force_full_refresh
        and thread_timeline_is_current(
            db_path,
            inbox_tid,
            peek_timeline_source_ids_for_thread(
                source_service,
                gmail_inbox_tid,
                inbox_shell_skip=shell_skip,
                inbox_lower=inbox_lower,
            ),
        )
    ):
        log.info("Thread unchanged (metadata peek): inbox_thread_id=%s", inbox_tid)
        return [], source_oauth_id
    rows = pull_timeline_messages_for_threads(
        source_service,
        source_oauth_id,
        [gmail_inbox_tid],
        include_body=include_body,
        fetch_oauth_account_id=source_oauth_id,
        inbox_shell_skip=shell_skip,
        db_path=db_path,
        timeline_thread_id=inbox_tid,
        force_full_refresh=force_full_refresh,
    )
    for r in rows:
        r["inbox_delivery_kind"] = route.value
    return rows, source_oauth_id


def process_inbox_pipeline(
    db_path: Optional[str] = None,
    *,
    lookback_days: int,
    max_results: int = 500,
    source_account: Optional[str] = None,
) -> None:
    """Pull inbox mail, route, track, and expand into timeline_entries."""
    db = db_path or DATABASE_NAME
    now = datetime.now(timezone.utc).isoformat()
    inbox_eff = (source_account or SOURCE_ACCOUNT or "").strip().lower()
    if not inbox_eff:
        log.warning(
            "process_inbox_pipeline: no inbox address (set SOURCE_ACCOUNT or pass source_account=)"
        )
        return

    pairs = get_gmail_services_for_account_id(SOURCE_OAUTH_ACCOUNT_ID)
    if not pairs:
        log.warning(
            "No Gmail OAuth for SOURCE_OAUTH_ACCOUNT_ID=%r",
            SOURCE_OAUTH_ACCOUNT_ID,
        )
        return
    oauth_account_id, source_service = pairs[0]
    account_email = get_account_email(source_service)

    messages = pull_fivelanes_inbox_messages(
        max_results=max_results,
        lookback_days=lookback_days,
        source_account=source_account,
        include_body=True,
    )

    if not messages:
        log.warning(
            "No new inbox seed messages in range; will refresh previously tracked threads."
        )

    by_tid: Dict[str, Dict[str, Any]] = {}
    gmail_threads_to_scan: set[str] = set()
    for m in messages:
        route = route_inbox_message(m, inbox_eff)
        log.info(
            "inbox route=%s thread_id=%s",
            route.value,
            m.get("thread_id") or m.get("message_id") or "",
        )
        if route == InboxRoute.TODO_PLAN:
            process_todo_plan(m, db)
            continue
        rewritten = rewrite_inbox_seed(
            source_service,
            oauth_account_id,
            account_email,
            m,
            route,
            inbox_eff,
        )
        track_row = build_tracking_row(rewritten, route, now_iso=now)
        if track_row:
            by_tid[track_row["inbox_thread_id"]] = track_row
            gmail_tid = (m.get("thread_id") or "").strip()
            if gmail_tid and route in (InboxRoute.FORWARD_TO, InboxRoute.CC_BCC):
                gmail_threads_to_scan.add(gmail_tid)

    for row in fetch_thread_tracking_rows(db):
        if is_removed(row.get("snoozed")):
            continue
        gtid = gmail_inbox_thread_id_for_tracking(row)
        if gtid:
            gmail_threads_to_scan.add(gtid)

    for gmail_tid in gmail_threads_to_scan:
        for track_row in enumerate_inbox_thread_tracking_rows(
            source_service,
            oauth_account_id,
            account_email,
            gmail_tid,
            inbox_eff,
            now_iso=now,
        ):
            by_tid[track_row["inbox_thread_id"]] = track_row

    removed_ids = fetch_removed_inbox_thread_ids(db)
    tt_list = list(by_tid.values())
    if tt_list:
        if removed_ids:
            before = len(tt_list)
            tt_list = [
                r
                for r in tt_list
                if (r.get("inbox_thread_id") or "").strip() not in removed_ids
            ]
            skipped = before - len(tt_list)
            if skipped:
                log.info(
                    "Skipping thread_tracking upsert for %d removed inbox thread(s)",
                    skipped,
                )
        if tt_list:
            upsert_thread_tracking(db, tt_list)
            log.info("Upserted %d thread_tracking row(s)", len(tt_list))

    for gmail_tid in gmail_threads_to_scan:
        if retire_legacy_gmail_forward_tracking(db, gmail_tid):
            log.info(
                "Retired legacy forward_to tracking for inbox Gmail thread %s",
                gmail_tid,
            )

    purge_tracked_todo_only_threads(db)

    tracked_rows = fetch_thread_tracking_rows(db)
    tracked_rows = [
        r
        for r in tracked_rows
        if not is_removed(r.get("snoozed"))
        and not str(r.get("inbox_thread_id") or "").strip().startswith("text:")
        and not str(r.get("inbox_thread_id") or "").strip().startswith("slack:")
        and not str(r.get("inbox_thread_id") or "").strip().startswith("linkedin:")
    ]
    if not tracked_rows:
        if removed_ids and not tt_list:
            log.info(
                "No thread refresh: %d removed thread(s); no active tracking rows",
                len(removed_ids),
            )
        else:
            log.warning(
                "Nothing to write: no persisted thread_tracking rows "
                "(check OAuth and .env: SOURCE_ACCOUNT, SOURCE_OAUTH_ACCOUNT_ID)."
            )
        return

    profile_by_norm = profile_email_to_account_id_map()
    all_timeline: List[Dict[str, Any]] = []
    resolved_updates: List[Dict[str, Any]] = []
    processed_inbox_ids: set[str] = set()
    threads_touched_this_run = set(by_tid.keys())
    snoozed_skipped = 0

    for row in tracked_rows:
        inbox_thread_id = (row.get("inbox_thread_id") or "").strip()
        if not inbox_thread_id or inbox_thread_id in processed_inbox_ids:
            continue
        processed_inbox_ids.add(inbox_thread_id)

        if (
            is_snoozed(row.get("snoozed"))
            and inbox_thread_id not in threads_touched_this_run
            and route_from_tracking(row) != InboxRoute.CC_BCC
        ):
            peek_ctx = _pinned_remote_peek(row)
            if peek_ctx is not None and not unseen_source_ids(
                db, inbox_thread_id, peek_ctx[3]
            ):
                snoozed_skipped += 1
                continue

        log.info(
            "Thread refresh start: inbox_thread_id=%r source_email=%r",
            inbox_thread_id,
            (row.get("source_email") or "").strip(),
        )
        expanded, fetch_oauth_used = expand_thread(
            row,
            source_service=source_service,
            source_oauth_id=oauth_account_id,
            inbox_lower=inbox_eff,
            include_body=True,
            db_path=db,
            force_full_refresh=inbox_thread_id in threads_touched_this_run,
        )
        pulled_ids = {
            str(x.get("source_id") or "").strip()
            for x in expanded
            if str(x.get("source_id") or "").strip()
        }
        fetch_key = str(fetch_oauth_used or "").strip()
        new_ids = unseen_source_ids(db, inbox_thread_id, pulled_ids)
        new_sent_ids = new_sent_source_ids(expanded, new_ids, fetch_key)
        log.info(
            "Thread refresh result: inbox_thread_id=%r fetch_oauth_account_id=%r "
            "pulled_messages=%d new_messages=%d new_sent_messages=%d",
            inbox_thread_id,
            fetch_oauth_used,
            len(expanded),
            len(new_ids),
            len(new_sent_ids),
        )
        if maybe_unsnooze_email_thread(db, row, expanded):
            row["snoozed"] = 0

        if expanded and db:
            pruned = prune_timeline_entries_for_thread(db, inbox_thread_id, pulled_ids)
            if pruned:
                log.info(
                    "Pruned %d stale timeline row(s) for inbox_thread_id=%r",
                    pruned,
                    inbox_thread_id,
                )

        all_timeline.extend(expanded)
        fwd = (row.get("source_email") or "").strip()
        owner_key = oauth_account_id_for_email(fwd) if fwd else None
        if owner_key or fetch_key:
            resolution_error = ""
        else:
            profiles = sorted(profile_by_norm.keys())
            resolution_error = (
                f"No OAuth token matches forwarder envelope address {fwd!r}. "
                f"Connected Gmail profiles (normalized): {profiles}. "
                f"Timeline fetch_oauth_account_id was {fetch_oauth_used!r}."
            )
            log.error(
                "thread_tracking: unresolved forwarder for inbox_thread_id=%r: %s",
                row.get("inbox_thread_id"),
                resolution_error,
            )
        resolved_updates.append(
            {
                **row,
                "resolved_oauth_account_id": fetch_key or owner_key or "",
                "resolution_error": resolution_error,
                "updated_at": now,
            }
        )

    if snoozed_skipped:
        log.info(
            "Skipped Gmail refresh for %d snoozed thread(s) with no new inbox activity",
            snoozed_skipped,
        )

    if resolved_updates:
        upsert_thread_tracking(db, resolved_updates)

    collapsed = collapse_thread_tracking_duplicates_by_inner_rfc(db)
    if collapsed:
        log.info(
            "Collapsed %d duplicate thread_tracking row(s) sharing inner RFC",
            collapsed,
        )

    nt = upsert_timeline_entries(db, all_timeline)
    log.info(
        "Upserted %d row(s) into timeline_entries (%d message(s))",
        nt,
        len(all_timeline),
    )
