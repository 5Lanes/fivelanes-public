export function str(v) {
    return typeof v === "string" ? v : "";
}
export function arr(v) {
    return Array.isArray(v) ? v : [];
}
export function escapeHtml(s) {
    if (s == null || s === "")
        return "";
    return String(s)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}
export function threadPageHref(threadId) {
    return `/threads?thread=${encodeURIComponent(threadId)}`;
}
/** Lowercase; strip ``+tag`` from local part (Gmail alias matching). */
export function normalizeGmailAddress(email) {
    const e = email.trim().toLowerCase();
    const at = e.indexOf("@");
    if (at < 0)
        return e;
    let local = e.slice(0, at);
    const domain = e.slice(at + 1);
    const plus = local.indexOf("+");
    if (plus >= 0)
        local = local.slice(0, plus);
    return `${local}@${domain}`;
}
export function extractEmailsLower(headerValue) {
    const out = new Set();
    const text = headerValue.trim();
    if (!text)
        return out;
    const re = /<?([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})>?/g;
    let match;
    while ((match = re.exec(text)) !== null) {
        out.add(match[1].toLowerCase());
    }
    return out;
}
export function recipientsContainAddress(raw, addressLower) {
    const want = normalizeGmailAddress(addressLower);
    if (!want || !want.includes("@"))
        return false;
    const fields = parseRecipientFields(raw);
    const combined = new Set([
        ...extractEmailsLower(fields.to),
        ...extractEmailsLower(fields.cc),
        ...extractEmailsLower(fields.bcc),
    ]);
    for (const addr of combined) {
        if (addr === addressLower || normalizeGmailAddress(addr) === want)
            return true;
    }
    return false;
}
function sourceAccountUsesPlusTag(sourceLower) {
    const local = sourceLower.split("@", 1)[0] ?? "";
    return local.includes("+");
}
function headerFieldContainsAddress(headerValue, addressLower) {
    const want = addressLower.trim().toLowerCase();
    if (!want.includes("@"))
        return false;
    const addrs = extractEmailsLower(headerValue);
    if (addrs.has(want))
        return true;
    if (sourceAccountUsesPlusTag(want))
        return false;
    const normalizedWant = normalizeGmailAddress(want);
    for (const addr of addrs) {
        if (normalizeGmailAddress(addr) === normalizedWant)
            return true;
    }
    return false;
}
/** True when the Fivelanes inbox is in the To header (forward delivery shell). */
export function toFieldContainsAddress(raw, addressLower) {
    const fields = parseRecipientFields(raw);
    return headerFieldContainsAddress(fields.to, addressLower);
}
function parseRecipientFields(raw) {
    if (!raw)
        return { to: "", cc: "", bcc: "" };
    if (typeof raw === "object" && raw !== null) {
        const o = raw;
        return { to: str(o.to), cc: str(o.cc), bcc: str(o.bcc) };
    }
    if (typeof raw === "string") {
        try {
            const o = JSON.parse(raw);
            if (o && typeof o === "object") {
                return { to: str(o.to), cc: str(o.cc), bcc: str(o.bcc) };
            }
        }
        catch {
            return { to: raw, cc: "", bcc: "" };
        }
    }
    return { to: String(raw), cc: "", bcc: "" };
}
export function formatRecipients(raw) {
    if (!raw)
        return "";
    try {
        const o = typeof raw === "string" ? JSON.parse(raw) : raw;
        if (o && typeof o === "object") {
            const parts = [];
            if (str(o.to))
                parts.push(`To: ${str(o.to)}`);
            if (str(o.cc))
                parts.push(`Cc: ${str(o.cc)}`);
            if (str(o.bcc))
                parts.push(`Bcc: ${str(o.bcc)}`);
            return parts.join(" · ");
        }
    }
    catch {
        return String(raw);
    }
    return String(raw);
}
export function formatDate(iso) {
    if (!iso)
        return "";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime()))
        return escapeHtml(iso);
    return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}
export function toneClass(tone) {
    const t = tone.toLowerCase();
    if (t === "informational")
        return "informational";
    if (t === "request")
        return "request";
    return "default";
}
