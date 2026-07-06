"""Register which Meet recording Docs appear in Threads; import summary tab on track."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

from services.email.config import SOURCE_ACCOUNT, SOURCE_OAUTH_ACCOUNT_ID
from services.meet_recordings.catalog import catalog_entry_by_id
from services.meet_recordings.config import MEET_RECORDINGS_DIR
from services.meet_recordings.docs import fetch_meet_recording_summary
from services.meet_recordings.pull import get_docs_service, meeting_title_from_doc_name
from services.thread_snooze import ACTIVE, is_removed, normalize_state

log = logging.getLogger(__name__)

MEET_THREAD_PREFIX = "meet:"
MEET_KIND = "meet_recording"
MEET_PAUSED_KIND = "meet_recording_paused"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def meet_inbox_thread_id(document_key: str) -> str:
    key = (document_key or "").strip()
    if not key:
        return ""
    if key.startswith(MEET_THREAD_PREFIX):
        return key
    if key.startswith("docs:"):
        key = key[5:]
    return f"{MEET_THREAD_PREFIX}{key}"


def parse_meet_inbox_thread_id(inbox_thread_id: str) -> Optional[str]:
    tid = (inbox_thread_id or "").strip()
    if not tid.startswith(MEET_THREAD_PREFIX):
        return None
    key = tid[len(MEET_THREAD_PREFIX) :].strip()
    return key or None


def imported_note_path(document_key: str, *, root: Path | None = None) -> Path:
    key = (document_key or "").strip()
    return (root or MEET_RECORDINGS_DIR) / f"{key}.json"


def load_imported_note(
    document_key: str, *, root: Path | None = None
) -> Optional[Dict[str, Any]]:
    path = imported_note_path(document_key, root=root)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def save_imported_note(note: Dict[str, Any], *, root: Path | None = None) -> Path:
    key = str(note.get("id") or "").strip()
    base = root or MEET_RECORDINGS_DIR
    base.mkdir(parents=True, exist_ok=True)
    path = imported_note_path(key, root=base)
    path.write_text(
        json.dumps(note, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def _meet_delivery_kind(row: Dict[str, Any]) -> str:
    return str(row.get("inbox_delivery_kind") or "").strip()


def _is_meet_tracking_row(row: Dict[str, Any]) -> bool:
    tid = str(row.get("inbox_thread_id") or "").strip()
    kind = _meet_delivery_kind(row)
    return tid.startswith(MEET_THREAD_PREFIX) or kind in (MEET_KIND, MEET_PAUSED_KIND)


def _is_sync_meet_row(row: Dict[str, Any]) -> bool:
    if is_removed(row.get("snoozed")):
        return False
    if not _is_meet_tracking_row(row):
        return False
    kind = _meet_delivery_kind(row)
    return kind in ("", MEET_KIND)


def fetch_visible_document_keys(db_path: str) -> List[str]:
    """All Meet recordings still shown on the dashboard (syncing or paused)."""
    from utils.database import fetch_thread_tracking_rows, load_lane_thread_memberships

    out: Set[str] = set()
    for row in fetch_thread_tracking_rows(db_path):
        if is_removed(row.get("snoozed")):
            continue
        if not _is_meet_tracking_row(row):
            continue
        key = parse_meet_inbox_thread_id(str(row.get("inbox_thread_id") or ""))
        if key:
            out.add(key)
    for thread_ids in load_lane_thread_memberships(db_path).values():
        for tid in thread_ids:
            key = parse_meet_inbox_thread_id(tid)
            if key:
                out.add(key)
    return sorted(out)


def fetch_tracked_document_keys(db_path: str) -> List[str]:
    """Meet recordings selected for import, summarize, and sync updates."""
    from utils.database import fetch_thread_tracking_rows

    out: List[str] = []
    for row in fetch_thread_tracking_rows(db_path):
        if not _is_sync_meet_row(row):
            continue
        key = parse_meet_inbox_thread_id(str(row.get("inbox_thread_id") or ""))
        if key:
            out.append(key)
    return sorted(set(out))


def _existing_meet_tracking_rows(db_path: str) -> Dict[str, Dict[str, Any]]:
    from utils.database import fetch_thread_tracking_rows

    by_key: Dict[str, Dict[str, Any]] = {}
    for row in fetch_thread_tracking_rows(db_path):
        key = parse_meet_inbox_thread_id(str(row.get("inbox_thread_id") or ""))
        if key:
            by_key[key] = row
    return by_key


def import_document_summary(document_key: str) -> Dict[str, Any]:
    """
    Fetch the conversation-summary tab for one catalog Doc and persist locally.

    Never imports the full transcript tab.
    """
    key = (document_key or "").strip()
    if not key:
        return {"ok": False, "error": "missing_document_key"}

    meta = catalog_entry_by_id(key) or {}
    account_id = str(meta.get("account_id") or SOURCE_OAUTH_ACCOUNT_ID or "").strip()
    if not account_id:
        return {"ok": False, "error": "missing_account_id", "document_key": key}

    docs = get_docs_service(account_id)
    if docs is None:
        return {"ok": False, "error": "docs_service_unavailable", "document_key": key}

    summary_payload = fetch_meet_recording_summary(docs, key)
    if not summary_payload:
        return {
            "ok": False,
            "error": "no_summary_tab",
            "document_key": key,
        }

    name = str(meta.get("name") or "").strip() or key
    meeting_title = (
        str(meta.get("label") or "").strip() or meeting_title_from_doc_name(name)
    )
    note = {
        "id": key,
        "name": name,
        "label": meeting_title,
        "body": summary_payload["body"],
        "tab_title": summary_payload.get("tab_title") or "",
        "datetime": str(
            meta.get("doc_date")
            or meta.get("created_time")
            or meta.get("modified_time")
            or ""
        ),
        "account_id": account_id,
        "owner_email": str(meta.get("owner_email") or ""),
        "web_view_link": str(meta.get("web_view_link") or ""),
        "imported_at": _utc_now_iso(),
    }
    save_imported_note(note)
    return {"ok": True, "document_key": key, "note": note}


def _timeline_row_from_note(note: Dict[str, Any], thread_id: str) -> Dict[str, Any]:
    body = str(note.get("body") or "").strip()
    title = str(note.get("label") or note.get("name") or "").strip()
    if title and not body.lower().startswith("meeting:"):
        body = f"Meeting: {title}\n\n{body}".strip()
    return {
        "source_id": f"docs:{note.get('id') or ''}",
        "type": "meeting",
        "datetime": note.get("datetime") or "",
        "sender": note.get("owner_email") or "",
        "recipients": "",
        "participants": str(note.get("owner_email") or "").strip(),
        "summary": note.get("name") or title or "(Meet recording)",
        "body": body,
        "thread_id": thread_id,
        "fetch_oauth_account_id": note.get("account_id") or "",
        "body_has_image": 0,
    }


def _tracking_row_for_note(
    note: Dict[str, Any],
    *,
    existing: Optional[Dict[str, Any]],
    now_iso: str,
    delivery_kind: str = MEET_KIND,
) -> Dict[str, Any]:
    key = str(note.get("id") or "").strip()
    source_email = (
        str(note.get("owner_email") or "").strip().lower()
        or (SOURCE_ACCOUNT or "").strip().lower()
    )
    return {
        "inbox_thread_id": meet_inbox_thread_id(key),
        "gmail_inbox_thread_id": "",
        "source_email": source_email,
        "snoozed": ACTIVE,
        "inner_rfc_message_id": "",
        "resolved_oauth_account_id": note.get("account_id") or SOURCE_OAUTH_ACCOUNT_ID or "",
        "resolution_error": "",
        "inbox_delivery_kind": delivery_kind,
        "created_at": str((existing or {}).get("created_at") or now_iso),
        "updated_at": now_iso,
    }


def _paused_tracking_row(row: Dict[str, Any], *, now_iso: str) -> Dict[str, Any]:
    return {
        "inbox_thread_id": str(row.get("inbox_thread_id") or "").strip(),
        "gmail_inbox_thread_id": str(row.get("gmail_inbox_thread_id") or ""),
        "source_email": str(row.get("source_email") or "").strip(),
        "snoozed": normalize_state(row.get("snoozed")),
        "inner_rfc_message_id": str(row.get("inner_rfc_message_id") or ""),
        "resolved_oauth_account_id": str(row.get("resolved_oauth_account_id") or ""),
        "resolution_error": str(row.get("resolution_error") or ""),
        "inbox_delivery_kind": MEET_PAUSED_KIND,
        "created_at": str(row.get("created_at") or now_iso),
        "updated_at": now_iso,
    }


def set_tracked_document_keys(
    db_path: str, document_keys: Iterable[str]
) -> Dict[str, Any]:
    """
    Enable sync for selected Meet recording Docs: import summary tab when needed.

    Other known Meet rows are paused (still visible on the dashboard, but not
    re-imported or re-summarized until checked again).
    """
    from utils.database import upsert_thread_tracking, upsert_timeline_entries

    desired: Set[str] = {str(k).strip() for k in document_keys if str(k).strip()}
    now = _utc_now_iso()
    existing = _existing_meet_tracking_rows(db_path)

    timeline_rows: List[Dict[str, Any]] = []
    tracking_rows: List[Dict[str, Any]] = []
    imported = 0
    import_errors: List[Dict[str, Any]] = []

    for key in sorted(desired):
        note = load_imported_note(key)
        if note is None or not str(note.get("body") or "").strip():
            result = import_document_summary(key)
            if not result.get("ok"):
                import_errors.append(
                    {
                        "document_key": key,
                        "error": result.get("error") or "import_failed",
                    }
                )
                continue
            note = result.get("note") or load_imported_note(key)
            imported += 1
        if not note:
            import_errors.append({"document_key": key, "error": "import_failed"})
            continue
        thread_id = meet_inbox_thread_id(key)
        timeline_rows.append(_timeline_row_from_note(note, thread_id))
        tracking_rows.append(
            _tracking_row_for_note(note, existing=existing.get(key), now_iso=now)
        )

    paused = 0
    for key, row in existing.items():
        if key in desired:
            continue
        if is_removed(row.get("snoozed")):
            continue
        if _meet_delivery_kind(row) == MEET_PAUSED_KIND:
            continue
        tracking_rows.append(_paused_tracking_row(row, now_iso=now))
        paused += 1

    applied = upsert_thread_tracking(db_path, tracking_rows, apply_snooze=True) if tracking_rows else 0
    n_time = upsert_timeline_entries(db_path, timeline_rows) if timeline_rows else 0

    tracked = sorted(desired - {e["document_key"] for e in import_errors})
    return {
        "ok": True,
        "tracked": tracked,
        "tracked_count": len(tracked),
        "upserted": applied,
        "timeline_rows": n_time,
        "imported": imported,
        "paused": paused,
        "untracked": 0,
        "errors": import_errors,
    }
