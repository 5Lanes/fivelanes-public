#!/usr/bin/env python3
"""
Run the fivelanes email + LLM pipeline on a fixed interval, with nightly quiet hours.

Active window defaults to 06:00–19:00 local time (no runs from 19:00 through 05:59).
The scheduler waits ``FIVELANES_INTERVAL_SEC`` after each run **finishes** (manual or
scheduled) before starting the next one. Only one pipeline run may execute at a time.

  python3 utils/run_fivelanes_scheduler.py

One shot (respects quiet hours; exits without running if currently quiet):

  python3 utils/run_fivelanes_scheduler.py --once

Environment (same names as dashboard_server where applicable):

  FIVELANES_INTERVAL_SEC       seconds after a run ends until the next (default 900)
  FIVELANES_LOOKBACK_DAYS      passed to fivelanes.main (default 180)
  FIVELANES_QUIET_START_HOUR   inclusive start of quiet period, 0–23 (default 19)
  FIVELANES_QUIET_END_HOUR     exclusive end of quiet period, 0–24 (default 6)
  FIVELANES_SCHEDULER_TZ       IANA timezone for quiet hours (default: system local)
  FIVELANES_SCHEDULER_WEEKDAYS run on Mon–Fri when 1/true (default: true)
  FIVELANES_SCHEDULER_WEEKENDS run on Sat–Sun when 1/true (default: true)
  CALENDAR_AVAILABILITY_DISABLE, CALENDAR_AVAILABILITY_WEEKS — same as dashboard_server

Also used by ``dashboard_server.py`` (background thread).
"""

from __future__ import annotations

import argparse
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from utils.runtime_paths import data_path, infra_root, load_env
from utils.scheduler_config import ScheduleConfig, get_schedule_config, scheduler_tz

load_env()

log = logging.getLogger(__name__)

from utils.lookback_config import get_lookback_days

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

_pipeline_run_lock = threading.Lock()
_last_run_finished_mono: float = 0.0


def pipeline_run_in_progress() -> bool:
    """True while a manual or scheduled pipeline cycle holds the global run lock."""
    return _pipeline_run_lock.locked()


def _mark_run_finished() -> None:
    global _last_run_finished_mono
    _last_run_finished_mono = time.monotonic()


def _seconds_until_next_interval() -> float:
    if _last_run_finished_mono <= 0:
        return 0.0
    interval = get_schedule_config().interval_sec
    return max(0.0, interval - (time.monotonic() - _last_run_finished_mono))


def _now_local(tz: ZoneInfo) -> datetime:
    return datetime.now(tz)


def in_quiet_hours(when: datetime, config: ScheduleConfig | None = None) -> bool:
    """True when ``when`` falls in the quiet window (no pipeline runs)."""
    cfg = config or get_schedule_config()
    hour = when.hour
    quiet_start = cfg.quiet_start_hour
    quiet_end = cfg.quiet_end_hour
    if quiet_start < quiet_end:
        return quiet_start <= hour < quiet_end
    if quiet_start > quiet_end:
        return hour >= quiet_start or hour < quiet_end
    return False


def is_active_day(when: datetime, config: ScheduleConfig | None = None) -> bool:
    """True when ``when`` falls on an allowed weekday or weekend."""
    cfg = config or get_schedule_config()
    is_weekday = when.weekday() < 5
    if is_weekday:
        return cfg.active_weekdays
    return cfg.active_weekends


def seconds_until_quiet_ends(when: datetime, config: ScheduleConfig | None = None) -> float:
    """Seconds until ``quiet_end`` o'clock on the same local calendar day as ``when``."""
    cfg = config or get_schedule_config()
    target = when.replace(hour=cfg.quiet_end_hour, minute=0, second=0, microsecond=0)
    if when >= target:
        target += timedelta(days=1)
    return max(0.0, (target - when).total_seconds())


def seconds_until_active_day(when: datetime, config: ScheduleConfig | None = None) -> float:
    """Seconds until the next allowed day at ``quiet_end_hour``."""
    cfg = config or get_schedule_config()
    if is_active_day(when, cfg):
        return 0.0
    for days_ahead in range(1, 8):
        target = (when + timedelta(days=days_ahead)).replace(
            hour=cfg.quiet_end_hour,
            minute=0,
            second=0,
            microsecond=0,
        )
        if is_active_day(target, cfg):
            return max(0.0, (target - when).total_seconds())
    return 86400.0


def run_fivelanes_cycle(*, trigger: str = "scheduler", blocking: bool = True) -> bool:
    """
    One full pipeline cycle: email + LLM (+ optional calendar export).

    Returns False when ``blocking`` is False and another run is already in progress.
    """
    if not _pipeline_run_lock.acquire(blocking=blocking):
        return False

    import fivelanes as fl
    from services.calendar_availability_export import run_calendar_availability_pull
    from utils.backend_config import get_backend
    from utils.pipeline_run_log import record_pipeline_run_finish, record_pipeline_run_start

    os.chdir(infra_root())
    backend = get_backend()
    started_at = record_pipeline_run_start(trigger=trigger, backend=backend)
    err: Optional[str] = None
    try:
        fl.main(lookback_days=get_lookback_days())
        from utils.features import is_enabled

        if is_enabled("availability") and not CALENDAR_AVAILABILITY_DISABLE:
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
        _pipeline_run_lock.release()
        _mark_run_finished()
    return True


def sleep_until_active_day(tz: ZoneInfo) -> None:
    now = _now_local(tz)
    cfg = get_schedule_config()
    wait_s = seconds_until_active_day(now, cfg)
    if wait_s <= 0:
        return
    day_bits = []
    if cfg.active_weekdays:
        day_bits.append("weekdays")
    if cfg.active_weekends:
        day_bits.append("weekends")
    log.info(
        "Inactive day (%s only); sleeping %.0fs until next allowed day",
        " + ".join(day_bits) or "none",
        wait_s,
    )
    time.sleep(wait_s)


def sleep_until_active(tz: ZoneInfo) -> None:
    now = _now_local(tz)
    cfg = get_schedule_config()
    if not in_quiet_hours(now, cfg):
        return
    wait_s = seconds_until_quiet_ends(now, cfg)
    log.info(
        "Quiet hours (%02d:00–%02d:00 %s); sleeping %.0fs until next run window",
        cfg.quiet_start_hour,
        cfg.quiet_end_hour,
        tz,
        wait_s,
    )
    time.sleep(wait_s)


def _wait_until_ready_for_scheduled_run(tz: ZoneInfo) -> None:
    """Sleep through inactive days, quiet hours, post-run interval, and any in-flight manual run."""
    while True:
        sleep_until_active_day(tz)
        sleep_until_active(tz)
        wait_s = _seconds_until_next_interval()
        if wait_s > 0:
            log.info(
                "Sleeping %.0fs until next run (interval after previous run ended)",
                wait_s,
            )
            time.sleep(wait_s)
        if not pipeline_run_in_progress():
            return
        log.info("Pipeline run in progress; waiting for it to finish")
        while pipeline_run_in_progress():
            time.sleep(1)


def _scheduler_is_idle(tz: ZoneInfo) -> bool:
    now = _now_local(tz)
    cfg = get_schedule_config()
    return is_active_day(now, cfg) and not in_quiet_hours(now, cfg)


def _warm_gai_chat_cache() -> None:
    """Refresh the GAI chat schema/snapshot cache after a run so the next chat turn is fast."""
    try:
        from utils.runtime_paths import database_path
        from services.gai.db_context import warm_chat_context_cache

        warm_chat_context_cache(database_path())
    except Exception:
        log.exception("Failed to warm GAI chat context cache")


def scheduler_loop(*, run_immediately: bool = True) -> None:
    tz = scheduler_tz()
    cfg = get_schedule_config()
    log.info(
        "Fivelanes scheduler loop started (pid=%d, %ds after each run ends, lookback_days=%d, "
        "quiet %02d:00–%02d:00 %s, weekdays=%s, weekends=%s)",
        os.getpid(),
        cfg.interval_sec,
        get_lookback_days(),
        cfg.quiet_start_hour,
        cfg.quiet_end_hour,
        tz,
        cfg.active_weekdays,
        cfg.active_weekends,
    )
    from utils.pipeline_run_log import reconcile_stale_pipeline_run

    if reconcile_stale_pipeline_run(in_progress=pipeline_run_in_progress()):
        log.warning("Reconciled stale pipeline run log (prior run did not finish cleanly)")
    if CALENDAR_AVAILABILITY_DISABLE:
        log.info("Calendar availability export: disabled")
    else:
        log.info(
            "Calendar availability export: after each run (%d weeks)",
            CALENDAR_AVAILABILITY_WEEKS,
        )

    if not run_immediately:
        _mark_run_finished()
        wait_s = _seconds_until_next_interval()
        if wait_s > 0:
            log.info("Waiting %.0fs before first run", wait_s)
            time.sleep(wait_s)

    while True:
        try:
            _wait_until_ready_for_scheduled_run(tz)
            started = time.monotonic()
            log.info("Starting fivelanes run")
            if not run_fivelanes_cycle(trigger="scheduler", blocking=False):
                continue
            elapsed = time.monotonic() - started
            log.info("Fivelanes run finished in %.1fs", elapsed)
            _warm_gai_chat_cache()
        except Exception:
            log.exception("Scheduled fivelanes run failed")


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

    tz = scheduler_tz()
    if not _scheduler_is_idle(tz):
        if args.once:
            log.info("Outside active schedule — skipping run")
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
