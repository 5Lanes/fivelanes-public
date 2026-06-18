#!/usr/bin/env python3
"""
Delete duplicate ``claude_message_outputs`` rows by (thread_id, source_id).

Keeps the newest row per pair by ``generated_at`` (then ``id`` as tie-breaker).

Usage (from repository root):
  python3 utils/remove_duplicate_claude_message_outputs.py
  python3 utils/remove_duplicate_claude_message_outputs.py --db /path/to/timeline.db
  python3 utils/remove_duplicate_claude_message_outputs.py --dry-run
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


def _duplicate_ids_query() -> str:
    return """
        SELECT id
        FROM (
            SELECT
                id,
                ROW_NUMBER() OVER (
                    PARTITION BY COALESCE(TRIM(thread_id), ''), TRIM(source_id)
                    ORDER BY generated_at DESC, id DESC
                ) AS rn
            FROM claude_message_outputs
            WHERE COALESCE(TRIM(source_id), '') != ''
        )
        WHERE rn > 1
    """


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

    with sqlite3.connect(db_path) as conn:
        table = conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name = 'claude_message_outputs'
            """
        ).fetchone()
        if not table:
            print(f"Table not found: claude_message_outputs ({db_path})", file=sys.stderr)
            return 1

        (dupe_count,) = conn.execute(
            f"SELECT COUNT(*) FROM ({_duplicate_ids_query()})"
        ).fetchone()
        if args.dry_run:
            print(
                f"Would delete {dupe_count} duplicate row(s) from "
                f"claude_message_outputs ({db_path})"
            )
            return 0

        conn.execute(
            f"DELETE FROM claude_message_outputs WHERE id IN ({_duplicate_ids_query()})"
        )
        (deleted,) = conn.execute("SELECT changes()").fetchone()
        conn.commit()

    print(f"Deleted {deleted} duplicate row(s) from claude_message_outputs ({db_path})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
