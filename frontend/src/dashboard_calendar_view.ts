/**
 * Dashboard schedule rail — unified calendar view (open slots, virtual-only, meetings).
 */

import { buildDayAgenda, getAvailabilityTimeZone, type AgendaItem } from "./availability_panel.js";
import { DASHBOARD_MEETINGS_LOOKAHEAD_DAYS } from "./dashboard_panel.js";
import {
  meetingDedupeKey,
  meetingsInNextDays,
  type MeetingRow,
  loadMeetings,
} from "./meetings_panel.js";
import {
  buildThreadMatchContexts,
  findMatchingThread,
  type ThreadMatchContext,
} from "./thread_meeting_match.js";
import { ensureFeaturesLoaded, isFeatureEnabled } from "./shared/features.js";
import { partitionThreadsBySnooze, threadLabel } from "./shared/thread_domain.js";
import {
  addDaysToYmd,
  dayHeadingLabelShort,
  formatTime12InTz,
  formatTimeRange12InTz,
  isoToYmdInZone,
  nextNDaysFromYmd,
  todayYmdInTz,
} from "./shared/time_ui.js";
import { getCurrentData, getCurrentThreads, threadTrackPath } from "./shared/summaries_store.js";
import type { LooseObj, ThreadView } from "./shared/types.js";
import {
  bindMeetingPrepInteractions,
  clearMeetingPrepContexts,
  configureMeetingPrep,
  meetingPrepLinkHtml,
} from "./meeting_prep_ui.js";
import { escapeHtml } from "./shared/utils.js";

export type CalendarDisplayMode = "all" | "open" | "meetings";

const CALENDAR_DISPLAY_KEY = "fivelanes_calendar_display_v1";

type CalendarEntry = {
  kind: "open" | "virtual" | "meeting";
  startMs: number;
  html: string;
};

let weekStartYmd = "";
let dayFilterYmd: string | null = null;
let displayMode: CalendarDisplayMode = "all";
let calendarBound = false;
let lastCalendarTz = "America/New_York";
let markedDates = new Set<string>();

function loadDisplayMode(): CalendarDisplayMode {
  try {
    const saved = localStorage.getItem(CALENDAR_DISPLAY_KEY);
    if (saved === "all" || saved === "open" || saved === "meetings") return saved;
  } catch {
    /* ignore */
  }
  return "all";
}

function saveDisplayMode(mode: CalendarDisplayMode): void {
  try {
    localStorage.setItem(CALENDAR_DISPLAY_KEY, mode);
  } catch {
    /* ignore */
  }
}

function formatWeekLabel(startYmd: string, endYmd: string): string {
  const start = new Date(`${startYmd}T12:00:00`);
  const end = new Date(`${endYmd}T12:00:00`);
  const opts: Intl.DateTimeFormatOptions = { month: "short", day: "numeric" };
  const a = start.toLocaleDateString("en-US", opts);
  const b = end.toLocaleDateString("en-US", { ...opts, year: "numeric" });
  return `${a} – ${b}`;
}

function ensureWeekStart(tz: string): string {
  if (!weekStartYmd) weekStartYmd = todayYmdInTz(tz);
  return weekStartYmd;
}

function trackingThreads(): ThreadView[] {
  const { active, snoozed } = partitionThreadsBySnooze(getCurrentThreads());
  return [...active, ...snoozed];
}

function availabilityRowHtml(item: AgendaItem, tz: string): string | null {
  if (item.layer === "open") {
    const startIso = item.start.toISOString();
    const endIso = item.end.toISOString();
    return `<div class="calendar-row calendar-row--open" data-kind="open">
      <span class="meet-time">${escapeHtml(formatTime12InTz(startIso, tz))}</span>
      <div>
        <div class="calendar-row-title">Open</div>
        <span class="calendar-row-range">${escapeHtml(formatTimeRange12InTz(startIso, endIso, tz))}</span>
      </div>
    </div>`;
  }
  if (item.layer === "child_home") {
    const startIso = item.start.toISOString();
    const endIso = item.end.toISOString();
    return `<div class="calendar-row calendar-row--virtual" data-kind="virtual">
      <span class="meet-time">${escapeHtml(formatTime12InTz(startIso, tz))}</span>
      <div>
        <div class="calendar-row-title">Virtual only</div>
        <span class="calendar-row-range">${escapeHtml(formatTimeRange12InTz(startIso, endIso, tz))}</span>
      </div>
    </div>`;
  }
  return null;
}

function meetingRowHtml(
  meeting: MeetingRow,
  thread: ThreadMatchContext | null,
  tz: string,
  data: LooseObj | null,
): string {
  const startTime = formatTime12InTz(meeting.start_iso, tz);
  const titleInner = escapeHtml(meeting.summary);
  const titleHtml = meeting.html_link
    ? `<a class="meet-title meet-title-link" href="${escapeHtml(meeting.html_link)}" target="_blank" rel="noopener noreferrer">${titleInner}</a>`
    : `<div class="meet-title">${titleInner}</div>`;
  const trackPath = thread ? threadTrackPath(data, thread.threadId) : null;
  let trackHtml = "";
  if (trackPath && thread) {
    trackHtml = meetingPrepLinkHtml(trackPath, meeting, thread);
  } else if (thread) {
    trackHtml = meetingPrepLinkHtml(thread.label, meeting, thread);
  } else if (meeting.location) {
    trackHtml = `<span class="meet-track-link meet-track-link--muted">${escapeHtml(meeting.location)}</span>`;
  }
  return `<div class="meet-row" data-kind="meeting">
    <span class="meet-time">${escapeHtml(startTime)}</span>
    <div>
      ${titleHtml}
      ${trackHtml}
    </div>
  </div>`;
}

function buildDayEntries(
  dateKey: string,
  availability: LooseObj | null,
  meetingsByDate: Map<string, MeetingRow[]>,
  matchByKey: Map<string, ThreadMatchContext>,
  tz: string,
  data: LooseObj | null,
): CalendarEntry[] {
  const entries: CalendarEntry[] = [];

  if (availability && isFeatureEnabled("availability")) {
    for (const item of buildDayAgenda(dateKey, availability)) {
      const html = availabilityRowHtml(item, tz);
      if (!html) continue;
      entries.push({
        kind: item.layer === "child_home" ? "virtual" : "open",
        startMs: item.start.getTime(),
        html,
      });
    }
  }

  for (const meeting of meetingsByDate.get(dateKey) || []) {
    const thread = matchByKey.get(meetingDedupeKey(meeting)) ?? null;
    entries.push({
      kind: "meeting",
      startMs: new Date(meeting.start_iso).getTime(),
      html: meetingRowHtml(meeting, thread, tz, data),
    });
  }

  entries.sort((a, b) => a.startMs - b.startMs);
  return entries;
}

function renderWeekStrip(tz: string): void {
  const strip = document.getElementById("calendar-week-strip");
  const label = document.getElementById("calendar-week-label");
  if (!strip || !label) return;

  const start = ensureWeekStart(tz);
  const days = nextNDaysFromYmd(start, 7);
  const end = days[days.length - 1] || start;
  label.textContent = formatWeekLabel(start, end);

  const today = todayYmdInTz(tz);
  strip.innerHTML = days
    .map((dateKey) => {
      const d = new Date(`${dateKey}T12:00:00`);
      const dow = d.toLocaleDateString("en-US", { weekday: "short" }).toUpperCase();
      const num = d.getDate();
      const classes = ["calendar-day"];
      if (dateKey === today) classes.push("is-today");
      if (dayFilterYmd === dateKey) classes.push("is-selected");
      if (markedDates.has(dateKey)) classes.push("has-events");
      return `<button type="button" class="${classes.join(" ")}" data-date="${escapeHtml(dateKey)}">
        <span class="calendar-day-dow">${escapeHtml(dow)}</span>
        <span class="calendar-day-num">${num}</span>
        <span class="calendar-day-dot" aria-hidden="true"></span>
      </button>`;
    })
    .join("");
}

function applyCalendarFilters(): void {
  document.querySelectorAll("#calendar-agenda .meet-day").forEach((dayEl) => {
    const date = (dayEl as HTMLElement).dataset.date || "";
    const dateOk = !dayFilterYmd || date === dayFilterYmd;
    let hasVisible = false;
    dayEl.querySelectorAll("[data-kind]").forEach((rowEl) => {
      const kind = (rowEl as HTMLElement).dataset.kind || "";
      const modeOk =
        displayMode === "all" ||
        (displayMode === "open" && (kind === "open" || kind === "virtual")) ||
        (displayMode === "meetings" && kind === "meeting");
      const show = dateOk && modeOk;
      rowEl.classList.toggle("is-filtered-out", !show);
      if (show) hasVisible = true;
    });
    dayEl.classList.toggle("is-filtered-out", !dateOk || !hasVisible);
  });

  document.querySelectorAll("[data-calendar-display]").forEach((btn) => {
    btn.classList.toggle(
      "active",
      (btn as HTMLElement).dataset.calendarDisplay === displayMode,
    );
  });

  const note = document.getElementById("calendar-filter-note");
  const dateSpan = document.getElementById("calendar-filter-date");
  if (note && dateSpan) {
    if (dayFilterYmd) {
      note.hidden = false;
      const d = new Date(`${dayFilterYmd}T12:00:00`);
      dateSpan.textContent = d.toLocaleDateString("en-US", {
        weekday: "long",
        month: "short",
        day: "numeric",
      });
    } else {
      note.hidden = true;
    }
  }
}

function renderAgendaHtml(
  tz: string,
  availability: LooseObj | null,
  meetings: MeetingRow[],
  matchByKey: Map<string, ThreadMatchContext>,
): string {
  const data = getCurrentData();
  const start = ensureWeekStart(tz);
  const days = nextNDaysFromYmd(start, 7);
  const meetingsByDate = new Map<string, MeetingRow[]>();
  for (const meeting of meetings) {
    if (!meeting.start_iso) continue;
    const dayKey = isoToYmdInZone(meeting.start_iso, tz);
    if (!meetingsByDate.has(dayKey)) meetingsByDate.set(dayKey, []);
    meetingsByDate.get(dayKey)!.push(meeting);
  }
  for (const rows of meetingsByDate.values()) {
    rows.sort((a, b) => a.start_iso.localeCompare(b.start_iso));
  }

  markedDates = new Set<string>();
  const sections: string[] = [];
  for (const dateKey of days) {
    const entries = buildDayEntries(dateKey, availability, meetingsByDate, matchByKey, tz, data);
    if (!entries.length) continue;
    markedDates.add(dateKey);
    sections.push(`<div class="meet-day" data-date="${escapeHtml(dateKey)}">
      <div class="meet-day-head">${escapeHtml(dayHeadingLabelShort(dateKey))}</div>
      ${entries.map((e) => e.html).join("")}
    </div>`);
  }

  if (!sections.length) {
    return `<p class="calendar-empty">Nothing scheduled this week.</p>`;
  }
  return sections.join("");
}

async function loadAvailabilityData(): Promise<LooseObj | null> {
  await ensureFeaturesLoaded();
  if (!isFeatureEnabled("availability")) return null;
  try {
    const res = await fetch(`/out/availability_calendar_latest.json?cb=${Date.now()}`, {
      credentials: "same-origin",
      cache: "no-store",
    });
    if (!res.ok) return null;
    return (await res.json()) as LooseObj;
  } catch {
    return null;
  }
}

export function bindDashboardCalendarInteractions(): void {
  if (calendarBound) return;
  calendarBound = true;
  displayMode = loadDisplayMode();
  bindMeetingPrepInteractions();

  document.addEventListener("click", (ev) => {
    const target = ev.target as HTMLElement;
    if (!target.closest("#schedule-calendar-view")) return;

    if (target.closest("#calendar-prev-week")) {
      const next = addDaysToYmd(ensureWeekStart(lastCalendarTz), -7);
      if (next) weekStartYmd = next;
      void refreshDashboardCalendarView();
      return;
    }
    if (target.closest("#calendar-next-week")) {
      const next = addDaysToYmd(ensureWeekStart(lastCalendarTz), 7);
      if (next) weekStartYmd = next;
      void refreshDashboardCalendarView();
      return;
    }
    if (target.closest("#calendar-clear-filter")) {
      dayFilterYmd = null;
      renderWeekStrip(lastCalendarTz);
      applyCalendarFilters();
      return;
    }

    const dayBtn = target.closest(".calendar-day") as HTMLButtonElement | null;
    if (dayBtn?.dataset.date) {
      const key = dayBtn.dataset.date;
      dayFilterYmd = dayFilterYmd === key ? null : key;
      renderWeekStrip(lastCalendarTz);
      applyCalendarFilters();
      return;
    }

    const displayBtn = target.closest("[data-calendar-display]") as HTMLButtonElement | null;
    if (displayBtn?.dataset.calendarDisplay) {
      const mode = displayBtn.dataset.calendarDisplay;
      if (mode === "all" || mode === "open" || mode === "meetings") {
        displayMode = mode;
        saveDisplayMode(mode);
        applyCalendarFilters();
      }
    }
  });
}

export async function refreshDashboardCalendarView(opts?: {
  meetingPreps?: LooseObj;
  onMeetingPrepSaved?: (cacheKey: string, prep: LooseObj) => void;
}): Promise<void> {
  const agendaEl = document.getElementById("calendar-agenda");
  if (!agendaEl) return;

  bindDashboardCalendarInteractions();

  const threads = trackingThreads();
  const contexts = buildThreadMatchContexts(threads, threadLabel);

  let meetings: MeetingRow[] = [];
  let tz = Intl.DateTimeFormat().resolvedOptions().timeZone || "America/New_York";

  try {
    const result = await loadMeetings(DASHBOARD_MEETINGS_LOOKAHEAD_DAYS);
    if (!("error" in result)) {
      tz = result.timezone || tz;
      meetings = meetingsInNextDays(result.meetings, DASHBOARD_MEETINGS_LOOKAHEAD_DAYS);
    }
  } catch {
    /* meetings optional */
  }

  const availability = await loadAvailabilityData();
  if (availability) {
    tz = getAvailabilityTimeZone(availability) || tz;
  }

  lastCalendarTz = tz;
  ensureWeekStart(tz);

  const threadsById = new Map(threads.map((t) => [t.id, t]));
  configureMeetingPrep({
    meetingPreps: opts?.meetingPreps,
    onMeetingPrepSaved: opts?.onMeetingPrepSaved,
    getThreadsById: () => threadsById,
    timezone: tz,
  });

  const matchByKey = new Map<string, ThreadMatchContext>();
  for (const meeting of meetings) {
    const thread = findMatchingThread(meeting.attendees, contexts);
    if (thread) matchByKey.set(meetingDedupeKey(meeting), thread);
  }

  clearMeetingPrepContexts();
  agendaEl.innerHTML = renderAgendaHtml(tz, availability, meetings, matchByKey);
  renderWeekStrip(tz);
  applyCalendarFilters();
}

export function calendarViewShellHtml(): string {
  return `
    <div class="calendar-week-nav">
      <button type="button" id="calendar-prev-week" aria-label="Previous week">←</button>
      <span id="calendar-week-label">Loading…</span>
      <button type="button" id="calendar-next-week" aria-label="Next week">→</button>
    </div>
    <div class="calendar-week-strip" id="calendar-week-strip" role="group" aria-label="Week days"></div>
    <div class="calendar-display-bar">
      <div class="thread-segmented" role="group" aria-label="Calendar display">
        <button type="button" data-calendar-display="all">All</button>
        <button type="button" data-calendar-display="open">Open</button>
        <button type="button" data-calendar-display="meetings">Meetings</button>
      </div>
      <ul class="calendar-legend" aria-label="Availability legend">
        <li><i class="swatch-open" aria-hidden="true"></i> Open</li>
        <li><i class="swatch-virtual" aria-hidden="true"></i> Virtual only</li>
        <li><i class="swatch-meeting" aria-hidden="true"></i> Meeting</li>
      </ul>
    </div>
    <p class="calendar-agenda-filter" id="calendar-filter-note" hidden>
      Showing <span id="calendar-filter-date"></span> ·
      <button type="button" id="calendar-clear-filter">Show all</button>
    </p>
    <div class="calendar-agenda" id="calendar-agenda"></div>`;
}
