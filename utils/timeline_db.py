"""
Optional SQLite bootstrap (standalone). Prefer ``utils.database`` schema helpers used by the app.
"""

import sqlite3
from pathlib import Path

from utils.database import _ensure_thread_tracking_schema, ensure_database_schema


def create_timeline_db(db_path: str = "timeline.db") -> None:
    """Create DB files and active tables if missing."""
    ensure_database_schema(db_path)


def create_threads_table(conn: sqlite3.Connection) -> None:
    """Ensure ``thread_tracking`` matches the application schema."""
    _ensure_thread_tracking_schema(conn)


if __name__ == "__main__":
    create_timeline_db()
