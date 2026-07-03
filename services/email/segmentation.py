"""segmentation module."""
import re
from typing import Any, Dict, List, Optional


_OUTLOOK_QUOTE_BLOCK = re.compile(
    r"(?m)^From:\s*.+\r?\nSent:\s",
    re.IGNORECASE,
)
_GMAIL_ON_WROTE = re.compile(r"(?m)^On .+wrote:\s*$", re.IGNORECASE)
# Gmail often splits the attribution across two lines: "On …" then "wrote:".
_GMAIL_ON_WROTE_SPLIT = re.compile(
    r"(?m)^On .+\n\s*wrote:\s*$",
    re.IGNORECASE,
)
_ORIGINAL_MESSAGE = re.compile(
    r"(?m)^-{3,}\s*Original Message\s*-{3,}\s*$",
    re.IGNORECASE,
)


def quoted_thread_start_index(body: str) -> Optional[int]:
    """
    Character index where a quoted prior-thread block likely begins, or ``None``.

    Used before LLM segmentation so reply tails (especially Outlook ``From:/Sent:`` blocks)
    do not dominate extraction.
    """
    text = (body or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return None
    candidates: List[int] = []
    for pat in (_OUTLOOK_QUOTE_BLOCK, _GMAIL_ON_WROTE, _GMAIL_ON_WROTE_SPLIT, _ORIGINAL_MESSAGE):
        m = pat.search(text)
        if m:
            candidates.append(m.start())
    if not candidates:
        return None
    idx = min(candidates)
    # Require some sender-written lines above the quote header (not a bare forward header).
    if idx <= 0:
        return None
    return idx
def strip_quoted_thread_tail(body: str) -> str:
    """Drop quoted prior-thread text from the bottom of a reply body."""
    text = body or ""
    idx = quoted_thread_start_index(text)
    if idx is None:
        return text.strip()
    return text[:idx].rstrip()
def _normalize_for_content_compare(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).lower()
def segmentation_content_from_quoted_tail_only(
    full_body: str, cleaned_content: str
) -> bool:
    """
    True when ``cleaned_content`` matches text that lives only in a quoted prior-thread tail.

    Used to re-run segmentation for rows that were saved before ``guard_segmentation_content``
    accepted a bad retry.
    """
    content = str(cleaned_content or "").strip()
    if not content or not (full_body or "").strip():
        return False
    idx = quoted_thread_start_index(full_body)
    if idx is None or idx <= 0:
        return False
    head = full_body[:idx].strip()
    tail = full_body[idx:].strip()
    nc = _normalize_for_content_compare(content)
    if len(nc) < 24:
        return False
    nh = _normalize_for_content_compare(head)
    nt = _normalize_for_content_compare(tail)
    return nc in nt and nc not in nh


def segmentation_content_not_from_reply_head(
    full_body: str, cleaned_content: str
) -> bool:
    """True when stored segmentation clearly did not come from the reply head."""
    content = str(cleaned_content or "").strip()
    if not content or not (full_body or "").strip():
        return False
    idx = quoted_thread_start_index(full_body)
    if idx is None or idx <= 0:
        return False
    head = full_body[:idx].strip()
    nc = _normalize_for_content_compare(content)
    nh = _normalize_for_content_compare(head)
    if len(nc) < 24:
        return False
    return nc not in nh


def guard_segmentation_content(
    full_body: str,
    seg: Dict[str, Any],
    *,
    resubmit_segmentation,
) -> Dict[str, Any]:
    """
    When the model returns text that appears only in the quoted tail, re-segment the head
    or fall back to the head slice.
    """
    if not isinstance(seg, dict):
        return seg
    content = str(seg.get("content") or "").strip()
    if not content or not (full_body or "").strip():
        return seg
    idx = quoted_thread_start_index(full_body)
    if idx is None or idx <= 0:
        return seg
    head = full_body[:idx].strip()
    tail = full_body[idx:].strip()
    nc = _normalize_for_content_compare(content)
    if len(nc) < 24:
        return seg
    nh = _normalize_for_content_compare(head)
    nt = _normalize_for_content_compare(tail)
    if nc not in nt or nc in nh:
        return seg
    try:
        retry = resubmit_segmentation(head)
        if isinstance(retry, dict):
            retry_content = str(retry.get("content") or "").strip()
            if retry_content:
                rc = _normalize_for_content_compare(retry_content)
                # Only accept a retry that plausibly came from the head slice — not quoted
                # tail text that happens to be absent from ``nt`` because the retry prompt
                # omitted the tail.
                if rc in nh and (len(rc) < 24 or rc not in nt):
                    return retry
    except Exception:
        pass
    out = dict(seg)
    out["content"] = head
    return out
