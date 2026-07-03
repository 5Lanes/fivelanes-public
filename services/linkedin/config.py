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

_LINKEDIN_SCRAPER_ENV = (os.getenv("LINKEDIN_SCRAPER_DIR") or "linkedin").strip()
LINKEDIN_SCRAPER_DIR = (
    Path(_LINKEDIN_SCRAPER_ENV)
    if Path(_LINKEDIN_SCRAPER_ENV).is_absolute()
    else data_root() / _LINKEDIN_SCRAPER_ENV
)

_SELECTIONS_ENV = (os.getenv("LINKEDIN_SELECTIONS_FILE") or "selections.txt").strip()
LINKEDIN_SELECTIONS_PATH = (
    Path(_SELECTIONS_ENV)
    if Path(_SELECTIONS_ENV).is_absolute()
    else LINKEDIN_SCRAPER_DIR / _SELECTIONS_ENV
)

_SCRAPER_DATA_ENV = (os.getenv("LINKEDIN_SCRAPER_DATA_DIR") or "data").strip()
LINKEDIN_SCRAPER_DATA_DIR = (
    Path(_SCRAPER_DATA_ENV)
    if Path(_SCRAPER_DATA_ENV).is_absolute()
    else LINKEDIN_SCRAPER_DIR / _SCRAPER_DATA_ENV
)

_SCRAPER_CSV_ENV = (os.getenv("LINKEDIN_SCRAPER_MESSAGES_CSV") or "messages.csv").strip()


def messages_csv_path() -> Path:
    """Primary LinkedIn export CSV (LinkedIn data export format)."""
    name = MESSAGES_CSV_FILENAME
    if Path(name).is_absolute():
        return Path(name)
    return LINKEDIN_MESSAGES_DIR / name


def scraper_messages_csv_path() -> Path:
    """Scraper output CSV under ``linkedin/data/messages.csv`` by default."""
    name = _SCRAPER_CSV_ENV
    if Path(name).is_absolute():
        return Path(name)
    return LINKEDIN_SCRAPER_DATA_DIR / name
