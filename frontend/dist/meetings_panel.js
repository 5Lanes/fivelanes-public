/**
 * Meetings view: deduplicated calendar events for the next N days.
 * Source: ``/api/meetings`` (timeline.db meetings table), else out/availability_calendar_latest.json.
 */
import { addDaysToYmd, dayHeadingLabelLong, formatTimeRangeInTz, isoToYmdInZone, nextNDaysFromYmd, todayYmdLocal } from "./shared/time_ui.js";
import { ensureFeaturesLoaded, isFeatureEnabled } from "./shared/features.js";
import { escapeHtml } from "./shared/utils.js";
export const MEETINGS_LOOKAHEAD_DAYS = 14;
const MEETINGS_CACHE_KEY = "fivelanes_meetings_cache";
const MEETINGS_ETAG_KEY = "fivelanes_meetings_etag";
function str(v) {
    return typeof v === "string" ? v : "";
}
function parseAttendees(v) {
    if (!Array.isArray(v))
        return [];
    const out = [];
    const seen = new Set();
    for (const item of v) {
        const e = typeof item === "string" ? item.trim().toLowerCase() : "";
        if (e && e.includes("@") && !seen.has(e)) {
            seen.add(e);
            out.push(e);
        }
    }
    return out;
}
function parseJsonBody(text) {
    const trimmed = text.trim();
    if (!trimmed.startsWith("{") && !trimmed.startsWith("["))
        return null;
    try {
        const parsed = JSON.parse(trimmed);
        return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : null;
    }
    catch {
        return null;
    }
}
function mergeAttendeeLists(a, b) {
    const seen = new Set(a);
    for (const e of b) {
        if (!seen.has(e))
            seen.add(e);
    }
    return [...seen].sort();
}
/** Stable key for a calendar event (matches ``meetings.dedupe_key`` in timeline.db). */
export function meetingDedupeKey(m) {
    return `${m.summary}|${m.start_iso}|${m.end_iso}`;
}
function dedupeMeetings(rows) {
    const byKey = new Map();
    const sorted = [...rows].sort((a, b) => a.start_iso.localeCompare(b.start_iso));
    for (const m of sorted) {
        const key = meetingDedupeKey(m);
        const existing = byKey.get(key);
        if (!existing) {
            byKey.set(key, { ...m, attendees: [...m.attendees] });
            continue;
        }
        existing.attendees = mergeAttendeeLists(existing.attendees, m.attendees);
        if (!existing.location && m.location)
            existing.location = m.location;
        if (!existing.html_link && m.html_link)
            existing.html_link = m.html_link;
    }
    return [...byKey.values()].sort((a, b) => a.start_iso.localeCompare(b.start_iso));
}
/** Meetings not yet ended whose start falls within the next ``days`` (includes in-progress today). */
export function meetingsInNextDays(rows, days) {
    const now = Date.now();
    const horizon = now + days * 24 * 60 * 60 * 1000;
    return rows.filter((m) => {
        if (!m.start_iso)
            return false;
        const start = new Date(m.start_iso).getTime();
        const end = m.end_iso ? new Date(m.end_iso).getTime() : start;
        return end >= now && start < horizon;
    });
}
export function calendarIndexHasAttendeesField(index) {
    return index.length > 0 && index.some((ev) => Object.prototype.hasOwnProperty.call(ev, "attendees"));
}
function meetingsFromDbRows(rows, days, exportedAt, tz) {
    const raw = rows.map((r) => ({
        summary: str(r.summary) || "(No title)",
        start_iso: str(r.start_iso),
        end_iso: str(r.end_iso),
        location: str(r.location),
        html_link: str(r.html_link),
        attendees: parseAttendees(r.attendees),
    }));
    const meetings = dedupeMeetings(meetingsInNextDays(raw, days));
    const sourceNote = exportedAt
        ? `from timeline.db · meetings (exported ${exportedAt})`
        : "from timeline.db · meetings";
    return { meetings, timezone: tz, days, sourceNote };
}
function meetingsFromAvailabilityDoc(data, days) {
    const meta = (data.meta || {});
    const tz = str(meta.timezone) || Intl.DateTimeFormat().resolvedOptions().timeZone;
    const index = Array.isArray(data.calendar_events_index) ? data.calendar_events_index : [];
    const raw = [];
    for (const ev of index) {
        raw.push({
            summary: str(ev.summary) || "(No title)",
            start_iso: str(ev.start_iso),
            end_iso: str(ev.end_iso),
            location: str(ev.location),
            html_link: str(ev.html_link),
            attendees: parseAttendees(ev.attendees),
        });
    }
    const meetings = dedupeMeetings(meetingsInNextDays(raw, days));
    const generatedAt = str(meta.generated_at);
    const sourceNote = generatedAt
        ? `from out/availability_calendar_latest.json (exported ${generatedAt})`
        : "from out/availability_calendar_latest.json";
    return {
        meetings,
        timezone: tz,
        days,
        sourceNote,
    };
}
function readMeetingsCache() {
    try {
        const raw = sessionStorage.getItem(MEETINGS_CACHE_KEY);
        if (!raw)
            return null;
        const parsed = JSON.parse(raw);
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed))
            return null;
        const o = parsed;
        const meetings = Array.isArray(o.meetings) ? o.meetings : [];
        const days = typeof o.days === "number" ? o.days : MEETINGS_LOOKAHEAD_DAYS;
        const timezone = str(o.timezone) || Intl.DateTimeFormat().resolvedOptions().timeZone;
        const sourceNote = str(o.sourceNote);
        return { meetings, timezone, days, sourceNote };
    }
    catch {
        return null;
    }
}
function writeMeetingsCache(result, etag) {
    try {
        sessionStorage.setItem(MEETINGS_CACHE_KEY, JSON.stringify(result));
        if (etag)
            sessionStorage.setItem(MEETINGS_ETAG_KEY, etag);
    }
    catch {
        /* quota / private mode */
    }
}
export function clearMeetingsCache() {
    try {
        sessionStorage.removeItem(MEETINGS_CACHE_KEY);
        sessionStorage.removeItem(MEETINGS_ETAG_KEY);
    }
    catch {
        /* private mode */
    }
}
async function loadMeetingsFromApi(days) {
    if (location.protocol === "file:")
        return null;
    const headers = {};
    try {
        const etag = sessionStorage.getItem(MEETINGS_ETAG_KEY);
        if (etag)
            headers["If-None-Match"] = etag;
    }
    catch {
        /* private mode */
    }
    const res = await fetch(`/api/meetings?days=${days}`, {
        credentials: "same-origin",
        headers,
    });
    if (res.status === 304) {
        const cached = readMeetingsCache();
        return cached && cached.days === days ? cached : null;
    }
    if (!res.ok)
        return null;
    const data = parseJsonBody(await res.text());
    if (!data || data.ok === false)
        return null;
    const rows = Array.isArray(data.meetings) ? data.meetings : [];
    const exportedAt = str(data.exported_at);
    const tz = str(data.timezone) || Intl.DateTimeFormat().resolvedOptions().timeZone;
    const result = meetingsFromDbRows(rows, days, exportedAt, tz);
    writeMeetingsCache(result, res.headers.get("ETag"));
    return result;
}
/** Warm meetings cache while the summaries bundle loads. */
export function prefetchMeetings(days = MEETINGS_LOOKAHEAD_DAYS) {
    if (location.protocol === "file:")
        return;
    void loadMeetings(days).catch(() => { });
}
export async function loadMeetings(days) {
    try {
        const fromApi = await loadMeetingsFromApi(days);
        if (fromApi)
            return fromApi;
    }
    catch {
        /* fall through to JSON */
    }
    await ensureFeaturesLoaded();
    if (!isFeatureEnabled("availability")) {
        return { error: "No meetings from API. Calendar availability export requires premium." };
    }
    try {
        const res = await fetch("/out/availability_calendar_latest.json", {
            credentials: "same-origin",
        });
        const text = await res.text();
        const data = parseJsonBody(text);
        if (!data) {
            return {
                error: `No meetings from API or availability export (HTTP ${res.status}). Run the scheduler or scripts/pull_calendar_availability.py.`,
            };
        }
        const result = meetingsFromAvailabilityDoc(data, days);
        writeMeetingsCache(result, null);
        return result;
    }
    catch (e) {
        return {
            error: e instanceof Error ? e.message : String(e),
        };
    }
}
export function meetingsByDate(meetings, tz) {
    const byDate = new Map();
    for (const m of meetings) {
        if (!m.start_iso)
            continue;
        const key = isoToYmdInZone(m.start_iso, tz);
        if (!byDate.has(key))
            byDate.set(key, []);
        byDate.get(key).push(m);
    }
    for (const rows of byDate.values()) {
        rows.sort((a, b) => a.start_iso.localeCompare(b.start_iso));
    }
    return byDate;
}
function renderMeetingRow(m, tz) {
    const timeLine = formatTimeRangeInTz(m.start_iso, m.end_iso, tz);
    const parts = [];
    if (m.location)
        parts.push(m.location);
    if (m.attendees.length > 0) {
        const n = m.attendees.length;
        parts.push(`${n} attendee${n === 1 ? "" : "s"}: ${m.attendees.join(", ")}`);
    }
    const detail = parts.join(" · ");
    const titleHtml = m.html_link
        ? `<a href="${escapeHtml(m.html_link)}" target="_blank" rel="noopener noreferrer">${escapeHtml(m.summary)}</a>`
        : escapeHtml(m.summary);
    const sub = detail ? `<div class="dash-avail-detail">${escapeHtml(detail)}</div>` : "";
    return `<li class="dash-avail-row dash-avail-row--commit">
    <div class="dash-avail-time">${escapeHtml(timeLine)}</div>
    <div class="dash-avail-body">
      <div class="dash-avail-title">${titleHtml}</div>
      ${sub}
    </div>
  </li>`;
}
function renderDaySection(dateKey, rows, tz) {
    const inner = rows.map((m) => renderMeetingRow(m, tz)).join("");
    return `<section class="dash-avail-day" data-date="${escapeHtml(dateKey)}">
    <header class="dash-avail-day-head">
      <div class="dash-avail-day-name">${escapeHtml(dayHeadingLabelLong(dateKey))}</div>
      <div class="dash-avail-day-ymd">${escapeHtml(dateKey)}</div>
    </header>
    <ul class="dash-avail-list">${inner}</ul>
  </section>`;
}
function renderAgendaHtml(meetings, tz, days) {
    const byDate = meetingsByDate(meetings, tz);
    const start = todayYmdLocal();
    const dayKeys = nextNDaysFromYmd(start, days);
    const sections = [];
    for (const dateKey of dayKeys) {
        const rows = byDate.get(dateKey) || [];
        if (!rows.length)
            continue;
        sections.push(renderDaySection(dateKey, rows, tz));
    }
    if (!sections.length) {
        return `<p class="dash-avail-error">No meetings in the next ${days} days.</p>`;
    }
    return `<div class="dash-avail-agenda">${sections.join("")}</div>`;
}
/** Compact "today/tomorrow" strip — used at the top of the onebox view. Empty string if nothing on either day. */
export function meetingsTodayTomorrowHtml(meetings, tz) {
    const byDate = meetingsByDate(meetings, tz);
    const today = todayYmdLocal();
    const tomorrow = addDaysToYmd(today, 1) || today;
    const todayRows = byDate.get(today) || [];
    const tomorrowRows = byDate.get(tomorrow) || [];
    if (!todayRows.length && !tomorrowRows.length)
        return "";
    const renderGroup = (label, rows) => {
        if (!rows.length)
            return "";
        const items = rows
            .map((m) => `<li class="onebox-meetings-item"><span class="onebox-meetings-time">${escapeHtml(formatTimeRangeInTz(m.start_iso, m.end_iso, tz))}</span><span class="onebox-meetings-title">${escapeHtml(m.summary)}</span></li>`)
            .join("");
        return `<div class="onebox-meetings-group">
      <div class="onebox-meetings-group-label">${label} · ${rows.length} meeting${rows.length === 1 ? "" : "s"}</div>
      <ul class="onebox-meetings-list">${items}</ul>
    </div>`;
    };
    return `<div class="onebox-meetings-summary-inner">${renderGroup("Today", todayRows)}${renderGroup("Tomorrow", tomorrowRows)}</div>`;
}
function applyMeetingsPanel(metaEl, agendaEl, result) {
    const { meetings, timezone: tz, days, sourceNote } = result;
    const count = meetings.length;
    metaEl.textContent =
        count === 0
            ? `No meetings in the next ${days} days (${tz}).`
            : `${count} meeting${count === 1 ? "" : "s"} in the next ${days} days (${tz}, deduplicated, ${sourceNote}).`;
    agendaEl.innerHTML = renderAgendaHtml(meetings, tz, days);
    return count;
}
/** Loads upcoming meetings and fills the Meetings view. Returns meeting count. */
export async function refreshMeetingsPanel(metaEl, agendaEl) {
    const cached = readMeetingsCache();
    if (cached && cached.days === MEETINGS_LOOKAHEAD_DAYS) {
        applyMeetingsPanel(metaEl, agendaEl, cached);
    }
    else {
        metaEl.textContent = "Loading meetings…";
        agendaEl.innerHTML = "";
    }
    try {
        const result = await loadMeetings(MEETINGS_LOOKAHEAD_DAYS);
        if ("error" in result) {
            if (!cached) {
                metaEl.textContent = `Could not load meetings: ${result.error}`;
                agendaEl.innerHTML = "";
            }
            return cached?.meetings.length ?? 0;
        }
        return applyMeetingsPanel(metaEl, agendaEl, result);
    }
    catch (e) {
        if (!cached) {
            metaEl.textContent = `Meetings load failed: ${e instanceof Error ? e.message : String(e)}`;
            agendaEl.innerHTML = "";
        }
        return cached?.meetings.length ?? 0;
    }
}
