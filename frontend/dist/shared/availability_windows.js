import { escapeHtml } from "./utils.js";
import { extractSlotMentions } from "./slot_mentions.js";
import { findStructuredMentionsInText } from "./structured_slot_mentions.js";
let currentDoc = null;
let availabilityPromise = null;
function parseMinuteInZone(iso, timeZone) {
    const parts = new Intl.DateTimeFormat("en-US", {
        timeZone,
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
    }).formatToParts(new Date(iso));
    const hour = Number(parts.find((p) => p.type === "hour")?.value || 0);
    const minute = Number(parts.find((p) => p.type === "minute")?.value || 0);
    return hour * 60 + minute;
}
function parseYmdInZone(iso, timeZone) {
    return new Intl.DateTimeFormat("en-CA", {
        timeZone,
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
    }).format(new Date(iso));
}
function addRange(map, dateKey, range) {
    const bucket = map.get(dateKey) || [];
    bucket.push(range);
    map.set(dateKey, bucket);
}
function addLabeledRange(map, dateKey, range) {
    const bucket = map.get(dateKey) || [];
    bucket.push(range);
    map.set(dateKey, bucket);
}
function overlappingLabeled(ranges, target) {
    if (!ranges?.length)
        return [];
    return ranges.filter((r) => intersects(target, r));
}
function intersects(a, b) {
    return a.start < b.end && b.start < a.end;
}
function buildAvailabilityDoc(data) {
    const meta = (data.meta || {});
    const timezone = String(meta.timezone || "").trim() || "America/New_York";
    const openByDate = new Map();
    const virtualOnlyByDate = new Map();
    const busyByDate = new Map();
    const blockedByDate = new Map();
    const eventsByDate = new Map();
    for (const row of (Array.isArray(data.availability_for_new_meetings_iso)
        ? data.availability_for_new_meetings_iso
        : [])) {
        const dateKey = String(row.date || "");
        for (const win of (Array.isArray(row.likely_open_windows) ? row.likely_open_windows : [])) {
            const startIso = String(win.start || "");
            const endIso = String(win.end || "");
            if (!dateKey || !startIso || !endIso)
                continue;
            addRange(openByDate, dateKey, {
                start: parseMinuteInZone(startIso, timezone),
                end: parseMinuteInZone(endIso, timezone),
            });
        }
    }
    for (const row of (Array.isArray(data.child_home_virtual_only_local)
        ? data.child_home_virtual_only_local
        : [])) {
        const dateKey = String(row.date || "");
        for (const iv of (Array.isArray(row.intervals_iso) ? row.intervals_iso : [])) {
            const startIso = String(iv.start || "");
            const endIso = String(iv.end || "");
            if (!dateKey || !startIso || !endIso)
                continue;
            addRange(virtualOnlyByDate, dateKey, {
                start: parseMinuteInZone(startIso, timezone),
                end: parseMinuteInZone(endIso, timezone),
            });
        }
    }
    for (const row of (Array.isArray(data.parenting_unavailable_local)
        ? data.parenting_unavailable_local
        : [])) {
        const dateKey = String(row.date || "");
        for (const iv of (Array.isArray(row.intervals_iso) ? row.intervals_iso : [])) {
            const startIso = String(iv.start || "");
            const endIso = String(iv.end || "");
            if (!dateKey || !startIso || !endIso)
                continue;
            addLabeledRange(blockedByDate, dateKey, {
                start: parseMinuteInZone(startIso, timezone),
                end: parseMinuteInZone(endIso, timezone),
                label: String(iv.id || "Parenting").trim() || "Parenting",
            });
        }
    }
    for (const row of (Array.isArray(data.busy_with_buffers_iso) ? data.busy_with_buffers_iso : [])) {
        const startIso = String(row.start || "");
        const endIso = String(row.end || "");
        if (!startIso || !endIso)
            continue;
        const dateKey = parseYmdInZone(startIso, timezone);
        addLabeledRange(busyByDate, dateKey, {
            start: parseMinuteInZone(startIso, timezone),
            end: parseMinuteInZone(endIso, timezone),
            label: String(row.source || "").trim(),
        });
    }
    for (const ev of (Array.isArray(data.calendar_events_index) ? data.calendar_events_index : [])) {
        const startIso = String(ev.start_iso || "");
        const endIso = String(ev.end_iso || "");
        if (!startIso || !endIso)
            continue;
        const dateKey = parseYmdInZone(startIso, timezone);
        addLabeledRange(eventsByDate, dateKey, {
            start: parseMinuteInZone(startIso, timezone),
            end: parseMinuteInZone(endIso, timezone),
            label: String(ev.summary || "(No title)").trim() || "(No title)",
        });
    }
    return { timezone, openByDate, virtualOnlyByDate, busyByDate, blockedByDate, eventsByDate };
}
function minuteToHm(minute) {
    const h = Math.max(0, Math.min(23, Math.floor(minute / 60)));
    const m = Math.max(0, Math.min(59, minute % 60));
    return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
}
function formatEventLine(label, start, end) {
    const title = label.trim() || "(No title)";
    return `${title} (${minuteToHm(start)}–${minuteToHm(end)})`;
}
function cleanBusySource(source) {
    return source.replace(/\s*\([^)]*\+buffer\)\s*$/, "").trim();
}
function uniqueEventLines(ranges) {
    const seen = new Set();
    const out = [];
    for (const range of ranges) {
        const line = formatEventLine(range.label, range.start, range.end);
        if (seen.has(line))
            continue;
        seen.add(line);
        out.push(line);
    }
    return out;
}
function busyDetails(doc, mention, target) {
    const events = uniqueEventLines(overlappingLabeled(doc.eventsByDate.get(mention.date_key), target));
    if (events.length)
        return events.join("; ");
    const buffered = overlappingLabeled(doc.busyByDate.get(mention.date_key), target)
        .map((r) => cleanBusySource(r.label))
        .filter(Boolean);
    const uniqueBuffered = [...new Set(buffered)];
    if (uniqueBuffered.length)
        return uniqueBuffered.join("; ");
    return "Busy on calendar.";
}
function blockedDetails(doc, mention, target) {
    const blocks = uniqueEventLines(overlappingLabeled(doc.blockedByDate.get(mention.date_key), target));
    if (blocks.length)
        return blocks.join("; ");
    return "Blocked by parenting constraints.";
}
function statusForRange(dateKey, startMinute, endMinute, doc) {
    if (!doc) {
        return {
            status: "unknown",
            date_key: dateKey,
            start_minute: startMinute,
            end_minute: endMinute,
            details: "Availability unknown (calendar export not loaded).",
        };
    }
    const target = { start: startMinute, end: endMinute };
    const open = (doc.openByDate.get(dateKey) || []).some((r) => intersects(target, r));
    const virtualOnly = (doc.virtualOnlyByDate.get(dateKey) || []).some((r) => intersects(target, r));
    const busy = (doc.busyByDate.get(dateKey) || []).some((r) => intersects(target, r));
    const blocked = (doc.blockedByDate.get(dateKey) || []).some((r) => intersects(target, r));
    let status = "unknown";
    if (open)
        status = "open";
    else if (virtualOnly)
        status = "virtual-only";
    else if (busy)
        status = "busy";
    else if (blocked)
        status = "blocked";
    const mention = {
        raw: "",
        start: 0,
        end: 0,
        date_key: dateKey,
        start_minute: startMinute,
        end_minute: endMinute,
        label: "",
    };
    return {
        status,
        date_key: dateKey,
        start_minute: startMinute,
        end_minute: endMinute,
        details: status === "open"
            ? "Open slot available."
            : status === "virtual-only"
                ? "Available virtually (child-home constraint)."
                : status === "busy"
                    ? busyDetails(doc, mention, target)
                    : status === "blocked"
                        ? blockedDetails(doc, mention, target)
                        : "No matching availability window found.",
    };
}
function statusForMention(mention, doc) {
    return statusForRange(mention.date_key, mention.start_minute, mention.end_minute, doc);
}
export function statusForCounterpartySlot(slot, doc) {
    const startMinute = parseHmToMinutes(slot.start);
    const endMinute = parseHmToMinutes(slot.end);
    if (startMinute === null || endMinute === null) {
        return {
            status: "unknown",
            date_key: slot.date,
            start_minute: 0,
            end_minute: 0,
            details: "Invalid time slot.",
        };
    }
    return statusForRange(slot.date, startMinute, endMinute, doc);
}
function parseHmToMinutes(hm) {
    const trimmed = hm.trim();
    const m24 = trimmed.match(/^(\d{1,2}):(\d{2})$/);
    if (m24) {
        const hour = Number(m24[1]);
        const minute = Number(m24[2]);
        if (hour < 0 || hour > 23 || minute < 0 || minute > 59)
            return null;
        return hour * 60 + minute;
    }
    const m12 = trimmed.match(/^(\d{1,2})(?::(\d{2}))?\s*(am|pm)$/i);
    if (!m12)
        return null;
    let hour = Number(m12[1]);
    const minute = Number(m12[2] || 0);
    const mer = m12[3].toLowerCase();
    if (hour < 1 || hour > 12 || minute < 0 || minute > 59)
        return null;
    if (hour === 12)
        hour = 0;
    if (mer === "pm")
        hour += 12;
    return hour * 60 + minute;
}
function tooltipForMatch(match) {
    return [
        `${match.date_key} ${minuteToHm(match.start_minute)}-${minuteToHm(match.end_minute)}`,
        `Availability: ${match.status}`,
        match.details,
    ].join("\n");
}
export async function ensureAvailabilityDocLoaded() {
    if (currentDoc)
        return;
    if (!availabilityPromise) {
        availabilityPromise = (async () => {
            try {
                const res = await fetch("/out/availability_calendar_latest.json", {
                    credentials: "same-origin",
                    cache: "no-store",
                });
                if (!res.ok)
                    return null;
                const data = (await res.json());
                return buildAvailabilityDoc(data);
            }
            catch {
                return null;
            }
        })();
    }
    currentDoc = await availabilityPromise;
}
export function counterpartySlotHighlightHtml(slot, displayLabel) {
    const match = statusForCounterpartySlot(slot, currentDoc);
    return `<span class="slot-mention slot-mention--${escapeHtml(match.status)}" title="${escapeHtml(tooltipForMatch(match))}">${escapeHtml(displayLabel)}</span>`;
}
export function highlightMentionsHtml(text, structuredSlots) {
    const mentions = structuredSlots?.length
        ? findStructuredMentionsInText(text, structuredSlots)
        : extractSlotMentions(text);
    if (!mentions.length)
        return escapeHtml(text);
    let out = "";
    let cursor = 0;
    for (const mention of mentions) {
        out += escapeHtml(text.slice(cursor, mention.start));
        const match = statusForMention(mention, currentDoc);
        out += `<span class="slot-mention slot-mention--${escapeHtml(match.status)}" title="${escapeHtml(tooltipForMatch(match))}">${escapeHtml(mention.raw)}</span>`;
        cursor = mention.end;
    }
    out += escapeHtml(text.slice(cursor));
    return out;
}
