import type { LooseObj } from "./types.js";

export function str(v: unknown): string {
  return typeof v === "string" ? v : "";
}

export function arr(v: unknown): unknown[] {
  return Array.isArray(v) ? v : [];
}

export function escapeHtml(s: unknown): string {
  if (s == null || s === "") return "";
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/** Lowercase; strip ``+tag`` from local part (Gmail alias matching). */
export function normalizeGmailAddress(email: string): string {
  const e = email.trim().toLowerCase();
  const at = e.indexOf("@");
  if (at < 0) return e;
  let local = e.slice(0, at);
  const domain = e.slice(at + 1);
  const plus = local.indexOf("+");
  if (plus >= 0) local = local.slice(0, plus);
  return `${local}@${domain}`;
}

export function extractEmailsLower(headerValue: string): Set<string> {
  const out = new Set<string>();
  const text = headerValue.trim();
  if (!text) return out;
  const re = /<?([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})>?/g;
  let match: RegExpExecArray | null;
  while ((match = re.exec(text)) !== null) {
    out.add(match[1].toLowerCase());
  }
  return out;
}

export function recipientsContainAddress(
  raw: unknown,
  addressLower: string,
): boolean {
  const want = normalizeGmailAddress(addressLower);
  if (!want || !want.includes("@")) return false;
  const fields = parseRecipientFields(raw);
  const combined = new Set<string>([
    ...extractEmailsLower(fields.to),
    ...extractEmailsLower(fields.cc),
    ...extractEmailsLower(fields.bcc),
  ]);
  for (const addr of combined) {
    if (addr === addressLower || normalizeGmailAddress(addr) === want) return true;
  }
  return false;
}

function parseRecipientFields(raw: unknown): { to: string; cc: string; bcc: string } {
  if (!raw) return { to: "", cc: "", bcc: "" };
  if (typeof raw === "object" && raw !== null) {
    const o = raw as LooseObj;
    return { to: str(o.to), cc: str(o.cc), bcc: str(o.bcc) };
  }
  if (typeof raw === "string") {
    try {
      const o = JSON.parse(raw) as LooseObj;
      if (o && typeof o === "object") {
        return { to: str(o.to), cc: str(o.cc), bcc: str(o.bcc) };
      }
    } catch {
      return { to: raw, cc: "", bcc: "" };
    }
  }
  return { to: String(raw), cc: "", bcc: "" };
}

export function formatRecipients(raw: unknown): string {
  if (!raw) return "";
  try {
    const o = typeof raw === "string" ? (JSON.parse(raw) as LooseObj) : (raw as LooseObj);
    if (o && typeof o === "object") {
      const parts: string[] = [];
      if (str(o.to)) parts.push(`To: ${str(o.to)}`);
      if (str(o.cc)) parts.push(`Cc: ${str(o.cc)}`);
      if (str(o.bcc)) parts.push(`Bcc: ${str(o.bcc)}`);
      return parts.join(" · ");
    }
  } catch {
    return String(raw);
  }
  return String(raw);
}

export function formatDate(iso: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return escapeHtml(iso);
  return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export function toneClass(tone: string): string {
  const t = tone.toLowerCase();
  if (t === "informational") return "informational";
  if (t === "request") return "request";
  return "default";
}
