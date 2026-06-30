"""Scan ``linkedin-messages/messages.csv`` for available LinkedIn threads."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from services.linkedin.config import LINKEDIN_MESSAGES_DIR, messages_csv_path
from services.linkedin.format import (
    _load_rows_by_conversation,
    conversation_metadata,
    load_messages_for_key,
)


def list_conversation_catalog(
    linkedin_messages_dir: Path | None = None,
) -> List[Dict[str, Any]]:
    root = linkedin_messages_dir or LINKEDIN_MESSAGES_DIR
    csv_path = messages_csv_path() if linkedin_messages_dir is None else root / "messages.csv"
    if not csv_path.is_file():
        return []

    grouped = _load_rows_by_conversation()
    catalog: List[Dict[str, Any]] = []
    for key in sorted(grouped.keys(), key=str.lower):
        messages = load_messages_for_key(key)
        meta = conversation_metadata(key, messages)
        catalog.append(
            {
                "id": key,
                "conversation_key": key,
                **meta,
            }
        )
    catalog.sort(
        key=lambda row: (row.get("last_message_at") or "", row.get("label") or ""),
        reverse=True,
    )
    return catalog
