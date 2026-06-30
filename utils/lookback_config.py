"""Runtime lookback-days setting for email pull and LLM pipeline."""

from __future__ import annotations

import os
from pathlib import Path

from services.email.config import DEFAULT_INBOX_LOOKBACK_DAYS
from utils.runtime_paths import env_file, load_env

load_env()
_ENV_PATH = env_file()

_runtime_lookback_days: int | None = None

_MIN_LOOKBACK_DAYS = 1
_MAX_LOOKBACK_DAYS = 3650


def _normalize_lookback_days(value: int | str | None) -> int:
    try:
        days = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        days = DEFAULT_INBOX_LOOKBACK_DAYS
    if days < _MIN_LOOKBACK_DAYS or days > _MAX_LOOKBACK_DAYS:
        raise ValueError(
            f"lookback_days must be {_MIN_LOOKBACK_DAYS}–{_MAX_LOOKBACK_DAYS}, got {days!r}"
        )
    return days


def get_lookback_days() -> int:
    global _runtime_lookback_days
    if _runtime_lookback_days is not None:
        return _runtime_lookback_days
    raw = (os.getenv("FIVELANES_LOOKBACK_DAYS") or str(DEFAULT_INBOX_LOOKBACK_DAYS)).strip()
    try:
        return max(_MIN_LOOKBACK_DAYS, int(raw))
    except ValueError:
        return DEFAULT_INBOX_LOOKBACK_DAYS


def apply_lookback_days(days: int | str) -> int:
    global _runtime_lookback_days
    normalized = _normalize_lookback_days(days)
    _runtime_lookback_days = normalized
    os.environ["FIVELANES_LOOKBACK_DAYS"] = str(normalized)
    return normalized


def persist_lookback_days(days: int | str, *, env_path: Path | None = None) -> int:
    normalized = _normalize_lookback_days(days)
    path = env_path or _ENV_PATH
    lines: list[str] = []
    if path.is_file():
        lines = path.read_text(encoding="utf-8").splitlines()
    updated: list[str] = []
    found = False
    for line in lines:
        if line.startswith("FIVELANES_LOOKBACK_DAYS="):
            updated.append(f"FIVELANES_LOOKBACK_DAYS={normalized}")
            found = True
        else:
            updated.append(line)
    if not found:
        updated.append(f"FIVELANES_LOOKBACK_DAYS={normalized}")
    path.write_text("\n".join(updated) + "\n", encoding="utf-8")
    return normalized


def set_lookback_days(days: int | str, *, persist: bool = True) -> int:
    applied = apply_lookback_days(days)
    if persist:
        persist_lookback_days(applied)
    return applied
