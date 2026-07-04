"""Paths for Meet recording Doc catalog and imported summaries."""

from __future__ import annotations

import os
from pathlib import Path

from utils.runtime_paths import data_root, load_env

load_env()

INDEX_FILENAME = "index.json"


def meet_recordings_dir() -> Path:
    raw = (os.getenv("MEET_RECORDINGS_DIR") or "meet-recordings").strip()
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = data_root() / path
    return path


MEET_RECORDINGS_DIR = meet_recordings_dir()
