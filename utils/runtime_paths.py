"""Resolve infrastructure (code) vs data (user-specific) paths.

The public OS repo ships code only. Runtime data lives under ``FIVELANES_DATA_ROOT``
(conventionally ``fivelanes-data/`` beside the clone). That directory holds ``.env``,
``credentials/``, ``timeline.db``, ``out/``, ``logs/``, and ``conversations/``.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path


def infra_root() -> Path:
    """Directory containing application code (this repository by default)."""
    override = (os.getenv("FIVELANES_INFRA_ROOT") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


def premium_root() -> Path:
    """Optional paid add-on package directory (sibling ``fivelanes-premium/`` by default)."""
    override = (os.getenv("FIVELANES_PREMIUM_ROOT") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return infra_root() / "fivelanes-premium"


def _data_root_from_env() -> Path:
    """Resolve data root from the current environment (before or after ``load_env``)."""
    override = (os.getenv("FIVELANES_DATA_ROOT") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return infra_root() / "fivelanes-data"


@lru_cache(maxsize=1)
def load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    bootstrap = infra_root() / ".env"
    if bootstrap.is_file():
        load_dotenv(bootstrap)
    data_env = _data_root_from_env() / ".env"
    if data_env.is_file():
        load_dotenv(data_env)


def data_root() -> Path:
    load_env()
    return _data_root_from_env()


def env_file() -> Path:
    return data_root() / ".env"


def credentials_dir() -> Path:
    return data_root() / "credentials"


def database_path() -> str:
    load_env()
    name = (os.getenv("DATABASE_NAME") or "timeline.db").strip() or "timeline.db"
    path = Path(name)
    if path.is_absolute():
        return str(path)
    return str(data_root() / path)


def data_path(*parts: str) -> Path:
    return data_root().joinpath(*parts)
