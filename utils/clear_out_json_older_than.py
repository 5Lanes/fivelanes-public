#!/usr/bin/env python3
"""
Remove JSON files under the repository ``out/`` directory whose modification time
is older than a given age (default: 12 hours).

Run periodically (for example cron or launchd) from any working directory:

  python3 utils/clear_out_json_older_than.py

Dry run (list what would be deleted):

  python3 utils/clear_out_json_older_than.py --dry-run
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_OUT = _ROOT / "out"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=_DEFAULT_OUT,
        help=f"Directory containing JSON outputs (default: {_DEFAULT_OUT})",
    )
    parser.add_argument(
        "--hours",
        type=float,
        default=12.0,
        help="Delete files whose mtime is older than this many hours (default: 24)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print paths that would be removed without deleting",
    )
    args = parser.parse_args()

    out_dir: Path = args.out_dir.resolve()
    if not out_dir.is_dir():
        print(f"clear_out_json_older_than: skip — not a directory: {out_dir}", file=sys.stderr)
        return 0

    max_age_s = max(0.0, float(args.hours)) * 3600.0
    now = time.time()
    cutoff = now - max_age_s

    removed = 0
    for path in sorted(out_dir.glob("*.json")):
        if not path.is_file():
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError as e:
            print(f"clear_out_json_older_than: stat failed {path}: {e}", file=sys.stderr)
            continue
        if mtime >= cutoff:
            continue
        try:
            rel = path.relative_to(_ROOT)
        except ValueError:
            rel = path
        if args.dry_run:
            print(f"would remove: {rel}")
        else:
            try:
                path.unlink()
                print(f"removed: {rel}")
                removed += 1
            except OSError as e:
                print(f"clear_out_json_older_than: unlink failed {path}: {e}", file=sys.stderr)

    if args.dry_run:
        print("clear_out_json_older_than: dry run complete (no files deleted)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
