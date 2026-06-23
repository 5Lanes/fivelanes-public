#!/usr/bin/env python3
"""Load calendar_events_index from availability JSON into timeline.db meetings table."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from utils.database import replace_meetings_from_availability_doc  # noqa: E402
from utils.runtime_paths import data_path, database_path, load_env  # noqa: E402


def main() -> None:
    load_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json",
        type=Path,
        default=data_path("out", "availability_calendar_latest.json"),
        help="Availability export JSON (default: $FIVELANES_DATA_ROOT/out/availability_calendar_latest.json)",
    )
    parser.add_argument(
        "--db",
        default=database_path(),
        help="SQLite path (default: DATABASE_NAME under FIVELANES_DATA_ROOT)",
    )
    args = parser.parse_args()
    json_path = args.json.resolve()
    if not json_path.is_file():
        raise SystemExit(f"JSON not found: {json_path}")
    with open(json_path, encoding="utf-8") as f:
        doc = json.load(f)
    db_path = Path(args.db).expanduser().resolve()
    n = replace_meetings_from_availability_doc(str(db_path), doc)
    print(f"Wrote {n} meeting row(s) to {db_path}")


if __name__ == "__main__":
    main()
