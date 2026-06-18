/**
 * Match calendar meetings to inbox threads by overlapping participant emails.
 */
import { getOwnerEmailHints, isLikelyOwnEmail as isOwnerEmail } from "./shared/owner_config.js";
function str(v) {
    return typeof v === "string" ? v : "";
}
export function normalizeEmail(email) {
    const e = email.trim().toLowerCase();
    const at = e.indexOf("@");
    if (at < 1)
        return e;
    const local = e.slice(0, at).split("+")[0];
    return `${local}@${e.slice(at + 1)}`;
}
/** Extract addresses from a header string or JSON recipients blob. */
export function extractEmailsFromText(raw) {
    const text = raw.trim();
    if (!text)
        return [];
    const out = new Set();
    try {
        const parsed = JSON.parse(text);
        if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
            const o = parsed;
            for (const key of ["to", "cc", "bcc"]) {
                for (const e of extractEmailsFromText(str(o[key])))
                    out.add(e);
            }
            return [...out];
        }
    }
    catch {
        /* plain header */
    }
    const re = /[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}/gi;
    for (const m of text.match(re) || []) {
        const e = normalizeEmail(m);
        if (e.includes("@"))
            out.add(e);
    }
    return [...out];
}
export function externalEmails(emails) {
    const out = new Set();
    for (const raw of emails) {
        const e = normalizeEmail(raw);
        if (e.includes("@") && !isOwnerEmail(e))
            out.add(e);
    }
    return out;
}
export function threadContactEmails(ctx) {
    return externalEmails(ctx.contactEmails);
}
export function meetingExternalAttendees(attendees) {
    return externalEmails(attendees);
}
export function findMatchingThread(attendees, contexts) {
    const meetingExternal = meetingExternalAttendees(attendees);
    if (!meetingExternal.size || !contexts.length)
        return null;
    let best = null;
    let bestOverlap = 0;
    let bestSnooze = 2;
    for (const ctx of contexts) {
        const snooze = Number(ctx.snoozed || 0);
        if (snooze === 2)
            continue;
        const threadExternal = threadContactEmails(ctx);
        let overlap = 0;
        for (const e of meetingExternal) {
            if (threadExternal.has(e))
                overlap += 1;
        }
        if (!overlap)
            continue;
        const better = overlap > bestOverlap ||
            (overlap === bestOverlap && snooze < bestSnooze) ||
            (overlap === bestOverlap &&
                snooze === bestSnooze &&
                ctx.latestIso > (best?.latestIso || ""));
        if (better) {
            best = ctx;
            bestOverlap = overlap;
            bestSnooze = snooze;
        }
    }
    return best;
}
export function buildThreadMatchContexts(threads, labelForThread) {
    void getOwnerEmailHints();
    const out = [];
    for (const thread of threads) {
        const emails = new Set();
        let latestIso = "";
        for (const m of thread.messages) {
            const c = m.cleaned || {};
            const s = m.summary || {};
            for (const e of extractEmailsFromText(str(c.sender)))
                emails.add(e);
            for (const e of extractEmailsFromText(str(c.recipients)))
                emails.add(e);
            const dt = str(c.datetime || s.datetime);
            if (dt && dt > latestIso)
                latestIso = dt;
            const parties = s.parties;
            if (parties && typeof parties === "object" && !Array.isArray(parties)) {
                const p = parties;
                for (const key of ["active_speakers", "audience"]) {
                    const arr = p[key];
                    if (Array.isArray(arr)) {
                        for (const item of arr) {
                            for (const e of extractEmailsFromText(str(item)))
                                emails.add(e);
                        }
                    }
                }
            }
        }
        const primary = thread.messages[0]?.summary || {};
        out.push({
            threadId: thread.id,
            label: labelForThread(thread),
            snoozed: Number(primary.snoozed || 0),
            latestIso,
            contactEmails: [...emails],
        });
    }
    return out;
}
