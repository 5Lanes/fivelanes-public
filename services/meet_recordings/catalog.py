"""Read the local Meet recording Doc catalog (names + dates)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from services.meet_recordings.config import INDEX_FILENAME, MEET_RECORDINGS_DIR


def _index_path(root: Path | None = None) -> Path:
    return (root or MEET_RECORDINGS_DIR) / INDEX_FILENAME


def load_catalog_index(root: Path | None = None) -> Dict[str, Any]:
    path = _index_path(root)
    if not path.is_file():
        return {"documents": [], "pulled_at": "", "lookback_days": None}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"documents": [], "pulled_at": "", "lookback_days": None}
    if not isinstance(raw, dict):
        return {"documents": [], "pulled_at": "", "lookback_days": None}
    docs = raw.get("documents")
    if not isinstance(docs, list):
        docs = []
    return {
        "documents": [d for d in docs if isinstance(d, dict)],
        "pulled_at": str(raw.get("pulled_at") or ""),
        "lookback_days": raw.get("lookback_days"),
    }


def catalog_entry_by_id(
    doc_id: str, *, root: Path | None = None
) -> Optional[Dict[str, Any]]:
    want = (doc_id or "").strip()
    if not want:
        return None
    for row in load_catalog_index(root).get("documents") or []:
        if str(row.get("id") or "").strip() == want:
            return row
    return None


def _drop_transcript_when_notes_exists(
    rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Hide Transcript-only docs when a Notes-by-Gemini doc exists for the same meeting.

    Each Meet instance produces two Drive files (notes + transcript). Only the notes
    doc has a conversation-summary tab we can import.
    """
    notes_keys: set[tuple[str, str]] = set()
    for row in rows:
        name = str(row.get("name") or "").lower()
        if "notes by gemini" not in name:
            continue
        notes_keys.add(
            (
                str(row.get("label") or "").strip(),
                str(row.get("doc_date") or row.get("created_time") or row.get("modified_time") or "").strip(),
            )
        )
    if not notes_keys:
        return rows

    kept: List[Dict[str, Any]] = []
    for row in rows:
        name = str(row.get("name") or "").lower()
        is_transcript_only = "transcript" in name and "notes by gemini" not in name
        if not is_transcript_only:
            kept.append(row)
            continue
        key = (
            str(row.get("label") or "").strip(),
            str(row.get("doc_date") or row.get("created_time") or row.get("modified_time") or "").strip(),
        )
        if key not in notes_keys:
            kept.append(row)
    return kept


def list_document_catalog(
    meet_recordings_dir: Path | None = None,
) -> List[Dict[str, Any]]:
    """Catalog rows for the setup UI (id, name/label, dates)."""
    index = load_catalog_index(meet_recordings_dir)
    catalog: List[Dict[str, Any]] = []
    for row in index.get("documents") or []:
        file_id = str(row.get("id") or "").strip()
        if not file_id:
            continue
        name = str(row.get("name") or "").strip()
        label = str(row.get("label") or "").strip() or name
        doc_date = str(row.get("doc_date") or row.get("created_time") or row.get("modified_time") or "")
        catalog.append(
            {
                "id": file_id,
                "document_key": file_id,
                "name": name,
                "label": label,
                "doc_date": doc_date,
                "created_time": str(row.get("created_time") or ""),
                "modified_time": str(row.get("modified_time") or ""),
                "account_id": str(row.get("account_id") or ""),
                "owner_email": str(row.get("owner_email") or ""),
                "web_view_link": str(row.get("web_view_link") or ""),
            }
        )
    catalog.sort(
        key=lambda row: (row.get("doc_date") or "", row.get("label") or ""),
        reverse=True,
    )
    return _drop_transcript_when_notes_exists(catalog)
