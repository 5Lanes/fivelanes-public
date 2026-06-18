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
    dedupe_timeline_rows_by_source_id,
    process_todo_plan,
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
    existing_source_ids_for_candidates,
    new_sent_source_ids,
    pick_best_thread_expansion,
    pull_timeline_messages_for_threads,
)
from services.gmail_client import (
    get_gmail_services_for_account_id,
    oauth_account_id_for_email,
    profile_email_to_account_id_map,
)
from utils.database import (
    clear_snooze_only_for_threads,
    collapse_thread_tracking_duplicates_by_inner_rfc,
    fetch_removed_inbox_thread_ids,
    fetch_thread_tracking_rows,
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
        target_ref = deliveries[-1] if deliveries else sorted_refs[-1]
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
            row = row_from_thread_ref(
                service,
                tid,
                account_id,
                account_email,
                cc_deliveries[-1],
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

    row = row_from_thread_ref(
        service,
        tid,
        account_id,
        account_email,
        kept[-1],
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
        source_email = primary_email_from_sender(str(m.get("sender") or ""))
    if not source_email:
        return None
    return {
        "inbox_thread_id": tid,
        "source_email": source_email,
        "snoozed": 0,
        "inner_rfc_message_id": resolve_ref_id(m, route),
        "resolved_oauth_account_id": "",
        "resolution_error": "",
        "inbox_delivery_kind": route.value,
        "created_at": now_iso,
        "updated_at": now_iso,
    }


def pull_messages_where_cc_bcc_only(
    service: Any,
    account_id: str,
    inbox_thread_id: str,
    inbox_lower: str,
    *,
    include_body: bool,
    fetch_oauth_account_id: str,
) -> List[Dict[str, Any]]:
    """Emit cc/bcc-only deliveries from the Fivelanes inbox Gmail thread."""
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
    rows: List[Dict[str, Any]] = []
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
        entry = api_message_row_to_timeline_entry(
            api_row,
            fetch_oauth_account_id=fetch_oauth_account_id,
            inbox_delivery_kind=InboxRoute.CC_BCC.value,
        )
        if entry.get("source_id"):
            rows.append(entry)
    return rows


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


def expand_thread(
    row: Dict[str, Any],
    *,
    source_service: Any,
    source_oauth_id: str,
    inbox_lower: str,
    include_body: bool = True,
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

    if route == InboxRoute.DIRECT_TO:
        rows = pull_timeline_messages_for_threads(
            source_service,
            source_oauth_id,
            [inbox_tid],
            include_body=include_body,
            fetch_oauth_account_id=source_oauth_id,
            inbox_shell_skip="none",
        )
        for r in rows:
            r["inbox_delivery_kind"] = route.value
        return rows, source_oauth_id

    effective_inner = inner_rfc
    if not effective_inner and route == InboxRoute.CC_BCC and inbox_lower:
        effective_inner = _envelope_ref_from_inbox_cc_thread(
            source_service, inbox_tid, inbox_lower
        )
        if effective_inner:
            log.info("Thread expand: cc_bcc using envelope RFC from inbox thread")

    if not effective_inner:
        shell_skip = "skip_to_inbox" if route == InboxRoute.CC_BCC else "skip_all_inbox"
        rows = pull_timeline_messages_for_threads(
            source_service,
            source_oauth_id,
            [inbox_tid],
            include_body=include_body,
            fetch_oauth_account_id=source_oauth_id,
            inbox_shell_skip=shell_skip,
        )
        for r in rows:
            r["inbox_delivery_kind"] = route.value
        return rows, source_oauth_id

    expand_row = (
        row
        if effective_inner == inner_rfc
        else {**row, "inner_rfc_message_id": effective_inner}
    )
    candidates = collect_thread_expansion_candidates(
        expand_row, include_body=include_body
    )
    best = pick_best_thread_expansion(candidates, preferred)

    if best:
        remote_rows = bind_timeline_rows_to_inbox_thread(list(best.rows), inbox_tid)
        for r in remote_rows:
            r["inbox_delivery_kind"] = route.value

        if route == InboxRoute.CC_BCC:
            inbox_cc_rows = pull_messages_where_cc_bcc_only(
                source_service,
                source_oauth_id,
                inbox_tid,
                inbox_lower,
                include_body=include_body,
                fetch_oauth_account_id=source_oauth_id,
            )
            merged = dedupe_timeline_rows_by_source_id(remote_rows + inbox_cc_rows)
            log.info(
                "Thread expand cc_bcc: remote=%d inbox_cc=%d merged=%d",
                len(remote_rows),
                len(inbox_cc_rows),
                len(merged),
            )
            return merged, best.account_id

        return remote_rows, best.account_id

    if route == InboxRoute.CC_BCC:
        log.warning(
            "Thread expand: no remote thread for cc_bcc inbox_thread_id=%s",
            inbox_tid,
        )
        return [], preferred or source_oauth_id

    shell_skip = "skip_all_inbox"
    rows = pull_timeline_messages_for_threads(
        source_service,
        source_oauth_id,
        [inbox_tid],
        include_body=include_body,
        fetch_oauth_account_id=source_oauth_id,
        inbox_shell_skip=shell_skip,
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

    tracked_rows = fetch_thread_tracking_rows(db)
    tracked_rows = [
        r
        for r in tracked_rows
        if int(r.get("snoozed") or 0) != 2
        and not str(r.get("inbox_thread_id") or "").strip().startswith("text:")
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

    for row in tracked_rows:
        inbox_thread_id = (row.get("inbox_thread_id") or "").strip()
        if not inbox_thread_id or inbox_thread_id in processed_inbox_ids:
            continue
        processed_inbox_ids.add(inbox_thread_id)

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
        )
        pulled_ids = {
            str(x.get("source_id") or "").strip()
            for x in expanded
            if str(x.get("source_id") or "").strip()
        }
        existing_ids = existing_source_ids_for_candidates(db, pulled_ids)
        new_ids = pulled_ids - existing_ids
        prior_in_timeline = bool(existing_ids)
        fetch_key = str(fetch_oauth_used or "").strip()
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
        should_clear_snooze = int(row.get("snoozed") or 0) == 1 and (
            (new_ids and prior_in_timeline) or new_sent_ids
        )
        if should_clear_snooze:
            clear_snooze_only_for_threads(db, [inbox_thread_id])
            row["snoozed"] = 0
            log.info("Cleared snooze for inbox_thread_id=%r", inbox_thread_id)

        all_timeline.extend(expanded)
        fwd = (row.get("source_email") or "").strip()
        owner_key = oauth_account_id_for_email(fwd) if fwd else None
        if owner_key:
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
