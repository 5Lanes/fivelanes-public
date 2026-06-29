import { persistPlanCompletionCheck, planLinkedThreadLabel, plansNeedingCompletionCheck, } from "./plan_helpers.js";
import { refreshPlanNotifications } from "./plan_notifications.js";
import { applyPlanCompletionAcknowledged, applyPlanCompletionDismissed, applyPlanDeleted, getCurrentData, getCurrentSourceLabel, getCurrentThreads, getThreadPlans, setBundle, } from "./summaries_store.js";
import { threadLabel } from "./thread_domain.js";
import { escapeHtml } from "./utils.js";
const BANNER_ID = "plan-completion-banner";
function labelForPlanThread(threadId) {
    const thread = getCurrentThreads().find((t) => t.id === threadId);
    return planLinkedThreadLabel(threadId, (id) => thread && id === threadId ? threadLabel(thread) : "(Unknown thread)");
}
export function planCompletionPromptHtml(plan) {
    const thread = escapeHtml(labelForPlanThread(plan.inbox_thread_id));
    const action = escapeHtml(plan.action);
    return `<div class="plan-completion-prompt" data-plan-id="${plan.id}">
    <p class="plan-completion-prompt-text">New email activity in <strong>${thread}</strong>. Did you complete this plan?</p>
    <p class="plan-completion-prompt-action">${action}</p>
    <div class="plan-completion-prompt-actions">
      <button type="button" class="plan-completion-yes" data-plan-id="${plan.id}">Yes, completed</button>
      <button type="button" class="plan-completion-no" data-plan-id="${plan.id}">Not yet</button>
    </div>
  </div>`;
}
export function threadPlanCompletionPromptsHtml(threadId) {
    const plans = plansNeedingCompletionCheck(getThreadPlans(getCurrentData())).filter((plan) => plan.inbox_thread_id === threadId);
    if (!plans.length)
        return "";
    return plans.map(planCompletionPromptHtml).join("");
}
function renderCompletionBanner(plans) {
    const appBody = document.querySelector(".app-body");
    if (!appBody)
        return;
    const existing = document.getElementById(BANNER_ID);
    if (!plans.length) {
        existing?.remove();
        return;
    }
    const banner = existing ?? (() => {
        const el = document.createElement("aside");
        el.id = BANNER_ID;
        el.className = "plan-completion-banner";
        el.setAttribute("role", "status");
        el.setAttribute("aria-live", "polite");
        appBody.insertBefore(el, appBody.firstChild);
        return el;
    })();
    const items = plans
        .map((plan) => {
        const thread = escapeHtml(labelForPlanThread(plan.inbox_thread_id));
        const action = escapeHtml(plan.action);
        return `<li class="plan-completion-banner-item" data-plan-id="${plan.id}">
        <span class="plan-completion-banner-copy"><strong>${action}</strong> <span class="plan-completion-banner-thread">${thread}</span></span>
        <span class="plan-completion-banner-actions">
          <button type="button" class="plan-completion-yes" data-plan-id="${plan.id}">Yes</button>
          <button type="button" class="plan-completion-no" data-plan-id="${plan.id}">Not yet</button>
        </span>
      </li>`;
    })
        .join("");
    const count = plans.length;
    banner.innerHTML = `<div class="plan-completion-head">
    <p class="plan-completion-title">${count} plan${count === 1 ? "" : "s"} with new email activity</p>
    <a href="/plans" class="plan-completion-view-link">View plans</a>
  </div>
  <ul class="plan-completion-list">${items}</ul>`;
}
export function refreshPlanCompletionPrompts() {
    const data = getCurrentData();
    if (!data)
        return;
    renderCompletionBanner(plansNeedingCompletionCheck(getThreadPlans(data)));
}
function rerenderAfterCompletionChange() {
    const data = getCurrentData();
    if (!data)
        return;
    setBundle(data, getCurrentSourceLabel());
    refreshPlanCompletionPrompts();
    refreshPlanNotifications();
    document.dispatchEvent(new CustomEvent("fivelanes:plans-changed"));
}
export async function handlePlanCompletionResponse(planId, completed) {
    if (planId <= 0)
        return;
    if (completed) {
        applyPlanDeleted(planId);
    }
    else {
        applyPlanCompletionDismissed(planId);
    }
    rerenderAfterCompletionChange();
    try {
        const updated = await persistPlanCompletionCheck(planId, completed);
        if (!completed && updated) {
            applyPlanCompletionAcknowledged(updated);
            rerenderAfterCompletionChange();
        }
    }
    catch (err) {
        console.error(err);
    }
}
let interactionsBound = false;
export function bindPlanCompletionInteractions() {
    if (interactionsBound)
        return;
    interactionsBound = true;
    document.addEventListener("click", (ev) => {
        const target = ev.target;
        if (!target)
            return;
        const yesBtn = target.closest(".plan-completion-yes");
        if (yesBtn) {
            const planId = Number(yesBtn.dataset.planId) || 0;
            if (!planId)
                return;
            void handlePlanCompletionResponse(planId, true);
            return;
        }
        const noBtn = target.closest(".plan-completion-no");
        if (noBtn) {
            const planId = Number(noBtn.dataset.planId) || 0;
            if (!planId)
                return;
            void handlePlanCompletionResponse(planId, false);
        }
    });
}
