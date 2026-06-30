"""Gmail thread expansion primitives and timeline population entrypoint.

Canonical message ids: ``timeline_entries.source_id`` comes from the source mailbox
thread (``resolve_source_mailbox_thread``), not from Fivelanes inbox forward/cc shells.
Inbox ``thread_tracking.inbox_thread_id`` is unchanged and still drives snooze/removal.
See README § "Thread identity: inbox tracking vs timeline messages".
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

from googleapiclient.errors import HttpError

from services.email.config import SOURCE_ACCOUNT
from services.email.gmail_message import (
    api_message_row_to_timeline_entry,
    get_account_email,
    get_header,
    internal_date_ms,
    row_from_full_message,
)
from services.email.message_body import gmail_message_is_draft
from services.email.forwarding import _angle_bracket_ids
from services.email.recipients import (
    extract_emails_lower,
    recipients_contain_address,
    to_field_contains_address,
)
from services.gmail_client import (
    find_thread_id_by_rfc_message_id,
    get_all_gmail_services,
    get_gmail_services_for_account_id,
    mailbox_identity_emails,
    oauth_account_id_for_email,
)
from utils.database import connect_sqlite

log = logging.getLogger(__name__)


def existing_source_ids_for_inbox_thread(
    db_path: str, inbox_thread_id: str
) -> set[str]:
    """Gmail ``source_id`` values already stored for a Fivelanes ``thread_id``."""
    tid = (inbox_thread_id or "").strip()
    if not tid:
        return set()
    try:
        with connect_sqlite(db_path) as conn:
            rows = conn.execute(
                """
                SELECT source_id FROM timeline_entries
                WHERE thread_id = ?
                  AND COALESCE(TRIM(source_id), '') != ''
                """,
                (tid,),
            ).fetchall()
    except sqlite3.Error as exc:
        log.warning(
            "Could not read timeline source_ids for thread %s: %s",
            tid,
            exc,
        )
        return set()
    return {str(r[0]).strip() for r in rows if r and str(r[0]).strip()}


def thread_timeline_is_current(
    db_path: str,
    timeline_thread_id: str,
    remote_source_ids: Optional[set[str]],
) -> bool:
    """True when every remote message id is already in ``timeline_entries``."""
    if remote_source_ids is None:
        return False
    if not remote_source_ids:
        return not existing_source_ids_for_inbox_thread(db_path, timeline_thread_id)
    db_ids = existing_source_ids_for_inbox_thread(db_path, timeline_thread_id)
    return not (remote_source_ids - db_ids)


def peek_timeline_source_ids_for_thread(
    service: Any,
    gmail_thread_id: str,
    *,
    inbox_shell_skip: str = "none",
    inbox_lower: str = "",
) -> Optional[set[str]]:
    """
    Gmail message ids that would be emitted by ``pull_timeline_messages_for_threads``
    (metadata-only; no bodies).
    """
    tid = (gmail_thread_id or "").strip()
    if not tid:
        return set()
    try:
        thr = (
            service.users()
            .threads()
            .get(
                userId="me",
                id=tid,
                format="metadata",
                metadataHeaders=["To", "Cc", "Bcc"],
            )
            .execute()
        )
    except HttpError as exc:
        log.warning("threads.get (metadata) failed for thread_id=%s: %s", tid, exc)
        return None

    out: set[str] = set()
    for msg in sorted(thr.get("messages") or [], key=internal_date_ms):
        if gmail_message_is_draft(msg):
            continue
        if inbox_shell_skip != "none" and inbox_lower:
            hdrs = (msg.get("payload") or {}).get("headers") or []
            to_h = get_header(hdrs, "To")
            cc_h = get_header(hdrs, "Cc")
            bcc_h = get_header(hdrs, "Bcc")
            if _should_skip_inbox_shell(
                to_h, cc_h, bcc_h, inbox_lower, inbox_shell_skip
            ):
                continue
        mid = str(msg.get("id") or "").strip()
        if mid:
            out.add(mid)
    return out


def existing_source_ids_for_candidates(
    db_path: str, source_ids: set[str]
) -> set[str]:
    """Existing timeline source_ids among a candidate set, for pull diagnostics."""
    clean_ids = sorted({str(x).strip() for x in source_ids if str(x).strip()})
    if not clean_ids:
        return set()
    try:
        with connect_sqlite(db_path) as conn:
            placeholders = ",".join("?" for _ in clean_ids)
            sql = (
                "SELECT source_id FROM timeline_entries "
                f"WHERE source_id IN ({placeholders})"
            )
            rows = conn.execute(sql, clean_ids).fetchall()
    except sqlite3.Error as exc:
        log.warning(
            "Could not read existing source_ids for %d candidates: %s",
            len(clean_ids),
            exc,
        )
        return set()
    return {str(r[0]).strip() for r in rows if r and str(r[0]).strip()}


def timeline_row_is_sent_by_account(
    row: Dict[str, Any], identity: frozenset
) -> bool:
    if not identity:
        return False
    from_emails = extract_emails_lower(str(row.get("sender") or ""))
    return bool(from_emails & identity)


def new_sent_source_ids(
    expanded: List[Dict[str, Any]],
    new_ids: set[str],
    fetch_oauth_account_id: Optional[str],
) -> set[str]:
    """Among newly pulled messages, those sent from the OAuth account that fetched the thread."""
    if not new_ids or not fetch_oauth_account_id:
        return set()
    pairs = get_gmail_services_for_account_id(fetch_oauth_account_id)
    if not pairs:
        return set()
    aid, svc = pairs[0]
    identity = mailbox_identity_emails(svc, aid)
    return {
        str(row.get("source_id") or "").strip()
        for row in expanded
        if str(row.get("source_id") or "").strip() in new_ids
        and timeline_row_is_sent_by_account(row, identity)
    }


class ThreadExpansionCandidate(NamedTuple):
    account_id: str
    remote_thread_id: str
    rows: List[Dict[str, Any]]
    message_count: int


def gmail_account_candidates(source_email: str) -> List[Tuple[str, Any]]:
    """Connected Gmail accounts; forwarder's OAuth account first when known."""
    preferred = oauth_account_id_for_email(source_email)
    ordered: List[Tuple[str, Any]] = []
    seen: set[str] = set()

    if preferred:
        direct = get_gmail_services_for_account_id(preferred)
        if direct:
            aid, svc = direct[0]
            ordered.append((aid, svc))
            seen.add(aid)
        else:
            log.warning(
                "Thread resolver: forwarder account %s has no Gmail service; trying others",
                preferred,
            )

    for aid, svc in get_all_gmail_services():
        aid_s = str(aid or "").strip()
        if not aid_s or aid_s in seen:
            continue
        seen.add(aid_s)
        ordered.append((aid_s, svc))
    return ordered


def latest_row_datetime(rows: List[Dict[str, Any]]) -> str:
    best = ""
    for row in rows:
        dt = str(row.get("datetime") or "").strip()
        if dt > best:
            best = dt
    return best


def _normalize_rfc_message_id(ref: str) -> str:
    return (ref or "").strip().strip("<>")


def _rfc_ids_from_gmail_message(msg: dict) -> set[str]:
    """RFC Message-IDs from Message-ID / In-Reply-To / References headers."""
    hdrs = (msg.get("payload") or {}).get("headers") or []
    found: set[str] = set()
    for header_name in ("Message-ID", "In-Reply-To", "References"):
        val = get_header(hdrs, header_name)
        for rid in _angle_bracket_ids(val):
            norm = _normalize_rfc_message_id(rid)
            if norm and "@" in norm:
                found.add(norm)
    return found


def _ingest_thread_rfc_headers(
    service: Any, thread_id: str, *, seen_rfcs: set[str], frontier: List[str]
) -> None:
    try:
        thr = (
            service.users()
            .threads()
            .get(userId="me", id=thread_id, format="full")
            .execute()
        )
    except HttpError as exc:
        log.warning("RFC header fetch failed thread_id=%s: %s", thread_id, exc)
        return
    for msg in thr.get("messages") or []:
        for rfc in _rfc_ids_from_gmail_message(msg):
            if rfc not in seen_rfcs:
                seen_rfcs.add(rfc)
                frontier.append(rfc)


def pull_rfc_linked_thread_rows(
    candidate: ThreadExpansionCandidate,
    *,
    include_body: bool,
    max_rfc_lookups: int = 50,
) -> List[Dict[str, Any]]:
    """
    Pull Gmail threads reachable via RFC Message-ID / References / In-Reply-To only.

    Does not search by sender, recipient, subject, or relay envelope addresses.
    """
    pairs = get_gmail_services_for_account_id(candidate.account_id)
    if not pairs:
        return []
    aid, svc = pairs[0]
    remote_tid = (candidate.remote_thread_id or "").strip()
    if not remote_tid:
        return []

    seen_threads: set[str] = {remote_tid}
    seen_rfcs: set[str] = set()
    frontier: List[str] = []
    merged: List[Dict[str, Any]] = []
    lookups = 0

    _ingest_thread_rfc_headers(svc, remote_tid, seen_rfcs=seen_rfcs, frontier=frontier)

    while frontier and lookups < max_rfc_lookups:
        rfc = frontier.pop()
        lookups += 1
        linked_tid = find_thread_id_by_rfc_message_id(svc, rfc)
        if not linked_tid or linked_tid in seen_threads:
            continue
        seen_threads.add(linked_tid)
        cont = pull_remote_thread_candidate(
            aid, svc, linked_tid, include_body=include_body
        )
        if not cont.rows:
            continue
        log.info(
            "Thread resolver RFC-linked: account=%s remote_thread_id=%s messages=%d anchor_rfc=%s",
            aid,
            linked_tid,
            cont.message_count,
            rfc,
        )
        merged.extend(cont.rows)
        _ingest_thread_rfc_headers(
            svc, linked_tid, seen_rfcs=seen_rfcs, frontier=frontier
        )

    return merged


def resolve_source_mailbox_thread(
    *,
    source_email: str,
    envelope_rfc: str,
) -> Optional[Tuple[str, Any, str]]:
    """
    Locate the Gmail thread where a conversation lives (not the Fivelanes inbox copy).

    Uses the forwarder's connected OAuth account and an envelope RFC Message-ID.
    Returns ``(account_id, service, remote_thread_id)`` or ``None``.
    """
    rfc = (envelope_rfc or "").strip().strip("<>")
    if not rfc:
        return None
    preferred = oauth_account_id_for_email(source_email)
    if not preferred:
        return None
    pairs = get_gmail_services_for_account_id(preferred)
    if not pairs:
        return None
    aid, svc = pairs[0]
    remote_tid = find_thread_id_by_rfc_message_id(svc, rfc)
    if not remote_tid:
        return None
    return aid, svc, remote_tid


def bind_timeline_rows_to_inbox_thread(
    rows: List[Dict[str, Any]], inbox_thread_id: str
) -> List[Dict[str, Any]]:
    """
    Use the Fivelanes ``inbox_thread_id`` as ``thread_id`` on timeline rows.

    Source-mailbox pulls use a different Gmail ``threadId``; the dashboard groups by
    ``thread_id``, so Cc/Bcc and forward resolutions must share the inbox tracking key.
    """
    tid = (inbox_thread_id or "").strip()
    if not tid:
        return rows
    for row in rows:
        row["thread_id"] = tid
    return rows


def _should_skip_inbox_shell(
    to_h: str,
    cc_h: str,
    bcc_h: str,
    inbox_lower: str,
    shell_skip: str,
) -> bool:
    if shell_skip == "none" or not inbox_lower:
        return False
    if shell_skip == "skip_to_inbox":
        return to_field_contains_address(to_h, inbox_lower)
    if shell_skip == "skip_all_inbox":
        return recipients_contain_address(to_h, cc_h, bcc_h, inbox_lower)
    return False


def pull_remote_thread_candidate(
    account_id: str,
    service: Any,
    remote_thread_id: str,
    *,
    include_body: bool,
) -> ThreadExpansionCandidate:
    rows = pull_timeline_messages_for_threads(
        service,
        account_id,
        [remote_thread_id],
        include_body=include_body,
        fetch_oauth_account_id=account_id,
        inbox_shell_skip="none",
    )
    return ThreadExpansionCandidate(
        account_id=account_id,
        remote_thread_id=remote_thread_id,
        rows=rows,
        message_count=len(rows),
    )


def pick_best_thread_expansion(
    candidates: List[ThreadExpansionCandidate],
    preferred_account_id: Optional[str],
) -> Optional[ThreadExpansionCandidate]:
    """
    Prefer the forwarder's mailbox when it has at least as many messages as any other
    candidate; otherwise use the fullest thread (e.g. partial copy on another account).
    """
    if not candidates:
        return None

    def _sort_key(c: ThreadExpansionCandidate) -> Tuple[int, str]:
        return (c.message_count, latest_row_datetime(c.rows))

    global_best = max(candidates, key=_sort_key)
    if not preferred_account_id:
        return global_best

    pref_pool = [c for c in candidates if c.account_id == preferred_account_id]
    if not pref_pool:
        return global_best

    pref_best = max(pref_pool, key=_sort_key)
    if pref_best.message_count >= global_best.message_count:
        if pref_best.account_id != global_best.account_id:
            log.info(
                "Thread resolver: using forwarder account %s (%d msgs) over %s (%d msgs)",
                pref_best.account_id,
                pref_best.message_count,
                global_best.account_id,
                global_best.message_count,
            )
        return pref_best

    log.info(
        "Thread resolver: forwarder account %s has %d msgs; using %s with %d msgs",
        preferred_account_id,
        pref_best.message_count,
        global_best.account_id,
        global_best.message_count,
    )
    return global_best


def collect_thread_expansion_candidates(
    row: Dict[str, Any],
    *,
    include_body: bool,
) -> List[ThreadExpansionCandidate]:
    """Resolve ``inner_rfc_message_id`` on every connected account and pull each hit."""
    source_email = (row.get("source_email") or "").strip()
    inbox_tid = (row.get("inbox_thread_id") or "").strip()
    inner_rfc = (row.get("inner_rfc_message_id") or "").strip() or None
    candidates: List[ThreadExpansionCandidate] = []
    seen_pulls: set[Tuple[str, str]] = set()

    def _add_candidate(account_id: str, service: Any, remote_tid: str) -> None:
        key = (account_id, remote_tid)
        if key in seen_pulls:
            return
        seen_pulls.add(key)
        cand = pull_remote_thread_candidate(
            account_id, service, remote_tid, include_body=include_body
        )
        if cand.message_count:
            candidates.append(cand)
            log.info(
                "Thread resolver candidate: account=%s remote_thread_id=%s messages=%d",
                account_id,
                remote_tid,
                cand.message_count,
            )

    if inner_rfc:
        for aid, svc in gmail_account_candidates(source_email):
            log.info(
                "Thread resolver: rfc822msgid lookup on account=%s inbox_thread_id=%s",
                aid,
                inbox_tid,
            )
            remote_tid = find_thread_id_by_rfc_message_id(svc, inner_rfc)
            if remote_tid:
                _add_candidate(aid, svc, remote_tid)

    return candidates


def pull_timeline_messages_for_threads(
    service: Any,
    account_id: str,
    thread_ids: List[str],
    *,
    include_body: bool = True,
    fetch_oauth_account_id: str = "",
    inbox_shell_skip: str = "none",
    db_path: Optional[str] = None,
    timeline_thread_id: Optional[str] = None,
    force_full_refresh: bool = False,
) -> List[Dict[str, Any]]:
    """
    For each Gmail thread id, emit one ``timeline_entries`` row per message.

    ``inbox_shell_skip``:
    - ``none``: keep all messages
    - ``skip_to_inbox``: skip To-deliveries to ``SOURCE_ACCOUNT``
    - ``skip_all_inbox``: skip any message delivering to ``SOURCE_ACCOUNT``
    """
    ordered: List[str] = []
    seen_tid: set[str] = set()
    for tid in thread_ids:
        t = (tid or "").strip()
        if not t or t in seen_tid:
            continue
        seen_tid.add(t)
        ordered.append(t)

    rows: List[Dict[str, Any]] = []
    account_email = get_account_email(service)
    use_account_prefix = False
    fetch_key = fetch_oauth_account_id or account_id
    fivelanes_inbox = (SOURCE_ACCOUNT or "").strip().lower()

    for tid in ordered:
        log.info(
            "Thread-id pull start: account_id=%s thread_id=%s",
            account_id,
            tid,
        )
        timeline_tid = (timeline_thread_id or "").strip()
        if (
            db_path
            and timeline_tid
            and not force_full_refresh
            and thread_timeline_is_current(
                db_path,
                timeline_tid,
                peek_timeline_source_ids_for_thread(
                    service,
                    tid,
                    inbox_shell_skip=inbox_shell_skip,
                    inbox_lower=fivelanes_inbox,
                ),
            )
        ):
            log.info(
                "Thread unchanged (metadata peek): account_id=%s thread_id=%s timeline_thread_id=%s",
                account_id,
                tid,
                timeline_tid,
            )
            continue
        try:
            thr = (
                service.users()
                .threads()
                .get(userId="me", id=tid, format="full")
                .execute()
            )
        except HttpError as e:
            log.warning("threads.get failed for thread_id=%s: %s", tid, e)
            continue

        msgs = list(thr.get("messages") or [])
        if not msgs:
            continue

        sorted_msgs = sorted(msgs, key=internal_date_ms)
        skipped_shell = 0
        skipped_draft = 0
        emitted = 0
        for full_msg in sorted_msgs:
            if gmail_message_is_draft(full_msg):
                skipped_draft += 1
                continue
            if inbox_shell_skip != "none" and fivelanes_inbox:
                pl = full_msg.get("payload") or {}
                hdrs = pl.get("headers") or []
                to_h = get_header(hdrs, "To")
                cc_h = get_header(hdrs, "Cc")
                bcc_h = get_header(hdrs, "Bcc")
                if _should_skip_inbox_shell(
                    to_h, cc_h, bcc_h, fivelanes_inbox, inbox_shell_skip
                ):
                    skipped_shell += 1
                    continue
            api_row = row_from_full_message(
                service,
                tid,
                account_id,
                account_email,
                full_msg,
                use_account_prefix,
                include_body,
            )
            entry = api_message_row_to_timeline_entry(
                api_row, fetch_oauth_account_id=fetch_key
            )
            if entry.get("source_id"):
                rows.append(entry)
                emitted += 1
        log.info(
            "Thread-id pull result: account_id=%s thread_id=%s emitted=%d "
            "skipped_shell=%d skipped_draft=%d",
            account_id,
            tid,
            emitted,
            skipped_shell,
            skipped_draft,
        )

    return rows


def populate_timeline(
    db_path: Optional[str] = None,
    *,
    lookback_days: int,
    max_results: int = 500,
    source_account: Optional[str] = None,
) -> None:
    """Delegate to ``inbox_process.process_inbox_pipeline``."""
    from services.email.inbox_process import process_inbox_pipeline

    process_inbox_pipeline(
        db_path,
        lookback_days=lookback_days,
        max_results=max_results,
        source_account=source_account,
    )
