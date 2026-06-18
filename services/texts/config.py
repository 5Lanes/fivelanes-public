"""Paths and env for text conversation storage."""

from __future__ import annotations

import os
from pathlib import Path

from utils.runtime_paths import data_root, infra_root

PROJECT_ROOT = infra_root()

_CONVERSATIONS_ENV = (os.getenv("TEXTS_CONVERSATIONS_DIR") or "conversations").strip()
CONVERSATIONS_DIR = (
    Path(_CONVERSATIONS_ENV)
    if Path(_CONVERSATIONS_ENV).is_absolute()
    else data_root() / _CONVERSATIONS_ENV
)


def conversation_file_path(conversation_key: str) -> Path:
    """JSON file for one thread (filename stem = ``conversation_key``)."""
    return CONVERSATIONS_DIR / f"{conversation_key}.json"
