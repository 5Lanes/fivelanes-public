#!/usr/bin/env python3
"""
Delete ``timeline_entries`` rows with no ``thread_id`` (NULL or blank).

Usage (from repository root):
  python3 utils/remove_timeline_entries_without_thread_id.py
  python3 utils/remove_timeline_entries_without_thread_id.py --db /path/to/timeline.db
  python3 utils/remove_timeline_entries_without_thread_id.py --dry-run
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env")
except ImportError:
    pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default=os.getenv("DATABASE_NAME") or "timeline.db",
        help="SQLite database path (default: DATABASE_NAME or timeline.db)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print how many rows would be deleted, do not delete",
    )
    args = parser.parse_args()
    db_path = Path(args.db).expanduser().resolve()
    if not db_path.is_file():
        print(f"Database not found: {db_path}", file=sys.stderr)
        return 1

    where = "COALESCE(TRIM(thread_id), '') = ''"
    with sqlite3.connect(db_path) as conn:
        (n,) = conn.execute(
            f"SELECT COUNT(*) FROM timeline_entries WHERE {where}"
        ).fetchone()
        if args.dry_run:
            print(f"Would delete {n} row(s) with empty thread_id from {db_path}")
            return 0
        conn.execute(f"DELETE FROM timeline_entries WHERE {where}")
        (deleted,) = conn.execute("SELECT changes()").fetchone()
        conn.commit()
    print(f"Deleted {deleted} row(s) from timeline_entries ({db_path})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
