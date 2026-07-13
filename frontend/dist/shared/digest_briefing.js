import { renderPlansRail } from "../dashboard_schedule_rail.js";
import { applyPlanCreated } from "./summaries_store.js";
import { persistPlanCreate } from "./plan_helpers.js";
import { escapeHtml } from "./utils.js";
const WRAP_ID = "dashboard-briefing-wrap";
const SESSION_DISMISS_KEY = "fivelanes_digest_briefing_dismissed_v1";
function pillHtml(text, modifier) {
    return `<span class="digest-pill digest-pill--${modifier}">${escapeHtml(text)}</span>`;
}
function pillsHtml(item) {
    const pills = [
        item.action ? pillHtml(item.action, "action") : "",
        item.person ? pillHtml(item.person, "person") : "",
        item.lane ? pillHtml(item.lane, "lane") : "",
    ].join("");
    return pills ? `<span class="dashboard-briefing-item-pills">${pills}</span>` : "";
}
async function fetchDigest() {
    try {
        const res = await fetch("/api/digest/latest");
        if (!res.ok)
            return null;
        const body = (await res.json().catch(() => null));
        return body && body.ok ? body : null;
    }
    catch {
        return null;
    }
}
async function dismissDigestItem(item) {
    // Permanent for the rest of the day (services/digest/store.py) — this is what makes both
    // "Clear" and a successful "Add to plans" stick instead of the item reappearing next poll.
    try {
        const res = await fetch("/api/digest/dismiss", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ id: item.id }),
        });
        return res.ok;
    }
    catch {
        return false;
    }
}
async function addDigestItemToPlans(item) {
    if (!item.inbox_thread_id)
        return false;
    try {
        // Same path the schedule rail's own "Add plan" form uses (persistPlanCreate +
        // applyPlanCreated + a rail re-render) — a bare fetch to /api/plans/create did create the
        // row server-side, but left the page's in-memory plans cache stale, so the new plan never
        // showed up anywhere in the UI until a full reload.
        const plan = await persistPlanCreate(item.inbox_thread_id, item.text, "follow up needed", "");
        applyPlanCreated(plan);
        renderPlansRail();
        return true;
    }
    catch {
        return false;
    }
}
function itemHtml(item, index) {
    const actions = item.votable
        ? `<span class="dashboard-briefing-item-actions">
      <button type="button" class="dashboard-briefing-clear" aria-label="Clear">Clear</button>
      <button type="button" class="dashboard-briefing-add-plan" aria-label="Add to plans">Add to plans</button>
    </span>`
        : "";
    return `<li class="dashboard-briefing-item" data-item-index="${index}">
    <span class="dashboard-briefing-item-main">
      ${pillsHtml(item)}
      <span class="dashboard-briefing-item-text">${escapeHtml(item.text)}</span>
    </span>
    ${actions}
  </li>`;
}
export async function refreshDigestBriefing() {
    const wrap = document.getElementById(WRAP_ID);
    if (!wrap)
        return;
    const digest = await fetchDigest();
    const items = digest?.items ?? [];
    if (!digest || !items.length) {
        wrap.hidden = true;
        wrap.innerHTML = "";
        return;
    }
    const fingerprint = digest.generated_at;
    if (sessionStorage.getItem(SESSION_DISMISS_KEY) === fingerprint) {
        wrap.hidden = true;
        return;
    }
    const itemsHtml = items.map((item, index) => itemHtml(item, index)).join("");
    wrap.hidden = false;
    wrap.innerHTML = `<aside class="dashboard-briefing" role="status" aria-live="polite">
    <div class="dashboard-briefing-head">
      <h3 class="dashboard-briefing-title">Alfred</h3>
      <button type="button" class="dashboard-briefing-dismiss" aria-label="Dismiss">×</button>
    </div>
    <ul class="dashboard-briefing-list">${itemsHtml}</ul>
  </aside>`;
    wrap.querySelector(".dashboard-briefing-dismiss")?.addEventListener("click", () => {
        sessionStorage.setItem(SESSION_DISMISS_KEY, fingerprint);
        wrap.hidden = true;
    });
    wrap.querySelectorAll(".dashboard-briefing-clear").forEach((btn) => {
        btn.addEventListener("click", () => {
            const li = btn.closest(".dashboard-briefing-item");
            const index = Number(li?.dataset.itemIndex);
            const item = Number.isNaN(index) ? undefined : items[index];
            // Remove immediately for responsiveness; the dismiss persists in the background so it
            // doesn't come back on the next poll/reload regardless of exactly when this resolves.
            li?.remove();
            if (item)
                void dismissDigestItem(item);
        });
    });
    wrap.querySelectorAll(".dashboard-briefing-add-plan").forEach((btn) => {
        btn.addEventListener("click", () => {
            const li = btn.closest(".dashboard-briefing-item");
            const index = Number(li?.dataset.itemIndex);
            if (!li || Number.isNaN(index))
                return;
            const item = items[index];
            if (!item)
                return;
            btn.disabled = true;
            btn.textContent = "Adding…";
            void addDigestItemToPlans(item).then((ok) => {
                if (ok) {
                    void dismissDigestItem(item);
                    li.remove();
                    return;
                }
                btn.textContent = "Couldn't add";
                btn.disabled = false;
            });
        });
    });
}
