import { refreshDashboardScheduleRail } from "../dashboard_schedule_rail.js";
import { refreshDashboardStatusBanner } from "../dashboard_status_banner.js";
import { renderDashboardThreadsSection } from "../dashboard_threads_section.js";
import { mountDashboardPage as mountShell } from "./dashboard_page_shell.js";
import { renderLanesList, syncLaneSummaryJobsFromServer } from "./lanes_page.js";
import { partitionThreadsBySnooze, threadLabel } from "../shared/thread_domain.js";
import { refreshPlanNotifications } from "../shared/plan_notifications.js";
import {
  getCurrentData,
  getCurrentThreads,
} from "../shared/summaries_store.js";
import type { LooseObj } from "../shared/types.js";

export function mountDashboardPage(root: HTMLElement): void {
  mountShell(root);
}

export async function applyDashboardLocationHash(): Promise<void> {
  const hash = location.hash.replace(/^#/, "").trim();
  if (!hash) return;
  if (hash === "schedule" || hash === "schedule-calendar") {
    const { showScheduleTab } = await import("../dashboard_schedule_rail.js");
    showScheduleTab("calendar");
    document.getElementById("dashboard-schedule-rail")?.scrollIntoView({ behavior: "smooth" });
    return;
  }
  if (hash === "schedule-plans") {
    const { showScheduleTab } = await import("../dashboard_schedule_rail.js");
    showScheduleTab("plans");
    document.getElementById("dashboard-schedule-rail")?.scrollIntoView({ behavior: "smooth" });
    return;
  }
  if (hash === "lanes") {
    document.getElementById("dashboard-lanes")?.scrollIntoView({ behavior: "smooth" });
  }
}

export async function renderDashboardPage(): Promise<void> {
  const data = getCurrentData();
  if (!data) return;

  const { active, snoozed } = partitionThreadsBySnooze(getCurrentThreads());
  const trackingThreads = [...active, ...snoozed];
  const meetingPreps = (data.meeting_preps || {}) as LooseObj;

  refreshDashboardStatusBanner();
  await refreshDashboardScheduleRail(trackingThreads, {
    threadLabel,
    meetingPreps,
    onMeetingPrepSaved: (cacheKey, prep) => {
      const current = getCurrentData();
      if (!current) return;
      const bucket = (current.meeting_preps ||= {}) as LooseObj;
      bucket[cacheKey] = prep;
    },
  });

  await syncLaneSummaryJobsFromServer();
  renderLanesList();
  renderDashboardThreadsSection();
  refreshPlanNotifications();
  focusDashboardThreadFromQuery();
  await applyDashboardLocationHash();
}

export function focusDashboardThreadFromQuery(): void {
  const params = new URLSearchParams(location.search);
  const threadId = params.get("thread")?.trim();
  if (!threadId) return;
  const el = document.getElementById(`thread-${threadId}`);
  if (!el) return;
  el.scrollIntoView({ behavior: "smooth", block: "start" });
  el.classList.add("is-focused");
  setTimeout(() => el.classList.remove("is-focused"), 2000);
}

export function bindDashboardInteractions(): void {
  // Lane interactions via bindLanesInteractions; threads/schedule via their modules.
}
