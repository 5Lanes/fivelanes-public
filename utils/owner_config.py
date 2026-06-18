"""Owner identity from environment (inbox user, routing aliases, display name)."""

from __future__ import annotations

import os
import re
from functools import lru_cache
from typing import List, Pattern


def owner_name() -> str:
    return (os.getenv("OWNER_NAME") or "").strip() or "Owner"


def _parse_csv_env(key: str) -> List[str]:
    raw = (os.getenv(key) or "").strip()
    if not raw:
        return []
    return [part.strip().lower() for part in raw.split(",") if part.strip()]


def summary_routing_aliases() -> List[str]:
    """Lowercase inbox and alias addresses used to mask routing in summaries."""
    aliases: List[str] = []
    seen: set[str] = set()
    source_account = (os.getenv("SOURCE_ACCOUNT") or "").strip().lower()
    if not source_account:
        raise ValueError(
            "SOURCE_ACCOUNT is required. Set it in .env to your Fivelanes inbox address."
        )
    for value in [source_account, *_parse_csv_env("OWNER_EMAIL_ALIASES")]:
        if value and value not in seen:
            seen.add(value)
            aliases.append(value)
    return aliases


def owner_email_hints() -> List[str]:
    """Substrings and local parts used to recognize the owner's email addresses."""
    hints: List[str] = []
    seen: set[str] = set()
    for alias in summary_routing_aliases():
        if "@" not in alias:
            if alias not in seen:
                seen.add(alias)
                hints.append(alias)
            continue
        local, domain = alias.split("@", 1)
        for part in [domain, local.split("+")[0], alias]:
            if part and part not in seen:
                seen.add(part)
                hints.append(part)
    return hints


def owner_name_variants() -> List[str]:
    """Name tokens for excluding the owner from passive-wait heuristics."""
    name = owner_name().strip()
    variants: List[str] = []
    seen: set[str] = set()
    for token in [name, *name.split()]:
        key = token.strip().lower()
        if key and key not in seen:
            seen.add(key)
            variants.append(key)
    return variants or ["owner"]


@lru_cache(maxsize=1)
def other_party_owes_pattern() -> Pattern[str]:
    alt = "|".join(re.escape(v) for v in owner_name_variants())
    return re.compile(
        rf"^(?!(?:{alt})\b)"
        r"(?:[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+(?:owes?|hasn't|has not|needs to|must)\b",
        re.IGNORECASE,
    )


def public_config_payload() -> dict:
    """Fields exposed to the dashboard via GET /api/config."""
    source_account = (os.getenv("SOURCE_ACCOUNT") or "").strip()
    try:
        aliases = summary_routing_aliases()
    except ValueError:
        aliases = []
    return {
        "owner_name": owner_name(),
        "source_account": source_account,
        "owner_email_hints": owner_email_hints(),
        "summary_routing_aliases": aliases,
    }
