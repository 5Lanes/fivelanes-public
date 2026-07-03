"""Persist LinkedIn pull selections from the dashboard to the Playwright scraper."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, List

from services.linkedin.config import LINKEDIN_SELECTIONS_PATH


def selection_strings_for_conversation_keys(conversation_keys: Iterable[str]) -> List[str]:
    """Map tracked/selected keys to scraper selection strings (conversation IDs only)."""
    return [k.strip() for k in conversation_keys if str(k).strip()]


def write_selections_for_conversation_keys(
    conversation_keys: Iterable[str],
    *,
    selections_path: Path | None = None,
) -> List[str]:
    """Write ``selections.txt`` for the next Playwright pull."""
    selections = selection_strings_for_conversation_keys(conversation_keys)
    path = selections_path or LINKEDIN_SELECTIONS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "# Written by Fivelanes — used for the next LinkedIn pull.",
        "# One conversation id per line.",
        "",
    ]
    lines.extend(selections)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return selections
