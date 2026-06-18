"""Subject-line helpers."""

import re

_SUBJECT_REPLY_PREFIX_RE = re.compile(
    r"^\s*(?:re|aw|sv|vs|antw|回复|转发)\s*:\s*",
    re.IGNORECASE,
)
_SUBJECT_FORWARD_PREFIX_RE = re.compile(
    r"^\s*(?:fw|fwd|forward|wg|i\.l|ilt|转发)\s*:\s*",
    re.IGNORECASE,
)
_TODO_SUBJECT_HEAD_RE = re.compile(r"^todo\b\s*[:.\-–—]?\s*", re.IGNORECASE)
_SINGLE_HTTP_URL_RE = re.compile(
    r"^https?://[^\s<>\[\]()]+(?:\([^\s<>\[\]()]*\)[^\s<>\[\]()]*)*$",
    re.IGNORECASE,
)
def strip_subject_prefix_chain(subject: str) -> str:
    """Strip nested ``Re:`` / ``Fwd:`` (etc.) prefixes from the subject line."""
    s = (subject or "").strip()
    while True:
        m = _SUBJECT_REPLY_PREFIX_RE.match(s)
        if m:
            s = s[m.end() :].lstrip()
            continue
        m = _SUBJECT_FORWARD_PREFIX_RE.match(s)
        if m:
            s = s[m.end() :].lstrip()
            continue
        break
    return s
def subject_core_indicates_todo(subject: str) -> bool:
    """True when the subject (after Re/Fwd strip) begins with ``todo``."""
    core = strip_subject_prefix_chain(subject)
    return bool(_TODO_SUBJECT_HEAD_RE.match(core))


def extract_todo_plan_action(subject: str) -> str:
    """Remainder of subject after the ``todo`` prefix, for plan action text."""
    core = strip_subject_prefix_chain(subject)
    m = _TODO_SUBJECT_HEAD_RE.match(core)
    if not m:
        return ""
    return core[m.end() :].strip()
def is_inbox_forward_subject(subject: str) -> bool:
    """
    True when the subject indicates a forward (Fw:/Fwd:/…), after stripping
    common reply prefixes (Re:, Aw:, …).
    """
    s = (subject or "").strip()
    if not s:
        return False
    while True:
        m = _SUBJECT_REPLY_PREFIX_RE.match(s)
        if not m:
            break
        s = s[m.end() :].lstrip()
    return bool(_SUBJECT_FORWARD_PREFIX_RE.match(s))
def _normalize_single_line_url(body: str) -> str:
    t = (body or "").strip()
    if t.startswith("<") and t.endswith(">"):
        t = t[1:-1].strip()
    return t
def _body_is_single_http_url(body: str) -> bool:
    t = _normalize_single_line_url(body)
    if not t or any(ch in t for ch in "\n\r"):
        return False
    return bool(_SINGLE_HTTP_URL_RE.match(t))
