#!/usr/bin/env python3
"""
CLI for calendar → availability JSON (same logic as dashboard_server scheduler).

Usage (from repo root):
  python scripts/pull_calendar_availability.py
  python scripts/pull_calendar_availability.py --weeks 4 --out out/my_availability.json

List every visible calendar (all OAuth accounts), then choose which count toward availability:

  python scripts/pull_calendar_availability.py --list-calendars
  python scripts/pull_calendar_availability.py --pick-calendars

Selection is stored in ``credentials/calendar_scheduling_rules.json`` under
``availability_include_calendars``. Omit that key (or run pick and enter ``all``)
to include every calendar again.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Set, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.calendar_availability_export import (  # noqa: E402
    run_calendar_availability_pull,
    write_availability_calendar_selection,
)
from services.calendar_service import list_calendar_list_entries  # noqa: E402

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None  # type: ignore[misc, assignment]

DEFAULT_RULES = ROOT / "credentials" / "calendar_scheduling_rules.json"
DEFAULT_OUT_DIR = ROOT / "out"


def _print_calendar_catalog(rows: List[dict]) -> None:
    if not rows:
        print("No calendars found (check OAuth tokens and calendar.readonly scope).")
        return
    print(f"{'#':>4}  {'account':<16}  {'P':^3}  calendar name")
    print("-" * 72)
    for i, r in enumerate(rows, start=1):
        acct = (r.get("account_id") or "")[:16]
        prim = "*" if r.get("primary") else ""
        name = r.get("summary") or r.get("calendar_id") or ""
        print(f"{i:4}  {acct:<16}  {prim:^3}  {name}")


def _parse_index_selection(spec: str, n: int) -> Optional[Set[int]]:
    """
    Parse e.g. ``all``, ``1,3,5``, ``1-4,7``. Returns None = all calendars (no filter).
    Indices are 1-based, inclusive.
    """
    s = (spec or "").strip().lower()
    if not s or s in ("all", "*"):
        return None
    out: Set[int] = set()
    for part in re.split(r"[\s,;]+", s):
        part = part.strip()
        if not part:
            continue
        if part in ("all", "*"):
            return None
        m = re.match(r"^(\d+)\s*-\s*(\d+)$", part)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            if lo > hi:
                lo, hi = hi, lo
            for j in range(lo, hi + 1):
                out.add(j)
            continue
        if part.isdigit():
            out.add(int(part))
            continue
        raise ValueError(f"unrecognized segment: {part!r}")
    for j in out:
        if j < 1 or j > n:
            raise ValueError(f"index {j} out of range (1–{n})")
    return out


def _cmd_list_calendars() -> int:
    rows = list_calendar_list_entries()
    _print_calendar_catalog(rows)
    return 0 if rows else 1


def _cmd_pick_calendars(rules_path: Path) -> int:
    rows = list_calendar_list_entries()
    if not rows:
        print("No calendars found (check OAuth tokens and calendar.readonly scope).", file=sys.stderr)
        return 1
    _print_calendar_catalog(rows)
    print()
    print(
        "Enter numbers of calendars to include for availability export (e.g. 1,3,5-7), "
        "or type all to clear the filter and include every calendar."
    )
    if not sys.stdin.isatty():
        print("stdin is not a TTY; cannot read selection.", file=sys.stderr)
        return 1
    try:
        line = input("Selection: ").strip()
    except EOFError:
        return 1
    try:
        picked = _parse_index_selection(line, len(rows))
    except ValueError as e:
        print(f"Invalid selection: {e}", file=sys.stderr)
        return 1
    if picked is None:
        write_availability_calendar_selection(rules_path, None)
        print(f"Cleared calendar filter → all calendars (updated {rules_path}).")
        return 0
    if not picked:
        print("Empty selection; nothing written.", file=sys.stderr)
        return 1
    pairs: List[Tuple[str, str]] = []
    for idx in sorted(picked):
        r = rows[idx - 1]
        pairs.append((str(r.get("account_id") or ""), str(r.get("calendar_id") or "")))
    write_availability_calendar_selection(rules_path, pairs)
    print(f"Saved {len(pairs)} calendar(s) to {rules_path} under availability_include_calendars.")
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Pull calendars → availability JSON.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--list-calendars",
        action="store_true",
        help="Print all calendar names/accounts and exit.",
    )
    mode.add_argument(
        "--pick-calendars",
        action="store_true",
        help="Interactive: choose calendars to save into scheduling rules.",
    )
    parser.add_argument("--weeks", type=int, default=4, help="Weeks forward from now (default 4).")
    parser.add_argument(
        "--rules",
        type=Path,
        default=DEFAULT_RULES,
        help="Path to calendar_scheduling_rules.json",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output JSON (default: out/availability_calendar_<UTC stamp>.json)",
    )
    args = parser.parse_args()
    if args.weeks < 1 or args.weeks > 52:
        parser.error("--weeks must be between 1 and 52")
    if ZoneInfo is None:
        print("Python 3.9+ with zoneinfo is required.", file=sys.stderr)
        sys.exit(1)

    rules_path = args.rules.resolve()

    if args.list_calendars:
        sys.exit(_cmd_list_calendars())
    if args.pick_calendars:
        sys.exit(_cmd_pick_calendars(rules_path))

    if args.out is None:
        DEFAULT_OUT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_path = DEFAULT_OUT_DIR / f"availability_calendar_{stamp}.json"
    else:
        out_path = args.out.resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)

    run_calendar_availability_pull(
        ROOT,
        weeks=args.weeks,
        rules_path=rules_path,
        out_path=out_path,
    )


if __name__ == "__main__":
    main()
