/**
 * Dashboard schedule rail: Calendar (availability + meetings + prep) and Plans tabs.
 */
import { bindDashboardCalendarInteractions, calendarViewShellHtml, refreshDashboardCalendarView, } from "./dashboard_calendar_view.js";
import { formatPlanByWhen, planDueBadgeHtml, planDueStatus, planDueStatusClass, planEditFormHtml, persistPlanCreate, persistPlanDelete, sortPlansByDueDate, } from "./shared/plan_helpers.js";
import { applyPlanCreated, applyPlanDeleted, getCurrentData, getCurrentThreads, getThreadPlans, threadTrackPath, } from "./shared/summaries_store.js";
import { partitionThreadsBySnooze, threadLabel } from "./shared/thread_domain.js";
import { escapeHtml } from "./shared/utils.js";
export { DASHBOARD_MEETINGS_LOOKAHEAD_DAYS } from "./dashboard_panel.js";
let scheduleView = "calendar";
let scheduleBound = false;
function trackingThreads() {
    const { active, snoozed } = partitionThreadsBySnooze(getCurrentThreads());
    return [...active, ...snoozed];
}
function ensureRailShell(rail) {
    if (rail.dataset.mounted === "1")
        return;
    rail.innerHTML = `
    <div class="thread-segmented schedule-tabs" role="tablist" aria-label="Schedule view">
      <button type="button" class="active" data-schedule-view="calendar" role="tab">Calendar</button>
      <button type="button" data-schedule-view="plans" role="tab">Plans</button>
    </div>
    <div class="schedule-view" id="schedule-calendar-view" data-schedule-view="calendar">${calendarViewShellHtml()}</div>
    <div class="schedule-view" id="schedule-plans-view" data-schedule-view="plans" hidden>
      <div class="plans-rail-toolbar">
        <button type="button" class="btn btn--primary" id="schedule-add-plan-btn">Add plan</button>
      </div>
      <form class="add-plan-form" id="schedule-add-plan-form" hidden>
        <label class="add-plan-field"><span>Thread</span><select id="schedule-plan-thread-select" required></select></label>
        <label class="add-plan-field"><span>Next step</span><input type="text" id="schedule-plan-action-input" required /></label>
        <label class="add-plan-field"><span>Type</span>
          <select id="schedule-plan-type-select">
            <option value="follow up needed">Follow up</option>
            <option value="response required">Response required</option>
          </select>
        </label>
        <label class="add-plan-field"><span>By when</span><input type="date" id="schedule-plan-by-when-input" class="plan-by-when-input" /></label>
        <div class="add-plan-form-actions">
          <button type="submit">Add plan</button>
          <button type="button" class="add-plan-cancel" id="schedule-add-plan-cancel">Cancel</button>
        </div>
      </form>
      <ul class="plans-rail-list" id="schedule-plans-list"></ul>
    </div>`;
    rail.dataset.mounted = "1";
    bindScheduleRailInteractions(rail);
    bindDashboardCalendarInteractions();
}
function setScheduleView(view) {
    scheduleView = view;
    const cal = document.getElementById("schedule-calendar-view");
    const plans = document.getElementById("schedule-plans-view");
    cal?.toggleAttribute("hidden", view !== "calendar");
    plans?.toggleAttribute("hidden", view !== "plans");
    document.querySelectorAll(".schedule-tabs [data-schedule-view]").forEach((btn) => {
        btn.classList.toggle("active", btn.dataset.scheduleView === view);
    });
}
export function showScheduleTab(view) {
    setScheduleView(view);
    void rerenderScheduleRail();
}
function planRailCardHtml(plan, data) {
    const dueStatus = planDueStatus(plan.by_when);
    const badge = planDueBadgeHtml(dueStatus);
    const when = formatPlanByWhen(plan.by_when);
    const trackPath = threadTrackPath(data, plan.inbox_thread_id);
    const thread = getCurrentThreads().find((t) => t.id === plan.inbox_thread_id);
    const threadName = thread ? threadLabel(thread) : plan.inbox_thread_id;
    const pathHtml = trackPath
        ? escapeHtml(trackPath)
        : escapeHtml(threadName);
    return `<li>
    <article class="plan-card-rail ${planDueStatusClass(dueStatus)}" data-plan-id="${plan.id}">
      <h3>${badge}${escapeHtml(plan.action)}</h3>
      <a class="plan-thread-link" href="/dashboard?thread=${encodeURIComponent(plan.inbox_thread_id)}">${pathHtml}</a>
      <p class="plan-meta">${escapeHtml(plan.step_type)}${when ? ` · by ${escapeHtml(when)}` : ""}</p>
      <div class="plan-card-actions">
        <button type="button" class="plan-edit-btn" data-plan-id="${plan.id}">Edit</button>
        <button type="button" class="plan-delete-btn" data-plan-id="${plan.id}">Remove</button>
      </div>
    </article>
  </li>`;
}
function renderPlansRail() {
    const list = document.getElementById("schedule-plans-list");
    const data = getCurrentData();
    if (!list || !data)
        return;
    const plans = sortPlansByDueDate(getThreadPlans(data), (p) => p.by_when, (p) => p.action);
    if (!plans.length) {
        list.innerHTML = `<li><p class="dashboard-plans-empty">No action plans yet.</p></li>`;
        return;
    }
    list.innerHTML = plans.map((p) => planRailCardHtml(p, data)).join("");
}
function populateSchedulePlanThreadSelect() {
    const select = document.getElementById("schedule-plan-thread-select");
    if (!select)
        return;
    const threads = trackingThreads();
    select.innerHTML = threads.length
        ? threads.map((t) => `<option value="${escapeHtml(t.id)}">${escapeHtml(threadLabel(t))}</option>`).join("")
        : `<option value="">No threads</option>`;
}
function bindScheduleRailInteractions(rail) {
    if (scheduleBound)
        return;
    scheduleBound = true;
    rail.addEventListener("click", (ev) => {
        const target = ev.target;
        const tab = target.closest("[data-schedule-view]");
        if (tab?.closest(".schedule-tabs")) {
            const view = tab.dataset.scheduleView;
            if (view) {
                setScheduleView(view);
                void rerenderScheduleRail();
            }
            return;
        }
        if (target.closest("#schedule-add-plan-btn")) {
            document.getElementById("schedule-add-plan-form")?.removeAttribute("hidden");
            populateSchedulePlanThreadSelect();
            return;
        }
        if (target.closest("#schedule-add-plan-cancel")) {
            document.getElementById("schedule-add-plan-form")?.setAttribute("hidden", "");
            return;
        }
        const editBtn = target.closest(".plan-edit-btn");
        if (editBtn) {
            const planId = Number(editBtn.dataset.planId) || 0;
            const plan = getThreadPlans(getCurrentData()).find((p) => p.id === planId);
            const card = editBtn.closest(".plan-card-rail");
            if (!plan || !card)
                return;
            card.insertAdjacentHTML("beforeend", planEditFormHtml({
                planId,
                threadSelectOptions: trackingThreads()
                    .map((t) => `<option value="${escapeHtml(t.id)}"${t.id === plan.inbox_thread_id ? " selected" : ""}>${escapeHtml(threadLabel(t))}</option>`)
                    .join(""),
                action: plan.action,
                stepType: plan.step_type,
                byWhen: plan.by_when,
            }));
            return;
        }
        const delBtn = target.closest(".plan-delete-btn");
        if (delBtn) {
            const planId = Number(delBtn.dataset.planId) || 0;
            const removed = getThreadPlans(getCurrentData()).find((p) => p.id === planId);
            if (!planId || !removed)
                return;
            applyPlanDeleted(planId);
            renderPlansRail();
            void persistPlanDelete(planId).catch(() => {
                applyPlanCreated(removed);
                renderPlansRail();
            });
        }
    });
    document.addEventListener("submit", (ev) => {
        const form = ev.target?.closest("#schedule-add-plan-form");
        if (!form)
            return;
        ev.preventDefault();
        void (async () => {
            const threadId = document.getElementById("schedule-plan-thread-select")?.value.trim();
            const action = document.getElementById("schedule-plan-action-input")?.value.trim();
            const stepType = document.getElementById("schedule-plan-type-select")?.value.trim() ||
                "follow up needed";
            const byWhen = document.getElementById("schedule-plan-by-when-input")?.value.trim();
            if (!threadId || !action)
                return;
            const plan = await persistPlanCreate(threadId, action, stepType, byWhen);
            applyPlanCreated(plan);
            form.setAttribute("hidden", "");
            renderPlansRail();
        })();
    });
}
async function rerenderScheduleRail() {
    const data = getCurrentData();
    if (!data)
        return;
    if (scheduleView === "plans") {
        renderPlansRail();
        return;
    }
    await refreshDashboardCalendarView();
}
export async function refreshDashboardScheduleRail(threads, opts) {
    const rail = document.getElementById("dashboard-schedule-rail");
    if (!rail)
        return;
    ensureRailShell(rail);
    void threads;
    void opts;
    await rerenderScheduleRail();
}
