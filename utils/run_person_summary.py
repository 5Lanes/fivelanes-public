#!/usr/bin/env python3
"""Run a person-level LLM summary for one person (CLI test / ad-hoc)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
# Running as `python3 utils/run_person_summary.py` puts ``utils/`` on sys.path and
# shadows stdlib ``logging`` via ``utils/logging.py``.
_script_dir = str(Path(__file__).resolve().parent)
while _script_dir in sys.path:
    sys.path.remove(_script_dir)

try:
    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env")
except ImportError:
    pass

from services.llm_service import get_llm_backend
from services.pipeline.fingerprint import person_summary_fingerprint
from services.prompts import format_person_summary_prompt
from utils.database import (
    load_person_summary,
    load_person_thread_summaries,
    normalize_person_summary_payload,
    save_person_summary,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate an LLM summary for one person")
    parser.add_argument("person", help="Person name (case-insensitive) or numeric person id")
    parser.add_argument(
        "--db",
        default=os.getenv("DATABASE_NAME") or "timeline.db",
        help="SQLite database path (default: DATABASE_NAME or timeline.db)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the prompt only; do not call the LLM",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Regenerate even when a cached summary matches current thread inputs",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=1500,
        help="Max tokens for the LLM response (default: 1500)",
    )
    args = parser.parse_args()

    person_arg = (args.person or "").strip()
    person_id: int | None = None
    person_name: str | None = person_arg
    if person_arg.isdigit():
        person_id = int(person_arg)
        person_name = None

    person, summaries = load_person_thread_summaries(
        args.db, person_name=person_name, person_id=person_id
    )
    if not person:
        print(f"Person not found: {person_arg!r}", file=sys.stderr)
        sys.exit(1)

    pid = int(person.get("id") or 0)
    name = str(person.get("name") or "").strip()
    print(f"Person: {name} (id={pid}) — {len(summaries)} thread(s) with summaries")
    for i, s in enumerate(summaries, start=1):
        label = (
            str(s.get("suggested_thread_label") or s.get("subject") or s.get("thread_id") or "")
            .strip()
        )
        print(f"  {i}. {s.get('datetime', '')} — {label}")

    if not summaries:
        print("No thread summaries for this person.", file=sys.stderr)
        sys.exit(1)

    prompt = format_person_summary_prompt(name, summaries)
    if args.dry_run:
        print("\n--- prompt ---\n")
        print(prompt.as_single_prompt())
        return

    llm = get_llm_backend(env_path=str(_ROOT / ".env"))
    thread_ids = [str(s.get("thread_id") or "").strip() for s in summaries]
    summary_datetimes = [str(s.get("datetime") or "").strip() for s in summaries]
    fp = person_summary_fingerprint(
        person_id=pid,
        thread_ids=thread_ids,
        summary_datetimes=summary_datetimes,
        backend=llm.name,
    )
    if not args.force:
        cached = load_person_summary(args.db, person_id=pid)
        if cached and str(cached.get("input_fingerprint") or "") == fp:
            print("\n--- cached result ---\n")
            print(json.dumps(cached, indent=2, ensure_ascii=False))
            return

    print("\nCalling LLM…")
    result = llm.submit_person_summary(prompt)
    summary = normalize_person_summary_payload(result) if isinstance(result, dict) else {}
    summary["input_fingerprint"] = fp
    updated_at = save_person_summary(args.db, person_id=pid, summary=summary)
    out = dict(summary)
    out["summary_updated_at"] = updated_at
    print("\n--- result ---\n")
    print(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
