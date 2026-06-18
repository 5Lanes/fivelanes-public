"""Resolve infrastructure (code) vs data (user-specific) paths.

The public OS repo ships code only. A private layer can point ``FIVELANES_DATA_ROOT``
at a separate directory that holds ``.env``, ``credentials/``, ``timeline.db``,
``out/``, ``logs/``, and ``conversations/``.
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


def data_root() -> Path:
    """Directory for user-specific runtime data."""
    override = (os.getenv("FIVELANES_DATA_ROOT") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return infra_root()


def env_file() -> Path:
    return data_root() / ".env"


def credentials_dir() -> Path:
    return data_root() / "credentials"


def database_path() -> str:
    name = (os.getenv("DATABASE_NAME") or "timeline.db").strip() or "timeline.db"
    path = Path(name)
    if path.is_absolute():
        return str(path)
    return str(data_root() / path)


def data_path(*parts: str) -> Path:
    return data_root().joinpath(*parts)


@lru_cache(maxsize=1)
def load_env() -> None:
    path = env_file()
    if not path.is_file():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(path)
