"""
Shared logging for fivelanes (dashboard, scheduler, CLI).

Writes to stdout and a rotating file under ``logs/`` (default ``logs/fivelanes.log``).

Environment:

  FIVELANES_LOG_LEVEL       DEBUG, INFO, … (default INFO)
  FIVELANES_LOG_DIR         directory for log files (default: project logs/)
  FIVELANES_LOG_FILE        log file name (default fivelanes.log)
  FIVELANES_LOG_MAX_BYTES   rotate size (default 10485760)
  FIVELANES_LOG_BACKUP_COUNT  kept rotations (default 5)
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from utils.runtime_paths import data_path

_CONFIGURED = False

_LOG_FORMAT = "%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s"
_LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"


def _log_level_from_env() -> int:
    raw = (os.getenv("FIVELANES_LOG_LEVEL") or "INFO").strip().upper()
    return getattr(logging, raw, logging.INFO)


def _log_path() -> Path:
    log_dir = Path(os.getenv("FIVELANES_LOG_DIR") or data_path("logs"))
    log_dir.mkdir(parents=True, exist_ok=True)
    name = (os.getenv("FIVELANES_LOG_FILE") or "fivelanes.log").strip() or "fivelanes.log"
    return log_dir / name


def configure_logging(*, level: int | None = None) -> Path:
    """Attach stdout + rotating file handlers to the root logger (once)."""
    global _CONFIGURED
    log_path = _log_path()
    if _CONFIGURED:
        return log_path

    lvl = level if level is not None else _log_level_from_env()
    root = logging.getLogger()
    root.setLevel(lvl)
    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    max_bytes = int(os.getenv("FIVELANES_LOG_MAX_BYTES", "10485760"))
    backup_count = int(os.getenv("FIVELANES_LOG_BACKUP_COUNT", "5"))
    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    _CONFIGURED = True
    logging.getLogger(__name__).info("Logging to %s (level=%s)", log_path, logging.getLevelName(lvl))
    return log_path
