import { refreshDashboard } from "../dashboard_panel.js";
import { renderLanesList } from "./lanes_page.js";
import { partitionThreadsBySnooze, threadLabel } from "../shared/thread_domain.js";
import {
  dashboardPlanEditFormHtml,
  persistPlanDelete,
  persistPlanUpdate,
} from "../shared/plan_helpers.js";
import {
  applyPlanDeleted,
  applyPlanUpdated,
  getCurrentData,
  getCurrentSourceLabel,
  getCurrentThreads,
  getThreadPlans,
  setBundle,
} from "../shared/summaries_store.js";
import type { LooseObj } from "../shared/types.js";
import { str } from "../shared/utils.js";

const PAGE_HTML = `
<div class="view-dashboard">
  <div class="dashboard-top-row">
    <section class="dashboard-plans-section" aria-labelledby="dashboard-plans-heading">
      <h2 id="dashboard-plans-heading" class="dashboard-section-title">Plans</h2>
      <div id="dashboard-plans-list" class="dashboard-plans-list"></div>
    </section>
    <section class="dashboard-meetings-section" aria-labelledby="dashboard-meetings-heading">
      <h2 id="dashboard-meetings-heading" class="dashboard-section-title">Upcoming meetings</h2>
      <p class="dashboard-meetings-meta" id="dashboard-meetings-meta">Loading meetings…</p>
      <div id="dashboard-meetings-agenda" class="meetings-agenda"></div>
    </section>
  </div>
  <section class="dashboard-lanes-section" aria-labelledby="dashboard-lanes-heading">
    <div class="dashboard-lanes-header">
      <h2 id="dashboard-lanes-heading" class="dashboard-section-title">Lanes</h2>
      <div class="lanes-toolbar dashboard-lanes-toolbar">
        <button type="button" class="create-lane-btn" id="create-lane-btn">Create lane</button>
      </div>
    </div>
    <form class="create-lane-form" id="create-lane-form" hidden>
      <input type="text" name="lane-name" id="lane-name-input" placeholder="Lane name" required />
      <button type="submit">Create</button>
      <button type="button" class="create-lane-cancel" id="create-lane-cancel">Cancel</button>
    </form>
    <div id="lanes-list" class="lanes-list dashboard-lanes-list"></div>
  </section>
</div>`;

let interactionsBound = false;

export function mountDashboardPage(root: HTMLElement): void {
  root.innerHTML = PAGE_HTML;
}

export async function renderDashboardPage(): Promise<void> {
  const data = getCurrentData();
  if (!data) return;

  const plansEl = document.getElementById("dashboard-plans-list") as HTMLDivElement | null;
  const meetingsMetaEl = document.getElementById("dashboard-meetings-meta") as HTMLParagraphElement | null;
  const meetingsAgendaEl = document.getElementById("dashboard-meetings-agenda") as HTMLDivElement | null;
  if (!plansEl || !meetingsMetaEl || !meetingsAgendaEl) return;

  const { active, snoozed } = partitionThreadsBySnooze(getCurrentThreads());
  const trackingThreads = [...active, ...snoozed];
  const meetingPreps = (data.meeting_preps || {}) as LooseObj;
  const plans = getThreadPlans(data);

  await refreshDashboard(
    trackingThreads,
    {
      plansEl,
      meetingsMetaEl,
      meetingsAgendaEl,
      threadLabel,
      plans,
      meetingPreps,
      onMeetingPrepSaved: (cacheKey, prep) => {
        const current = getCurrentData();
        if (!current) return;
        const bucket = (current.meeting_preps ||= {}) as LooseObj;
        bucket[cacheKey] = prep;
      },
    },
  );

  renderLanesList();
}

function closeDashboardPlanEdit(row: HTMLElement): void {
  row.querySelector(".dashboard-plan-edit-form")?.remove();
  row.classList.remove("is-editing");
  row.querySelector(".dashboard-plan-view")?.removeAttribute("hidden");
}

function openDashboardPlanEdit(row: HTMLElement): void {
  document.querySelectorAll<HTMLElement>(".dashboard-plan-row.is-editing").forEach((other) => {
    if (other !== row) closeDashboardPlanEdit(other);
  });
  const planId = Number(row.dataset.planId) || 0;
  if (!planId) return;
  closeDashboardPlanEdit(row);
  row.classList.add("is-editing");
  row.querySelector(".dashboard-plan-view")?.setAttribute("hidden", "");
  const view = row.querySelector(".dashboard-plan-view");
  view?.insertAdjacentHTML(
    "afterend",
    dashboardPlanEditFormHtml({
      planId,
      action: str(row.dataset.planAction),
      stepType: str(row.dataset.planStepType) || "follow up needed",
      byWhen: str(row.dataset.planByWhen),
    }),
  );
  row.querySelector<HTMLInputElement>(".dashboard-plan-edit-action")?.focus();
}

function reloadDashboard(): void {
  const data = getCurrentData();
  if (data) {
    setBundle(data, getCurrentSourceLabel());
    void renderDashboardPage();
  }
}

export function bindDashboardInteractions(): void {
  if (interactionsBound) return;
  interactionsBound = true;

  document.addEventListener("click", (ev) => {
    const target = ev.target as HTMLElement | null;
    if (!target?.closest(".view-dashboard")) return;

    const editBtn = target.closest(".dashboard-plan-edit-btn") as HTMLButtonElement | null;
    if (editBtn) {
      const row = editBtn.closest(".dashboard-plan-row") as HTMLElement | null;
      if (row) openDashboardPlanEdit(row);
      return;
    }

    const editCancel = target.closest(".dashboard-plan-edit-cancel") as HTMLButtonElement | null;
    if (editCancel) {
      const row = editCancel.closest(".dashboard-plan-row") as HTMLElement | null;
      if (row) closeDashboardPlanEdit(row);
      return;
    }

    const removeBtn = target.closest(".dashboard-plan-remove-btn") as HTMLButtonElement | null;
    if (removeBtn) {
      const planId = Number(removeBtn.dataset.planId) || 0;
      if (!planId) return;
      applyPlanDeleted(planId);
      reloadDashboard();
      void persistPlanDelete(planId).catch((err) => console.error(err));
      return;
    }
  });

  document.addEventListener("submit", (ev) => {
    const form = (ev.target as HTMLElement | null)?.closest(".dashboard-plan-edit-form");
    if (!form || !form.closest(".view-dashboard")) return;
    ev.preventDefault();
    void (async () => {
      const planId = Number((form as HTMLElement).dataset.planId) || 0;
      const row = form.closest(".dashboard-plan-row");
      const threadId = str(row?.getAttribute("data-thread-id"));
      const action = form.querySelector<HTMLInputElement>(".dashboard-plan-edit-action")?.value.trim() ?? "";
      const stepType =
        form.querySelector<HTMLSelectElement>(".dashboard-plan-edit-type")?.value.trim() ||
        "follow up needed";
      const byWhen = form.querySelector<HTMLInputElement>(".dashboard-plan-edit-when")?.value.trim() ?? "";
      if (!planId || !threadId || !action) return;
      try {
        const plan = await persistPlanUpdate(planId, threadId, action, stepType, byWhen);
        applyPlanUpdated(plan);
        reloadDashboard();
      } catch (err) {
        console.error(err);
      }
    })();
  });
}
