import {
  partitionPlansByDueStatus,
} from "./shared/plan_helpers.js";
import {
  getCurrentData,
  getCurrentThreads,
  getThreadPlans,
  threadTrackPath,
} from "./shared/summaries_store.js";
import {
  newSinceRefreshCountForThread,
  threadLabel,
} from "./shared/thread_domain.js";
import { sourcePillHtml, threadChannelForThread } from "./shared/source_ui.js";
import { fetchPipelineStatus, lastPipelineRefreshTime } from "./pipeline_run_meta.js";
import { escapeHtml } from "./shared/utils.js";

const DISMISS_KEY = "fivelanes_dashboard_status_banner_dismiss";
const BANNER_SEP = `<span class="dashboard-status-banner-sep" aria-hidden="true">·</span>`;

function pendingSinceRefreshCount(): number {
  const data = getCurrentData();
  if (!data) return 0;
  let total = 0;
  for (const thread of getCurrentThreads()) {
    total += newSinceRefreshCountForThread(thread, data);
  }
  return total;
}

function newMessagesHtml(refreshAt: string | null, pipelineRunning: boolean): string {
  const count = pendingSinceRefreshCount();
  if (!count) return "";
  const sincePart = refreshAt
    ? `since last refresh at ${escapeHtml(refreshAt)}`
    : pipelineRunning
      ? "awaiting pipeline"
      : "since last refresh";
  return `<span class="dashboard-status-banner-messages"><strong>${count} new message${count === 1 ? "" : "s"}</strong> ${sincePart}</span>`;
}

function joinBannerSummaryParts(parts: string[]): string {
  return parts.map((part) => `<span>${part}</span>`).join(BANNER_SEP);
}

export async function refreshDashboardStatusBanner(): Promise<void> {
  const wrap = document.getElementById("dashboard-status-banner-wrap");
  if (!wrap) return;

  const pipelineStatus = await fetchPipelineStatus();
  const refreshAt = pipelineStatus ? lastPipelineRefreshTime(pipelineStatus) : null;
  const pipelineRunning = Boolean(pipelineStatus?.running);

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
  const summaryParts: string[] = [];
  if (dueToday.length) summaryParts.push(`<strong>${dueToday.length} plan${dueToday.length === 1 ? "" : "s"} due today</strong>`);
  if (overdue.length) summaryParts.push(`<strong>${overdue.length} overdue</strong>`);
  const summaryHtml = joinBannerSummaryParts(summaryParts);
  const msgHtml = newMessagesHtml(refreshAt, pipelineRunning);
  const sep = summaryHtml && msgHtml ? BANNER_SEP : "";

  const dueItems = [...overdue, ...dueToday]
    .slice(0, 8)
    .map((plan) => {
      const path = data ? threadTrackPath(data, plan.inbox_thread_id) : null;
      const ctx = path ? `<span class="dashboard-status-banner-context">${escapeHtml(path)}</span>` : "";
      return `<li><a href="/dashboard?thread=${encodeURIComponent(plan.inbox_thread_id)}" class="dashboard-status-banner-link">${escapeHtml(plan.action)}</a> ${ctx}</li>`;
    })
    .join("");

  const newThreads = getCurrentThreads()
    .filter((t) => newSinceRefreshCountForThread(t, data) > 0)
    .slice(0, 6)
    .map((t) => {
      const ch = threadChannelForThread(t);
      return `<li><a href="/dashboard?thread=${encodeURIComponent(t.id)}" class="dashboard-status-banner-link">${escapeHtml(threadLabel(t))}</a> ${sourcePillHtml(ch)}</li>`;
    })
    .join("");

  wrap.hidden = false;
  wrap.innerHTML = `<aside class="dashboard-status-banner dashboard-status-banner--due-today" role="status" aria-live="polite">
    <div class="dashboard-status-banner-head">
      <p class="dashboard-status-banner-summary">${summaryHtml}${sep}${msgHtml}</p>
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
