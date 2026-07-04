"""
Legacy entrypoint: Gmail inbox + calendar merged into ``timeline_entries`` only.

The main Fivelanes pipeline is ``fivelanes.main`` (``thread_tracking`` +
``timeline_entries`` + multi-mailbox thread expansion).
"""

import json
import logging
from typing import Any, Dict, List

from utils.database import upsert_timeline_entries
from utils.runtime_paths import database_path
from services.calendar_service import pull_events_for_contacts
from services.gmail_client import get_gmail_services_for_account_id
from services.email.config import SOURCE_ACCOUNT, SOURCE_OAUTH_ACCOUNT_ID
from services.email.inbox_pull import pull_fivelanes_inbox_messages

log = logging.getLogger(__name__)

def _calendar_rows_for_timeline(events: List[dict]) -> List[Dict[str, Any]]:
    """Calendar events as ``timeline_entries`` rows (``type='meeting'``)."""
    rows: List[Dict[str, Any]] = []
    for ev in events:
        participants = ev.get("attendees_emails") or []
        if isinstance(participants, list):
            participants_str = ", ".join(sorted(participants))
        else:
            participants_str = str(participants)
        rows.append(
            {
                "source_id": ev.get("id") or "",
                "type": "meeting",
                "datetime": ev.get("start_iso") or "",
                "sender": "",
                "recipients": "",
                "participants": participants_str,
                "summary": (ev.get("summary") or "").strip() or "(No title)",
                "body": "",
            }
        )
    return rows


def _timeline_rows(messages: List[dict]) -> List[Dict[str, Any]]:
    """Rows for ``timeline_entries``."""
    rows: List[Dict[str, Any]] = []
    for m in messages:
        t = m.get("type") or "email"
        if t not in ("email", "meeting_invite"):
            t = "email"
        rec = m.get("recipients") or {}
        if isinstance(rec, dict):
            recipients_str = json.dumps(rec, ensure_ascii=False)
        else:
            recipients_str = str(rec)
        rows.append(
            {
                "source_id": m.get("message_id") or "",
                "type": t,
                "datetime": m.get("datetime") or m.get("timestamp") or "",
                "sender": m.get("sender") or m.get("from") or "",
                "recipients": recipients_str,
                "participants": "",
                "summary": (m.get("subject") or "").strip() or "(No subject)",
                "body": (m.get("body") or "").strip(),
            }
        )
    return rows


def populate_timeline(
    db_path: str | None = None,
    *,
    lookback_days: int,
    lookforward_days: int,
) -> None:
    """
    1. Gmail: mail to the configured Fivelanes inbox (see ``services/email`` / ``.env``).
    2. Calendar: same OAuth key; events listing that inbox as participant.
    3. Upsert into ``timeline_entries``.
    """
    messages = pull_fivelanes_inbox_messages(
        max_results=500,
        lookback_days=lookback_days,
    )

    calendar_events = pull_events_for_contacts(
        [SOURCE_ACCOUNT],
        lookback_days=lookback_days,
        lookforward_days=lookforward_days,
        max_results_per_account=250,
        only_account_id=SOURCE_OAUTH_ACCOUNT_ID,
    )

    if not messages and not calendar_events:
        log.warning(
            "Nothing to write: no Gmail messages and no calendar events in range "
            "(check OAuth and .env: SOURCE_ACCOUNT, SOURCE_OAUTH_ACCOUNT_ID)."
        )
        return

    meeting_rows = _calendar_rows_for_timeline(calendar_events)
    email_rows = _timeline_rows(messages)
    all_rows = meeting_rows + email_rows
    all_rows.sort(key=lambda r: (r.get("datetime") or ""))

    db = db_path or database_path()
    n = upsert_timeline_entries(db, all_rows)
    log.info(
        "Upserted %d rows into timeline_entries (%d calendar meetings, %d Gmail messages)",
        n,
        len(calendar_events),
        len(messages),
    )


def create_summary():
    pass


def main(lookback_days: int | None = None, lookforward_days: int = 14):
    from utils.lookback_config import get_lookback_days

    logging.basicConfig(level=logging.INFO)

    populate_timeline(
        lookback_days=get_lookback_days() if lookback_days is None else lookback_days,
        lookforward_days=lookforward_days,
    )


if __name__ == "__main__":
    main()
