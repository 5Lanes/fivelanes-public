/**
 * Dashboard: lane overview and upcoming meetings (next 7 days).
 */
import { loadMeetings, meetingDedupeKey, meetingsInNextDays, } from "./meetings_panel.js";
import { buildThreadMatchContexts, findMatchingThread, } from "./thread_meeting_match.js";
import { formatPlanByWhen, sortPlansByDueDate, planLinkedThreadLabel, planDueStatus, planDueStatusClass, planDueBadgeHtml, } from "./shared/plan_helpers.js";
import { renderMentionAwareText } from "./shared/thread_domain.js";
import { dayHeadingLabelLong, formatTimeRangeInTz, isoToYmdInZone, nextNDaysFromYmd, todayYmdLocal, } from "./shared/time_ui.js";
import { ensureAvailabilityDocLoaded } from "./shared/availability_windows.js";
import { escapeHtml } from "./shared/utils.js";
import { bindMeetingPrepInteractions, clearMeetingPrepContexts, configureMeetingPrep, meetingPrepLinkHtml, } from "./meeting_prep_ui.js";
export const DASHBOARD_MEETINGS_LOOKAHEAD_DAYS = 7;
export function renderDashboardPlans(plansEl, plans, labelForThreadId) {
    plansEl.hidden = false;
    if (!plans.length) {
        plansEl.innerHTML =
            '<p class="dashboard-plans-empty">No action plans yet. Add plans from the Plans page.</p>';
        return;
    }
    const sorted = sortPlansByDueDate(plans, (p) => p.by_when, (p) => p.action);
    const rowHtml = (plan) => {
        const when = formatPlanByWhen(plan.by_when);
        const dueStatus = planDueStatus(plan.by_when);
        const dueClass = planDueStatusClass(dueStatus);
        const badge = planDueBadgeHtml(dueStatus);
        const whenHtml = when ? `<span class="dashboard-plan-when">by ${escapeHtml(when)}</span>` : "";
        const threadLabel = escapeHtml(planLinkedThreadLabel(plan.inbox_thread_id, labelForThreadId));
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
export function matchMeetingsToThreads(meetings, contexts) {
    const out = [];
    for (const meeting of meetings) {
        const thread = findMatchingThread(meeting.attendees, contexts);
        if (thread)
            out.push({ meeting, thread });
    }
    out.sort((a, b) => a.meeting.start_iso.localeCompare(b.meeting.start_iso));
    return out;
}
function renderDashboardMeetingRow(meeting, thread, tz) {
    const timeLine = formatTimeRangeInTz(meeting.start_iso, meeting.end_iso, tz);
    const titleHtml = meeting.html_link
        ? `<a href="${escapeHtml(meeting.html_link)}" target="_blank" rel="noopener noreferrer">${escapeHtml(meeting.summary)}</a>`
        : escapeHtml(meeting.summary);
    const detailParts = [];
    if (thread) {
        detailParts.push(meetingPrepLinkHtml(`↔ ${thread.label}`, meeting, thread, "dashboard-meeting-thread-link"));
    }
    if (meeting.location)
        detailParts.push(escapeHtml(meeting.location));
    if (meeting.attendees.length > 0) {
        detailParts.push(`${meeting.attendees.length} attendee${meeting.attendees.length === 1 ? "" : "s"}`);
    }
    const detail = detailParts.join(" · ");
    return `<li class="dash-avail-row dash-avail-row--commit">
    <div class="dash-avail-time">${escapeHtml(timeLine)}</div>
    <div class="dash-avail-body">
      <div class="dash-avail-title">${titleHtml}</div>
      ${detail ? `<div class="dash-avail-detail">${detail}</div>` : ""}
    </div>
  </li>`;
}
function renderDashboardAgendaHtml(meetings, matchByKey, tz, days) {
    const byDate = new Map();
    for (const meeting of meetings) {
        if (!meeting.start_iso)
            continue;
        const key = isoToYmdInZone(meeting.start_iso, tz);
        if (!byDate.has(key))
            byDate.set(key, []);
        byDate.get(key).push(meeting);
    }
    for (const rows of byDate.values()) {
        rows.sort((a, b) => a.start_iso.localeCompare(b.start_iso));
    }
    const start = todayYmdLocal();
    const dayKeys = nextNDaysFromYmd(start, days);
    const sections = [];
    for (const dateKey of dayKeys) {
        const rows = byDate.get(dateKey) || [];
        if (!rows.length)
            continue;
        const inner = rows
            .map((meeting) => {
            const key = meetingDedupeKey(meeting);
            const thread = matchByKey.get(key) ?? null;
            return renderDashboardMeetingRow(meeting, thread, tz);
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
export async function refreshDashboard(threads, opts) {
    await ensureAvailabilityDocLoaded();
    const tracking = threads.filter((t) => {
        const snooze = Number(t.messages[0]?.summary?.snoozed || 0);
        return snooze === 0 || snooze === 1;
    });
    opts.meetingsMetaEl.textContent = "Loading meetings…";
    opts.meetingsAgendaEl.innerHTML = "";
    const contexts = buildThreadMatchContexts(tracking, opts.threadLabel);
    let result;
    try {
        result = await loadMeetings(DASHBOARD_MEETINGS_LOOKAHEAD_DAYS);
    }
    catch (e) {
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
    const matchByKey = new Map();
    const matchedForPrep = [];
    for (const meeting of upcoming) {
        const thread = findMatchingThread(meeting.attendees, contexts);
        if (!thread)
            continue;
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
    const threadsById = new Map(tracking.map((t) => [t.id, t]));
    bindMeetingPrepInteractions();
    configureMeetingPrep({
        meetingPreps: opts.meetingPreps,
        onMeetingPrepSaved: opts.onMeetingPrepSaved,
        getThreadsById: () => threadsById,
        timezone: tz,
    });
    clearMeetingPrepContexts();
    opts.meetingsAgendaEl.innerHTML = renderDashboardAgendaHtml(upcoming, matchByKey, tz, days);
    return {
        meetingCount: upcoming.length,
        linkedMeetingCount: linkedCount,
        trackingThreadCount: tracking.length,
    };
}
