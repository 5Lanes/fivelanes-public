import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from utils.runtime_paths import database_path, load_env

load_env()

DATABASE_NAME = database_path()
FIVELANES_BACKEND = os.getenv("FIVELANES_BACKEND") or "llama"


def run_email_pipeline(
    lookback_days: int = 14,
    *,
    db_path: Optional[str] = None,
    max_results: int = 500,
    source_account: Optional[str] = None,
) -> None:
    """
    Pull emails from the Fivelanes inbox.

    Inbox address defaults to ``SOURCE_ACCOUNT`` from ``.env`` (loaded in
    ``services.email``). Pass ``source_account`` only to override for this run.
    """
    pull_source_emails(
        lookback_days=lookback_days,
        source_account=source_account,
        max_results=max_results,
        db_path=db_path or DATABASE_NAME,
    )

def run_llm_pipeline(
    lookback_days: int = 14,
    *,
    db_path: Optional[str] = None,
    backend: Optional[str] = None,
) -> None:
    """Segment messages in ``timeline_entries`` (grouped by thread) and summarize threads."""
    run_fivelanes_llm_pipeline(
        lookback_days=lookback_days,
        db_path=db_path or DATABASE_NAME,
        backend=backend or FIVELANES_BACKEND,
    )


def run_force_resummary_active_threads(
    lookback_days: int = 14,
    *,
    db_path: Optional[str] = None,
    dry_run: bool = False,
) -> None:
    """Re-summarize active threads from DB cleaned bodies (no re-segmentation)."""
    from utils.resummary_active_threads import force_resummary_active_threads

    n = force_resummary_active_threads(
        lookback_days=lookback_days,
        db_path=db_path or DATABASE_NAME,
        dry_run=dry_run,
    )
    log.info("Force resummary finished: %d thread(s) updated", n)


def main(
    lookback_days: int = 180,
    *,
    force_resummary: bool = False,
    resummary_lookback_days: int = 14,
    dry_run: bool = False,
) -> None:
    from utils.logging import configure_logging

    configure_logging()
    if force_resummary:
        run_force_resummary_active_threads(
            lookback_days=resummary_lookback_days,
            dry_run=dry_run,
        )
        return
    run_email_pipeline(lookback_days=lookback_days, max_results=500)
    run_llm_pipeline(lookback_days=lookback_days)
    try:
        from services.texts.summarize import summarize_tracked_text_threads

        summary_result = summarize_tracked_text_threads(DATABASE_NAME)
        log.info(
            "Text thread summaries: %d updated, %d skipped",
            summary_result.get("summarized", 0),
            summary_result.get("skipped", 0),
        )
    except Exception as exc:
        log.warning("Text thread summarization skipped: %s", exc)
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
        "--resummary-lookback-days",
        type=int,
        default=14,
        help="Only threads with activity in the last N days (default: 14)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List active threads without calling the LLM",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=180,
        help="Email pull / full pipeline lookback (default: 180)",
    )
    args = parser.parse_args()
    main(
        lookback_days=args.lookback_days,
        force_resummary=args.force_resummary,
        resummary_lookback_days=args.resummary_lookback_days,
        dry_run=args.dry_run,
    )