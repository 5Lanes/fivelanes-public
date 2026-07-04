import {
  partitionPlansByDueStatus,
  planLinkedThreadLabel,
} from "./plan_helpers.js";
import { getCurrentData, getCurrentThreads, getThreadPlans } from "./summaries_store.js";
import { threadLabel } from "./thread_domain.js";
import type { PlanView } from "./types.js";
import { escapeHtml } from "./utils.js";

const BANNER_ID = "plan-notifications-banner";
const SESSION_NOTIFY_KEY = "fivelanes_plan_desktop_notified_v1";
const SESSION_BANNER_DISMISS_KEY = "fivelanes_plan_banner_dismissed_v1";

function labelForPlanThread(threadId: string): string {
  const thread = getCurrentThreads().find((t) => t.id === threadId);
  return planLinkedThreadLabel(threadId, (id) => (thread && id === threadId ? threadLabel(thread) : "(Unknown thread)"));
}

function bannerSummary(overdue: PlanView[], dueToday: PlanView[]): string {
  const parts: string[] = [];
  if (overdue.length) {
    parts.push(`${overdue.length} overdue plan${overdue.length === 1 ? "" : "s"}`);
  }
  if (dueToday.length) {
    parts.push(`${dueToday.length} due today`);
  }
  return parts.join(", ");
}

function planListHtml(plans: PlanView[], heading: string): string {
  if (!plans.length) return "";
  const items = plans
    .map((plan) => {
      const thread = escapeHtml(labelForPlanThread(plan.inbox_thread_id));
      const action = escapeHtml(plan.action);
      return `<li><a href="/plans" class="plan-notification-link">${action}</a> <span class="plan-notification-thread">${thread}</span></li>`;
    })
    .join("");
  return `<div class="plan-notifications-group">
    <h3 class="plan-notifications-subhead">${escapeHtml(heading)}</h3>
    <ul class="plan-notifications-list">${items}</ul>
  </div>`;
}

function renderBanner(overdue: PlanView[], dueToday: PlanView[]): void {
  const onDashboard = location.pathname.replace(/\/+$/, "") === "/dashboard";
  const existing = document.getElementById(BANNER_ID);
  if (onDashboard) {
    existing?.remove();
    return;
  }

  const appBody = document.querySelector(".app-body");
  if (!appBody) return;

  if (!overdue.length && !dueToday.length) {
    existing?.remove();
    sessionStorage.removeItem(SESSION_BANNER_DISMISS_KEY);
    return;
  }

  const fingerprint = [...overdue, ...dueToday]
    .map((plan) => `${plan.id}:${plan.by_when}`)
    .sort()
    .join("|");
  if (sessionStorage.getItem(SESSION_BANNER_DISMISS_KEY) === fingerprint) {
    existing?.remove();
    return;
  }
  if (sessionStorage.getItem(SESSION_BANNER_DISMISS_KEY) && sessionStorage.getItem(SESSION_BANNER_DISMISS_KEY) !== fingerprint) {
    sessionStorage.removeItem(SESSION_BANNER_DISMISS_KEY);
  }

  const banner =
    existing ?? (() => {
      const el = document.createElement("aside");
      el.id = BANNER_ID;
      el.className = "plan-notifications-banner";
      el.setAttribute("role", "status");
      el.setAttribute("aria-live", "polite");
      appBody.insertBefore(el, appBody.firstChild);
      return el;
    })();

  const toneClass = overdue.length ? "plan-notifications-banner--overdue" : "plan-notifications-banner--due-today";
  banner.className = `plan-notifications-banner ${toneClass}`;

  const enableBtn =
    typeof Notification !== "undefined" && Notification.permission === "default"
      ? `<button type="button" class="plan-notifications-enable-btn">Enable desktop alerts</button>`
      : "";

  banner.innerHTML = `<div class="plan-notifications-head">
    <p class="plan-notifications-title">${escapeHtml(bannerSummary(overdue, dueToday))}</p>
    <div class="plan-notifications-actions">
      ${enableBtn}
      <a href="/dashboard#schedule-plans" class="plan-notifications-view-link">View plans</a>
      <button type="button" class="plan-notifications-dismiss" aria-label="Dismiss">×</button>
    </div>
  </div>
  ${planListHtml(overdue, "Overdue")}
  ${planListHtml(dueToday, "Due today")}`;

  banner.querySelector(".plan-notifications-dismiss")?.addEventListener("click", () => {
    sessionStorage.setItem(SESSION_BANNER_DISMISS_KEY, fingerprint);
    banner.remove();
  });

  banner.querySelector(".plan-notifications-enable-btn")?.addEventListener("click", () => {
    void Notification.requestPermission().then((result) => {
      if (result === "granted") {
        showDesktopPlanNotification(overdue, dueToday, { force: true });
        banner.querySelector(".plan-notifications-enable-btn")?.remove();
      }
    });
  });
}

function updateNavBadge(overdue: number, dueToday: number): void {
  const link = document.querySelector<HTMLAnchorElement>('.app-nav-link[data-route="plans"]');
  if (!link) return;

  let badge = link.querySelector<HTMLSpanElement>(".nav-plan-badge");
  const total = overdue + dueToday;
  if (!total) {
    badge?.remove();
    return;
  }

  if (!badge) {
    badge = document.createElement("span");
    badge.className = "nav-plan-badge";
    link.appendChild(badge);
  }

  badge.textContent = String(total);
  badge.classList.toggle("nav-plan-badge--overdue", overdue > 0);
  badge.title =
    overdue > 0 && dueToday > 0
      ? `${overdue} overdue, ${dueToday} due today`
      : overdue > 0
        ? `${overdue} overdue plan${overdue === 1 ? "" : "s"}`
        : `${dueToday} plan${dueToday === 1 ? "" : "s"} due today`;
}

function showDesktopPlanNotification(
  overdue: PlanView[],
  dueToday: PlanView[],
  opts: { force?: boolean } = {},
): void {
  if (typeof Notification === "undefined" || Notification.permission !== "granted") return;
  if (!overdue.length && !dueToday.length) return;

  const fingerprint = [...overdue, ...dueToday]
    .map((plan) => `${plan.id}:${plan.by_when}`)
    .sort()
    .join("|");
  if (!opts.force && sessionStorage.getItem(SESSION_NOTIFY_KEY) === fingerprint) return;
  sessionStorage.setItem(SESSION_NOTIFY_KEY, fingerprint);

  const bodyParts: string[] = [];
  for (const plan of overdue.slice(0, 3)) {
    bodyParts.push(`Overdue: ${plan.action}`);
  }
  for (const plan of dueToday.slice(0, Math.max(0, 3 - overdue.length))) {
    bodyParts.push(`Due today: ${plan.action}`);
  }
  const hidden = overdue.length + dueToday.length - bodyParts.length;
  if (hidden > 0) bodyParts.push(`+${hidden} more`);

  const title =
    overdue.length && dueToday.length
      ? `${overdue.length} overdue, ${dueToday.length} due today`
      : overdue.length
        ? `${overdue.length} overdue plan${overdue.length === 1 ? "" : "s"}`
        : `${dueToday.length} plan${dueToday.length === 1 ? "" : "s"} due today`;

  const notification = new Notification("Fivelanes plans", {
    body: bodyParts.join("\n"),
    tag: "fivelanes-plan-due",
  });
  notification.onclick = () => {
    window.focus();
    if (location.pathname !== "/plans") location.href = "/plans";
    notification.close();
  };
}

export function refreshPlanNotifications(): void {
  const data = getCurrentData();
  if (!data) return;

  const { overdue, dueToday } = partitionPlansByDueStatus(getThreadPlans(data));
  updateNavBadge(overdue.length, dueToday.length);
  renderBanner(overdue, dueToday);
  showDesktopPlanNotification(overdue, dueToday);
}
