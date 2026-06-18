import type { LooseObj, PlanView } from "./types.js";
import { escapeHtml, str } from "./utils.js";

export function planActionKey(threadId: string, action: string): string {
  return `${threadId}::${action.trim().toLowerCase()}`;
}

export function planExistsForStep(plans: PlanView[], threadId: string, action: string): boolean {
  const key = planActionKey(threadId, action);
  return plans.some((plan) => planActionKey(plan.inbox_thread_id, plan.action) === key);
}

export function formatPlanByWhen(raw: string): string {
  const s = raw.trim();
  if (!s) return "";
  if (/^\d{4}-\d{2}-\d{2}$/.test(s)) {
    const d = new Date(`${s}T12:00:00`);
    if (!Number.isNaN(d.getTime())) {
      return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
    }
  }
  return s;
}

/** Parse ``by_when`` for sorting; ISO dates use noon local to avoid TZ drift. */
export function planDueTimestamp(byWhen: string): number | null {
  const s = byWhen.trim();
  if (!s) return null;
  if (/^\d{4}-\d{2}-\d{2}$/.test(s)) {
    const d = new Date(`${s}T12:00:00`);
    return Number.isNaN(d.getTime()) ? null : d.getTime();
  }
  const d = new Date(s);
  return Number.isNaN(d.getTime()) ? null : d.getTime();
}

function comparePlanByWhen(a: string, b: string): number {
  const ta = planDueTimestamp(a);
  const tb = planDueTimestamp(b);
  if (ta !== null && tb !== null) return ta - tb;
  if (ta !== null) return -1;
  if (tb !== null) return 1;
  return a.trim().localeCompare(b.trim(), undefined, { sensitivity: "base" });
}

export function partitionPlansByDueDate<T>(
  plans: T[],
  dueOf: (plan: T) => string,
  actionOf: (plan: T) => string,
): { withDueDate: T[]; withoutDueDate: T[] } {
  const withDueDate: T[] = [];
  const withoutDueDate: T[] = [];
  for (const plan of plans) {
    if (dueOf(plan).trim()) withDueDate.push(plan);
    else withoutDueDate.push(plan);
  }
  withDueDate.sort((a, b) => comparePlanByWhen(dueOf(a), dueOf(b)));
  withoutDueDate.sort((a, b) =>
    actionOf(a).localeCompare(actionOf(b), undefined, { sensitivity: "base" }),
  );
  return { withDueDate, withoutDueDate };
}

/** Earliest due first, then undated plans alphabetically by action. */
export function sortPlansByDueDate<T>(
  plans: T[],
  dueOf: (plan: T) => string,
  actionOf: (plan: T) => string,
): T[] {
  const { withDueDate, withoutDueDate } = partitionPlansByDueDate(plans, dueOf, actionOf);
  return [...withDueDate, ...withoutDueDate];
}

export async function persistPlanCreate(
  threadId: string,
  action: string,
  stepType: string,
  byWhen: string,
): Promise<PlanView> {
  const res = await fetch("/api/plans/create", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      thread_id: threadId,
      action,
      step_type: stepType,
      by_when: byWhen,
    }),
  });
  const body = (await res.json().catch(() => ({}))) as LooseObj;
  if (!res.ok) throw new Error(str(body.error) || `Create plan failed (${res.status})`);
  const planRaw = body.plan as LooseObj;
  return planFromApiRow(planRaw, {
    inbox_thread_id: threadId,
    action,
    step_type: stepType,
    by_when: byWhen,
  });
}

export function planByWhenInputValue(byWhen: string): string {
  const s = byWhen.trim();
  return /^\d{4}-\d{2}-\d{2}$/.test(s) ? s : "";
}

export function stepTypeSelectHtml(selected: string, selectClass = "plan-step-type-select"): string {
  const sel = (value: string) => (selected === value ? " selected" : "");
  return `<select class="${selectClass}" name="step-type">
    <option value="follow up needed"${sel("follow up needed")}>Follow up</option>
    <option value="response required"${sel("response required")}>Response required</option>
  </select>`;
}

export function planEditFormHtml(opts: {
  planId: number;
  action: string;
  stepType: string;
  byWhen: string;
  threadSelectOptions: string;
}): string {
  const dateValue = escapeHtml(planByWhenInputValue(opts.byWhen));
  return `<form class="plan-edit-form add-plan-form" data-plan-id="${opts.planId}">
    <label class="add-plan-field">
      <span>Thread</span>
      <select name="thread-id" class="plan-edit-thread-select" required>${opts.threadSelectOptions}</select>
    </label>
    <label class="add-plan-field">
      <span>Next step</span>
      <input type="text" name="action" class="plan-edit-action-input" value="${escapeHtml(opts.action)}" required />
    </label>
    <label class="add-plan-field">
      <span>Type</span>
      ${stepTypeSelectHtml(opts.stepType, "plan-edit-type-select")}
    </label>
    <label class="add-plan-field">
      <span>By when (optional)</span>
      <input type="date" name="by-when" class="plan-edit-when-input plan-by-when-input" value="${dateValue}" />
    </label>
    <div class="add-plan-form-actions">
      <button type="submit">Save</button>
      <button type="button" class="plan-edit-cancel add-plan-cancel">Cancel</button>
    </div>
  </form>`;
}

export function dashboardPlanEditFormHtml(opts: {
  planId: number;
  action: string;
  stepType: string;
  byWhen: string;
}): string {
  const dateValue = escapeHtml(planByWhenInputValue(opts.byWhen));
  return `<form class="dashboard-plan-edit-form" data-plan-id="${opts.planId}">
    <input type="text" class="dashboard-plan-edit-action" value="${escapeHtml(opts.action)}" required aria-label="Next step" />
    ${stepTypeSelectHtml(opts.stepType, "dashboard-plan-edit-type")}
    <input type="date" class="dashboard-plan-edit-when plan-by-when-input" value="${dateValue}" aria-label="By when" />
    <div class="dashboard-plan-edit-actions">
      <button type="submit">Save</button>
      <button type="button" class="dashboard-plan-edit-cancel">Cancel</button>
    </div>
  </form>`;
}

function planFromApiRow(planRaw: LooseObj, fallback: Partial<PlanView>): PlanView {
  return {
    id: Number(planRaw.id) || fallback.id || 0,
    inbox_thread_id: str(planRaw.inbox_thread_id) || str(fallback.inbox_thread_id),
    action: str(planRaw.action) || str(fallback.action),
    step_type: str(planRaw.step_type) || str(fallback.step_type) || "follow up needed",
    by_when: str(planRaw.by_when ?? fallback.by_when),
    created_at: str(planRaw.created_at) || str(fallback.created_at),
    updated_at: str(planRaw.updated_at) || str(fallback.updated_at),
  };
}

export async function persistPlanUpdate(
  planId: number,
  threadId: string,
  action: string,
  stepType: string,
  byWhen: string,
): Promise<PlanView> {
  const res = await fetch("/api/plans/update", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      plan_id: planId,
      thread_id: threadId,
      action,
      step_type: stepType,
      by_when: byWhen,
    }),
  });
  const body = (await res.json().catch(() => ({}))) as LooseObj;
  if (!res.ok) throw new Error(str(body.error) || `Update plan failed (${res.status})`);
  const planRaw = body.plan as LooseObj;
  return planFromApiRow(planRaw, {
    id: planId,
    inbox_thread_id: threadId,
    action,
    step_type: stepType,
    by_when: byWhen,
  });
}

export async function persistPlanDelete(planId: number): Promise<void> {
  const res = await fetch("/api/plans/delete", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ plan_id: planId }),
  });
  const body = (await res.json().catch(() => ({}))) as LooseObj;
  if (!res.ok) throw new Error(str(body.error) || `Delete plan failed (${res.status})`);
}
