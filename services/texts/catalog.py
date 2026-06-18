"""Scan ``conversations/`` for available text threads."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from services.texts.config import CONVERSATIONS_DIR
from services.texts.format import conversation_metadata, load_conversation_messages


def list_conversation_catalog(
    conversations_dir: Path | None = None,
) -> List[Dict[str, Any]]:
    root = conversations_dir or CONVERSATIONS_DIR
    if not root.is_dir():
        return []

    catalog: List[Dict[str, Any]] = []
    for path in sorted(root.glob("*.json"), key=lambda p: p.name.lower()):
        key = path.stem
        messages = load_conversation_messages(path)
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
