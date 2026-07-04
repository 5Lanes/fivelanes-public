#!/usr/bin/env python3
"""Interactive migration: assign each existing lane (track) to a lane area tab."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.database import (  # noqa: E402
    assign_lane_to_area,
    create_lane_area,
    load_all_lane_areas,
    load_all_lanes,
    load_lane_thread_memberships,
)
from utils.runtime_paths import database_path


def _prompt_area(areas: list[dict], lane_name: str) -> int:
    print(f"\nLane: {lane_name!r}")
    if areas:
        print("Existing areas:")
        for i, area in enumerate(areas, start=1):
            print(f"  [{i}] {area['name']} (id={area['id']})")
        print("  [N] Create new area")
        choice = input("Assign to (number or new name): ").strip()
        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(areas):
                return int(areas[idx - 1]["id"])
        if choice.lower() in ("n", "new", ""):
            name = input("New area name: ").strip()
            if not name:
                raise SystemExit("Area name required.")
            area = create_lane_area(database_path(), name=name, color_index=len(areas))
            areas.append(area)
            print(f"Created area {area['name']!r} (id={area['id']})")
            return int(area["id"])
        if choice:
            area = create_lane_area(database_path(), name=choice, color_index=len(areas))
            areas.append(area)
            print(f"Created area {area['name']!r} (id={area['id']})")
            return int(area["id"])
    name = input("First area name for this lane: ").strip()
    if not name:
        raise SystemExit("Area name required.")
    area = create_lane_area(database_path(), name=name, color_index=0)
    areas.append(area)
    print(f"Created area {area['name']!r} (id={area['id']})")
    return int(area["id"])


def main() -> None:
    parser = argparse.ArgumentParser(description="Assign lanes to lane area tabs.")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Data directory (default: FIVELANES_DATA or ./data)",
    )
    parser.add_argument(
        "--audit-log",
        type=Path,
        default=None,
        help="Write JSON audit log (default: <data-dir>/lane_area_migration.json)",
    )
    parser.add_argument(
        "--one-per-lane",
        action="store_true",
        help="Create one lane area per track (area name = track name) and assign each lane to it",
    )
    parser.add_argument(
        "--apply-mapping",
        type=Path,
        default=None,
        help='JSON file: [{"lane_id": 1, "area_name": "Divorce"}, ...] — create areas as needed',
    )
    args = parser.parse_args()
    if args.data_dir:
        import os

        os.environ["FIVELANES_DATA"] = str(args.data_dir.resolve())

    db = database_path()
    lanes = load_all_lanes(db)
    memberships = load_lane_thread_memberships(db)
    areas = load_all_lane_areas(db)

    if args.one_per_lane:
        audit: list[dict] = []
        area_by_name = {a["name"].casefold(): a for a in areas}
        for lane in lanes:
            name = str(lane["name"]).strip()
            if not name:
                continue
            key = name.casefold()
            area = area_by_name.get(key)
            if not area:
                area = create_lane_area(db, name=name, color_index=len(area_by_name))
                area_by_name[key] = area
                areas.append(area)
                print(f"Created area {area['name']!r} (id={area['id']})")
            area_id = int(area["id"])
            if lane.get("area_id") == area_id:
                print(f"Skip lane {lane['id']} {name!r} — already in {area['name']!r}")
                continue
            ok = assign_lane_to_area(db, lane_id=int(lane["id"]), area_id=area_id)
            if not ok:
                raise SystemExit(f"Failed to assign lane {lane['id']}")
            audit.append(
                {
                    "lane_id": lane["id"],
                    "lane_name": name,
                    "area_id": area_id,
                    "area_name": area["name"],
                    "migrated_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            print(f"{name!r} -> {area['name']!r}")
        if audit:
            log_path = args.audit_log or (Path(db).parent / "lane_area_migration.json")
            existing: list = []
            if log_path.is_file():
                try:
                    existing = json.loads(log_path.read_text(encoding="utf-8"))
                    if not isinstance(existing, list):
                        existing = []
                except json.JSONDecodeError:
                    existing = []
            existing.extend(audit)
            log_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
            print(f"\nWrote audit log: {log_path}")
            print(f"Reassigned {len(audit)} lane(s).")
        else:
            print("No lane assignments changed.")
        return

    if args.apply_mapping:
        mapping_raw = json.loads(args.apply_mapping.read_text(encoding="utf-8"))
        if not isinstance(mapping_raw, list):
            raise SystemExit("Mapping JSON must be a list of {lane_id, area_name} objects.")
        area_by_name = {a["name"].casefold(): a for a in areas}
        audit = []
        for row in mapping_raw:
            if not isinstance(row, dict):
                continue
            lane_id = int(row.get("lane_id") or 0)
            area_name = str(row.get("area_name") or "").strip()
            if lane_id <= 0 or not area_name:
                continue
            key = area_name.casefold()
            area = area_by_name.get(key)
            if not area:
                area = create_lane_area(db, name=area_name, color_index=len(area_by_name))
                area_by_name[key] = area
                areas.append(area)
                print(f"Created area {area['name']!r} (id={area['id']})")
            area_id = int(area["id"])
            lane = next((l for l in lanes if int(l["id"]) == lane_id), None)
            lane_name = str(lane["name"]) if lane else f"id={lane_id}"
            ok = assign_lane_to_area(db, lane_id=lane_id, area_id=area_id)
            if not ok:
                raise SystemExit(f"Failed to assign lane {lane_id}")
            audit.append(
                {
                    "lane_id": lane_id,
                    "lane_name": lane_name,
                    "area_id": area_id,
                    "area_name": area["name"],
                    "migrated_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            print(f"{lane_name!r} -> {area['name']!r}")
        log_path = args.audit_log or (Path(db).parent / "lane_area_migration.json")
        existing: list = []
        if log_path.is_file():
            try:
                existing = json.loads(log_path.read_text(encoding="utf-8"))
                if not isinstance(existing, list):
                    existing = []
            except json.JSONDecodeError:
                existing = []
        existing.extend(audit)
        log_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
        print(f"\nWrote audit log: {log_path}")
        print(f"Applied {len(audit)} assignment(s).")
        return

    unassigned = [lane for lane in lanes if lane.get("area_id") is None]

    if not lanes:
        print("No lanes in database.")
        return
    if not unassigned:
        print("All lanes already assigned to areas.")
        return

    print(f"Found {len(unassigned)} lane(s) without an area (of {len(lanes)} total).")
    audit: list[dict] = []
    for lane in unassigned:
        thread_count = len(memberships.get(str(lane["id"]), []))
        label = f"{lane['name']} ({thread_count} threads, id={lane['id']})"
        area_id = _prompt_area(areas, label)
        ok = assign_lane_to_area(db, lane_id=int(lane["id"]), area_id=area_id)
        if not ok:
            raise SystemExit(f"Failed to assign lane {lane['id']}")
        area_name = next((a["name"] for a in areas if a["id"] == area_id), str(area_id))
        audit.append(
            {
                "lane_id": lane["id"],
                "lane_name": lane["name"],
                "area_id": area_id,
                "area_name": area_name,
                "migrated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        print(f"  -> {area_name}")

    log_path = args.audit_log or (Path(db).parent / "lane_area_migration.json")
    existing: list = []
    if log_path.is_file():
        try:
            existing = json.loads(log_path.read_text(encoding="utf-8"))
            if not isinstance(existing, list):
                existing = []
        except json.JSONDecodeError:
            existing = []
    existing.extend(audit)
    log_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote audit log: {log_path}")
    print(f"Migrated {len(audit)} lane(s).")


if __name__ == "__main__":
    main()
