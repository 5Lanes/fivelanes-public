#!/usr/bin/env python3
"""
Rename thread_tracking.participant_email -> snoozed in SQLite.

SQLite does not support direct column rename in older environments, so this script
rebuilds the table and copies data across.
"""

import argparse
import sqlite3
from pathlib import Path

from utils.runtime_paths import database_path, load_env

load_env()


def has_table(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
        (table,),
    ).fetchone()
    return bool(row)


def table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def migrate(db_path: Path, dry_run: bool = False) -> None:
    with sqlite3.connect(db_path) as conn:
        if not has_table(conn, "thread_tracking"):
            print("No thread_tracking table found. Nothing to migrate.")
            return

        cols = table_columns(conn, "thread_tracking")
        if "participant_email" not in cols and "snoozed" in cols:
            print("Already migrated: thread_tracking.snoozed exists.")
            return
        if "participant_email" not in cols and "snoozed" not in cols:
            print("Neither participant_email nor snoozed exists. No changes made.")
            return

        print(f"Migrating {db_path}...")
        if dry_run:
            print("Dry run: would rebuild thread_tracking and replace participant_email with snoozed.")
            return

        conn.execute(
            """
            CREATE TABLE _thread_tracking_migrated (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                inbox_thread_id TEXT NOT NULL,
                source_email TEXT NOT NULL,
                snoozed INTEGER NOT NULL DEFAULT 0,
                inner_rfc_message_id TEXT,
                resolved_oauth_account_id TEXT,
                resolution_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (inbox_thread_id)
            )
            """
        )

        # Preserve existing snoozed values when present; otherwise default to 0.
        if "snoozed" in cols:
            conn.execute(
                """
                INSERT INTO _thread_tracking_migrated (
                    id, inbox_thread_id, source_email, snoozed, inner_rfc_message_id,
                    resolved_oauth_account_id, resolution_error, created_at, updated_at
                )
                SELECT
                    id, inbox_thread_id, source_email, COALESCE(snoozed, 0), inner_rfc_message_id,
                    resolved_oauth_account_id, COALESCE(resolution_error, ''), created_at, updated_at
                FROM thread_tracking
                """
            )
        else:
            conn.execute(
                """
                INSERT INTO _thread_tracking_migrated (
                    id, inbox_thread_id, source_email, snoozed, inner_rfc_message_id,
                    resolved_oauth_account_id, resolution_error, created_at, updated_at
                )
                SELECT
                    id, inbox_thread_id, source_email, 0, inner_rfc_message_id,
                    resolved_oauth_account_id, COALESCE(resolution_error, ''), created_at, updated_at
                FROM thread_tracking
                """
            )

        conn.execute("DROP TABLE thread_tracking")
        conn.execute("ALTER TABLE _thread_tracking_migrated RENAME TO thread_tracking")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_thread_tracking_inbox_thread_id "
            "ON thread_tracking(inbox_thread_id)"
        )
        conn.commit()
        print("Migration complete.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rename thread_tracking.participant_email to snoozed."
    )
    parser.add_argument(
        "--db",
        default=database_path(),
        help="Path to SQLite database (default: DATABASE_NAME under FIVELANES_DATA_ROOT)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen without changing the database",
    )
    args = parser.parse_args()
    migrate(Path(args.db), dry_run=args.dry_run)


if __name__ == "__main__":
    main()
