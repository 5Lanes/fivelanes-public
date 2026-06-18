#!/usr/bin/env python3
"""
Run the fivelanes email + LLM pipeline on a fixed interval, with nightly quiet hours.

Active window defaults to 06:00–24:00 local time (no runs from midnight through 05:59).
Between runs the process sleeps; during quiet hours it sleeps until the next active window.

  python3 utils/run_fivelanes_scheduler.py

One shot (respects quiet hours; exits without running if currently quiet):

  python3 utils/run_fivelanes_scheduler.py --once

Environment (same names as dashboard_server where applicable):

  FIVELANES_INTERVAL_SEC       seconds between runs (default 900 = 15 minutes)
  FIVELANES_LOOKBACK_DAYS      passed to fivelanes.main (default 14)
  FIVELANES_QUIET_START_HOUR   inclusive start of quiet period, 0–23 (default 0)
  FIVELANES_QUIET_END_HOUR     exclusive end of quiet period, 0–24 (default 6)
  FIVELANES_SCHEDULER_TZ       IANA timezone for quiet hours (default: system local)
  CALENDAR_AVAILABILITY_DISABLE, CALENDAR_AVAILABILITY_WEEKS — same as dashboard_server

Also used by ``dashboard_server.py`` (background thread).
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from utils.runtime_paths import data_path, env_file, infra_root, load_env

load_env()

log = logging.getLogger(__name__)

FIVELANES_INTERVAL_SEC = int(os.getenv("FIVELANES_INTERVAL_SEC", "900"))
FIVELANES_LOOKBACK_DAYS = int(os.getenv("FIVELANES_LOOKBACK_DAYS", "14"))
FIVELANES_QUIET_START_HOUR = int(os.getenv("FIVELANES_QUIET_START_HOUR", "0"))
FIVELANES_QUIET_END_HOUR = int(os.getenv("FIVELANES_QUIET_END_HOUR", "6"))

CALENDAR_AVAILABILITY_DISABLE = (os.getenv("CALENDAR_AVAILABILITY_DISABLE") or "").strip().lower() in (
    "1",
    "true",
    "yes",
)


def _calendar_availability_weeks_from_env() -> int:
    raw = (os.getenv("CALENDAR_AVAILABILITY_WEEKS") or "4").strip() or "4"
    try:
        return max(1, min(52, int(raw)))
    except ValueError:
        return 4


CALENDAR_AVAILABILITY_WEEKS = _calendar_availability_weeks_from_env()


def _scheduler_tz() -> ZoneInfo:
    name = (os.getenv("FIVELANES_SCHEDULER_TZ") or "").strip()
    if name:
        return ZoneInfo(name)
    return datetime.now().astimezone().tzinfo or ZoneInfo("UTC")


def _now_local(tz: ZoneInfo) -> datetime:
    return datetime.now(tz)


def in_quiet_hours(
    when: datetime,
    *,
    quiet_start: int = FIVELANES_QUIET_START_HOUR,
    quiet_end: int = FIVELANES_QUIET_END_HOUR,
) -> bool:
    """True when ``when`` falls in the quiet window (no pipeline runs)."""
    hour = when.hour
    if quiet_start < quiet_end:
        return quiet_start <= hour < quiet_end
    if quiet_start > quiet_end:
        return hour >= quiet_start or hour < quiet_end
    return False


def seconds_until_quiet_ends(
    when: datetime,
    *,
    quiet_end: int = FIVELANES_QUIET_END_HOUR,
) -> float:
    """Seconds until ``quiet_end`` o'clock on the same local calendar day as ``when``."""
    target = when.replace(hour=quiet_end, minute=0, second=0, microsecond=0)
    if when >= target:
        target += timedelta(days=1)
    return max(0.0, (target - when).total_seconds())


def run_fivelanes_cycle(*, trigger: str = "scheduler") -> None:
    """One full scheduled cycle: pipeline, out/ cleanup, optional calendar export."""
    import fivelanes as fl
    from services.calendar_availability_export import run_calendar_availability_pull
    from utils.backend_config import get_backend
    from utils.clear_out_json_older_than import main as clear_out_json_older_than_main
    from utils.pipeline_run_log import record_pipeline_run_finish, record_pipeline_run_start

    os.chdir(infra_root())
    backend = get_backend()
    started_at = record_pipeline_run_start(trigger=trigger, backend=backend)
    err: Optional[str] = None
    try:
        fl.main(lookback_days=FIVELANES_LOOKBACK_DAYS)
        clear_out_json_older_than_main()
        if not CALENDAR_AVAILABILITY_DISABLE:
            out_json = data_path("out", "availability_calendar_latest.json")
            try:
                run_calendar_availability_pull(
                    data_path(),
                    weeks=CALENDAR_AVAILABILITY_WEEKS,
                    out_path=out_json,
                )
            except Exception:
                log.exception("Calendar availability export failed")
    except Exception as exc:
        err = str(exc)
        log.exception("Fivelanes cycle failed")
        raise
    finally:
        record_pipeline_run_finish(
            started_at=started_at,
            trigger=trigger,
            backend=backend,
            ok=err is None,
            error=err,
        )


def sleep_until_active(tz: ZoneInfo) -> None:
    now = _now_local(tz)
    if not in_quiet_hours(now):
        return
    wait_s = seconds_until_quiet_ends(now)
    log.info(
        "Quiet hours (%02d:00–%02d:00 %s); sleeping %.0fs until next run window",
        FIVELANES_QUIET_START_HOUR,
        FIVELANES_QUIET_END_HOUR,
        tz,
        wait_s,
    )
    time.sleep(wait_s)


def scheduler_loop(*, run_immediately: bool = True) -> None:
    tz = _scheduler_tz()
    log.info(
        "Fivelanes scheduler loop started (pid=%d, every %ds, lookback_days=%d, quiet %02d:00–%02d:00 %s)",
        os.getpid(),
        FIVELANES_INTERVAL_SEC,
        FIVELANES_LOOKBACK_DAYS,
        FIVELANES_QUIET_START_HOUR,
        FIVELANES_QUIET_END_HOUR,
        tz,
    )
    if CALENDAR_AVAILABILITY_DISABLE:
        log.info("Calendar availability export: disabled")
    else:
        log.info(
            "Calendar availability export: after each run (%d weeks)",
            CALENDAR_AVAILABILITY_WEEKS,
        )

    if not run_immediately:
        log.info("Waiting %ds before first run", FIVELANES_INTERVAL_SEC)
        time.sleep(FIVELANES_INTERVAL_SEC)

    while True:
        try:
            sleep_until_active(tz)
            started = time.monotonic()
            log.info("Starting fivelanes run")
            run_fivelanes_cycle()
            elapsed = time.monotonic() - started
            log.info("Fivelanes run finished in %.1fs", elapsed)
        except Exception:
            log.exception("Scheduled fivelanes run failed")
        log.info("Sleeping %ds until next run", FIVELANES_INTERVAL_SEC)
        time.sleep(FIVELANES_INTERVAL_SEC)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single cycle if not in quiet hours, then exit",
    )
    parser.add_argument(
        "--no-immediate",
        action="store_true",
        help="Wait one interval before the first run (loop mode only)",
    )
    args = parser.parse_args()

    from utils.logging import configure_logging

    configure_logging()

    tz = _scheduler_tz()
    if in_quiet_hours(_now_local(tz)):
        if args.once:
            log.info("Quiet hours — skipping run")
            return 0
        scheduler_loop(run_immediately=not args.no_immediate)
        return 0

    if args.once:
        try:
            run_fivelanes_cycle()
        except Exception:
            log.exception("Fivelanes run failed")
            return 1
        return 0

    scheduler_loop(run_immediately=not args.no_immediate)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
