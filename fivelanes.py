import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from routes.email_routes import pull_source_emails
from routes.llm_routes import run_fivelanes_llm_pipeline
from utils.database import ensure_database_schema
from utils.lookback_config import get_lookback_days
from utils.runtime_paths import database_path, load_env

log = logging.getLogger(__name__)

load_env()

DATABASE_NAME = database_path()
FIVELANES_BACKEND = os.getenv("FIVELANES_BACKEND") or "llama"


def run_email_pipeline(
    lookback_days: Optional[int] = None,
    *,
    db_path: Optional[str] = None,
    max_results: int = 500,
    source_account: Optional[str] = None,
) -> None:
    """
    Pull emails from the Fivelanes inbox.

    Inbox address defaults to ``SOURCE_ACCOUNT`` from ``.env`` (loaded in
    ``services.email``). Pass ``source_account`` only to override for this run.
    Lookback defaults to ``FIVELANES_LOOKBACK_DAYS`` from ``.env``.
    """
    pull_source_emails(
        lookback_days=get_lookback_days() if lookback_days is None else lookback_days,
        source_account=source_account,
        max_results=max_results,
        db_path=db_path or DATABASE_NAME,
    )

def run_llm_pipeline(
    lookback_days: Optional[int] = None,
    *,
    db_path: Optional[str] = None,
    backend: Optional[str] = None,
) -> None:
    """Segment messages in ``timeline_entries`` (grouped by thread) and summarize threads."""
    run_fivelanes_llm_pipeline(
        lookback_days=get_lookback_days() if lookback_days is None else lookback_days,
        db_path=db_path or DATABASE_NAME,
        backend=backend or FIVELANES_BACKEND,
    )


def run_force_resummary_active_threads(
    *,
    db_path: Optional[str] = None,
    dry_run: bool = False,
) -> None:
    """Re-summarize active threads from DB cleaned bodies (no re-segmentation)."""
    from utils.resummary_active_threads import force_resummary_active_threads

    n = force_resummary_active_threads(
        db_path=db_path or DATABASE_NAME,
        dry_run=dry_run,
    )
    log.info("Force resummary finished: %d thread(s) updated", n)


def main(
    lookback_days: Optional[int] = None,
    *,
    force_resummary: bool = False,
    dry_run: bool = False,
) -> None:
    from utils.logging import configure_logging

    configure_logging()
    ensure_database_schema(DATABASE_NAME)
    days = get_lookback_days() if lookback_days is None else lookback_days
    if force_resummary:
        run_force_resummary_active_threads(
            dry_run=dry_run,
        )
        return
    run_email_pipeline(lookback_days=days, max_results=500)
    run_llm_pipeline(lookback_days=days)
    try:
        from utils.features import is_enabled

        if is_enabled("texts"):
            from services.texts.summarize import summarize_tracked_text_threads

            summary_result = summarize_tracked_text_threads(DATABASE_NAME)
            log.info(
                "Text thread summaries: %d updated, %d skipped",
                summary_result.get("summarized", 0),
                summary_result.get("skipped", 0),
            )
        if is_enabled("slack"):
            from services.slack.summarize import summarize_tracked_slack_threads

            slack_result = summarize_tracked_slack_threads(DATABASE_NAME)
            log.info(
                "Slack thread summaries: %d updated, %d skipped",
                slack_result.get("summarized", 0),
                slack_result.get("skipped", 0),
            )
        if is_enabled("linkedin"):
            from services.linkedin.summarize import summarize_tracked_linkedin_threads

            linkedin_result = summarize_tracked_linkedin_threads(DATABASE_NAME)
            log.info(
                "LinkedIn thread summaries: %d updated, %d skipped",
                linkedin_result.get("summarized", 0),
                linkedin_result.get("skipped", 0),
            )
        if is_enabled("meet_recordings"):
            from services.meet_recordings.summarize import summarize_tracked_meet_recordings

            meet_result = summarize_tracked_meet_recordings(DATABASE_NAME)
            log.info(
                "Meet recording summaries: %d updated, %d skipped",
                meet_result.get("summarized", 0),
                meet_result.get("skipped", 0),
            )
        if is_enabled("calendar_events"):
            from services.calendar_events.matching import link_calendar_threads
            from services.calendar_events.summarize import summarize_tracked_calendar_event_threads
            from services.calendar_events.tracking import sync_calendar_event_threads

            sync_result = sync_calendar_event_threads(DATABASE_NAME)
            link_result = link_calendar_threads(DATABASE_NAME)
            cal_result = summarize_tracked_calendar_event_threads(DATABASE_NAME)
            log.info(
                "Calendar event threads: %d synced, %d linked to conversations, "
                "%d summaries updated, %d skipped",
                sync_result.get("synced", 0),
                link_result.get("linked", 0),
                cal_result.get("summarized", 0),
                cal_result.get("skipped", 0),
            )
    except Exception as exc:
        log.warning("Channel thread summarization skipped: %s", exc)
    if (os.getenv("FIVELANES_RETRY_FAILED") or "1").strip().lower() not in (
        "0",
        "false",
        "no",
    ):
        try:
            from utils.retry_failed_pipeline_outputs import (
                list_latest_failed_segmentation_pairs,
                list_thread_ids_with_bad_summary,
                retry_failed_pipeline_outputs,
            )

            if list_latest_failed_segmentation_pairs(DATABASE_NAME) or list_thread_ids_with_bad_summary(
                DATABASE_NAME
            ):
                retry_failed_pipeline_outputs(db_path=DATABASE_NAME)
        except Exception as exc:
            log.warning("Post-pipeline retry of failed outputs skipped: %s", exc)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Fivelanes inbox pipeline")
    parser.add_argument(
        "--force-resummary",
        action="store_true",
        help="Re-run summaries for active threads (uses existing cleaned bodies in DB)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List active threads without calling the LLM",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=None,
        help="Email pull / full pipeline lookback (default: FIVELANES_LOOKBACK_DAYS from .env)",
    )
    args = parser.parse_args()
    main(
        lookback_days=args.lookback_days,
        force_resummary=args.force_resummary,
        dry_run=args.dry_run,
    )