#!/usr/bin/env python3
"""Re-run segmentation and summaries when stored output looks like an API error."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from utils.retry_failed_pipeline_outputs import retry_failed_pipeline_outputs  # noqa: E402
from utils.runtime_paths import database_path, load_env  # noqa: E402


def main() -> None:
    load_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default=database_path(),
        help="SQLite path (default: DATABASE_NAME under FIVELANES_DATA_ROOT)",
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
    retry_failed_pipeline_outputs(
        db_path=str(Path(args.db).expanduser().resolve()),
        dry_run=args.dry_run,
        thread_id=args.thread_id,
    )


if __name__ == "__main__":
    main()
