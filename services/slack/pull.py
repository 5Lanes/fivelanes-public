"""Pull Slack 1:1 DMs via user OAuth token and write JSON exports."""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from services.slack.config import INDEX_FILENAME, SLACK_DMS_DIR

SLACK_API = "https://slack.com/api"
DEFAULT_SLEEP_SEC = 1.2

log = logging.getLogger(__name__)


def _ts_to_iso(ts: str) -> str:
    try:
        seconds = float(ts)
    except (TypeError, ValueError):
        return ""
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()


class SlackClient:
    def __init__(self, token: str, *, sleep_sec: float = DEFAULT_SLEEP_SEC) -> None:
        self._token = token
        self._sleep_sec = sleep_sec
        self._session = requests.Session()
        self._session.headers["Authorization"] = f"Bearer {token}"

    def _get(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{SLACK_API}/{method}"
        while True:
            resp = self._session.get(url, params=params or {}, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            if data.get("ok"):
                time.sleep(self._sleep_sec)
                return data
            err = str(data.get("error") or "unknown_error")
            if err == "ratelimited":
                retry_after = int(resp.headers.get("Retry-After", "5"))
                time.sleep(retry_after)
                continue
            raise RuntimeError(f"{method} failed: {err}")

    def auth_test(self) -> Dict[str, Any]:
        return self._get("auth.test")

    def list_im_channels(self) -> List[Dict[str, Any]]:
        channels: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        while True:
            params: Dict[str, Any] = {"types": "im", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            data = self._get("conversations.list", params)
            channels.extend(data.get("channels") or [])
            cursor = (data.get("response_metadata") or {}).get("next_cursor") or ""
            if not cursor:
                break
        return channels

    def user_info(self, user_id: str) -> Dict[str, Any]:
        return self._get("users.info", {"user": user_id})

    def channel_history(
        self, channel_id: str, *, limit: int = 200
    ) -> List[Dict[str, Any]]:
        messages: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        while True:
            params: Dict[str, Any] = {"channel": channel_id, "limit": limit}
            if cursor:
                params["cursor"] = cursor
            data = self._get("conversations.history", params)
            messages.extend(data.get("messages") or [])
            cursor = (data.get("response_metadata") or {}).get("next_cursor") or ""
            if not cursor:
                break
        return messages


def _user_label(profile: Dict[str, Any]) -> str:
    return (
        str(profile.get("display_name") or "").strip()
        or str(profile.get("real_name") or "").strip()
        or str(profile.get("name") or "").strip()
        or str(profile.get("id") or "").strip()
    )


def _safe_filename(label: str, user_id: str) -> str:
    stem = "".join(c if c.isalnum() or c in "-_" else "_" for c in label.strip())
    stem = stem.strip("_") or user_id
    return f"{stem}_{user_id}.json"


def _normalize_message(msg: Dict[str, Any], *, my_user_id: str) -> Optional[Dict[str, Any]]:
    if msg.get("type") != "message":
        return None
    if msg.get("subtype") in ("channel_join", "channel_leave"):
        return None
    user = str(msg.get("user") or "").strip()
    ts = str(msg.get("ts") or "").strip()
    if not ts:
        return None
    text = str(msg.get("text") or "").strip()
    if not text and not msg.get("files"):
        return None
    return {
        "ts": ts,
        "datetime": _ts_to_iso(ts),
        "user": user,
        "is_from_me": bool(user and user == my_user_id),
        "text": text,
        "has_files": bool(msg.get("files")),
    }


def pull_slack_dms(
    *,
    token: str | None = None,
    out_dir: Path | None = None,
    limit_per_channel: int = 200,
    sleep_sec: float = DEFAULT_SLEEP_SEC,
) -> Dict[str, Any]:
    """Fetch all 1:1 DMs and write JSON exports under ``out_dir``."""
    tok = (token or os.getenv("SLACK_USER_TOKEN") or "").strip()
    if not tok:
        raise RuntimeError("missing_slack_user_token")

    root = out_dir or SLACK_DMS_DIR
    root.mkdir(parents=True, exist_ok=True)

    client = SlackClient(tok, sleep_sec=sleep_sec)
    auth = client.auth_test()
    my_user_id = str(auth.get("user_id") or "").strip()
    if not my_user_id:
        raise RuntimeError("auth.test did not return user_id")

    channels = client.list_im_channels()
    user_cache: Dict[str, Dict[str, Any]] = {}
    index: List[Dict[str, Any]] = []
    total_messages = 0

    for ch in channels:
        channel_id = str(ch.get("id") or "").strip()
        other_user_id = str(ch.get("user") or "").strip()
        if not channel_id or not other_user_id:
            continue

        try:
            if other_user_id not in user_cache:
                info = client.user_info(other_user_id)
                user_cache[other_user_id] = info.get("user") or {}

            profile = user_cache[other_user_id]
            label = _user_label(profile)
            raw_messages = client.channel_history(
                channel_id, limit=min(limit_per_channel, 200)
            )
        except Exception:
            log.exception(
                "Slack pull: skipping channel_id=%s (user_id=%s) after fetch failure",
                channel_id,
                other_user_id,
            )
            continue
        normalized = [
            row
            for row in (_normalize_message(m, my_user_id=my_user_id) for m in raw_messages)
            if row
        ]
        normalized.sort(key=lambda m: m["ts"])
        total_messages += len(normalized)

        payload = {
            "channel_id": channel_id,
            "user_id": other_user_id,
            "user_name": label,
            "exported_at": datetime.now(tz=timezone.utc).isoformat(),
            "message_count": len(normalized),
            "messages": normalized,
        }

        out_path = root / _safe_filename(label, other_user_id)
        out_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        index.append(
            {
                "channel_id": channel_id,
                "user_id": other_user_id,
                "user_name": label,
                "message_count": len(normalized),
                "file": out_path.name,
            }
        )

    index_path = root / INDEX_FILENAME
    index_path.write_text(
        json.dumps(
            {
                "exported_at": datetime.now(tz=timezone.utc).isoformat(),
                "my_user_id": my_user_id,
                "dm_count": len(index),
                "conversations": index,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    return {
        "ok": True,
        "slack_user": str(auth.get("user") or ""),
        "my_user_id": my_user_id,
        "dm_count": len(index),
        "message_count": total_messages,
        "out_dir": str(root),
    }
