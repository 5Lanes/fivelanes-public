"""Gmail thread expansion primitives and timeline population entrypoint."""
from __future__ import annotations

import logging
import sqlite3
from email.utils import getaddresses
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
from services.email.forwarding import primary_email_from_sender
from services.email.recipients import (
    extract_emails_lower,
    recipients_contain_address,
    to_field_contains_address,
)
from services.email.subject import strip_subject_prefix_chain
from services.gmail_client import (
    find_thread_id_by_rfc_message_id,
    get_all_gmail_services,
    get_gmail_services_for_account_id,
    mailbox_identity_emails,
    normalize_gmail_address,
    oauth_account_id_for_email,
)

log = logging.getLogger(__name__)


def existing_source_ids_for_inbox_thread(
    db_path: str, inbox_thread_id: str
) -> set[str]:
    """Gmail ``source_id`` values already stored for a Fivelanes ``thread_id``."""
    tid = (inbox_thread_id or "").strip()
    if not tid:
        return set()
    try:
        with sqlite3.connect(db_path) as conn:
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
        with sqlite3.connect(db_path) as conn:
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


def anchor_metadata_from_inner_rfc(inner_rfc: str) -> Optional[Tuple[str, str]]:
    """
    Return ``(counterparty_email, subject_core)`` from the first connected mailbox
    that contains the inner RFC Message-ID (used to search the forwarder's account).
    """
    mid = (inner_rfc or "").strip().strip("<>")
    if not mid:
        return None
    q = f"rfc822msgid:{mid}"
    for aid, svc in get_all_gmail_services():
        try:
            resp = (
                svc.users()
                .messages()
                .list(userId="me", q=q, maxResults=1)
                .execute()
            )
            refs = resp.get("messages") or []
            if not refs or not refs[0].get("id"):
                continue
            meta = (
                svc.users()
                .messages()
                .get(
                    userId="me",
                    id=refs[0]["id"],
                    format="metadata",
                    metadataHeaders=["From", "To", "Subject"],
                )
                .execute()
            )
        except HttpError as exc:
            log.debug(
                "anchor metadata fetch failed on account=%s: %s", aid, exc
            )
            continue
        hdrs = (meta.get("payload") or {}).get("headers") or []
        from_h = get_header(hdrs, "From")
        to_h = get_header(hdrs, "To")
        subject_core = strip_subject_prefix_chain(get_header(hdrs, "Subject") or "")
        counterparty = ""
        for hdr in (from_h, to_h):
            for _, addr in getaddresses([hdr or ""]):
                em = normalize_gmail_address(addr)
                if em and "@" in em:
                    counterparty = em
                    break
            if counterparty:
                break
        if not counterparty:
            counterparty = primary_email_from_sender(from_h)
        if counterparty:
            log.info(
                "Thread resolver anchor from account=%s: counterparty=%s subject_core=%r",
                aid,
                counterparty,
                subject_core[:80] if subject_core else "",
            )
            return counterparty, subject_core
    return None


def gmail_thread_ids_for_correspondence(
    service: Any,
    counterparty_email: str,
    subject_core: str,
    *,
    max_results: int = 25,
) -> List[str]:
    """Gmail thread ids involving ``counterparty_email`` (optional subject filter)."""
    cp = (counterparty_email or "").strip().lower()
    if not cp or "@" not in cp:
        return []
    q_parts = [f"(from:{cp} OR to:{cp})"]
    if subject_core:
        safe_subj = (subject_core or "").replace('"', "").strip()
        if safe_subj:
            q_parts.append(f'subject:"{safe_subj}"')
    q = " ".join(q_parts)
    try:
        resp = (
            service.users()
            .messages()
            .list(userId="me", q=q, maxResults=max_results)
            .execute()
        )
    except HttpError as exc:
        log.warning("correspondence thread search failed: %s", exc)
        return []
    ordered: List[str] = []
    seen: set[str] = set()
    for ref in resp.get("messages") or []:
        tid = (ref.get("threadId") or "").strip()
        if tid and tid not in seen:
            seen.add(tid)
            ordered.append(tid)
    return ordered


def add_correspondence_candidates_on_forwarder(
    candidates: List[ThreadExpansionCandidate],
    *,
    preferred_account_id: str,
    inner_rfc: str,
    include_body: bool,
    seen_pulls: set[Tuple[str, str]],
    add_fn: Any,
) -> None:
    """
    When the forwarder's mailbox has no (or a shorter) RFC-resolved thread, search by
    counterparty + subject from the anchor message on another account.
    """
    pairs = get_gmail_services_for_account_id(preferred_account_id)
    if not pairs:
        return
    pref_max = max(
        (c.message_count for c in candidates if c.account_id == preferred_account_id),
        default=0,
    )
    global_max = max((c.message_count for c in candidates), default=0)
    if pref_max >= global_max and pref_max > 0:
        return

    anchor = anchor_metadata_from_inner_rfc(inner_rfc)
    if not anchor:
        return
    counterparty, subject_core = anchor
    aid, svc = pairs[0]
    thread_ids = gmail_thread_ids_for_correspondence(
        svc, counterparty, subject_core
    )
    log.info(
        "Thread resolver: correspondence search on forwarder account=%s "
        "counterparty=%s threads=%d (pref_max=%d global_max=%d)",
        aid,
        counterparty,
        len(thread_ids),
        pref_max,
        global_max,
    )
    for tid in thread_ids:
        add_fn(aid, svc, tid)


def collect_thread_expansion_candidates(
    row: Dict[str, Any],
    *,
    include_body: bool,
) -> List[ThreadExpansionCandidate]:
    """
    Resolve ``inner_rfc_message_id`` on every connected account and pull each hit.
    Also try a direct pull on ``resolved_oauth_account_id`` + ``inbox_thread_id`` when set.
    """
    source_email = (row.get("source_email") or "").strip()
    inbox_tid = (row.get("inbox_thread_id") or "").strip()
    inner_rfc = (row.get("inner_rfc_message_id") or "").strip() or None
    preferred = oauth_account_id_for_email(source_email)
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

    pinned_account = (row.get("resolved_oauth_account_id") or "").strip()
    if pinned_account and inbox_tid:
        pairs = get_gmail_services_for_account_id(pinned_account)
        if pairs:
            aid, svc = pairs[0]
            log.info(
                "Thread resolver: direct pull account=%s thread_id=%s",
                aid,
                inbox_tid,
            )
            _add_candidate(aid, svc, inbox_tid)

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

        if preferred:
            add_correspondence_candidates_on_forwarder(
                candidates,
                preferred_account_id=preferred,
                inner_rfc=inner_rfc,
                include_body=include_body,
                seen_pulls=seen_pulls,
                add_fn=_add_candidate,
            )

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
