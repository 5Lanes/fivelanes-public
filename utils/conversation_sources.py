"""
Single source of truth for the conversation-source prefix vocabulary.

Every integration (email, LinkedIn, Slack, Meet recordings, texts, and the synthetic
``todo:`` bucket) identifies its ``inbox_thread_id``/``thread_id`` rows by gluing a
source prefix onto a raw key. Previously each ``services/<source>/tracking.py`` module
duplicated its own prefix constant plus ``make``/``parse``/``is_this_source`` helpers,
and ``utils/database.py`` re-parsed the same prefixes inline in several places. This
module centralizes that so there is exactly one prefix table and one classifier.
"""

from __future__ import annotations

from typing import Optional

# Order matters: classify_source checks these in order, so a prefix that is a substring
# of another (none currently are) would need the longer one listed first.
SOURCE_PREFIXES: dict[str, str] = {
    "linkedin": "linkedin:",
    "slack": "slack:",
    "meet": "meet:",
    "text": "text:",
    "todo": "todo:",
    "email": "rfc:",
}

# Every non-email source has a real prefix; email is also the fallback for legacy
# no-prefix ids, so it isn't included in this set.
_NON_EMAIL_SOURCES = tuple(s for s in SOURCE_PREFIXES if s != "email")


def classify_source(conversation_id: str) -> str:
    """Return the source type ('email', 'linkedin', 'slack', 'meet', 'text', 'todo')."""
    key = (conversation_id or "").strip()
    for source in _NON_EMAIL_SOURCES:
        if key.startswith(SOURCE_PREFIXES[source]):
            return source
    return "email"


def make_source_key(source: str, external_key: str) -> str:
    """Build a prefixed conversation id for ``source`` from a raw external key."""
    key = (external_key or "").strip()
    if not key:
        return ""
    prefix = SOURCE_PREFIXES.get(source, "")
    if not prefix or key.startswith(prefix):
        return key
    return f"{prefix}{key}"


def parse_source_key(source: str, conversation_id: str) -> Optional[str]:
    """Strip ``source``'s prefix off ``conversation_id``, or ``None`` if it isn't that source."""
    prefix = SOURCE_PREFIXES.get(source, "")
    tid = (conversation_id or "").strip()
    if not prefix or not tid.startswith(prefix):
        return None
    key = tid[len(prefix):].strip()
    return key or None


def external_key(conversation_id: str) -> str:
    """Return ``conversation_id`` with its source prefix (if any) stripped."""
    source = classify_source(conversation_id)
    prefix = SOURCE_PREFIXES.get(source, "")
    tid = (conversation_id or "").strip()
    if prefix and tid.startswith(prefix):
        return tid[len(prefix):].strip()
    return tid
