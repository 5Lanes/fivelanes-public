"""Read Meet recording Google Docs; use only the conversation-summary tab."""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from googleapiclient.errors import HttpError

log = logging.getLogger(__name__)

# Tab titles we treat as the conversation summary (never the full transcript).
_SUMMARY_TITLE_RE = re.compile(
    r"\b(conversation\s+summary|summary|notes)\b",
    re.IGNORECASE,
)
_TRANSCRIPT_TITLE_RE = re.compile(r"\btranscript\b", re.IGNORECASE)


def _text_from_structural_elements(elements: Iterable[Dict[str, Any]]) -> str:
    parts: List[str] = []
    for el in elements:
        if not isinstance(el, dict):
            continue
        para = el.get("paragraph")
        if isinstance(para, dict):
            for pe in para.get("elements") or []:
                if not isinstance(pe, dict):
                    continue
                run = pe.get("textRun")
                if isinstance(run, dict):
                    content = run.get("content")
                    if isinstance(content, str) and content:
                        parts.append(content)
            continue
        table = el.get("table")
        if isinstance(table, dict):
            for row in table.get("tableRows") or []:
                if not isinstance(row, dict):
                    continue
                for cell in row.get("tableCells") or []:
                    if not isinstance(cell, dict):
                        continue
                    parts.append(
                        _text_from_structural_elements(cell.get("content") or [])
                    )
                    parts.append("\n")
            continue
        toc = el.get("tableOfContents")
        if isinstance(toc, dict):
            parts.append(_text_from_structural_elements(toc.get("content") or []))
    return "".join(parts)


def _text_from_document_tab(document_tab: Dict[str, Any]) -> str:
    body = document_tab.get("body") if isinstance(document_tab, dict) else None
    if not isinstance(body, dict):
        return ""
    return _text_from_structural_elements(body.get("content") or []).strip()


def _iter_tabs(tabs: Any) -> Iterable[Dict[str, Any]]:
    if not isinstance(tabs, list):
        return
    for tab in tabs:
        if not isinstance(tab, dict):
            continue
        yield tab
        yield from _iter_tabs(tab.get("childTabs"))


def _tab_title(tab: Dict[str, Any]) -> str:
    props = tab.get("tabProperties")
    if not isinstance(props, dict):
        return ""
    return str(props.get("title") or "").strip()


def _is_transcript_tab(title: str) -> bool:
    return bool(_TRANSCRIPT_TITLE_RE.search(title or ""))


def _is_summary_tab(title: str) -> bool:
    if not title or _is_transcript_tab(title):
        return False
    return bool(_SUMMARY_TITLE_RE.search(title))


def _summary_tab_score(title: str) -> int:
    """Higher is better; prefer explicit conversation-summary titles."""
    t = (title or "").strip().lower()
    if "conversation summary" in t:
        return 3
    if "summary" in t:
        return 2
    if "notes" in t:
        return 1
    return 0


def pick_summary_tab_text(document: Dict[str, Any]) -> Tuple[str, str]:
    """
    Return ``(text, tab_title)`` from the conversation-summary tab only.

    Never returns transcript-tab content. Empty text when no summary tab exists.
    """
    tabs = list(_iter_tabs(document.get("tabs")))
    if not tabs:
        # Legacy single-body docs: do not guess; caller should skip.
        return "", ""

    candidates: List[Tuple[int, str, str]] = []
    for tab in tabs:
        title = _tab_title(tab)
        if not _is_summary_tab(title):
            continue
        doc_tab = tab.get("documentTab")
        if not isinstance(doc_tab, dict):
            continue
        text = _text_from_document_tab(doc_tab)
        if not text:
            continue
        candidates.append((_summary_tab_score(title), title, text))

    if not candidates:
        return "", ""

    candidates.sort(key=lambda item: (-item[0], item[1].lower()))
    _score, title, text = candidates[0]
    return text, title


def fetch_meet_recording_summary(
    docs_service: Any,
    document_id: str,
) -> Optional[Dict[str, str]]:
    """
    Load a Meet recording Doc and return only the conversation-summary tab.

    Returns ``{"body", "tab_title"}`` or ``None`` when the summary tab is missing.
    """
    doc_id = (document_id or "").strip()
    if not doc_id:
        return None
    try:
        document = (
            docs_service.documents()
            .get(documentId=doc_id, includeTabsContent=True)
            .execute()
        )
    except HttpError as exc:
        log.warning("Docs get failed for %s: %s", doc_id, exc)
        return None
    except Exception as exc:
        log.warning("Docs get failed for %s: %s", doc_id, exc)
        return None

    body, tab_title = pick_summary_tab_text(document if isinstance(document, dict) else {})
    if not body:
        log.info(
            "Skip meet recording doc %s: no conversation-summary tab (transcript-only or empty)",
            doc_id,
        )
        return None
    return {"body": body, "tab_title": tab_title}
