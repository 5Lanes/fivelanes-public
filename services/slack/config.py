"""Paths and env for Slack DM exports."""

from __future__ import annotations

import os
from pathlib import Path

from utils.runtime_paths import data_root

_SLACK_DMS_ENV = (os.getenv("SLACK_DMS_DIR") or "slack_dms").strip()
SLACK_DMS_DIR = (
    Path(_SLACK_DMS_ENV)
    if Path(_SLACK_DMS_ENV).is_absolute()
    else data_root() / _SLACK_DMS_ENV
)

INDEX_FILENAME = "index.json"


def conversation_file_path(conversation_key: str) -> Path:
    """Resolve on-disk JSON for a Slack DM ``channel_id``."""
    key = (conversation_key or "").strip()
    if not key:
        return SLACK_DMS_DIR / "_.json"
    index_path = SLACK_DMS_DIR / INDEX_FILENAME
    if index_path.is_file():
        try:
            import json

            raw = json.loads(index_path.read_text(encoding="utf-8"))
            for row in raw.get("conversations") or []:
                if str(row.get("channel_id") or "").strip() == key:
                    name = str(row.get("file") or "").strip()
                    if name:
                        return SLACK_DMS_DIR / name
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    for path in sorted(SLACK_DMS_DIR.glob("*.json")):
        if path.name == INDEX_FILENAME:
            continue
        try:
            import json

            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if str(raw.get("channel_id") or "").strip() == key:
            return path
    return SLACK_DMS_DIR / f"{key}.json"
