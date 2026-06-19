"""Pure inbox routing and todo-plan helpers (no Gmail API dependencies)."""
from __future__ import annotations

import logging
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from services.email.forwarding import (
    body_contains_embedded_forwarded_thread,
    extract_envelope_rfc_message_id,
    extract_inner_rfc_message_id,
)
from services.email.recipients import (
    bcc_field_contains_address,
    cc_field_contains_address,
    to_field_contains_address,
)
from services.email.subject import extract_todo_plan_action, subject_core_indicates_todo
from utils.database import (
    create_thread_plan,
    fetch_thread_tracking_rows,
    load_thread_subjects,
    plan_exists_for_thread_action,
    todo_plan_is_dismissed,
    untrack_todo_plan_inbox_thread,
)

log = logging.getLogger(__name__)


class InboxRoute(str, Enum):
    TODO_PLAN = "todo_plan"
    FORWARD_TO = "forward_to"
    CC_BCC = "cc_bcc"
    DIRECT_TO = "direct_to"


def _recipient_headers_from_row(m: dict) -> Tuple[str, str, str]:
    rec = m.get("recipients") or {}
    if isinstance(rec, dict):
        return (
            str(rec.get("to") or m.get("to") or ""),
            str(rec.get("cc") or m.get("cc") or ""),
            str(rec.get("bcc") or m.get("bcc") or ""),
        )
    return (
        str(m.get("to") or ""),
        str(m.get("cc") or ""),
        str(m.get("bcc") or ""),
    )


def route_inbox_message(m: dict, inbox_lower: str) -> InboxRoute:
    """Classify how one inbox search hit should be handled."""
    inbox = (inbox_lower or "").strip().lower()
    to_, cc_, bcc_ = _recipient_headers_from_row(m)
    subject = str(m.get("subject") or "")
    body = str(m.get("body") or "")

    if inbox and to_field_contains_address(to_, inbox):
        if subject_core_indicates_todo(subject):
            return InboxRoute.TODO_PLAN
        if body_contains_embedded_forwarded_thread(body):
            return InboxRoute.FORWARD_TO
        return InboxRoute.DIRECT_TO

    if inbox and not to_field_contains_address(to_, inbox) and (
        cc_field_contains_address(cc_, inbox)
        or bcc_field_contains_address(bcc_, inbox)
    ):
        return InboxRoute.CC_BCC

    return InboxRoute.DIRECT_TO


def resolve_ref_id(m: dict, route: InboxRoute) -> str:
    """RFC Message-ID used to resolve the source mailbox thread."""
    if route not in (InboxRoute.FORWARD_TO, InboxRoute.CC_BCC):
        return ""
    body = m.get("body") or ""
    inner_body = extract_inner_rfc_message_id(body) or ""
    inner_env = extract_envelope_rfc_message_id(m) if isinstance(m, dict) else ""
    return inner_body or inner_env


def route_from_tracking(row: Dict[str, Any]) -> InboxRoute:
    raw = str(row.get("inbox_delivery_kind") or "").strip()
    if raw == "cc_bcc_only":
        raw = InboxRoute.CC_BCC.value
    try:
        return InboxRoute(raw)
    except ValueError:
        return InboxRoute.FORWARD_TO


def dedupe_timeline_rows_by_source_id(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: set[str] = set()
    out: List[Dict[str, Any]] = []
    for row in rows:
        sid = str(row.get("source_id") or "").strip()
        if not sid or sid in seen:
            continue
        seen.add(sid)
        out.append(row)
    return out


def process_todo_plan(m: dict, db_path: str) -> None:
    """Create a plan from a todo-subject email; no tracking or timeline."""
    action = extract_todo_plan_action(str(m.get("subject") or ""))
    tid = (m.get("thread_id") or "").strip()
    if not action:
        log.warning("Todo email thread_id=%s has empty plan action; skipping", tid)
        return
    if not tid:
        log.warning("Todo email missing thread_id; skipping plan")
        return
    if plan_exists_for_thread_action(db_path, tid, action):
        log.info("Todo plan already exists for thread_id=%s action=%r", tid, action)
    elif todo_plan_is_dismissed(db_path, tid, action):
        log.info(
            "Todo plan dismissed for thread_id=%s action=%r; skipping recreate",
            tid,
            action,
        )
    else:
        create_thread_plan(db_path, inbox_thread_id=tid, action=action)
        log.info("Created todo plan for thread_id=%s action=%r", tid, action)
    if untrack_todo_plan_inbox_thread(db_path, inbox_thread_id=tid):
        log.info("Removed tracking/timeline for todo-only inbox thread_id=%s", tid)


def purge_tracked_todo_only_threads(db_path: str) -> int:
    """
    Untrack inbox threads that only exist because of Todo: emails.

    Covers legacy rows tracked before todo routing skipped ``thread_tracking``.
    """
    purged = 0
    for row in fetch_thread_tracking_rows(db_path):
        tid = str(row.get("inbox_thread_id") or "").strip()
        if not tid or tid.startswith("text:"):
            continue
        kind = str(row.get("inbox_delivery_kind") or "").strip()
        if kind == InboxRoute.TODO_PLAN.value:
            untrack_todo_plan_inbox_thread(db_path, inbox_thread_id=tid)
            purged += 1
            continue
        subjects = load_thread_subjects(db_path, tid)
        if subjects and all(subject_core_indicates_todo(s) for s in subjects):
            untrack_todo_plan_inbox_thread(db_path, inbox_thread_id=tid)
            purged += 1
    if purged:
        log.info("Purged tracking/timeline for %d todo-only inbox thread(s)", purged)
    return purged
