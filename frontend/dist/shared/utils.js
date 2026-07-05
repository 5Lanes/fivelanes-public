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
/** Pipeline run_stamp (UTC ``YYYYMMDD_HHMMSS``) or ISO ``generated_at`` for UI labels. */
export function formatPipelineRefreshTime(raw) {
    if (!raw)
        return "";
    const stampMatch = /^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})$/.exec(raw.trim());
    if (stampMatch) {
        const [, y, mo, d, h, mi, s] = stampMatch;
        const dt = new Date(Date.UTC(+y, +mo - 1, +d, +h, +mi, +s));
        if (!Number.isNaN(dt.getTime())) {
            return dt.toLocaleString(undefined, { timeStyle: "short" });
        }
    }
    const parsed = new Date(raw);
    if (!Number.isNaN(parsed.getTime())) {
        return parsed.toLocaleString(undefined, { timeStyle: "short" });
    }
    return raw;
}
/** Short relative label for track/thread headers (e.g. "Today", "1d ago"). */
export function formatRelativeShort(iso) {
    if (!iso)
        return "";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime()))
        return "";
    const diffMs = Date.now() - d.getTime();
    if (diffMs < 0)
        return "Today";
    const diffDays = Math.floor(diffMs / (24 * 60 * 60 * 1000));
    if (diffDays === 0)
        return "Today";
    if (diffDays === 1)
        return "1d ago";
    if (diffDays < 7)
        return `${diffDays}d ago`;
    if (diffDays < 14)
        return "1w ago";
    if (diffDays < 30)
        return `${Math.floor(diffDays / 7)}w ago`;
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
/** Summary meta date (e.g. "Jul 3"). */
export function formatSummaryUpdated(iso) {
    if (!iso)
        return "";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime()))
        return iso.slice(0, 10);
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
export function toneClass(tone) {
    const t = tone.toLowerCase();
    if (t === "informational")
        return "informational";
    if (t === "request")
        return "request";
    return "default";
}
