"""Email address normalization (no Gmail API dependencies)."""


_GMAIL_DOMAINS = {"gmail.com", "googlemail.com"}


def normalize_gmail_address(email: str) -> str:
    """
    Lowercase; strip ``+tag`` from the local part so ``you+fivelanes@x`` matches ``you@x``.
    For ``gmail.com``/``googlemail.com`` addresses, also strip dots from the local part
    (Gmail ignores them, so ``a.b@gmail.com`` and ``ab@gmail.com`` are the same mailbox).
    """
    e = (email or "").strip().lower()
    if "@" not in e:
        return e
    local, domain = e.split("@", 1)
    if "+" in local:
        local = local.split("+", 1)[0]
    if domain in _GMAIL_DOMAINS:
        local = local.replace(".", "")
    return f"{local}@{domain}"
