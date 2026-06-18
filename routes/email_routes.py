import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

from services.email import (
    DATABASE_NAME,
    SOURCE_ACCOUNT,
    SOURCE_OAUTH_ACCOUNT_ID,
    build_tracking_row,
    populate_timeline,
    pull_fivelanes_inbox_messages,
    rewrite_inbox_seed,
    route_inbox_message,
)
from services.email.gmail_message import get_account_email
from services.gmail_client import get_gmail_services_for_account_id
from utils.database import upsert_thread_tracking

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

log = logging.getLogger(__name__)


def pull_source_emails(
    *,
    lookback_days: int,
    source_account: Optional[str] = None,
    db_path: Optional[str] = None,
    max_results: int = 500,
) -> None:
    """Route entrypoint: sync Gmail → ``thread_tracking`` + ``timeline_entries``."""
    db = db_path or DATABASE_NAME
    populate_timeline(
        db_path=db,
        lookback_days=lookback_days,
        source_account=source_account,
        max_results=max_results,
    )


def add_new_thread_tracking_row(
    *,
    source_email: Optional[str] = None,
    db_path: Optional[str] = None,
) -> None:
    """
    Add thread_tracking rows for inbox messages whose forwarder matches ``source_email``.
    """
    want = (source_email or "").strip().lower()
    if not want:
        return
    now_iso = datetime.now(timezone.utc).isoformat()
    inbox = (SOURCE_ACCOUNT or "").strip().lower()
    pairs = get_gmail_services_for_account_id(SOURCE_OAUTH_ACCOUNT_ID)
    if not pairs:
        log.warning("No Gmail OAuth for add_new_thread_tracking_row")
        return
    oauth_account_id, service = pairs[0]
    account_email = get_account_email(service)
    messages = pull_fivelanes_inbox_messages(
        max_results=1000,
        lookback_days=0,
    )
    rows: List[Dict[str, Any]] = []
    for message in messages:
        route = route_inbox_message(message, inbox)
        if route.value == "todo_plan":
            continue
        rewritten = rewrite_inbox_seed(
            service,
            oauth_account_id,
            account_email,
            message,
            route,
            inbox,
        )
        row = build_tracking_row(rewritten, route, now_iso=now_iso)
        if row and (row.get("source_email") or "").strip().lower() == want:
            rows.append(row)
    if rows:
        upsert_thread_tracking(db_path or DATABASE_NAME, rows)
        log.info("Upserted %d thread_tracking row(s) for source_email=%r", len(rows), want)
    else:
        log.warning("No matching inbox messages for source_email=%r", want)


def update_thread_tracking_row(
    *,
    thread_id: str,
    source_email: str,
) -> None:
    """
    Update the thread tracking row for the given thread id.
    """
    pass
