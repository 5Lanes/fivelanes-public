"""Paths and env for LinkedIn message exports."""

from __future__ import annotations

import os
from pathlib import Path

from utils.runtime_paths import data_root

_LINKEDIN_MESSAGES_ENV = (os.getenv("LINKEDIN_MESSAGES_DIR") or "linkedin-messages").strip()
LINKEDIN_MESSAGES_DIR = (
    Path(_LINKEDIN_MESSAGES_ENV)
    if Path(_LINKEDIN_MESSAGES_ENV).is_absolute()
    else data_root() / _LINKEDIN_MESSAGES_ENV
)

_MESSAGES_CSV_ENV = (os.getenv("LINKEDIN_MESSAGES_CSV") or "messages.csv").strip()
MESSAGES_CSV_FILENAME = _MESSAGES_CSV_ENV


def messages_csv_path() -> Path:
    """Primary LinkedIn export CSV (LinkedIn data export format)."""
    name = MESSAGES_CSV_FILENAME
    if Path(name).is_absolute():
        return Path(name)
    return LINKEDIN_MESSAGES_DIR / name
