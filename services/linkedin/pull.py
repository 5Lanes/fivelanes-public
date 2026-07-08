"""Pull LinkedIn DMs via Playwright and merge new messages into ``messages.csv``."""

from __future__ import annotations

import csv
import logging
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Set, Tuple

from services.linkedin.config import (
    LINKEDIN_SCRAPER_DATA_DIR,
    LINKEDIN_SCRAPER_DIR,
    LINKEDIN_SELECTIONS_PATH,
    messages_csv_path,
    scraper_messages_csv_path,
)
from services.linkedin.format import clear_csv_cache
from services.linkedin.selections import write_selections_for_conversation_keys
from services.linkedin.tracking import fetch_tracked_conversation_keys
from utils.owner_config import owner_name

log = logging.getLogger(__name__)

_EXPORT_HEADERS = [
    "CONVERSATION ID",
    "CONVERSATION TITLE",
    "FROM",
    "SENDER PROFILE URL",
    "TO",
    "RECIPIENT PROFILE URLS",
    "DATE",
    "SUBJECT",
    "CONTENT",
    "FOLDER",
    "ATTACHMENTS",
    "IS MESSAGE DRAFT",
    "IS CONVERSATION DRAFT",
]


_WEEKDAY_INDEX = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

_MONTH_INDEX = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


def _parse_scraped_at(value: str) -> datetime:
    text = (value or "").strip()
    if not text:
        return datetime.now(timezone.utc)
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _combine_date_and_clock(date: datetime, clock: str) -> datetime:
    match = re.match(r"^(\d{1,2}):(\d{2})\s*(AM|PM)$", clock.strip(), re.IGNORECASE)
    if not match:
        return date
    hour = int(match.group(1)) % 12
    if match.group(3).upper() == "PM":
        hour += 12
    minute = int(match.group(2))
    return date.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _normalize_scraper_timestamp(timestamp: str, *, scraped_at: str) -> str:
    raw = (timestamp or "").strip()
    if not raw:
        return raw
    if re.match(r"^\d{4}-\d{2}-\d{2}", raw):
        return raw

    anchor = _parse_scraped_at(scraped_at)
    lower = raw.lower()
    if lower == "today":
        return _format_utc(anchor)
    if lower == "yesterday":
        return _format_utc(anchor - timedelta(days=1))

    weekday = _WEEKDAY_INDEX.get(lower)
    if weekday is not None:
        days_back = (anchor.weekday() - weekday) % 7
        return _format_utc(anchor - timedelta(days=days_back))

    month_day = re.match(r"^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\s+(\d{1,2})$", lower)
    if month_day:
        month = _MONTH_INDEX[month_day.group(1)]
        day = int(month_day.group(2))
        year = anchor.year
        try:
            return _format_utc(datetime(year, month, day, tzinfo=timezone.utc))
        except ValueError:
            return raw

    clock_only = re.match(r"^(\d{1,2}:\d{2}\s*(?:AM|PM))$", raw, re.IGNORECASE)
    if clock_only:
        return _format_utc(_combine_date_and_clock(anchor, clock_only.group(1)))

    return raw


def _dedupe_export_rows(rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
    seen: Set[Tuple[str, str, str]] = set()
    deduped: List[Dict[str, str]] = []
    for row in rows:
        key = (
            str(row.get("CONVERSATION ID") or "").strip(),
            str(row.get("FROM") or "").strip(),
            str(row.get("CONTENT") or "").strip(),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def _is_absolute_date(value: str) -> bool:
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}", (value or "").strip()))


def _prior_absolute_dates(rows: List[Dict[str, str]]) -> Dict[Tuple[str, str, str], str]:
    """Map (conversation, sender, content) -> earliest known absolute DATE seen so far.

    LinkedIn sometimes shows only a bare clock time (no date) for a message that isn't
    actually from today; ``_normalize_scraper_timestamp`` then anchors it to the scrape
    time, so re-scraping the same old message stamps it with a new "today" every run.
    Keeping the earliest absolute date we've already recorded for identical content
    stops that content from drifting forward in time on each re-scrape.
    """
    lookup: Dict[Tuple[str, str, str], str] = {}
    for row in rows:
        date = str(row.get("DATE") or "").strip()
        if not _is_absolute_date(date):
            continue
        key = (
            str(row.get("CONVERSATION ID") or "").strip(),
            str(row.get("FROM") or "").strip(),
            str(row.get("CONTENT") or "").strip(),
        )
        existing = lookup.get(key)
        if existing is None or date < existing:
            lookup[key] = date
    return lookup


def _owner_profile_url() -> str:
    return (os.getenv("LINKEDIN_PROFILE_URL") or "").strip()


def _normalize_keys(conversation_keys: Iterable[str] | None) -> List[str]:
    return [k.strip() for k in (conversation_keys or []) if str(k).strip()]


def _tsx_cli(scraper_dir: Path) -> Path:
    return scraper_dir / "node_modules" / "tsx" / "dist" / "cli.mjs"


def _run_scraper(*, output_path: Path, selections_path: Path) -> None:
    scraper_dir = LINKEDIN_SCRAPER_DIR
    package_json = scraper_dir / "package.json"
    if not package_json.is_file():
        raise RuntimeError(f"linkedin_scraper_not_found: {scraper_dir}")

    tsx_cli = _tsx_cli(scraper_dir)
    if not tsx_cli.is_file():
        raise RuntimeError(
            f"linkedin_scraper_not_installed: {scraper_dir} "
            "(run npm install in your data linkedin/ folder)"
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    if os.getenv("LINKEDIN_SCRAPER_HEADED", "").lower() in ("1", "true", "yes"):
        env["HEADLESS"] = "false"
    elif os.getenv("DISPLAY"):
        env.setdefault("HEADLESS", "false")

    cmd = [
        "node",
        str(tsx_cli),
        "src/index.ts",
        "--selections",
        str(selections_path),
        "--output",
        str(output_path),
    ]
    log.info("Running LinkedIn scraper: %s (cwd=%s)", " ".join(cmd), scraper_dir)
    completed = subprocess.run(
        cmd,
        cwd=scraper_dir,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
        env=env,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(detail or "linkedin_scraper_failed")
    if not output_path.is_file():
        raise RuntimeError(f"linkedin_scraper_produced_no_output: {output_path}")


def _read_export_rows(path: Path) -> List[Dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return [dict(row) for row in reader if isinstance(row, dict)]


def _owner_display_name(scraped_rows: List[Dict[str, str]]) -> str:
    configured = owner_name().strip()
    if configured and configured.lower() != "owner":
        return configured
    for row in scraped_rows:
        if str(row.get("is_from_me") or "").strip().lower() != "true":
            continue
        sender = str(row.get("sender") or "").strip()
        if sender and sender.lower() != "unknown":
            return sender
    return configured or "Owner"


def _scraper_row_to_export(
    row: Dict[str, str],
    *,
    owner_display: str,
    prior_dates: Dict[Tuple[str, str, str], str] | None = None,
) -> Dict[str, str]:
    is_from_me = str(row.get("is_from_me") or "").strip().lower() == "true"
    participant = str(row.get("participant") or "").strip()
    participant_url = str(row.get("participant_url") or "").strip()
    sender = str(row.get("sender") or "").strip()
    if sender.lower() == "unknown":
        sender = ""
    owner_url = _owner_profile_url()
    conversation_id = str(row.get("conversation_id") or "").strip()
    content = str(row.get("message") or "")
    raw_timestamp = str(row.get("timestamp") or "")
    timestamp = _normalize_scraper_timestamp(
        raw_timestamp,
        scraped_at=str(row.get("scraped_at") or ""),
    )

    if is_from_me:
        from_name = sender or owner_display
        to_name = participant
        from_url = owner_url
        to_urls = participant_url
    else:
        from_name = sender or participant
        to_name = owner_display
        from_url = participant_url
        to_urls = owner_url

    if not _is_absolute_date(raw_timestamp) and prior_dates:
        prior = prior_dates.get((conversation_id, from_name.strip(), content.strip()))
        if prior:
            timestamp = prior

    return {
        "CONVERSATION ID": conversation_id,
        "CONVERSATION TITLE": "",
        "FROM": from_name,
        "SENDER PROFILE URL": from_url,
        "TO": to_name,
        "RECIPIENT PROFILE URLS": to_urls,
        "DATE": timestamp,
        "SUBJECT": "",
        "CONTENT": content,
        "FOLDER": "INBOX",
        "ATTACHMENTS": "",
        "IS MESSAGE DRAFT": "No",
        "IS CONVERSATION DRAFT": "No",
    }


def _read_scraped_rows(path: Path) -> List[Dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return [dict(row) for row in reader if isinstance(row, dict)]


def _merge_scraped_into_export(
    scraped_path: Path,
    *,
    conversation_keys: Set[str],
) -> Dict[str, int]:
    """Replace pulled conversations in the export CSV with fresh scraper output."""
    export_path = messages_csv_path()
    export_path.parent.mkdir(parents=True, exist_ok=True)

    existing = _read_export_rows(export_path)
    scraped = _read_scraped_rows(scraped_path)
    if conversation_keys:
        scraped = [
            row
            for row in scraped
            if str(row.get("conversation_id") or "").strip() in conversation_keys
        ]

    owner_display = _owner_display_name(scraped)
    prior_dates = _prior_absolute_dates(existing)
    converted = _dedupe_export_rows(
        [
            _scraper_row_to_export(row, owner_display=owner_display, prior_dates=prior_dates)
            for row in scraped
            if row.get("conversation_id")
        ]
    )

    pulled_conversation_ids = {
        str(row.get("CONVERSATION ID") or "").strip() for row in converted if row.get("CONVERSATION ID")
    }

    if conversation_keys and not scraped:
        raise RuntimeError(
            f"no_matching_messages_in_scraper_output: {scraped_path} "
            f"(selected {len(conversation_keys)} conversation(s))"
        )

    kept = [
        row
        for row in existing
        if str(row.get("CONVERSATION ID") or "").strip() not in pulled_conversation_ids
    ]
    pulled_rows = _dedupe_export_rows(converted)
    merged = kept + pulled_rows
    with export_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_EXPORT_HEADERS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(merged)

    clear_csv_cache()
    return {
        "conversation_count": len(pulled_conversation_ids),
        "message_count": len(pulled_rows),
        "total_rows": len(merged),
    }


def _repair_db_owner_senders(db_path: str) -> int:
    from utils.database import connect_sqlite

    with connect_sqlite(db_path) as conn:
        cur = conn.execute(
            """
            UPDATE claude_message_outputs
            SET sender = 'me'
            WHERE thread_id LIKE 'linkedin:%'
              AND COALESCE(TRIM(sender), '') = 'Owner'
            """
        )
        conn.commit()
        return int(cur.rowcount or 0)


def pull_linkedin_messages(
    db_path: str | None = None,
    *,
    conversation_keys: Iterable[str] | None = None,
    run_scraper: bool = True,
) -> Dict[str, Any]:
    """
    Scrape selected LinkedIn conversations via Playwright, then replace those
    conversations in ``linkedin-messages/messages.csv`` with fresh scraper output.

    ``conversation_keys`` comes from the setup UI checkboxes. When omitted, falls back
    to tracked keys in ``thread_tracking``.
    """
    keys = _normalize_keys(conversation_keys)
    if not keys and db_path:
        keys = fetch_tracked_conversation_keys(db_path)
    if not keys:
        return {"ok": True, "skipped": True, "reason": "no_conversations_selected"}

    selections = write_selections_for_conversation_keys(keys)
    scraped_path = scraper_messages_csv_path()

    if run_scraper:
        _run_scraper(output_path=scraped_path, selections_path=LINKEDIN_SELECTIONS_PATH)
    elif not scraped_path.is_file():
        raise RuntimeError(
            f"scraper_messages_not_found: {scraped_path} "
            "(run the LinkedIn scraper in your data linkedin/ folder first)"
        )

    merge_stats = _merge_scraped_into_export(scraped_path, conversation_keys=set(keys))
    repair_stats = repair_linkedin_export_timestamps()
    repaired_db = _repair_db_owner_senders(db_path) if db_path else 0

    return {
        "ok": True,
        "conversation_keys": keys,
        "selections": selections,
        "selections_path": str(LINKEDIN_SELECTIONS_PATH),
        "scraper_messages_csv": str(scraped_path),
        "scraper_data_dir": str(LINKEDIN_SCRAPER_DATA_DIR),
        "scraper_dir": str(LINKEDIN_SCRAPER_DIR),
        "messages_csv": str(messages_csv_path()),
        "repaired_db_senders": repaired_db,
        "scraped": run_scraper,
        **merge_stats,
        "repaired_timestamps": repair_stats.get("changed", 0),
        "deduped_rows": repair_stats.get("deduped", 0),
    }


def repair_linkedin_export_timestamps() -> Dict[str, int]:
    """Normalize relative DATE values and drop duplicate rows in the export CSV."""
    export_path = messages_csv_path()
    rows = _read_export_rows(export_path)
    if not rows:
        return {"changed": 0, "deduped": 0, "total_rows": 0}

    anchor = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    changed = 0
    for row in rows:
        old = str(row.get("DATE") or "").strip()
        if not old or re.match(r"^\d{4}-\d{2}-\d{2}", old):
            continue
        new = _normalize_scraper_timestamp(old, scraped_at=anchor)
        if new != old:
            row["DATE"] = new
            changed += 1

    deduped = _dedupe_export_rows(rows)
    removed = len(rows) - len(deduped)
    if changed or removed:
        with export_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=_EXPORT_HEADERS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(deduped)
        clear_csv_cache()

    return {"changed": changed, "deduped": removed, "total_rows": len(deduped)}
