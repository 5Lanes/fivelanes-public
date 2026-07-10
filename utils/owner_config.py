"""Owner identity from environment (inbox user, routing aliases, display name)."""

from __future__ import annotations

import logging
import os
import re
from functools import lru_cache
from typing import List, Pattern

from services.email.address import normalize_gmail_address

log = logging.getLogger(__name__)


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
    """Domains and full addresses used to recognize the owner's email addresses.

    Bare entries (no ``@``) are treated as owned *domains* (e.g. ``ainovva.com``); entries
    with ``@`` are exact addresses, matched via :func:`normalize_gmail_address` so aliasing
    (``+tag``, and dots in Gmail local parts) doesn't cause false negatives.
    """
    hints: List[str] = []
    seen: set[str] = set()
    for alias in summary_routing_aliases():
        if "@" not in alias:
            if alias not in seen:
                seen.add(alias)
                hints.append(alias)
            continue
        _, domain = alias.split("@", 1)
        for part in [domain, normalize_gmail_address(alias)]:
            if part and part not in seen:
                seen.add(part)
                hints.append(part)
    source_domain = (os.getenv("SOURCE_DOMAIN") or "").strip().lower()
    if source_domain and source_domain not in seen:
        seen.add(source_domain)
        hints.append(source_domain)
    for email in _connected_account_emails():
        if email not in seen:
            seen.add(email)
            hints.append(email)
    return hints


@lru_cache(maxsize=1)
def _connected_account_emails() -> frozenset:
    """Normalized addresses for every connected mailbox: ``tokens.json`` ``account`` fields
    (fast, offline) plus live Gmail profile/send-as identities (covers aliases that were
    never saved to ``tokens.json``, e.g. a send-as address on a different name).

    Cached per process; results never change within a single pipeline run.
    """
    emails: set[str] = set()
    try:
        from services.gmail_client import _load_tokens

        for data in _load_tokens().values():
            acct = (data.get("account") or "").strip().lower()
            if acct and "@" in acct:
                emails.add(normalize_gmail_address(acct))
    except Exception:
        log.debug("Could not read tokens.json account fields for owner recognition", exc_info=True)
    try:
        from services.gmail_client import get_all_gmail_services, mailbox_identity_emails

        for account_id, service in get_all_gmail_services():
            emails.update(mailbox_identity_emails(service, account_id))
    except Exception:
        log.debug("Could not fetch live mailbox identities for owner recognition", exc_info=True)
    return frozenset(emails)


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


def is_likely_own_email(email: str) -> bool:
    """Whether ``email`` matches an owner alias/hint (see :func:`owner_email_hints`).

    Matching is exact (address or domain), not substring — a coworker or contact who
    happens to share the owner's first name must not be misclassified as "the owner".
    """
    e = normalize_gmail_address(email)
    if not e or "@" not in e:
        return False
    domain = e.split("@", 1)[1]
    try:
        hints = owner_email_hints()
    except ValueError:
        hints = []
    for hint in hints:
        if not hint:
            continue
        if "@" in hint:
            if e == hint:
                return True
            continue
        if domain == hint:
            return True
    return False


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
