/**
 * Dashboard: lane overview and upcoming meetings (next 7 days).
 */

import {
  loadMeetings,
  meetingDedupeKey,
  meetingsInNextDays,
  type MeetingRow,
} from "./meetings_panel.js";
import {
  buildThreadMatchContexts,
  findMatchingThread,
  type ThreadMatchContext,
} from "./thread_meeting_match.js";
import {
  formatPlanByWhen,
  sortPlansByDueDate,
  planLinkedThreadLabel,
  planDueStatus,
  planDueStatusClass,
  planDueBadgeHtml,
} from "./shared/plan_helpers.js";
import type { PlanView } from "./shared/types.js";
import { renderMentionAwareText } from "./shared/thread_domain.js";
import {
  dayHeadingLabelLong,
  formatTimeRangeInTz,
  isoToYmdInZone,
  nextNDaysFromYmd,
  todayYmdLocal,
} from "./shared/time_ui.js";
import { ensureAvailabilityDocLoaded } from "./shared/availability_windows.js";
import { isFeatureEnabled } from "./shared/features.js";
import { escapeHtml, threadPageHref } from "./shared/utils.js";

type LooseObj = Record<string, unknown>;

export const DASHBOARD_MEETINGS_LOOKAHEAD_DAYS = 7;

export interface ThreadView {
  id: string;
  messages: Array<{ cleaned: LooseObj | null; summary: LooseObj | null }>;
}

export interface MatchedMeeting {
  meeting: MeetingRow;
  thread: ThreadMatchContext;
}

export function renderDashboardPlans(
  plansEl: HTMLElement,
  plans: PlanView[],
  labelForThreadId: (threadId: string) => string,
): void {
  plansEl.hidden = false;
  if (!plans.length) {
    plansEl.innerHTML =
      '<p class="dashboard-plans-empty">No action plans yet. Add plans from the Plans page.</p>';
    return;
  }
  const sorted = sortPlansByDueDate(plans, (p) => p.by_when, (p) => p.action);

  const rowHtml = (plan: PlanView) => {
    const when = formatPlanByWhen(plan.by_when);
    const dueStatus = planDueStatus(plan.by_when);
    const dueClass = planDueStatusClass(dueStatus);
    const badge = planDueBadgeHtml(dueStatus);
    const whenHtml = when ? `<span class="dashboard-plan-when">by ${escapeHtml(when)}</span>` : "";
    const threadLabel = escapeHtml(
      planLinkedThreadLabel(plan.inbox_thread_id, labelForThreadId),
    );
    return `<li class="dashboard-plan-row${dueClass ? ` ${dueClass}` : ""}" data-plan-id="${plan.id}" data-thread-id="${escapeHtml(plan.inbox_thread_id)}" data-plan-action="${escapeHtml(plan.action)}" data-plan-step-type="${escapeHtml(plan.step_type)}" data-plan-by-when="${escapeHtml(plan.by_when)}">
        <div class="dashboard-plan-view">
          <div class="dashboard-plan-main">
            ${badge}
            <span class="dashboard-plan-action">${renderMentionAwareText(plan.action)}</span>
            ${whenHtml}
          </div>
          <span class="dashboard-plan-thread">${threadLabel}</span>
          <div class="dashboard-plan-row-actions">
            <button type="button" class="dashboard-plan-edit-btn" data-plan-id="${plan.id}">Edit</button>
            <button type="button" class="dashboard-plan-remove-btn" data-plan-id="${plan.id}">Remove</button>
          </div>
        </div>
      </li>`;
  };

  plansEl.innerHTML = `<ul class="dashboard-plans-column-list">${sorted.map((p) => rowHtml(p)).join("")}</ul>`;
}

export function matchMeetingsToThreads(
  meetings: MeetingRow[],
  contexts: ThreadMatchContext[],
): MatchedMeeting[] {
  const out: MatchedMeeting[] = [];
  for (const meeting of meetings) {
    const thread = findMatchingThread(meeting.attendees, contexts);
    if (thread) out.push({ meeting, thread });
  }
  out.sort((a, b) => a.meeting.start_iso.localeCompare(b.meeting.start_iso));
  return out;
}

function strField(v: unknown): string {
  return typeof v === "string" ? v : "";
}

function threadMessagesForPrep(
  thread: ThreadView,
): Array<{ datetime: string; sender: string; recipients: string; content: string }> {
  return thread.messages.map((row) => {
    const c = (row.cleaned || {}) as LooseObj;
    const s = (row.summary || {}) as LooseObj;
    const content = strField(c.cleaned_content) || strField(c.raw_text) || "";
    return {
      datetime: strField(c.datetime || s.datetime),
      sender: strField(c.sender || c.forwarded_from),
      recipients: strField(c.recipients),
      content,
    };
  });
}

export function meetingPrepCacheKey(meeting: MeetingRow, threadId: string): string {
  return `${meetingDedupeKey(meeting)}|${threadId}`;
}

function prepFieldsFromPayload(payload: LooseObj): LooseObj {
  return {
    prep_summary: payload.prep_summary,
    talking_points: payload.talking_points,
    open_loops: payload.open_loops,
    suggested_opener: payload.suggested_opener,
    open_questions: payload.open_questions,
  };
}

function formatPrepPayloadHtml(payload: LooseObj): string {
  const summary = strField(payload.prep_summary).trim();
  const opener = strField(payload.suggested_opener).trim();
  const points = Array.isArray(payload.talking_points)
    ? payload.talking_points.map((x) => strField(x).trim()).filter(Boolean)
    : [];
  const loops = Array.isArray(payload.open_loops)
    ? payload.open_loops.map((x) => strField(x).trim()).filter(Boolean)
    : [];
  const questions = Array.isArray(payload.open_questions)
    ? payload.open_questions.map((x) => strField(x).trim()).filter(Boolean)
    : [];
  const chunks: string[] = [];
  if (summary) chunks.push(`<p class="meeting-prep-summary">${escapeHtml(summary)}</p>`);
  if (opener) {
    chunks.push(
      `<p class="meeting-prep-opener"><strong>Suggested opener:</strong> ${escapeHtml(opener)}</p>`,
    );
  }
  if (points.length) {
    chunks.push(
      `<h4 class="meeting-prep-subhead">Talking points</h4><ul>${points.map((p) => `<li>${escapeHtml(p)}</li>`).join("")}</ul>`,
    );
  }
  if (loops.length) {
    chunks.push(
      `<h4 class="meeting-prep-subhead">Open loops</h4><ul>${loops.map((p) => `<li>${escapeHtml(p)}</li>`).join("")}</ul>`,
    );
  }
  if (questions.length) {
    chunks.push(
      `<h4 class="meeting-prep-subhead">Check before the meeting</h4><ul>${questions.map((p) => `<li>${escapeHtml(p)}</li>`).join("")}</ul>`,
    );
  }
  return chunks.length
    ? chunks.join("")
    : `<p class="meeting-prep-error">No prep content returned.</p>`;
}

async function requestMeetingPrep(
  thread: ThreadView,
  meeting: MeetingRow,
  threadLabelText: string,
): Promise<LooseObj> {
  const res = await fetch("/api/meeting-prep", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      thread_id: thread.id,
      thread_label: threadLabelText,
      meeting_title: meeting.summary,
      meeting_start: meeting.start_iso,
      meeting_end: meeting.end_iso,
      meeting_location: meeting.location,
      meeting_attendees: meeting.attendees.join(", "),
      messages: threadMessagesForPrep(thread),
    }),
  });
  const data = (await res.json()) as LooseObj;
  if (!res.ok || data.ok === false) {
    throw new Error(strField(data.error) || `Request failed (${res.status})`);
  }
  return data;
}

function renderDashboardMeetingRow(
  meeting: MeetingRow,
  thread: ThreadMatchContext | null,
  tz: string,
  prepIndex: number | null,
): string {
  const timeLine = formatTimeRangeInTz(meeting.start_iso, meeting.end_iso, tz);
  const titleHtml = meeting.html_link
    ? `<a href="${escapeHtml(meeting.html_link)}" target="_blank" rel="noopener noreferrer">${escapeHtml(meeting.summary)}</a>`
    : escapeHtml(meeting.summary);
  const detailParts: string[] = [];
  if (thread) {
    detailParts.push(
      `<a class="dashboard-meeting-thread-link" href="${escapeHtml(threadPageHref(thread.threadId))}">↔ ${escapeHtml(thread.label)}</a>`,
    );
  }
  if (meeting.location) detailParts.push(escapeHtml(meeting.location));
  if (meeting.attendees.length > 0) {
    detailParts.push(
      `${meeting.attendees.length} attendee${meeting.attendees.length === 1 ? "" : "s"}`,
    );
  }
  const detail = detailParts.join(" · ");
  const prepClass = prepIndex !== null ? " meeting-prep-row" : "";
  const prepData = prepIndex !== null ? ` data-prep-row="${prepIndex}"` : "";
  const prepActions =
    prepIndex !== null
      ? `<div class="meeting-prep-actions">
        <button type="button" class="meeting-prep-btn" data-prep-btn="${prepIndex}">Prepare</button>
      </div>
      <div class="meeting-prep-panel" id="meeting-prep-panel-${prepIndex}" hidden></div>`
      : "";
  return `<li class="dash-avail-row dash-avail-row--commit${prepClass}"${prepData}>
    <div class="dash-avail-time">${escapeHtml(timeLine)}</div>
    <div class="dash-avail-body">
      <div class="dash-avail-title">${titleHtml}</div>
      ${detail ? `<div class="dash-avail-detail">${detail}</div>` : ""}
      ${prepActions}
    </div>
  </li>`;
}

function renderDashboardAgendaHtml(
  meetings: MeetingRow[],
  matchByKey: Map<string, ThreadMatchContext>,
  matchedForPrep: MatchedMeeting[],
  tz: string,
  days: number,
): string {
  const byDate = new Map<string, MeetingRow[]>();
  for (const meeting of meetings) {
    if (!meeting.start_iso) continue;
    const key = isoToYmdInZone(meeting.start_iso, tz);
    if (!byDate.has(key)) byDate.set(key, []);
    byDate.get(key)!.push(meeting);
  }
  for (const rows of byDate.values()) {
    rows.sort((a, b) => a.start_iso.localeCompare(b.start_iso));
  }

  const prepIndexByKey = new Map<string, number>();
  matchedForPrep.forEach((m, i) => prepIndexByKey.set(meetingDedupeKey(m.meeting), i));

  const start = todayYmdLocal();
  const dayKeys = nextNDaysFromYmd(start, days);
  const sections: string[] = [];
  for (const dateKey of dayKeys) {
    const rows = byDate.get(dateKey) || [];
    if (!rows.length) continue;
    const inner = rows
      .map((meeting) => {
        const key = meetingDedupeKey(meeting);
        const thread = matchByKey.get(key) ?? null;
        const prepIndex = prepIndexByKey.has(key) ? prepIndexByKey.get(key)! : null;
        return renderDashboardMeetingRow(meeting, thread, tz, prepIndex);
      })
      .join("");
    sections.push(`<section class="dash-avail-day" data-date="${escapeHtml(dateKey)}">
    <header class="dash-avail-day-head">
      <div class="dash-avail-day-name">${escapeHtml(dayHeadingLabelLong(dateKey))}</div>
      <div class="dash-avail-day-ymd">${escapeHtml(dateKey)}</div>
    </header>
    <ul class="dash-avail-list">${inner}</ul>
  </section>`);
  }
  if (!sections.length) {
    return `<p class="dash-avail-error">No meetings in the next ${days} days.</p>`;
  }
  return `<div class="dash-avail-agenda">${sections.join("")}</div>`;
}

function bindMeetingPrepHandlers(
  agendaEl: HTMLElement,
  matched: MatchedMeeting[],
  threadsById: Map<string, ThreadView>,
  meetingPreps: LooseObj,
  onPrepSaved?: (cacheKey: string, prep: LooseObj) => void,
): void {
  for (const btn of Array.from(agendaEl.querySelectorAll<HTMLButtonElement>("[data-prep-btn]"))) {
    const idx = Number(btn.dataset.prepBtn);
    if (!Number.isFinite(idx) || idx < 0 || idx >= matched.length) continue;
    const entry = matched[idx];
    const panel = agendaEl.querySelector(`#meeting-prep-panel-${idx}`) as HTMLElement | null;
    if (!panel) continue;

    const cacheKey = meetingPrepCacheKey(entry.meeting, entry.thread.threadId);
    const cached = meetingPreps[cacheKey] as LooseObj | undefined;
    if (cached && typeof cached === "object") {
      panel.hidden = false;
      panel.innerHTML = formatPrepPayloadHtml(cached);
      btn.textContent = "Refresh prep";
    }

    btn.addEventListener("click", async () => {
      const thread = threadsById.get(entry.thread.threadId);
      if (!thread) {
        panel.hidden = false;
        panel.innerHTML = `<p class="meeting-prep-error">Thread not found in loaded bundle.</p>`;
        return;
      }
      btn.disabled = true;
      panel.hidden = false;
      panel.innerHTML = `<p class="meeting-prep-loading">Preparing from email thread…</p>`;
      try {
        const payload = await requestMeetingPrep(thread, entry.meeting, entry.thread.label);
        const prep = prepFieldsFromPayload(payload);
        panel.innerHTML = formatPrepPayloadHtml(prep);
        meetingPreps[cacheKey] = prep;
        onPrepSaved?.(cacheKey, prep);
        btn.textContent = "Refresh prep";
      } catch (e) {
        panel.innerHTML = `<p class="meeting-prep-error">${escapeHtml(e instanceof Error ? e.message : String(e))}</p>`;
      } finally {
        btn.disabled = false;
      }
    });
  }
}

export async function refreshDashboard(
  threads: ThreadView[],
  opts: {
    meetingsMetaEl: HTMLElement;
    meetingsAgendaEl: HTMLElement;
    threadLabel: (t: ThreadView) => string;
    meetingPreps?: LooseObj;
    onMeetingPrepSaved?: (cacheKey: string, prep: LooseObj) => void;
  },
): Promise<{ meetingCount: number; linkedMeetingCount: number; trackingThreadCount: number }> {
  if (isFeatureEnabled("availability")) {
    await ensureAvailabilityDocLoaded();
  }
  const tracking = threads.filter((t) => {
    const snooze = Number(t.messages[0]?.summary?.snoozed || 0);
    return snooze === 0 || snooze === 1;
  });

  opts.meetingsMetaEl.textContent = "Loading meetings…";
  opts.meetingsAgendaEl.innerHTML = "";

  const contexts = buildThreadMatchContexts(tracking, opts.threadLabel);
  let result: Awaited<ReturnType<typeof loadMeetings>>;
  try {
    result = await loadMeetings(DASHBOARD_MEETINGS_LOOKAHEAD_DAYS);
  } catch (e) {
    opts.meetingsMetaEl.textContent = `Meetings: ${e instanceof Error ? e.message : String(e)}`;
    return { meetingCount: 0, linkedMeetingCount: 0, trackingThreadCount: tracking.length };
  }

  if ("error" in result) {
    opts.meetingsMetaEl.textContent = `Meetings: ${result.error}`;
    return { meetingCount: 0, linkedMeetingCount: 0, trackingThreadCount: tracking.length };
  }

  const { timezone: tz } = result;
  const days = DASHBOARD_MEETINGS_LOOKAHEAD_DAYS;
  const upcoming = meetingsInNextDays(result.meetings, days);
  const matchByKey = new Map<string, ThreadMatchContext>();
  const matchedForPrep: MatchedMeeting[] = [];
  for (const meeting of upcoming) {
    const thread = findMatchingThread(meeting.attendees, contexts);
    if (!thread) continue;
    matchByKey.set(meetingDedupeKey(meeting), thread);
    matchedForPrep.push({ meeting, thread });
  }

  const linkedCount = matchedForPrep.length;
  opts.meetingsMetaEl.textContent =
    upcoming.length === 0
      ? `No meetings in the next ${days} days (${tz}).`
      : linkedCount === 0
        ? `${upcoming.length} meeting${upcoming.length === 1 ? "" : "s"} in the next ${days} days (${tz}).`
        : `${upcoming.length} meeting${upcoming.length === 1 ? "" : "s"} in the next ${days} days (${tz}); ${linkedCount} linked to threads.`;

  opts.meetingsAgendaEl.innerHTML = renderDashboardAgendaHtml(
    upcoming,
    matchByKey,
    matchedForPrep,
    tz,
    days,
  );
  const threadsById = new Map(tracking.map((t) => [t.id, t]));
  const meetingPreps = opts.meetingPreps && typeof opts.meetingPreps === "object" ? opts.meetingPreps : {};
  bindMeetingPrepHandlers(
    opts.meetingsAgendaEl,
    matchedForPrep,
    threadsById,
    meetingPreps,
    opts.onMeetingPrepSaved,
  );

  return {
    meetingCount: upcoming.length,
    linkedMeetingCount: linkedCount,
    trackingThreadCount: tracking.length,
  };
}
