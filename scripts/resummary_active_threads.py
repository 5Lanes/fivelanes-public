"""
CLI for re-running thread summaries on active inbox threads.

Usage (from repo root):

  python3 scripts/resummary_active_threads.py
  python3 scripts/resummary_active_threads.py --thread-id 19e84f5c156babd9
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> None:
    import argparse

    from utils.resummary_active_threads import force_resummary_active_threads

    os.chdir(ROOT)
    from utils.runtime_paths import load_env

    load_env()

    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="Re-summarize active inbox threads")
    parser.add_argument("--db", default=None, help="SQLite path (default: DATABASE_NAME or timeline.db)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--thread-id", default=None, help="Only this inbox thread id")
    args = parser.parse_args()
    force_resummary_active_threads(
        db_path=args.db,
        dry_run=args.dry_run,
        thread_id=args.thread_id,
    )


if __name__ == "__main__":
    main()
