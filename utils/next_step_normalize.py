"""Normalize LLM ``next_steps`` into owner-owned, human-readable actions."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

from utils.owner_config import other_party_owes_pattern

_SNAKE_CASE_RE = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)+$")

_PASSIVE_SNAKE_ACTIONS = frozenset(
    {
        "await_call",
        "await_response",
        "await_reply",
        "await_meeting",
        "wait_for_call",
        "wait_for_response",
        "wait_for_reply",
    }
)

_PASSIVE_WAIT_START_RE = re.compile(
    r"^(?:wait(?:ing)?\s+(?:for|on)\b|ball\s+is\s+with\b|pending\s+from\b|await(?:ing)?\b|await_|wait_for_)",
    re.IGNORECASE,
)


def humanize_action(action: str) -> str:
    """Turn snake_case identifiers into short natural-language phrases."""
    text = str(action or "").strip()
    if not text:
        return text
    if _SNAKE_CASE_RE.match(text):
        return text.replace("_", " ").capitalize()
    return text


def is_passive_wait_action(action: str) -> bool:
    """True when the step is waiting on someone else, not an owner deliverable."""
    text = str(action or "").strip()
    if not text:
        return True
    lowered = text.lower().replace("-", "_")
    if lowered in _PASSIVE_SNAKE_ACTIONS:
        return True
    if _PASSIVE_WAIT_START_RE.search(text):
        return True
    if other_party_owes_pattern().search(text):
        return True
    return False


def passive_wait_update_line(action: str, by_when: str) -> str:
    """One-line latest_updates entry for a filtered passive-wait next_step."""
    when = str(by_when or "").strip()
    if when:
        return f"Pending from others: {when}"
    return f"Pending from others: {humanize_action(action)}"


def normalize_next_steps(
    steps: Any,
) -> Tuple[List[Dict[str, str]], List[str]]:
    """
    Return (owner_owned_steps, passive_wait_lines_for_latest_updates).

    Each kept step has humanized ``action``, normalized ``type``, and ``by_when``.
    """
    if not isinstance(steps, list):
        return [], []

    kept: List[Dict[str, str]] = []
    passive_updates: List[str] = []
    seen_passive: set[str] = set()

    for item in steps:
        if not isinstance(item, dict):
            action = humanize_action(str(item or "").strip())
            if not action:
                continue
            if is_passive_wait_action(action):
                line = passive_wait_update_line(action, "")
                if line not in seen_passive:
                    seen_passive.add(line)
                    passive_updates.append(line)
                continue
            kept.append({"type": "response required", "action": action, "by_when": ""})
            continue

        action = humanize_action(str(item.get("action") or item.get("description") or "").strip())
        if not action:
            continue
        by_when = str(item.get("by_when") or item.get("due_date") or "").strip()
        step_type = str(item.get("type") or "response required").strip() or "response required"

        if is_passive_wait_action(action):
            line = passive_wait_update_line(action, by_when)
            if line not in seen_passive:
                seen_passive.add(line)
                passive_updates.append(line)
            continue

        kept.append({"type": step_type, "action": action, "by_when": by_when})

    return kept, passive_updates
