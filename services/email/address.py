"""Email address normalization (no Gmail API dependencies)."""


def normalize_gmail_address(email: str) -> str:
    """
    Lowercase; strip ``+tag`` from the local part so ``you+fivelanes@x`` matches ``you@x``.
    """
    e = (email or "").strip().lower()
    if "@" not in e:
        return e
    local, domain = e.split("@", 1)
    if "+" in local:
        local = local.split("+", 1)[0]
    return f"{local}@{domain}"
