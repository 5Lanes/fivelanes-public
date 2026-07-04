import { partitionPlansByDueStatus, } from "./shared/plan_helpers.js";
import { getCurrentData, getCurrentThreads, getThreadPlans, threadTrackPath, } from "./shared/summaries_store.js";
import { pendingMessageCountForThread, threadLabel, } from "./shared/thread_domain.js";
import { sourcePillHtml, threadChannelForThread } from "./shared/source_ui.js";
import { escapeHtml } from "./shared/utils.js";
const DISMISS_KEY = "fivelanes_dashboard_status_banner_dismiss";
function pendingSinceRefreshCount() {
    const data = getCurrentData();
    if (!data)
        return 0;
    let total = 0;
    for (const thread of getCurrentThreads()) {
        total += pendingMessageCountForThread(thread, data);
    }
    return total;
}
function newMessagesHtml() {
    const count = pendingSinceRefreshCount();
    if (!count)
        return "";
    const runStamp = String(getCurrentData()?.run_stamp || getCurrentData()?.generated_at || "last refresh");
    return `<span class="dashboard-status-banner-messages"><strong>${count} new message${count === 1 ? "" : "s"}</strong> since ${escapeHtml(runStamp.slice(0, 16))}</span>`;
}
export function refreshDashboardStatusBanner() {
    const wrap = document.getElementById("dashboard-status-banner-wrap");
    if (!wrap)
        return;
    const plans = getThreadPlans(getCurrentData());
    const { overdue, dueToday } = partitionPlansByDueStatus(plans);
    const pending = pendingSinceRefreshCount();
    if (!overdue.length && !dueToday.length && !pending) {
        wrap.hidden = true;
        wrap.innerHTML = "";
        return;
    }
    const fingerprint = [
        ...overdue.map((p) => `o${p.id}`),
        ...dueToday.map((p) => `d${p.id}`),
        `p${pending}`,
    ].join("|");
    if (sessionStorage.getItem(DISMISS_KEY) === fingerprint) {
        wrap.hidden = true;
        return;
    }
    const data = getCurrentData();
    const summaryParts = [];
    if (dueToday.length)
        summaryParts.push(`<strong>${dueToday.length} plan${dueToday.length === 1 ? "" : "s"} due today</strong>`);
    if (overdue.length)
        summaryParts.push(`<strong>${overdue.length} overdue</strong>`);
    const msgHtml = newMessagesHtml();
    const sep = summaryParts.length && msgHtml ? `<span class="dashboard-status-banner-sep" aria-hidden="true">·</span>` : "";
    const dueItems = [...overdue, ...dueToday]
        .slice(0, 8)
        .map((plan) => {
        const path = data ? threadTrackPath(data, plan.inbox_thread_id) : null;
        const ctx = path ? `<span class="dashboard-status-banner-context">${escapeHtml(path)}</span>` : "";
        return `<li><a href="/dashboard?thread=${encodeURIComponent(plan.inbox_thread_id)}" class="dashboard-status-banner-link">${escapeHtml(plan.action)}</a> ${ctx}</li>`;
    })
        .join("");
    const newThreads = getCurrentThreads()
        .filter((t) => pendingMessageCountForThread(t, data) > 0)
        .slice(0, 6)
        .map((t) => {
        const ch = threadChannelForThread(t);
        return `<li><a href="/dashboard?thread=${encodeURIComponent(t.id)}" class="dashboard-status-banner-link">${escapeHtml(threadLabel(t))}</a> ${sourcePillHtml(ch)}</li>`;
    })
        .join("");
    wrap.hidden = false;
    wrap.innerHTML = `<aside class="dashboard-status-banner dashboard-status-banner--due-today" role="status" aria-live="polite">
    <div class="dashboard-status-banner-head">
      <p class="dashboard-status-banner-summary">${summaryParts.join("")}${sep}${msgHtml}</p>
      <div class="dashboard-status-banner-actions">
        <button type="button" class="dashboard-status-banner-btn" id="status-banner-plans-btn">View plans</button>
        <button type="button" class="dashboard-status-banner-btn" id="status-banner-threads-btn">View threads</button>
        <button type="button" class="dashboard-status-banner-dismiss" id="status-banner-dismiss" aria-label="Dismiss">×</button>
      </div>
    </div>
    ${dueItems ? `<div class="dashboard-status-banner-group"><h3 class="dashboard-status-banner-subhead">Due</h3><ul class="dashboard-status-banner-list">${dueItems}</ul></div>` : ""}
    ${newThreads ? `<div class="dashboard-status-banner-group"><h3 class="dashboard-status-banner-subhead">New since refresh</h3><ul class="dashboard-status-banner-list">${newThreads}</ul></div>` : ""}
  </aside>`;
    document.getElementById("status-banner-dismiss")?.addEventListener("click", () => {
        sessionStorage.setItem(DISMISS_KEY, fingerprint);
        wrap.hidden = true;
    });
    document.getElementById("status-banner-plans-btn")?.addEventListener("click", () => {
        import("./dashboard_schedule_rail.js").then((m) => m.showScheduleTab("plans"));
        document.getElementById("dashboard-schedule-rail")?.scrollIntoView({ behavior: "smooth" });
    });
    document.getElementById("status-banner-threads-btn")?.addEventListener("click", () => {
        document.getElementById("dashboard-threads")?.scrollIntoView({ behavior: "smooth" });
    });
}
