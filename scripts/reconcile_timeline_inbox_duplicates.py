#!/usr/bin/env python3
"""
One-time cleanup for inbox-copy duplicate ``timeline_entries``.

After source-thread ingestion (README § Thread identity), re-run the inbox pipeline
to fetch canonical ``source_id`` values, then prune leftover rows that share the same
message body but used a Fivelanes inbox or secondary-mailbox Gmail message id.

Usage (from repository root):

  # Recommended: refresh from Gmail (needs OAuth), then prune DB stragglers
  python3 scripts/reconcile_timeline_inbox_duplicates.py --all

  # Gmail refresh only
  python3 scripts/reconcile_timeline_inbox_duplicates.py --refresh-inbox --lookback-days 90

  # Offline: drop content-duplicate rows without calling Gmail
  python3 scripts/reconcile_timeline_inbox_duplicates.py --prune-content-dupes

  python3 scripts/reconcile_timeline_inbox_duplicates.py --all --dry-run
  python3 scripts/reconcile_timeline_inbox_duplicates.py --db /path/to/timeline.db
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env")
except ImportError:
    pass

from utils.runtime_paths import database_path, load_env  # noqa: E402

load_env()


def _content_key(row: sqlite3.Row) -> Tuple[str, str, str, str]:
    return (
        str(row["datetime"] or "").strip(),
        str(row["sender"] or "").strip(),
        str(row["summary"] or "").strip(),
        str(row["body"] or "").strip(),
    )


def _tracking_by_inbox_thread_id(
    conn: sqlite3.Connection,
) -> Dict[str, Dict[str, str]]:
    conn.row_factory = sqlite3.Row
    out: Dict[str, Dict[str, str]] = {}
    for row in conn.execute(
        """
        SELECT inbox_thread_id, source_email, resolved_oauth_account_id
        FROM thread_tracking
        WHERE COALESCE(TRIM(inbox_thread_id), '') != ''
        """
    ):
        tid = str(row["inbox_thread_id"] or "").strip()
        if tid:
            out[tid] = {
                "source_email": str(row["source_email"] or "").strip().lower(),
                "resolved_oauth_account_id": str(
                    row["resolved_oauth_account_id"] or ""
                ).strip(),
            }
    return out


def prune_content_duplicates(db_path: Path, *, dry_run: bool) -> Tuple[int, int]:
    from services.email.config import SOURCE_OAUTH_ACCOUNT_ID

    inbox_oauth = (SOURCE_OAUTH_ACCOUNT_ID or "").strip()

    def score_with_inbox_penalty(
        row: sqlite3.Row,
        tracking: Dict[str, Dict[str, str]],
    ) -> Tuple[int, int]:
        fetch = str(row["fetch_oauth_account_id"] or "").strip()
        tid = str(row["thread_id"] or "").strip()
        meta = tracking.get(tid, {})
        resolved = meta.get("resolved_oauth_account_id", "")

        score = 0
        if fetch and resolved and fetch == resolved:
            score += 100
        elif fetch and inbox_oauth and fetch != inbox_oauth:
            score += 40
        if not fetch:
            score -= 10
        return score, -int(row["id"] or 0)

    with sqlite3.connect(db_path) as conn:
        tracking = _tracking_by_inbox_thread_id(conn)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, source_id, thread_id, datetime, sender, summary, body,
                   fetch_oauth_account_id
            FROM timeline_entries
            WHERE type = 'email'
              AND COALESCE(TRIM(source_id), '') != ''
            ORDER BY id ASC
            """
        ).fetchall()

        by_content: Dict[Tuple[str, str, str, str], List[sqlite3.Row]] = defaultdict(list)
        for row in rows:
            key = _content_key(row)
            if not key[0] or not key[1]:
                continue
            by_content[key].append(row)

        losers: List[Dict[str, Any]] = []
        for group in by_content.values():
            if len(group) < 2:
                continue
            ranked = sorted(
                group,
                key=lambda r: score_with_inbox_penalty(r, tracking),
                reverse=True,
            )
            keeper = ranked[0]
            for row in ranked[1:]:
                losers.append(
                    {
                        "id": int(row["id"]),
                        "source_id": str(row["source_id"] or "").strip(),
                    }
                )

        if not losers:
            return 0, 0
        if dry_run:
            source_ids = sorted({x["source_id"] for x in losers if x["source_id"]})
            (claude_would,) = conn.execute(
                f"""
                SELECT COUNT(*) FROM claude_message_outputs
                WHERE source_id IN ({",".join("?" for _ in source_ids)})
                """,
                source_ids,
            ).fetchone()
            return len(losers), int(claude_would or 0)

        loser_ids = [x["id"] for x in losers]
        source_ids = sorted({x["source_id"] for x in losers if x["source_id"]})
        placeholders = ",".join("?" for _ in loser_ids)
        conn.execute(
            f"DELETE FROM timeline_entries WHERE id IN ({placeholders})",
            loser_ids,
        )
        (timeline_deleted,) = conn.execute("SELECT changes()").fetchone()
        claude_deleted = 0
        if source_ids:
            ph = ",".join("?" for _ in source_ids)
            conn.execute(
                f"DELETE FROM claude_message_outputs WHERE source_id IN ({ph})",
                source_ids,
            )
            (claude_deleted,) = conn.execute("SELECT changes()").fetchone()
        conn.commit()
        return int(timeline_deleted or 0), int(claude_deleted or 0)


def refresh_inbox_timeline(*, lookback_days: int, db_path: str) -> None:
    from services.email.inbox_process import process_inbox_pipeline

    process_inbox_pipeline(db_path, lookback_days=lookback_days)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default=database_path(),
        help="SQLite database path (default: DATABASE_NAME under FIVELANES_DATA_ROOT)",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=90,
        help="Inbox lookback for --refresh-inbox (default: 90)",
    )
    parser.add_argument(
        "--refresh-inbox",
        action="store_true",
        help="Re-run inbox pipeline (Gmail OAuth) using source-thread expansion",
    )
    parser.add_argument(
        "--prune-content-dupes",
        action="store_true",
        help="Delete timeline rows that duplicate the same body under another source_id",
    )
    parser.add_argument(
        "--prune-inbox-shells",
        action="store_true",
        help="Delete Cc/Bcc inbox shell rows whose source_id is gmail_inbox_thread_id",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="--refresh-inbox, --prune-inbox-shells, then --prune-content-dupes",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report counts only; do not call Gmail or delete rows",
    )
    args = parser.parse_args(argv)

    if not (args.refresh_inbox or args.prune_content_dupes or args.prune_inbox_shells or args.all):
        parser.error("Specify --all or at least one of --refresh-inbox, --prune-content-dupes, --prune-inbox-shells")

    db_file = Path(args.db).expanduser().resolve()
    if not db_file.is_file():
        print(f"Database not found: {db_file}", file=sys.stderr)
        return 1

    do_refresh = args.all or args.refresh_inbox
    do_shells = args.all or args.prune_inbox_shells
    do_content = args.all or args.prune_content_dupes

    if do_refresh:
        if args.dry_run:
            print(
                f"Would refresh inbox timeline via Gmail "
                f"(lookback_days={args.lookback_days}, db={db_file})"
            )
        else:
            print(
                f"Refreshing inbox timeline (lookback_days={args.lookback_days})…"
            )
            refresh_inbox_timeline(lookback_days=args.lookback_days, db_path=str(db_file))
            print("Inbox refresh complete.")

    if do_shells:
        from utils.database import prune_inbox_shell_duplicate_entries

        if args.dry_run:
            from utils.database import fetch_thread_tracking_rows

            shell_ids: set[str] = set()
            for row in fetch_thread_tracking_rows(str(db_file)):
                inbox_tid = str(row.get("inbox_thread_id") or "").strip()
                if not inbox_tid.startswith("rfc:"):
                    continue
                gid = str(row.get("gmail_inbox_thread_id") or "").strip()
                if gid:
                    shell_ids.add(gid)
            with sqlite3.connect(db_file) as conn:
                if not shell_ids:
                    t_n, c_n = 0, 0
                else:
                    ph = ",".join("?" for _ in shell_ids)
                    params = sorted(shell_ids)
                    (t_n,) = conn.execute(
                        f"SELECT COUNT(*) FROM timeline_entries WHERE source_id IN ({ph})",
                        params,
                    ).fetchone()
                    (c_n,) = conn.execute(
                        f"SELECT COUNT(*) FROM claude_message_outputs WHERE source_id IN ({ph})",
                        params,
                    ).fetchone()
            print(
                f"Would delete {t_n} inbox-shell timeline row(s) and "
                f"{c_n} claude_message_outputs row(s)"
            )
        else:
            t_del, c_del = prune_inbox_shell_duplicate_entries(str(db_file))
            print(
                f"Deleted {t_del} inbox-shell timeline row(s) and "
                f"{c_del} claude_message_outputs row(s)"
            )

    if do_content:
        t_del, c_del = prune_content_duplicates(db_file, dry_run=args.dry_run)
        if args.dry_run:
            print(
                f"Would delete {t_del} content-duplicate timeline row(s) and "
                f"{c_del} matching claude_message_outputs row(s)"
            )
        else:
            print(
                f"Deleted {t_del} content-duplicate timeline row(s) and "
                f"{c_del} matching claude_message_outputs row(s)"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
