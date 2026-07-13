"""
Persists one generated briefing per calendar day, plus per-item dismissed state:
- The build in ``services/digest/build.py`` only actually runs (LLM call + deterministic
  assembly) once per day — every request within the same day is served the same stored batch,
  so Alfred doesn't re-narrate your day differently on every poll.
- "Clear" and "Add to plans" are permanent for that item: dismissing one removes it from every
  future response for the rest of the day, not just the current tab/session.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from utils.runtime_paths import data_path

_DAILY_DIGEST_FILENAME = "digest_daily.json"


def _daily_digest_path() -> Path:
    return data_path(_DAILY_DIGEST_FILENAME)


def _today_key(*, as_of: Optional[date] = None) -> str:
    return (as_of or datetime.now(timezone.utc).date()).isoformat()


def item_id(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def load_daily_digest(*, as_of: Optional[date] = None) -> Optional[Dict[str, Any]]:
    """Returns the stored digest only when it was generated today (UTC); ``None`` otherwise —
    the caller then builds a fresh one and saves it via ``save_daily_digest``."""
    path = _daily_digest_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict) or data.get("date") != _today_key(as_of=as_of):
        return None
    return data


def save_daily_digest(payload: Dict[str, Any]) -> Dict[str, Any]:
    stored = dict(payload)
    stored["date"] = _today_key()
    path = _daily_digest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stored, indent=2, ensure_ascii=False), encoding="utf-8")
    return stored


def dismiss_item(item_id_value: str) -> bool:
    """Marks one item permanently dismissed in today's stored digest. Returns ``False`` when
    there's no digest stored for today, or the id isn't in it — nothing to do either way."""
    stored = load_daily_digest()
    if not stored:
        return False
    items = stored.get("items")
    if not isinstance(items, list):
        return False
    found = False
    for item in items:
        if isinstance(item, dict) and item.get("id") == item_id_value:
            item["dismissed"] = True
            found = True
    if found:
        save_daily_digest(stored)
    return found
