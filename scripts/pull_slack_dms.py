#!/usr/bin/env python3
"""
Pull Slack 1:1 DMs using a user OAuth token (xoxp-...).

Usage (from repo root):
  python scripts/pull_slack_dms.py
  python scripts/pull_slack_dms.py --out fivelanes-data/slack_dms
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.runtime_paths import load_env  # noqa: E402

load_env()

from services.slack.config import SLACK_DMS_DIR  # noqa: E402
from services.slack.pull import pull_slack_dms  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Pull Slack 1:1 DMs to JSON files.")
    parser.add_argument(
        "--out",
        type=Path,
        default=SLACK_DMS_DIR,
        help="Output directory for JSON exports",
    )
    parser.add_argument(
        "--limit-per-channel",
        type=int,
        default=200,
        help="Page size for conversations.history (max 200)",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=1.2,
        help="Seconds to sleep between Slack API calls",
    )
    args = parser.parse_args()

    try:
        result = pull_slack_dms(
            out_dir=args.out.resolve(),
            limit_per_channel=min(args.limit_per_channel, 200),
            sleep_sec=args.sleep,
        )
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(
        f"Done — {result.get('dm_count', 0)} DM(s), "
        f"{result.get('message_count', 0)} message(s) in {result.get('out_dir')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
