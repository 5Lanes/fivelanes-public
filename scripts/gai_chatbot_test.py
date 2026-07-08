#!/usr/bin/env python3
"""
Interactive test chatbot for GAI database querying.

Usage (from repo root):
  python3 scripts/gai_chatbot_test.py
  python3 scripts/gai_chatbot_test.py --once "How many lanes are there?"
  python3 scripts/gai_chatbot_test.py --check-db
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.runtime_paths import database_path, load_env  # noqa: E402

load_env()

from services.gai.chat import answer_question  # noqa: E402
from services.gai.db_context import (  # noqa: E402
    database_schema_summary,
    execute_readonly_sql,
    format_query_result,
    structured_snapshot,
)


def check_database(db_path: str) -> None:
    """Print a quick DB health snapshot without calling the LLM."""
    print(f"Database: {db_path}")
    print()
    print("Schema:")
    print(database_schema_summary(db_path))
    print()
    snapshot = structured_snapshot(db_path)
    print("Structured snapshot:")
    print(json.dumps(snapshot, indent=2, default=str))
    print()
    sample = execute_readonly_sql(
        db_path,
        "SELECT id, name FROM lane_areas ORDER BY sort_order LIMIT 5",
    )
    print("Sample query (lane areas):")
    print(format_query_result(sample))


def interactive_loop(db_path: str, *, verbose: bool = False) -> int:
    history: list[dict[str, str]] = []
    session_context: dict[str, object] = {}
    print("GAI database test chatbot")
    print(f"Database: {db_path}")
    print("Type a question, or 'quit' / Ctrl-D to exit.\n")

    while True:
        try:
            question = input("you> ").strip()
        except EOFError:
            print()
            break
        if not question:
            continue
        if question.lower() in {"quit", "exit", "q"}:
            break

        try:
            result = answer_question(
                db_path,
                question,
                history=history,
                session_context=session_context,
            )
        except Exception as exc:
            print(f"alfred> Error: {exc}\n")
            continue

        if not result.get("ok"):
            print(f"alfred> Query failed: {result.get('error')}")
            print(f"     SQL: {result.get('sql')}\n")
            continue

        print(f"alfred> {result['answer']}")
        if verbose:
            print(f"     ({result['row_count']} rows, sql={result['sql']})")
        print()

        history.append({"role": "user", "content": question})
        history.append({"role": "assistant", "content": result["answer"]})
        if result.get("last_person"):
            session_context["last_person"] = result["last_person"]

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Test GAI database querying")
    parser.add_argument("--once", metavar="QUESTION", help="Answer a single question and exit")
    parser.add_argument("--check-db", action="store_true", help="Print schema without calling the LLM")
    parser.add_argument("--verbose", action="store_true", help="Show generated SQL and reasoning")
    parser.add_argument("--db", help="Override database path (default: from FIVELANES_DATA_ROOT)")
    args = parser.parse_args()

    db_path = args.db or str(database_path())
    if not Path(db_path).is_file():
        print(f"Database not found: {db_path}", file=sys.stderr)
        return 1

    if args.check_db:
        check_database(db_path)
        return 0

    if args.once:
        result = answer_question(db_path, args.once)
        if not result.get("ok"):
            print(f"Query failed: {result.get('error')}", file=sys.stderr)
            print(f"SQL: {result.get('sql')}", file=sys.stderr)
            return 1
        print(result["answer"])
        if args.verbose:
            print(f"\nSQL: {result['sql']}")
            print(f"Rows: {result['row_count']}")
        return 0

    return interactive_loop(db_path, verbose=args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
