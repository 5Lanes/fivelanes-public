"""Runtime scheduler settings shared by dashboard and background loop."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from utils.runtime_paths import env_file, load_env

load_env()
_ENV_PATH = env_file()

_runtime_schedule: "ScheduleConfig | None" = None


@dataclass(frozen=True)
class ScheduleConfig:
    interval_sec: int
    quiet_start_hour: int
    quiet_end_hour: int
    timezone: str
    active_weekdays: bool
    active_weekends: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "interval_sec": self.interval_sec,
            "quiet_start_hour": self.quiet_start_hour,
            "quiet_end_hour": self.quiet_end_hour,
            "timezone": self.timezone,
            "active_weekdays": self.active_weekdays,
            "active_weekends": self.active_weekends,
        }


def _env_bool(key: str, *, default: bool) -> bool:
    raw = (os.getenv(key) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes")


def _normalize_hour(value: int | str | None, *, default: int) -> int:
    try:
        hour = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if hour < 0 or hour > 23:
        raise ValueError(f"Hour must be 0–23, got {hour!r}")
    return hour


def _normalize_interval(value: int | str | None) -> int:
    try:
        sec = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        sec = 900
    if sec < 60:
        raise ValueError(f"interval_sec must be at least 60, got {sec!r}")
    return sec


def _normalize_timezone(value: str | None) -> str:
    tz = (value or "").strip()
    if not tz:
        return ""
    ZoneInfo(tz)
    return tz


def _schedule_from_env() -> ScheduleConfig:
    return ScheduleConfig(
        interval_sec=_normalize_interval(os.getenv("FIVELANES_INTERVAL_SEC", "900")),
        quiet_start_hour=_normalize_hour(os.getenv("FIVELANES_QUIET_START_HOUR"), default=19),
        quiet_end_hour=_normalize_hour(os.getenv("FIVELANES_QUIET_END_HOUR"), default=6),
        timezone=_normalize_timezone(os.getenv("FIVELANES_SCHEDULER_TZ")),
        active_weekdays=_env_bool("FIVELANES_SCHEDULER_WEEKDAYS", default=True),
        active_weekends=_env_bool("FIVELANES_SCHEDULER_WEEKENDS", default=True),
    )


def get_schedule_config() -> ScheduleConfig:
    global _runtime_schedule
    if _runtime_schedule is not None:
        return _runtime_schedule
    return _schedule_from_env()


def scheduler_tz(config: ScheduleConfig | None = None) -> ZoneInfo:
    cfg = config or get_schedule_config()
    if cfg.timezone:
        return ZoneInfo(cfg.timezone)
    from datetime import datetime

    return datetime.now().astimezone().tzinfo or ZoneInfo("UTC")


def parse_schedule_config(data: dict[str, Any]) -> ScheduleConfig:
    active_weekdays = data.get("active_weekdays", True)
    active_weekends = data.get("active_weekends", True)
    if isinstance(active_weekdays, str):
        active_weekdays = active_weekdays.strip().lower() in ("1", "true", "yes")
    if isinstance(active_weekends, str):
        active_weekends = active_weekends.strip().lower() in ("1", "true", "yes")
    if not active_weekdays and not active_weekends:
        raise ValueError("At least one of active_weekdays or active_weekends must be enabled")

    return ScheduleConfig(
        interval_sec=_normalize_interval(data.get("interval_sec")),
        quiet_start_hour=_normalize_hour(data.get("quiet_start_hour"), default=19),
        quiet_end_hour=_normalize_hour(data.get("quiet_end_hour"), default=6),
        timezone=_normalize_timezone(str(data.get("timezone") or "")),
        active_weekdays=bool(active_weekdays),
        active_weekends=bool(active_weekends),
    )


def apply_schedule_config(config: ScheduleConfig) -> ScheduleConfig:
    global _runtime_schedule
    _runtime_schedule = config
    os.environ["FIVELANES_INTERVAL_SEC"] = str(config.interval_sec)
    os.environ["FIVELANES_QUIET_START_HOUR"] = str(config.quiet_start_hour)
    os.environ["FIVELANES_QUIET_END_HOUR"] = str(config.quiet_end_hour)
    os.environ["FIVELANES_SCHEDULER_TZ"] = config.timezone
    os.environ["FIVELANES_SCHEDULER_WEEKDAYS"] = "1" if config.active_weekdays else "0"
    os.environ["FIVELANES_SCHEDULER_WEEKENDS"] = "1" if config.active_weekends else "0"
    return config


def _persist_env_values(updates: dict[str, str], *, env_path: Path | None = None) -> None:
    path = env_path or _ENV_PATH
    lines: list[str] = []
    if path.is_file():
        lines = path.read_text(encoding="utf-8").splitlines()
    updated: list[str] = []
    found: set[str] = set()
    for line in lines:
        matched = False
        for key, value in updates.items():
            if line.startswith(f"{key}="):
                updated.append(f"{key}={value}")
                found.add(key)
                matched = True
                break
        if not matched:
            updated.append(line)
    for key, value in updates.items():
        if key not in found:
            updated.append(f"{key}={value}")
    path.write_text("\n".join(updated) + "\n", encoding="utf-8")


def persist_schedule_config(config: ScheduleConfig, *, env_path: Path | None = None) -> ScheduleConfig:
    _persist_env_values(
        {
            "FIVELANES_INTERVAL_SEC": str(config.interval_sec),
            "FIVELANES_QUIET_START_HOUR": str(config.quiet_start_hour),
            "FIVELANES_QUIET_END_HOUR": str(config.quiet_end_hour),
            "FIVELANES_SCHEDULER_TZ": config.timezone,
            "FIVELANES_SCHEDULER_WEEKDAYS": "1" if config.active_weekdays else "0",
            "FIVELANES_SCHEDULER_WEEKENDS": "1" if config.active_weekends else "0",
        },
        env_path=env_path,
    )
    return config


def set_schedule_config(
    config: ScheduleConfig | dict[str, Any],
    *,
    persist: bool = True,
) -> ScheduleConfig:
    parsed = parse_schedule_config(config) if isinstance(config, dict) else config
    applied = apply_schedule_config(parsed)
    if persist:
        persist_schedule_config(applied)
    return applied
