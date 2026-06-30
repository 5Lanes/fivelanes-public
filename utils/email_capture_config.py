"""Runtime email capture mode (forwards vs labels) for dashboard settings."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from utils.runtime_paths import env_file, load_env

load_env()
_ENV_PATH = env_file()

EmailCaptureMode = Literal["forwards", "labels"]

_runtime_email_capture: EmailCaptureMode | None = None


def _normalize_email_capture(value: str | None) -> EmailCaptureMode:
    mode = (value or "forwards").strip().lower()
    if mode not in ("forwards", "labels"):
        raise ValueError(f"Invalid FIVELANES_EMAIL_CAPTURE: {mode!r}")
    return mode  # type: ignore[return-value]


def get_email_capture_mode() -> EmailCaptureMode:
    global _runtime_email_capture
    if _runtime_email_capture is not None:
        return _runtime_email_capture
    return _normalize_email_capture(os.getenv("FIVELANES_EMAIL_CAPTURE"))


def apply_email_capture_mode(mode: str) -> EmailCaptureMode:
    global _runtime_email_capture
    normalized = _normalize_email_capture(mode)
    _runtime_email_capture = normalized
    os.environ["FIVELANES_EMAIL_CAPTURE"] = normalized
    return normalized


def persist_email_capture_mode(mode: str, *, env_path: Path | None = None) -> EmailCaptureMode:
    normalized = _normalize_email_capture(mode)
    path = env_path or _ENV_PATH
    lines: list[str] = []
    if path.is_file():
        lines = path.read_text(encoding="utf-8").splitlines()
    updated: list[str] = []
    found = False
    for line in lines:
        if line.startswith("FIVELANES_EMAIL_CAPTURE="):
            updated.append(f"FIVELANES_EMAIL_CAPTURE={normalized}")
            found = True
        else:
            updated.append(line)
    if not found:
        updated.append(f"FIVELANES_EMAIL_CAPTURE={normalized}")
    path.write_text("\n".join(updated) + "\n", encoding="utf-8")
    return normalized


def set_email_capture_mode(mode: str, *, persist: bool = True) -> EmailCaptureMode:
    applied = apply_email_capture_mode(mode)
    if persist:
        persist_email_capture_mode(applied)
    return applied
