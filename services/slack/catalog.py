"""Scan ``slack_dms/`` for available Slack DM exports."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from services.slack.config import INDEX_FILENAME, SLACK_DMS_DIR
from services.slack.format import conversation_metadata, load_messages_for_key


def list_conversation_catalog(
    slack_dms_dir: Path | None = None,
) -> List[Dict[str, Any]]:
    root = slack_dms_dir or SLACK_DMS_DIR
    if not root.is_dir():
        return []

    catalog: List[Dict[str, Any]] = []
    index_path = root / INDEX_FILENAME
    entries: List[Dict[str, Any]] = []

    if index_path.is_file():
        try:
            raw = json.loads(index_path.read_text(encoding="utf-8"))
            convs = raw.get("conversations")
            if isinstance(convs, list):
                entries = [e for e in convs if isinstance(e, dict)]
        except (OSError, json.JSONDecodeError):
            entries = []

    if entries:
        for row in entries:
            key = str(row.get("channel_id") or "").strip()
            if not key:
                continue
            messages = load_messages_for_key(key)
            meta = conversation_metadata(key, messages)
            catalog.append(
                {
                    "id": key,
                    "conversation_key": key,
                    "file": str(row.get("file") or ""),
                    **meta,
                }
            )
    else:
        for path in sorted(root.glob("*.json"), key=lambda p: p.name.lower()):
            if path.name == INDEX_FILENAME:
                continue
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(raw, dict):
                continue
            key = str(raw.get("channel_id") or path.stem).strip()
            messages = raw.get("messages")
            msg_list = [m for m in messages if isinstance(m, dict)] if isinstance(messages, list) else []
            meta = conversation_metadata(key, msg_list)
            catalog.append(
                {
                    "id": key,
                    "conversation_key": key,
                    "file": path.name,
                    **meta,
                }
            )

    catalog.sort(
        key=lambda row: (row.get("last_message_at") or "", row.get("label") or ""),
        reverse=True,
    )
    return catalog
