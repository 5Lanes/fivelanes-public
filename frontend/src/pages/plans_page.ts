import {
  formatPlanByWhen,
  sortPlansByDueDate,
  persistPlanCreate,
  persistPlanDelete,
  persistPlanUpdate,
  planEditFormHtml,
} from "../shared/plan_helpers.js";
import {
  applyPlanCreated,
  applyPlanDeleted,
  applyPlanUpdated,
  applySavedThreadDraft,
  getCurrentData,
  getCurrentSourceLabel,
  getCurrentThreads,
  getThreadPlans,
  setBundle,
} from "../shared/summaries_store.js";
import {
  formatDraftReplyMarkdown,
  partitionThreadsBySnooze,
  threadLabel,
  threadMessagesForReply,
} from "../shared/thread_domain.js";
import type { LooseObj, ThreadView } from "../shared/types.js";
import { escapeHtml, str } from "../shared/utils.js";

type PlanItem = {
  key: string;
  threadId: string;
  action: string;
  stepType: string;
  byWhen: string;
  planId: number;
};

const PAGE_HTML = `
<div class="view-plans">
  <div class="plans-toolbar">
    <button type="button" class="add-plan-btn" id="add-plan-btn">Add plan</button>
  </div>
  <form class="add-plan-form" id="add-plan-form" hidden>
    <label class="add-plan-field">
      <span>Thread</span>
      <select name="thread-id" id="plan-thread-select" required></select>
    </label>
    <label class="add-plan-field">
      <span>Next step</span>
      <input type="text" name="action" id="plan-action-input" placeholder="e.g. Follow up with Tom" required />
    </label>
    <label class="add-plan-field">
      <span>Type</span>
      <select name="step-type" id="plan-type-select">
        <option value="follow up needed">Follow up</option>
        <option value="response required">Response required</option>
      </select>
    </label>
    <label class="add-plan-field">
      <span>By when (optional)</span>
      <input type="date" name="by-when" id="plan-by-when-input" class="plan-by-when-input" />
    </label>
    <div class="add-plan-form-actions">
      <button type="submit">Add plan</button>
      <button type="button" class="add-plan-cancel" id="add-plan-cancel">Cancel</button>
    </div>
  </form>
  <div id="plans-list" class="plans-list"></div>
</div>`;

let interactionsBound = false;

function trackingThreads(): ThreadView[] {
  const { active, snoozed } = partitionThreadsBySnooze(getCurrentThreads());
  return [...active, ...snoozed];
}

function stepTypeLabel(stepType: string): string {
  return stepType === "follow up needed" ? "Follow up needed" : "Response required";
}

function stepTypeClass(stepType: string): string {
  return stepType === "follow up needed" ? "next-step-type follow-up" : "next-step-type";
}

function collectPlanItems(data: LooseObj): PlanItem[] {
  return getThreadPlans(data).map((plan) => ({
    key: `user-${plan.id}`,
    threadId: plan.inbox_thread_id,
    action: plan.action,
    stepType: plan.step_type,
    byWhen: plan.by_when,
    planId: plan.id,
  }));
}

function threadSelectOptions(selectedId = ""): string {
  const threads = trackingThreads();
  if (!threads.length) {
    return `<option value="">No active threads</option>`;
  }
  return threads
    .map((thread) => {
      const label = threadLabel(thread);
      const selected = thread.id === selectedId ? " selected" : "";
      return `<option value="${escapeHtml(thread.id)}"${selected}>${escapeHtml(label)}</option>`;
    })
    .join("");
}

function savedDraftForThread(threadId: string): LooseObj | undefined {
  const data = getCurrentData();
  const drafts = (data?.thread_drafts || {}) as LooseObj;
  return drafts[threadId] as LooseObj | undefined;
}

function planCardHtml(item: PlanItem): string {
  const thread = getCurrentThreads().find((t) => t.id === item.threadId);
  const label = thread ? threadLabel(thread) : "(Unknown thread)";
  const whenLabel = formatPlanByWhen(item.byWhen);
  const when = whenLabel ? ` <span class="next-step-when">by ${escapeHtml(whenLabel)}</span>` : "";
  const savedDraft = savedDraftForThread(item.threadId);
  const savedIntent = savedDraft ? str(savedDraft.response_intent) : item.action;
  const savedMd = savedDraft ? str(savedDraft.markdown) : "";
  const showSavedOut = Boolean(savedMd);

  return `<article class="plan-card" data-plan-key="${escapeHtml(item.key)}" data-plan-id="${item.planId}" data-thread-id="${escapeHtml(item.threadId)}">
    <div class="plan-card-view">
      <header class="plan-card-header">
        <div class="plan-card-title-row">
          <h3 class="plan-action">${escapeHtml(item.action)}</h3>
        </div>
        <p class="plan-thread-label">${escapeHtml(label)}</p>
        <p class="plan-meta">
          <span class="${stepTypeClass(item.stepType)}">${escapeHtml(stepTypeLabel(item.stepType))}</span>${when}
        </p>
      </header>
      <div class="plan-card-actions">
        <button type="button" class="plan-edit-btn" data-plan-id="${item.planId}">Edit</button>
        <button type="button" class="plan-draft-btn" data-plan-key="${escapeHtml(item.key)}">Draft email</button>
        <button type="button" class="plan-delete-btn" data-plan-id="${item.planId}">Remove</button>
      </div>
    </div>
    <div class="draft-reply-panel plan-draft-panel" data-plan-key="${escapeHtml(item.key)}" hidden>
      <p class="draft-reply-hint">What should this reply communicate?</p>
      <textarea class="draft-intent-input" rows="2" autocomplete="off" placeholder="e.g. Follow up on the proposal and suggest a call next week">${escapeHtml(savedIntent)}</textarea>
      <div class="draft-reply-actions">
        <button type="button" class="draft-generate-btn" data-plan-key="${escapeHtml(item.key)}" data-thread-id="${escapeHtml(item.threadId)}">Generate</button>
      </div>
      <p class="draft-reply-error" hidden></p>
      <label class="draft-output-label">Markdown — copy below</label>
      <textarea class="draft-markdown-output" readonly ${showSavedOut ? "" : "hidden"} rows="12" spellcheck="false">${escapeHtml(savedMd)}</textarea>
    </div>
  </article>`;
}

function renderPlansList(): void {
  const listEl = document.getElementById("plans-list");
  const data = getCurrentData();
  if (!listEl || !data) return;

  const items = collectPlanItems(data);
  if (!items.length) {
    listEl.innerHTML = `<p class="plans-empty">No plans yet. Use <strong>Add plan</strong> to set a next step for a thread. Summary suggestions stay on the Threads page.</p>`;
    return;
  }

  const sorted = sortPlansByDueDate(items, (p) => p.byWhen, (p) => p.action);
  listEl.innerHTML = `<ul class="plans-due-list">${sorted.map(planCardHtml).join("")}</ul>`;
}

function populateThreadSelect(): void {
  const select = document.getElementById("plan-thread-select") as HTMLSelectElement | null;
  if (!select) return;
  select.innerHTML = threadSelectOptions();
}

function planItemForCard(card: HTMLElement): PlanItem | null {
  const planId = Number(card.dataset.planId) || 0;
  if (!planId) return null;
  const data = getCurrentData();
  if (!data) return null;
  return collectPlanItems(data).find((item) => item.planId === planId) ?? null;
}

function closePlanEdit(card: HTMLElement): void {
  card.querySelector(".plan-edit-form")?.remove();
  card.classList.remove("is-editing");
  card.querySelector(".plan-card-view")?.removeAttribute("hidden");
}

function openPlanEdit(card: HTMLElement): void {
  document.querySelectorAll<HTMLElement>(".plan-card.is-editing").forEach((other) => {
    if (other !== card) closePlanEdit(other);
  });
  const item = planItemForCard(card);
  if (!item) return;
  closePlanEdit(card);
  card.classList.add("is-editing");
  card.querySelector(".plan-card-view")?.setAttribute("hidden", "");
  card.querySelector(".plan-draft-panel")?.setAttribute("hidden", "");
  const draftPanel = card.querySelector(".plan-draft-panel");
  draftPanel?.insertAdjacentHTML(
    "beforebegin",
    planEditFormHtml({
      planId: item.planId,
      action: item.action,
      stepType: item.stepType,
      byWhen: item.byWhen,
      threadSelectOptions: threadSelectOptions(item.threadId),
    }),
  );
  card.querySelector<HTMLInputElement>(".plan-edit-action-input")?.focus();
}

async function requestEmailReplyDraft(
  threadId: string,
  responseIntent: string,
  threadSubject: string,
): Promise<LooseObj> {
  const thread = getCurrentThreads().find((t) => t.id === threadId);
  if (!thread) throw new Error("Thread not found.");
  const res = await fetch("/api/claude/email-reply", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      thread_id: threadId,
      response_intent: responseIntent,
      thread_subject: threadSubject,
      messages: threadMessagesForReply(thread),
    }),
  });
  const data = (await res.json()) as LooseObj;
  if (!res.ok || data.ok === false) {
    const msg = str(data.error) || `Request failed (${res.status})`;
    throw new Error(msg);
  }
  return data;
}

function reloadFromStore(): void {
  const data = getCurrentData();
  if (data) {
    setBundle(data, getCurrentSourceLabel());
    void renderPlansPage();
  }
}

export function mountPlansPage(root: HTMLElement): void {
  root.innerHTML = PAGE_HTML;
}

export async function renderPlansPage(): Promise<void> {
  const data = getCurrentData();
  if (!data) return;

  populateThreadSelect();
  renderPlansList();
}

export function bindPlansInteractions(): void {
  if (interactionsBound) return;
  interactionsBound = true;

  document.addEventListener("click", (ev) => {
    const target = ev.target as HTMLElement | null;
    if (!target) return;
    if (!document.getElementById("page-root")?.contains(target)) return;

    if (target.id === "add-plan-btn") {
      const form = document.getElementById("add-plan-form");
      const btn = document.getElementById("add-plan-btn");
      form?.removeAttribute("hidden");
      btn?.setAttribute("hidden", "");
      populateThreadSelect();
      document.getElementById("plan-action-input")?.focus();
      return;
    }

    if (target.id === "add-plan-cancel") {
      const form = document.getElementById("add-plan-form") as HTMLFormElement | null;
      const btn = document.getElementById("add-plan-btn");
      form?.reset();
      form?.setAttribute("hidden", "");
      btn?.removeAttribute("hidden");
      return;
    }

    const editBtn = target.closest(".plan-edit-btn") as HTMLButtonElement | null;
    if (editBtn) {
      const card = editBtn.closest(".plan-card") as HTMLElement | null;
      if (card) openPlanEdit(card);
      return;
    }

    const editCancel = target.closest(".plan-edit-cancel") as HTMLButtonElement | null;
    if (editCancel) {
      const card = editCancel.closest(".plan-card") as HTMLElement | null;
      if (card) closePlanEdit(card);
      return;
    }

    const draftBtn = target.closest(".plan-draft-btn") as HTMLButtonElement | null;
    if (draftBtn) {
      const planKey = str(draftBtn.dataset.planKey);
      const card = draftBtn.closest(".plan-card");
      const panel = card?.querySelector(`.plan-draft-panel[data-plan-key="${planKey}"]`) as HTMLElement | null;
      if (panel) {
        panel.hidden = !panel.hidden;
        if (!panel.hidden) panel.querySelector<HTMLTextAreaElement>(".draft-intent-input")?.focus();
      }
      return;
    }

    const draftGen = target.closest(".draft-generate-btn") as HTMLButtonElement | null;
    if (draftGen && draftGen.closest(".plan-card")) {
      void (async () => {
        const planKey = str(draftGen.dataset.planKey);
        const threadId = str(draftGen.dataset.threadId);
        const card = draftGen.closest(".plan-card");
        const panel = card?.querySelector(`.plan-draft-panel[data-plan-key="${planKey}"]`);
        const intentEl = panel?.querySelector<HTMLTextAreaElement>(".draft-intent-input");
        const intent = intentEl?.value.trim() ?? "";
        const outEl = panel?.querySelector<HTMLTextAreaElement>(".draft-markdown-output");
        const errEl = panel?.querySelector(".draft-reply-error") as HTMLElement | null;
        if (!threadId || !outEl) return;
        if (!intent) {
          if (errEl) {
            errEl.textContent = "Add what the reply should say (required).";
            errEl.hidden = false;
          }
          return;
        }
        if (errEl) errEl.hidden = true;
        draftGen.disabled = true;
        const thread = getCurrentThreads().find((t) => t.id === threadId);
        const primary = thread?.messages[0];
        const c0 = (primary?.cleaned || {}) as LooseObj;
        const subj = str(c0.subject);
        try {
          const payload = await requestEmailReplyDraft(threadId, intent, subj);
          const markdown = str(payload.markdown) || formatDraftReplyMarkdown(payload);
          outEl.value = markdown;
          outEl.hidden = false;
          applySavedThreadDraft(threadId, payload, intent);
        } catch (e) {
          const msg = e instanceof Error ? e.message : String(e);
          outEl.value = ["## Draft reply", "", `**Error:** ${msg}`, ""].join("\n");
          outEl.hidden = false;
        } finally {
          draftGen.disabled = false;
        }
      })();
      return;
    }

    const deleteBtn = target.closest(".plan-delete-btn") as HTMLButtonElement | null;
    if (deleteBtn) {
      const planId = Number(deleteBtn.dataset.planId) || 0;
      if (!planId) return;
      applyPlanDeleted(planId);
      reloadFromStore();
      void persistPlanDelete(planId).catch((err) => console.error(err));
      return;
    }
  });

  document.addEventListener("submit", (ev) => {
    const addForm = (ev.target as HTMLElement | null)?.closest("#add-plan-form");
    if (addForm) {
      ev.preventDefault();
      void (async () => {
        const threadSelect = document.getElementById("plan-thread-select") as HTMLSelectElement | null;
        const actionInput = document.getElementById("plan-action-input") as HTMLInputElement | null;
        const typeSelect = document.getElementById("plan-type-select") as HTMLSelectElement | null;
        const whenInput = document.getElementById("plan-by-when-input") as HTMLInputElement | null;
        const threadId = threadSelect?.value.trim() ?? "";
        const action = actionInput?.value.trim() ?? "";
        const stepType = typeSelect?.value.trim() || "follow up needed";
        const byWhen = whenInput?.value.trim() ?? "";
        if (!threadId || !action) return;
        try {
          const plan = await persistPlanCreate(threadId, action, stepType, byWhen);
          applyPlanCreated(plan);
          (addForm as HTMLFormElement).reset();
          addForm.setAttribute("hidden", "");
          document.getElementById("add-plan-btn")?.removeAttribute("hidden");
          reloadFromStore();
        } catch (err) {
          console.error(err);
        }
      })();
      return;
    }

    const editForm = (ev.target as HTMLElement | null)?.closest(".plan-edit-form");
    if (!editForm) return;
    ev.preventDefault();
    void (async () => {
      const planId = Number((editForm as HTMLElement).dataset.planId) || 0;
      const threadSelect = editForm.querySelector<HTMLSelectElement>(".plan-edit-thread-select");
      const actionInput = editForm.querySelector<HTMLInputElement>(".plan-edit-action-input");
      const typeSelect = editForm.querySelector<HTMLSelectElement>(".plan-edit-type-select");
      const whenInput = editForm.querySelector<HTMLInputElement>(".plan-edit-when-input");
      const threadId = threadSelect?.value.trim() ?? "";
      const action = actionInput?.value.trim() ?? "";
      const stepType = typeSelect?.value.trim() || "follow up needed";
      const byWhen = whenInput?.value.trim() ?? "";
      if (!planId || !threadId || !action) return;
      try {
        const plan = await persistPlanUpdate(planId, threadId, action, stepType, byWhen);
        applyPlanUpdated(plan);
        reloadFromStore();
      } catch (err) {
        console.error(err);
      }
    })();
  });
}
