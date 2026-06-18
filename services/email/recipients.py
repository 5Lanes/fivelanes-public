"""Parse To/Cc/Bcc headers and match inbox addresses."""

from email.utils import getaddresses

from services.email.address import normalize_gmail_address


def extract_emails_lower(header_value: str) -> set:
    """Extract email addresses from a header value as a lowercase set."""
    if not header_value or not header_value.strip():
        return set()
    addrs = getaddresses([header_value])
    return {addr.lower() for _, addr in addrs if addr and "@" in addr}


def matches_contact_emails(
    from_addr: str,
    to_addr: str,
    cc_addr: str,
    bcc_addr: str,
    contact_emails: set,
) -> bool:
    """True if any sender or recipient is in the given contact_emails set."""
    all_addrs = (
        extract_emails_lower(from_addr)
        | extract_emails_lower(to_addr)
        | extract_emails_lower(cc_addr)
        | extract_emails_lower(bcc_addr)
    )
    return bool(all_addrs & contact_emails)


def recipients_contain_address(
    to_addr: str, cc_addr: str, bcc_addr: str, address_lower: str
) -> bool:
    """True if address_lower appears in To, Cc, or Bcc."""
    if not address_lower or "@" not in address_lower:
        return False
    combined = (
        extract_emails_lower(to_addr)
        | extract_emails_lower(cc_addr)
        | extract_emails_lower(bcc_addr)
    )
    if address_lower in combined:
        return True
    want = normalize_gmail_address(address_lower)
    return any(normalize_gmail_address(a) == want for a in combined)


def to_field_contains_address(to_addr: str, address_lower: str) -> bool:
    """True if address_lower appears among parsed addresses in the To header only."""
    if not address_lower or "@" not in address_lower:
        return False
    addrs = extract_emails_lower(to_addr)
    if address_lower in addrs:
        return True
    want = normalize_gmail_address(address_lower)
    return any(normalize_gmail_address(a) == want for a in addrs)


def cc_field_contains_address(cc_addr: str, address_lower: str) -> bool:
    """True if address_lower appears among parsed addresses in the Cc header only."""
    if not address_lower or "@" not in address_lower:
        return False
    addrs = extract_emails_lower(cc_addr)
    if address_lower in addrs:
        return True
    want = normalize_gmail_address(address_lower)
    return any(normalize_gmail_address(a) == want for a in addrs)


def bcc_field_contains_address(bcc_addr: str, address_lower: str) -> bool:
    """True if address_lower appears among parsed addresses in the Bcc header only."""
    if not address_lower or "@" not in address_lower:
        return False
    addrs = extract_emails_lower(bcc_addr)
    if address_lower in addrs:
        return True
    want = normalize_gmail_address(address_lower)
    return any(normalize_gmail_address(a) == want for a in addrs)


def is_cc_bcc_only_recipient(
    to_addr: str, cc_addr: str, bcc_addr: str, inbox_lower: str
) -> bool:
    """True when the inbox is on Cc or Bcc but not on To."""
    if not inbox_lower or "@" not in inbox_lower:
        return False
    if to_field_contains_address(to_addr, inbox_lower):
        return False
    return cc_field_contains_address(cc_addr, inbox_lower) or bcc_field_contains_address(
        bcc_addr, inbox_lower
    )
