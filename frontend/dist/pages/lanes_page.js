import { partitionThreadsBySnooze, threadEmailSubject, } from "../shared/thread_domain.js";
import { applyLaneCreated, applyLaneThreadMembership, getCurrentData, getCurrentSourceLabel, getCurrentThreads, getLaneThreadIds, getLanes, setBundle, } from "../shared/summaries_store.js";
import { escapeHtml, str } from "../shared/utils.js";
const PAGE_HTML = `
<div class="view-lanes">
  <div class="lanes-toolbar">
    <button type="button" class="create-lane-btn" id="create-lane-btn">Create lane</button>
  </div>
  <form class="create-lane-form" id="create-lane-form" hidden>
    <input type="text" name="lane-name" id="lane-name-input" placeholder="Lane name" required />
    <button type="submit">Create</button>
    <button type="button" class="create-lane-cancel" id="create-lane-cancel">Cancel</button>
  </form>
  <div id="lanes-list" class="lanes-list"></div>
</div>`;
let interactionsBound = false;
let assignLaneId = null;
function trackingThreads() {
    const { active, snoozed } = partitionThreadsBySnooze(getCurrentThreads());
    return [...active, ...snoozed];
}
function threadPickerHtml(laneId, selectedIds) {
    const threads = trackingThreads();
    if (!threads.length) {
        return `<p class="lane-thread-picker-empty">No active or snoozed threads to add.</p>`;
    }
    const rows = threads
        .map((thread) => {
        const checked = selectedIds.has(thread.id) ? " checked" : "";
        const subject = threadEmailSubject(thread);
        return `<label class="lane-thread-option">
        <input type="checkbox" class="lane-thread-checkbox" data-lane-id="${laneId}" data-thread-id="${escapeHtml(thread.id)}"${checked} />
        <span>${escapeHtml(subject)}</span>
      </label>`;
    })
        .join("");
    return `<div class="lane-thread-picker">
    <p class="lane-thread-picker-title">Add threads by email subject</p>
    <div class="lane-thread-options">${rows}</div>
  </div>`;
}
function laneCardHtml(lane, threadIds, expanded) {
    const selected = new Set(threadIds);
    const threadLabels = threadIds
        .map((tid) => {
        const thread = getCurrentThreads().find((t) => t.id === tid);
        if (!thread)
            return "";
        return `<li>${escapeHtml(threadEmailSubject(thread))}</li>`;
    })
        .filter(Boolean)
        .join("");
    const threadsBlock = threadLabels
        ? `<ul class="lane-assigned-threads">${threadLabels}</ul>`
        : `<p class="lane-empty-threads">No threads yet.</p>`;
    const picker = expanded ? threadPickerHtml(lane.id, selected) : "";
    return `<article class="user-lane-card" data-lane-id="${lane.id}">
    <header class="user-lane-header">
      <h2>${escapeHtml(lane.name)}</h2>
      <span class="lane-count-pill">${threadIds.length} thread${threadIds.length === 1 ? "" : "s"}</span>
    </header>
    ${threadsBlock}
    ${picker}
    <div class="user-lane-actions">
      <button type="button" class="lane-edit-threads-btn" data-lane-id="${lane.id}">
        ${expanded ? "Done" : threadIds.length ? "Edit threads" : "Add threads"}
      </button>
    </div>
  </article>`;
}
function renderLanesList() {
    const listEl = document.getElementById("lanes-list");
    const data = getCurrentData();
    if (!listEl || !data)
        return;
    const lanes = getLanes(data);
    if (!lanes.length) {
        listEl.innerHTML = `<p class="lanes-empty">No lanes yet. Create one to group threads.</p>`;
        return;
    }
    listEl.innerHTML = lanes
        .map((lane) => {
        const threadIds = getLaneThreadIds(data, lane.id);
        const expanded = assignLaneId === lane.id;
        return laneCardHtml(lane, threadIds, expanded);
    })
        .join("");
}
async function persistLaneCreate(name) {
    const res = await fetch("/api/lanes/create", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
    });
    const body = (await res.json().catch(() => ({})));
    if (!res.ok)
        throw new Error(str(body.error) || `Create lane failed (${res.status})`);
    const laneRaw = body.lane;
    return {
        id: Number(laneRaw.id) || 0,
        name: str(laneRaw.name) || name,
        created_at: str(laneRaw.created_at),
        updated_at: str(laneRaw.updated_at),
    };
}
async function persistLaneThread(laneId, threadId, inLane) {
    const path = inLane ? "/api/lanes/add-thread" : "/api/lanes/remove-thread";
    const res = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ lane_id: laneId, thread_id: threadId }),
    });
    const body = (await res.json().catch(() => ({})));
    if (!res.ok)
        throw new Error(str(body.error) || `Lane update failed (${res.status})`);
}
function reloadFromStore() {
    const data = getCurrentData();
    if (data) {
        setBundle(data, getCurrentSourceLabel());
        void renderLanesPage();
    }
}
export function mountLanesPage(root) {
    root.innerHTML = PAGE_HTML;
}
export async function renderLanesPage() {
    const data = getCurrentData();
    if (!data)
        return;
    renderLanesList();
}
export function bindLanesInteractions() {
    if (interactionsBound)
        return;
    interactionsBound = true;
    document.addEventListener("click", (ev) => {
        const target = ev.target;
        if (!target)
            return;
        if (!document.getElementById("page-root")?.contains(target))
            return;
        if (target.id === "create-lane-btn") {
            const form = document.getElementById("create-lane-form");
            const btn = document.getElementById("create-lane-btn");
            form?.removeAttribute("hidden");
            btn?.setAttribute("hidden", "");
            document.getElementById("lane-name-input")?.focus();
            return;
        }
        if (target.id === "create-lane-cancel") {
            const form = document.getElementById("create-lane-form");
            const btn = document.getElementById("create-lane-btn");
            form?.reset();
            form?.setAttribute("hidden", "");
            btn?.removeAttribute("hidden");
            return;
        }
        const editBtn = target.closest(".lane-edit-threads-btn");
        if (editBtn) {
            const laneId = Number(editBtn.dataset.laneId) || 0;
            assignLaneId = assignLaneId === laneId ? null : laneId;
            renderLanesList();
            return;
        }
    });
    document.addEventListener("submit", (ev) => {
        const form = ev.target?.closest("#create-lane-form");
        if (!form)
            return;
        ev.preventDefault();
        void (async () => {
            const input = document.getElementById("lane-name-input");
            const name = input?.value.trim() ?? "";
            if (!name)
                return;
            try {
                const lane = await persistLaneCreate(name);
                applyLaneCreated(lane);
                assignLaneId = lane.id;
                form.setAttribute("hidden", "");
                document.getElementById("create-lane-btn")?.removeAttribute("hidden");
                form.reset();
                reloadFromStore();
            }
            catch (err) {
                console.error(err);
            }
        })();
    });
    document.addEventListener("change", (ev) => {
        const checkbox = ev.target?.closest(".lane-thread-checkbox");
        if (!checkbox)
            return;
        const laneId = Number(checkbox.dataset.laneId) || 0;
        const threadId = str(checkbox.dataset.threadId);
        if (!laneId || !threadId)
            return;
        void (async () => {
            const inLane = checkbox.checked;
            applyLaneThreadMembership(laneId, threadId, inLane);
            try {
                await persistLaneThread(laneId, threadId, inLane);
                reloadFromStore();
            }
            catch (err) {
                applyLaneThreadMembership(laneId, threadId, !inLane);
                checkbox.checked = !inLane;
                console.error(err);
            }
        })();
    });
}
