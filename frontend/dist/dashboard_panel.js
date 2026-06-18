/**
 * Dashboard: response required, follow-ups (active + snoozed threads), and
 * upcoming meetings that match those threads by attendee email overlap.
 */
import { MEETINGS_LOOKAHEAD_DAYS, loadMeetings, meetingDedupeKey, } from "./meetings_panel.js";
import { buildThreadMatchContexts, findMatchingThread, } from "./thread_meeting_match.js";
import { formatPlanByWhen, sortPlansByDueDate, planExistsForStep, } from "./shared/plan_helpers.js";
import { ownerNextStepsForThread } from "./shared/thread_domain.js";
function escapeHtml(s) {
    return s
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}
function isoToYmdInZone(iso, timeZone) {
    return new Intl.DateTimeFormat("en-CA", {
        timeZone,
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
    }).format(new Date(iso));
}
function formatTimeRangeInTz(startIso, endIso, timeZone) {
    const opts = {
        timeZone,
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
    };
    const fmt = new Intl.DateTimeFormat("en-GB", opts);
    const start = new Date(startIso);
    const end = endIso ? new Date(endIso) : start;
    return `${fmt.format(start)}–${fmt.format(end)}`;
}
function dayHeadingLabel(dateKey) {
    const d = new Date(`${dateKey}T12:00:00`);
    return d.toLocaleDateString(undefined, { weekday: "long" });
}
function todayYmdLocal() {
    const now = new Date();
    const yy = now.getFullYear();
    const mm = String(now.getMonth() + 1).padStart(2, "0");
    const dd = String(now.getDate()).padStart(2, "0");
    return `${yy}-${mm}-${dd}`;
}
function nextNDaysFromYmd(startYmd, n) {
    const out = [];
    const [y, m, d] = startYmd.split("-").map(Number);
    const cur = new Date(y, m - 1, d);
    for (let i = 0; i < n; i += 1) {
        const yy = cur.getFullYear();
        const mm = String(cur.getMonth() + 1).padStart(2, "0");
        const dd = String(cur.getDate()).padStart(2, "0");
        out.push(`${yy}-${mm}-${dd}`);
        cur.setDate(cur.getDate() + 1);
    }
    return out;
}
function threadNavLabel(t, labelForThread) {
    const base = labelForThread(t);
    const snooze = Number(t.messages[0]?.summary?.snoozed || 0);
    if (snooze === 1)
        return `${base} (snoozed)`;
    return base;
}
function normalizeStepType(step) {
    return step.type === "follow up needed" ? "follow up needed" : "response required";
}
function suggestedStepLiHtml(threadId, step) {
    return `<li class="dashboard-suggested-step" data-thread-id="${escapeHtml(threadId)}" data-step-type="${escapeHtml(normalizeStepType(step))}" data-step-action="${escapeHtml(step.action)}">
    <span class="suggested-step-action">${escapeHtml(step.action)}</span>
    <span class="suggested-step-schedule">
      <input type="date" class="suggested-step-date" aria-label="Due date for plan" />
      <button type="button" class="add-suggested-plan-btn" disabled>Add plan</button>
    </span>
  </li>`;
}
function laneSuggestedStepsHtml(threads, stepsForThread, existingPlans, labelForThread) {
    const chunks = [];
    for (const t of threads) {
        const steps = stepsForThread(t).filter((step) => !planExistsForStep(existingPlans, t.id, step.action));
        if (!steps.length)
            continue;
        const title = escapeHtml(threadNavLabel(t, labelForThread));
        const inner = steps.map((step) => suggestedStepLiHtml(t.id, step)).join("");
        chunks.push(`<li class="lane-thread-group"><details class="lane-thread-details" open><summary class="lane-thread-title">${title}</summary><ul class="lane-thread-items lane-thread-items--suggested">${inner}</ul></details></li>`);
    }
    return chunks.length ? `<ul class="lane-threads">${chunks.join("")}</ul>` : "";
}
export function renderDashboardPlans(plansEl, plans, labelForThreadId) {
    plansEl.hidden = false;
    if (!plans.length) {
        plansEl.innerHTML =
            '<p class="dashboard-plans-empty">No action plans yet. Choose a due date on a suggested step below, then click <strong>Add plan</strong>.</p>';
        return;
    }
    const sorted = sortPlansByDueDate(plans, (p) => p.by_when, (p) => p.action);
    const rowHtml = (plan) => {
        const when = formatPlanByWhen(plan.by_when);
        const whenHtml = when ? `<span class="dashboard-plan-when">by ${escapeHtml(when)}</span>` : "";
        const threadLabel = escapeHtml(labelForThreadId(plan.inbox_thread_id));
        return `<li class="dashboard-plan-row" data-plan-id="${plan.id}" data-thread-id="${escapeHtml(plan.inbox_thread_id)}" data-plan-action="${escapeHtml(plan.action)}" data-plan-step-type="${escapeHtml(plan.step_type)}" data-plan-by-when="${escapeHtml(plan.by_when)}">
        <div class="dashboard-plan-view">
          <div class="dashboard-plan-main">
            <span class="dashboard-plan-action">${escapeHtml(plan.action)}</span>
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
const SUGGESTED_STEPS_EMPTY = "No suggested next steps on active or snoozed threads (or they are already action plans).";
export function renderDashboardLanes(responseLaneEl, followUpLaneEl, threads, opts) {
    responseLaneEl.innerHTML = "";
    followUpLaneEl.innerHTML = "";
    const responseSteps = (t) => ownerNextStepsForThread(t).filter((step) => step.type !== "follow up needed");
    const followUpSteps = (t) => ownerNextStepsForThread(t).filter((step) => step.type === "follow up needed");
    const todosHtml = laneSuggestedStepsHtml(threads, responseSteps, opts.plans, opts.threadLabel);
    const followUpsHtml = laneSuggestedStepsHtml(threads, followUpSteps, opts.plans, opts.threadLabel);
    if (todosHtml) {
        responseLaneEl.hidden = false;
        responseLaneEl.innerHTML = `<div class="lane to-do">
      <h2>Response required</h2>
      ${todosHtml}
    </div>`;
    }
    else {
        responseLaneEl.hidden = false;
        responseLaneEl.innerHTML = `<div class="lane to-do">
      <h2>Response required</h2>
      <p class="empty-state">${SUGGESTED_STEPS_EMPTY}</p>
    </div>`;
    }
    if (followUpsHtml) {
        followUpLaneEl.hidden = false;
        followUpLaneEl.innerHTML = `<div class="lane follow-up">
      <h2>Follow up needed</h2>
      ${followUpsHtml}
    </div>`;
    }
    else {
        followUpLaneEl.hidden = true;
    }
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
function strField(v) {
    return typeof v === "string" ? v : "";
}
function threadMessagesForPrep(thread) {
    return thread.messages.map((row) => {
        const c = (row.cleaned || {});
        const s = (row.summary || {});
        const content = strField(c.cleaned_content) || strField(c.raw_text) || "";
        return {
            datetime: strField(c.datetime || s.datetime),
            sender: strField(c.sender || c.forwarded_from),
            recipients: strField(c.recipients),
            content,
        };
    });
}
export function meetingPrepCacheKey(meeting, threadId) {
    return `${meetingDedupeKey(meeting)}|${threadId}`;
}
function prepFieldsFromPayload(payload) {
    return {
        prep_summary: payload.prep_summary,
        talking_points: payload.talking_points,
        open_loops: payload.open_loops,
        suggested_opener: payload.suggested_opener,
        open_questions: payload.open_questions,
    };
}
function formatPrepPayloadHtml(payload) {
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
    const chunks = [];
    if (summary)
        chunks.push(`<p class="meeting-prep-summary">${escapeHtml(summary)}</p>`);
    if (opener) {
        chunks.push(`<p class="meeting-prep-opener"><strong>Suggested opener:</strong> ${escapeHtml(opener)}</p>`);
    }
    if (points.length) {
        chunks.push(`<h4 class="meeting-prep-subhead">Talking points</h4><ul>${points.map((p) => `<li>${escapeHtml(p)}</li>`).join("")}</ul>`);
    }
    if (loops.length) {
        chunks.push(`<h4 class="meeting-prep-subhead">Open loops</h4><ul>${loops.map((p) => `<li>${escapeHtml(p)}</li>`).join("")}</ul>`);
    }
    if (questions.length) {
        chunks.push(`<h4 class="meeting-prep-subhead">Check before the meeting</h4><ul>${questions.map((p) => `<li>${escapeHtml(p)}</li>`).join("")}</ul>`);
    }
    return chunks.length
        ? chunks.join("")
        : `<p class="meeting-prep-error">No prep content returned.</p>`;
}
async function requestMeetingPrep(thread, meeting, threadLabelText) {
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
    const data = (await res.json());
    if (!res.ok || data.ok === false) {
        throw new Error(strField(data.error) || `Request failed (${res.status})`);
    }
    return data;
}
function renderMatchedMeetingRow(m, tz, rowIndex) {
    const { meeting, thread } = m;
    const timeLine = formatTimeRangeInTz(meeting.start_iso, meeting.end_iso, tz);
    const parts = [];
    const snoozeNote = thread.snoozed === 1 ? "snoozed thread" : "active thread";
    parts.push(`↔ ${thread.label} (${snoozeNote})`);
    if (meeting.location)
        parts.push(meeting.location);
    if (meeting.attendees.length > 0) {
        parts.push(`${meeting.attendees.length} attendee${meeting.attendees.length === 1 ? "" : "s"}`);
    }
    const detail = parts.join(" · ");
    const titleHtml = meeting.html_link
        ? `<a href="${escapeHtml(meeting.html_link)}" target="_blank" rel="noopener noreferrer">${escapeHtml(meeting.summary)}</a>`
        : escapeHtml(meeting.summary);
    return `<li class="dash-avail-row dash-avail-row--commit meeting-prep-row" data-prep-row="${rowIndex}">
    <div class="dash-avail-time">${escapeHtml(timeLine)}</div>
    <div class="dash-avail-body">
      <div class="dash-avail-title">${titleHtml}</div>
      <div class="dash-avail-detail">${escapeHtml(detail)}</div>
      <div class="meeting-prep-actions">
        <button type="button" class="meeting-prep-btn" data-prep-btn="${rowIndex}">Prepare</button>
      </div>
      <div class="meeting-prep-panel" id="meeting-prep-panel-${rowIndex}" hidden></div>
    </div>
  </li>`;
}
function bindMeetingPrepHandlers(agendaEl, matched, threadsById, meetingPreps, onPrepSaved) {
    for (const btn of Array.from(agendaEl.querySelectorAll("[data-prep-btn]"))) {
        const idx = Number(btn.dataset.prepBtn);
        if (!Number.isFinite(idx) || idx < 0 || idx >= matched.length)
            continue;
        const entry = matched[idx];
        const panel = agendaEl.querySelector(`#meeting-prep-panel-${idx}`);
        if (!panel)
            continue;
        const cacheKey = meetingPrepCacheKey(entry.meeting, entry.thread.threadId);
        const cached = meetingPreps[cacheKey];
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
            }
            catch (e) {
                panel.innerHTML = `<p class="meeting-prep-error">${escapeHtml(e instanceof Error ? e.message : String(e))}</p>`;
            }
            finally {
                btn.disabled = false;
            }
        });
    }
}
function renderMatchedAgendaHtml(matched, tz, days) {
    const byDate = new Map();
    for (const m of matched) {
        if (!m.meeting.start_iso)
            continue;
        const key = isoToYmdInZone(m.meeting.start_iso, tz);
        if (!byDate.has(key))
            byDate.set(key, []);
        byDate.get(key).push(m);
    }
    for (const rows of byDate.values()) {
        rows.sort((a, b) => a.meeting.start_iso.localeCompare(b.meeting.start_iso));
    }
    const start = todayYmdLocal();
    const dayKeys = nextNDaysFromYmd(start, days);
    const sections = [];
    for (const dateKey of dayKeys) {
        const rows = byDate.get(dateKey) || [];
        if (!rows.length)
            continue;
        const inner = rows
            .map((m, i) => renderMatchedMeetingRow(m, tz, matched.indexOf(m) >= 0 ? matched.indexOf(m) : i))
            .join("");
        sections.push(`<section class="dash-avail-day" data-date="${escapeHtml(dateKey)}">
    <header class="dash-avail-day-head">
      <div class="dash-avail-day-name">${escapeHtml(dayHeadingLabel(dateKey))}</div>
      <div class="dash-avail-day-ymd">${escapeHtml(dateKey)}</div>
    </header>
    <ul class="dash-avail-list">${inner}</ul>
  </section>`);
    }
    if (!sections.length) {
        return `<p class="dash-avail-error">No upcoming meetings in the next ${days} days match active or snoozed threads.</p>`;
    }
    return `<div class="dash-avail-agenda">${sections.join("")}</div>`;
}
export async function refreshDashboard(threads, opts) {
    const tracking = threads.filter((t) => {
        const snooze = Number(t.messages[0]?.summary?.snoozed || 0);
        return snooze === 0 || snooze === 1;
    });
    renderDashboardPlans(opts.plansEl, opts.plans, (threadId) => {
        const thread = tracking.find((t) => t.id === threadId);
        return thread ? opts.threadLabel(thread) : "(Unknown thread)";
    });
    renderDashboardLanes(opts.responseLaneEl, opts.followUpLaneEl, tracking, {
        threadLabel: opts.threadLabel,
        plans: opts.plans,
    });
    opts.meetingsMetaEl.textContent = "Loading meetings…";
    opts.meetingsAgendaEl.innerHTML = "";
    const contexts = buildThreadMatchContexts(tracking, opts.threadLabel);
    let result;
    try {
        result = await loadMeetings(MEETINGS_LOOKAHEAD_DAYS);
    }
    catch (e) {
        opts.meetingsMetaEl.textContent = `Meetings: ${e instanceof Error ? e.message : String(e)}`;
        return { matchedMeetingCount: 0, trackingThreadCount: tracking.length, planCount: opts.plans.length };
    }
    if ("error" in result) {
        opts.meetingsMetaEl.textContent = `Meetings: ${result.error}`;
        return { matchedMeetingCount: 0, trackingThreadCount: tracking.length, planCount: opts.plans.length };
    }
    const matched = matchMeetingsToThreads(result.meetings, contexts);
    const { timezone: tz, days } = result;
    opts.meetingsMetaEl.textContent =
        matched.length === 0
            ? `No meetings in the next ${days} days (${tz}) overlap active or snoozed thread contacts.`
            : `${matched.length} meeting${matched.length === 1 ? "" : "s"} in the next ${days} days (${tz}) match ${tracking.length} active or snoozed thread${tracking.length === 1 ? "" : "s"}.`;
    opts.meetingsAgendaEl.innerHTML = renderMatchedAgendaHtml(matched, tz, days);
    const threadsById = new Map(tracking.map((t) => [t.id, t]));
    const meetingPreps = opts.meetingPreps && typeof opts.meetingPreps === "object" ? opts.meetingPreps : {};
    bindMeetingPrepHandlers(opts.meetingsAgendaEl, matched, threadsById, meetingPreps, opts.onMeetingPrepSaved);
    return {
        matchedMeetingCount: matched.length,
        trackingThreadCount: tracking.length,
        planCount: opts.plans.length,
    };
}
