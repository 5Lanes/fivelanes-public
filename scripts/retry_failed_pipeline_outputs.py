#!/usr/bin/env python3
"""Re-run segmentation and summaries when stored output looks like an API error."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env")
except ImportError:
    pass

from utils.retry_failed_pipeline_outputs import retry_failed_pipeline_outputs  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default=os.getenv("DATABASE_NAME") or "timeline.db",
        help="SQLite path (default: DATABASE_NAME or timeline.db)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List retry candidates without calling the LLM",
    )
    parser.add_argument(
        "--thread-id",
        default=None,
        help="Only retry this inbox thread id",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    db_path = Path(args.db)
    if not db_path.is_absolute():
        db_path = (_ROOT / db_path).resolve()
    retry_failed_pipeline_outputs(
        db_path=str(db_path),
        dry_run=args.dry_run,
        thread_id=args.thread_id,
    )


if __name__ == "__main__":
    main()
