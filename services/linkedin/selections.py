"""Persist LinkedIn pull selections from the dashboard to the Playwright scraper."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, List

from services.linkedin.catalog import list_conversation_catalog
from services.linkedin.config import LINKEDIN_SELECTIONS_PATH
from services.linkedin.format import load_messages_for_key


def _counterparty_label(conversation_key: str, *, fallback: str = "") -> str:
    messages = load_messages_for_key(conversation_key)
    for msg in reversed(messages):
        if msg.get("is_from_me"):
            continue
        name = str(msg.get("from_name") or "").strip()
        if name:
            return name
    for msg in messages:
        if msg.get("is_from_me"):
            continue
        name = str(msg.get("to_name") or "").strip()
        if name:
            return name
    return fallback or conversation_key


def selection_strings_for_conversation_keys(conversation_keys: Iterable[str]) -> List[str]:
    """Map tracked/selected ``conversation_key`` values to scraper selection strings."""
    keys = [k.strip() for k in conversation_keys if str(k).strip()]
    if not keys:
        return []

    by_key = {
        str(row.get("conversation_key") or row.get("id") or "").strip(): row
        for row in list_conversation_catalog()
    }

    selections: List[str] = []
    seen: set[str] = set()
    for key in keys:
        row = by_key.get(key)
        label = _counterparty_label(
            key,
            fallback=str(row.get("label") or "").strip() if row else "",
        )
        candidates = [label, key]
        if row:
            catalog_label = str(row.get("label") or "").strip()
            if catalog_label and catalog_label not in candidates:
                candidates.append(catalog_label)
        for candidate in candidates:
            lowered = candidate.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            selections.append(candidate)
    return selections


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
        "# One participant name or conversation id per line.",
        "",
    ]
    lines.extend(selections)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return selections
