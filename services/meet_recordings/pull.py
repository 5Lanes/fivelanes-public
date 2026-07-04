"""List Meet recording Google Docs (names + dates only) into a local catalog."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from services.gmail_client import _get_credentials, list_connected_accounts
from services.meet_recordings.config import INDEX_FILENAME, MEET_RECORDINGS_DIR

log = logging.getLogger(__name__)

DOC_MIME = "application/vnd.google-apps.document"

_TITLE_SUFFIX_RE = re.compile(
    r"\s*-\s*(?:Notes by Gemini|Transcript)\s*$",
    re.IGNORECASE,
)
_DATE_SUFFIX_RE = re.compile(
    r"\s*-\s*\d{4}/\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2}(?:\s*[A-Za-z0-9_+\-:/]+)?\s*$"
)


def meeting_title_from_doc_name(name: str) -> str:
    """Strip Gemini/Meet suffixes from a Drive file name."""
    title = (name or "").strip()
    if not title:
        return ""
    title = _TITLE_SUFFIX_RE.sub("", title).strip()
    title = _DATE_SUFFIX_RE.sub("", title).strip()
    return title or (name or "").strip()


def get_drive_services(
    account_id: Optional[str] = None,
) -> List[Tuple[str, Any]]:
    """Return ``(account_id, drive_service)`` for connected accounts."""
    account_ids = (
        [account_id.strip()]
        if account_id and account_id.strip()
        else list_connected_accounts()
    )
    out: List[Tuple[str, Any]] = []
    for aid in account_ids:
        creds = _get_credentials(aid)
        if not creds:
            continue
        try:
            drive = build("drive", "v3", credentials=creds, cache_discovery=False)
        except Exception as exc:
            log.warning("Drive service for %s: %s", aid, exc)
            continue
        out.append((aid, drive))
    return out


def get_docs_service(account_id: str) -> Optional[Any]:
    creds = _get_credentials(account_id)
    if not creds:
        return None
    try:
        return build("docs", "v1", credentials=creds, cache_discovery=False)
    except Exception as exc:
        log.warning("Docs service for %s: %s", account_id, exc)
        return None


def _lookback_bound_iso(lookback_days: int) -> str:
    days = max(0, int(lookback_days))
    bound = datetime.now(timezone.utc) - timedelta(days=days)
    return bound.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _drive_query(lookback_days: int) -> str:
    modified = _lookback_bound_iso(lookback_days)
    return (
        f"mimeType = '{DOC_MIME}' and trashed = false "
        f"and modifiedTime >= '{modified}' "
        "and (name contains 'Notes by Gemini' or name contains 'Transcript')"
    )


def _drive_api_error_message(exc: HttpError) -> str:
    """Human-readable message for Drive API failures (esp. API-not-enabled)."""
    status = int(getattr(exc, "status_code", None) or getattr(exc.resp, "status", 0) or 0)
    text = str(exc)
    if status == 403 and (
        "accessNotConfigured" in text
        or "has not been used" in text
        or "is disabled" in text
    ):
        return (
            "Google Drive API is not enabled for this OAuth project. "
            "Enable it at https://console.cloud.google.com/apis/library/drive.googleapis.com "
            "and Google Docs API at "
            "https://console.cloud.google.com/apis/library/docs.googleapis.com "
            "(same project as credentials.json), wait a minute, then retry."
        )
    if status == 403:
        return f"Drive API permission denied: {exc}"
    return f"Drive API error: {exc}"


def list_meet_recording_docs(
    drive_service: Any,
    *,
    lookback_days: int,
    max_results: int = 200,
    account_id: str = "",
) -> List[Dict[str, Any]]:
    """List candidate Meet recording Docs (metadata only — no body)."""
    query = _drive_query(lookback_days)
    files: List[Dict[str, Any]] = []
    page_token: Optional[str] = None
    while True:
        try:
            resp = (
                drive_service.files()
                .list(
                    q=query,
                    spaces="drive",
                    fields=(
                        "nextPageToken, files(id, name, createdTime, modifiedTime, "
                        "owners(emailAddress), webViewLink)"
                    ),
                    pageSize=min(100, max(1, max_results - len(files))),
                    pageToken=page_token,
                    orderBy="modifiedTime desc",
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
        except HttpError as exc:
            log.warning("Drive files.list failed for %s: %s", account_id or "?", exc)
            raise RuntimeError(_drive_api_error_message(exc)) from exc
        for item in resp.get("files") or []:
            if not isinstance(item, dict):
                continue
            file_id = str(item.get("id") or "").strip()
            if not file_id:
                continue
            owners = item.get("owners") or []
            owner_email = ""
            if owners and isinstance(owners[0], dict):
                owner_email = str(owners[0].get("emailAddress") or "").strip()
            name = str(item.get("name") or "").strip()
            created = str(item.get("createdTime") or "").strip()
            modified = str(item.get("modifiedTime") or "").strip()
            files.append(
                {
                    "id": file_id,
                    "name": name,
                    "label": meeting_title_from_doc_name(name) or name,
                    "created_time": created,
                    "modified_time": modified,
                    "doc_date": created or modified,
                    "owner_email": owner_email,
                    "web_view_link": str(item.get("webViewLink") or "").strip(),
                    "account_id": account_id,
                }
            )
            if len(files) >= max_results:
                return files
        page_token = resp.get("nextPageToken") or None
        if not page_token:
            break
    return files


def pull_meet_recording_catalog(
    *,
    lookback_days: int = 90,
    account_id: Optional[str] = None,
    max_results_per_account: int = 200,
    out_dir: Path | None = None,
) -> Dict[str, Any]:
    """
    Pull Meet recording Doc names and dates into ``meet-recordings/index.json``.

    Does not download summary or transcript content — selection happens later.
    """
    root = out_dir or MEET_RECORDINGS_DIR
    root.mkdir(parents=True, exist_ok=True)

    services = get_drive_services(account_id)
    if not services:
        raise RuntimeError(
            "No connected Google accounts with valid OAuth tokens. "
            "Run: python utils/add_account.py <account> --serve --open"
        )

    by_id: Dict[str, Dict[str, Any]] = {}
    errors: List[str] = []
    for aid, drive in services:
        try:
            files = list_meet_recording_docs(
                drive,
                lookback_days=lookback_days,
                max_results=max_results_per_account,
                account_id=aid,
            )
        except RuntimeError as exc:
            errors.append(f"{aid}: {exc}")
            log.warning("Meet recording catalog failed for %s: %s", aid, exc)
            continue
        log.info(
            "Meet recording catalog: account=%s candidates=%d",
            aid,
            len(files),
        )
        for meta in files:
            file_id = meta["id"]
            prev = by_id.get(file_id)
            if prev is None or (meta.get("modified_time") or "") > (
                prev.get("modified_time") or ""
            ):
                by_id[file_id] = meta

    if not by_id and errors:
        # Prefer a single clear message when every account failed the same way.
        messages = []
        for err in errors:
            # "Account: message" → message
            msg = err.split(": ", 1)[-1] if ": " in err else err
            if msg not in messages:
                messages.append(msg)
        raise RuntimeError(messages[0] if len(messages) == 1 else "; ".join(errors))

    docs = sorted(
        by_id.values(),
        key=lambda row: (row.get("doc_date") or "", row.get("name") or ""),
        reverse=True,
    )
    payload = {
        "pulled_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "lookback_days": lookback_days,
        "documents": docs,
    }
    index_path = root / INDEX_FILENAME
    index_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    result: Dict[str, Any] = {
        "ok": True,
        "doc_count": len(docs),
        "meet_recordings_dir": str(root),
        "pulled_at": payload["pulled_at"],
    }
    if errors:
        result["warnings"] = errors
    return result
