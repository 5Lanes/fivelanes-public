import {
  listSection,
  partitionThreadsBySnooze,
  threadEmailSubject,
} from "../shared/thread_domain.js";
import {
  applyLaneArchived,
  applyLaneCreated,
  applyLaneRemoved,
  applyLaneSummary,
  applyLaneThreadMembership,
  clearSummariesBundleCache,
  getCurrentData,
  getCurrentSourceLabel,
  getCurrentThreads,
  getLaneSummary,
  getLaneThreadIds,
  getLanes,
  loadLatestBundle,
  normalizeBundle,
  setBundle,
} from "../shared/summaries_store.js";
import { escapeHtml, str } from "../shared/utils.js";
import type { LaneSummaryView, LaneView, LooseObj } from "../shared/types.js";

const LANES_SORT_KEY = "fivelanes_lanes_sort_v2";

export type LaneSortMode = "updated-desc" | "created-desc";

const PAGE_HTML = `
<div class="view-lanes">
  <div class="lanes-toolbar">
    <label class="lanes-sort-control">
      <span class="lanes-sort-label">Sort</span>
      <select id="lanes-sort" class="lanes-sort-select" aria-label="Sort lanes">
        <option value="updated-desc">Recently updated</option>
        <option value="created-desc">Recently added</option>
      </select>
    </label>
    <button type="button" class="lanes-show-archived-btn" id="lanes-show-archived-btn" aria-pressed="false">Show archived</button>
    <button type="button" class="create-lane-btn" id="create-lane-btn">Create lane</button>
  </div>
  <form class="create-lane-form" id="create-lane-form" hidden>
    <input type="text" name="lane-name" id="lane-name-input" placeholder="Lane name" required />
    <button type="submit">Create</button>
    <button type="button" class="create-lane-cancel" id="create-lane-cancel">Cancel</button>
    <p class="lane-create-error" id="lane-create-error" hidden></p>
  </form>
  <div id="lanes-list" class="lanes-list"></div>
</div>`;

let interactionsBound = false;
let assignLaneId: number | null = null;
let activeLaneTabId: number | null = null;
let showArchivedLanes = false;
const laneSummaryErrors = new Map<number, string>();
const laneSummaryPending = new Set<number>();
const laneSummaryWatching = new Set<number>();

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function laneSummaryHasContent(body: LooseObj): boolean {
  if (str(body.summary).trim()) return true;
  if (str(body.tone_overview).trim()) return true;
  for (const key of ["highlights", "current_priorities", "waiting_on_others"] as const) {
    const val = body[key];
    if (Array.isArray(val) && val.some((x) => str(x).trim())) return true;
  }
  return false;
}

function isDashboardLanesList(listEl: HTMLElement): boolean {
  return !!listEl.closest(".view-dashboard");
}

function isLaneSortMode(value: string): value is LaneSortMode {
  return value === "updated-desc" || value === "created-desc";
}

export function getLaneSortMode(): LaneSortMode {
  try {
    const stored = localStorage.getItem(LANES_SORT_KEY);
    if (stored && isLaneSortMode(stored)) return stored;
  } catch {
    /* ignore storage errors */
  }
  return "updated-desc";
}

export function setLaneSortMode(mode: LaneSortMode): void {
  try {
    localStorage.setItem(LANES_SORT_KEY, mode);
  } catch {
    /* ignore storage errors */
  }
}

function threadLatestMessageAt(threadId: string): string {
  const thread = getCurrentThreads().find((t) => t.id === threadId);
  if (!thread || !thread.messages.length) return "";
  const row = thread.messages[0];
  return str(row.cleaned?.datetime || row.summary?.datetime);
}

function laneLatestThreadMessageAt(data: LooseObj, laneId: number): string {
  let latest = "";
  for (const threadId of getLaneThreadIds(data, laneId)) {
    const at = threadLatestMessageAt(threadId);
    if (at && (!latest || at.localeCompare(latest) > 0)) latest = at;
  }
  return latest;
}

function compareLaneNames(a: LaneView, b: LaneView): number {
  return a.name.localeCompare(b.name, undefined, { sensitivity: "base" });
}

export function sortLanes(lanes: LaneView[], mode: LaneSortMode, data: LooseObj): LaneView[] {
  const copy = [...lanes];
  switch (mode) {
    case "created-desc":
      return copy.sort(
        (a, b) => str(b.created_at).localeCompare(str(a.created_at)) || compareLaneNames(a, b),
      );
    case "updated-desc":
      return copy.sort(
        (a, b) =>
          laneLatestThreadMessageAt(data, b.id).localeCompare(laneLatestThreadMessageAt(data, a.id)) ||
          compareLaneNames(a, b),
      );
    default:
      return copy.sort(compareLaneNames);
  }
}

function syncLaneSortSelect(): void {
  const select = document.getElementById("lanes-sort") as HTMLSelectElement | null;
  if (!select) return;
  select.value = getLaneSortMode();
}

function lanesForCurrentView(data: LooseObj): LaneView[] {
  return getLanes(data).filter((lane) => Boolean(lane.archived) === showArchivedLanes);
}

function syncArchivedViewToolbar(): void {
  const toggleBtn = document.getElementById("lanes-show-archived-btn") as HTMLButtonElement | null;
  const createBtn = document.getElementById("create-lane-btn");
  const createForm = document.getElementById("create-lane-form");
  if (toggleBtn) {
    toggleBtn.textContent = showArchivedLanes ? "Show active" : "Show archived";
    toggleBtn.setAttribute("aria-pressed", showArchivedLanes ? "true" : "false");
  }
  if (showArchivedLanes) {
    createBtn?.setAttribute("hidden", "");
    createForm?.setAttribute("hidden", "");
    showLaneCreateError("");
  } else {
    if (!createForm || createForm.hasAttribute("hidden")) {
      createBtn?.removeAttribute("hidden");
    }
  }
}

function trackingThreads() {
  const { active, snoozed } = partitionThreadsBySnooze(getCurrentThreads());
  return [...active, ...snoozed];
}

function threadPickerHtml(laneId: number, selectedIds: Set<string>): string {
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

function laneSummaryHtml(summary: LaneSummaryView | null, laneId: number): string {
  const err = laneSummaryErrors.get(laneId);
  if (err) {
    return `<p class="lane-summary-error">${escapeHtml(err)}</p>`;
  }
  if (laneSummaryPending.has(laneId)) {
    return `<p class="lane-summary-empty">Generating summary… this can take several minutes for lanes with long threads.</p>`;
  }
  if (!summary) {
    return `<p class="lane-summary-empty">No summary yet. Assign threads and click Refresh summary.</p>`;
  }
  const tone = summary.tone_overview.trim();
  const updated = summary.updated_at.trim();
  const metaParts: string[] = [];
  if (tone) metaParts.push(escapeHtml(tone));
  if (updated) metaParts.push(`Updated ${escapeHtml(updated.slice(0, 10))}`);
  const meta = metaParts.length
    ? `<p class="lane-summary-meta">${metaParts.join(" · ")}</p>`
    : "";
  const body = summary.summary.trim()
    ? `<p class="lane-summary-text">${escapeHtml(summary.summary)}</p>`
    : "";
  return `<div class="lane-summary">
    ${meta}
    ${body}
    ${listSection("Highlights", summary.highlights)}
    ${listSection("Current priorities", summary.current_priorities)}
    ${listSection("Waiting on others", summary.waiting_on_others)}
  </div>`;
}

function laneCardHtml(
  lane: LaneView,
  threadIds: string[],
  summary: LaneSummaryView | null,
  expanded: boolean,
  opts: { tabbed?: boolean; archivedView?: boolean } = {},
): string {
  const selected = new Set(threadIds);
  const threadLabels = threadIds
    .map((tid) => {
      const thread = getCurrentThreads().find((t) => t.id === tid);
      if (!thread) return "";
      return `<li>${escapeHtml(threadEmailSubject(thread))}</li>`;
    })
    .filter(Boolean)
    .join("");
  const threadsBlock = threadLabels
    ? `<ul class="lane-assigned-threads">${threadLabels}</ul>`
    : `<p class="lane-empty-threads">No threads yet.</p>`;
  const picker = expanded ? threadPickerHtml(lane.id, selected) : "";
  const header = opts.tabbed
    ? ""
    : `<header class="user-lane-header">
      <h2>${escapeHtml(lane.name)}</h2>
      <span class="lane-count-pill">${threadIds.length} thread${threadIds.length === 1 ? "" : "s"}</span>
    </header>`;
  const tag = opts.tabbed ? "div" : "article";
  const className = opts.tabbed ? "user-lane-panel" : "user-lane-card";
  const archiveLabel = opts.archivedView ? "Unarchive" : "Archive";
  return `<${tag} class="${className}" data-lane-id="${lane.id}">
    ${header}
    ${laneSummaryHtml(summary, lane.id)}
    ${threadsBlock}
    ${picker}
    <div class="user-lane-actions">
      <button type="button" class="lane-refresh-summary-btn" data-lane-id="${lane.id}"${threadIds.length && !laneSummaryPending.has(lane.id) ? "" : " disabled"}>
        ${laneSummaryPending.has(lane.id) ? "Refreshing…" : "Refresh summary"}
      </button>
      <button type="button" class="lane-edit-threads-btn" data-lane-id="${lane.id}">
        ${expanded ? "Done" : threadIds.length ? "Edit threads" : "Add threads"}
      </button>
      <button type="button" class="lane-archive-btn" data-lane-id="${lane.id}" data-lane-name="${escapeHtml(lane.name)}" data-archived="${opts.archivedView ? "true" : "false"}">
        ${archiveLabel}
      </button>
      <button type="button" class="lane-delete-btn" data-lane-id="${lane.id}" data-lane-name="${escapeHtml(lane.name)}">
        Delete lane
      </button>
    </div>
  </${tag}>`;
}

function renderDashboardLanesTabs(
  listEl: HTMLElement,
  lanes: LaneView[],
  data: NonNullable<ReturnType<typeof getCurrentData>>,
): void {
  const laneIds = new Set(lanes.map((lane) => lane.id));
  if (activeLaneTabId == null || !laneIds.has(activeLaneTabId)) {
    activeLaneTabId = lanes[0]?.id ?? null;
  }

  const tabButtons = lanes
    .map((lane) => {
      const threadIds = getLaneThreadIds(data, lane.id);
      const active = lane.id === activeLaneTabId;
      return `<button type="button" class="lane-tab${active ? " is-active" : ""}" role="tab" aria-selected="${active ? "true" : "false"}" id="lane-tab-${lane.id}" aria-controls="lane-panel-${lane.id}" data-lane-id="${lane.id}">
        <span class="lane-tab-label">${escapeHtml(lane.name)}</span>
        <span class="lane-tab-count">${threadIds.length}</span>
      </button>`;
    })
    .join("");

  const panels = lanes
    .map((lane) => {
      const threadIds = getLaneThreadIds(data, lane.id);
      const summary = getLaneSummary(data, lane.id);
      const expanded = assignLaneId === lane.id;
      const active = lane.id === activeLaneTabId;
      return `<div class="lanes-tab-panel${active ? " is-active" : ""}" role="tabpanel" id="lane-panel-${lane.id}" aria-labelledby="lane-tab-${lane.id}" data-lane-id="${lane.id}"${active ? "" : " hidden"}>
        ${laneCardHtml(lane, threadIds, summary, expanded, { tabbed: true, archivedView: showArchivedLanes })}
      </div>`;
    })
    .join("");

  listEl.innerHTML = `<div class="lanes-tabs">
    <div class="lanes-tab-bar" role="tablist" aria-label="Lanes">${tabButtons}</div>
    <div class="lanes-tab-panels">${panels}</div>
  </div>`;
}

function renderLanesList(): void {
  const listEl = document.getElementById("lanes-list");
  const data = getCurrentData();
  if (!listEl || !data) return;

  const lanes = sortLanes(lanesForCurrentView(data), getLaneSortMode(), data);
  syncLaneSortSelect();
  syncArchivedViewToolbar();
  if (!lanes.length) {
    listEl.innerHTML = showArchivedLanes
      ? `<p class="lanes-empty">No archived lanes.</p>`
      : `<p class="lanes-empty">No lanes yet. Create one to group threads.</p>`;
    activeLaneTabId = null;
    return;
  }

  if (isDashboardLanesList(listEl)) {
    renderDashboardLanesTabs(listEl, lanes, data);
    return;
  }

  listEl.innerHTML = lanes
    .map((lane) => {
      const threadIds = getLaneThreadIds(data, lane.id);
      const summary = getLaneSummary(data, lane.id);
      const expanded = assignLaneId === lane.id;
      return laneCardHtml(lane, threadIds, summary, expanded, { archivedView: showArchivedLanes });
    })
    .join("");
}

export { renderLanesList };

async function persistLaneCreate(name: string): Promise<LaneView> {
  const res = await fetch("/api/lanes/create", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  const body = (await res.json().catch(() => ({}))) as LooseObj;
  if (!res.ok) throw new Error(str(body.error) || `Create lane failed (${res.status})`);
  const laneRaw = body.lane as LooseObj;
  return {
    id: Number(laneRaw.id) || 0,
    name: str(laneRaw.name) || name,
    created_at: str(laneRaw.created_at),
    updated_at: str(laneRaw.updated_at),
    archived: Boolean(laneRaw.archived),
  };
}

async function persistLaneThread(laneId: number, threadId: string, inLane: boolean): Promise<void> {
  const path = inLane ? "/api/lanes/add-thread" : "/api/lanes/remove-thread";
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ lane_id: laneId, thread_id: threadId }),
  });
  const body = (await res.json().catch(() => ({}))) as LooseObj;
  if (!res.ok) throw new Error(str(body.error) || `Lane update failed (${res.status})`);
}

function isTransientFetchError(err: unknown): boolean {
  if (!(err instanceof Error)) return false;
  if (err.name === "AbortError" || err.name === "NetworkError") return true;
  const msg = err.message.toLowerCase();
  return msg.includes("networkerror") || msg.includes("failed to fetch") || msg.includes("network error");
}

async function fetchLaneSummaryStatusResilient(laneId: number, maxAttempts = 8): Promise<LooseObj> {
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    try {
      return await fetchLaneSummaryStatus(laneId);
    } catch (err) {
      if (!isTransientFetchError(err) || attempt === maxAttempts) throw err;
      await sleep(Math.min(3000 * attempt, 15000));
    }
  }
  throw new Error("Lane summary status unreachable");
}

async function fetchLaneSummaryStatus(laneId: number): Promise<LooseObj> {
  const res = await fetch(`/api/lanes/summary?lane_id=${laneId}`, {
    credentials: "same-origin",
  });
  const body = (await res.json().catch(() => ({}))) as LooseObj;
  if (!res.ok) {
    throw new Error(str(body.error) || `Lane summary status failed (${res.status})`);
  }
  return body;
}

async function waitForLaneSummary(laneId: number, maxWaitMs = 20 * 60 * 1000): Promise<LooseObj> {
  const start = Date.now();
  let transientFailures = 0;
  while (Date.now() - start < maxWaitMs) {
    try {
      const body = await fetchLaneSummaryStatus(laneId);
      transientFailures = 0;
      if (body.ok === false) {
        throw new Error(str(body.error) || "Lane summary failed");
      }
      if (!body.pending && laneSummaryHasContent(body)) {
        return body;
      }
    } catch (err) {
      if (isTransientFetchError(err) && Date.now() - start < maxWaitMs) {
        transientFailures += 1;
        await sleep(Math.min(3000 * transientFailures, 15000));
        continue;
      }
      throw err;
    }
    await sleep(3000);
  }
  throw new Error(
    "Lane summary is still running. You can leave this page and click Refresh summary again later.",
  );
}

async function startLaneSummaryJob(laneId: number, force = false): Promise<LooseObj> {
  for (let attempt = 1; attempt <= 8; attempt++) {
    try {
      const res = await fetch("/api/lanes/summary", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ lane_id: laneId, force }),
      });
      const body = (await res.json().catch(() => ({}))) as LooseObj;
      if (!res.ok || body.ok === false) {
        throw new Error(str(body.error) || `Lane summary failed (${res.status})`);
      }
      return body;
    } catch (err) {
      if (!isTransientFetchError(err) || attempt === 8) throw err;
      await sleep(Math.min(3000 * attempt, 15000));
    }
  }
  throw new Error("Lane summary start unreachable");
}

function resetLaneRefreshButton(laneId: number): void {
  const btn = document.querySelector(
    `.lane-refresh-summary-btn[data-lane-id="${laneId}"]`,
  ) as HTMLButtonElement | null;
  if (btn) {
    btn.disabled = false;
    btn.textContent = "Refresh summary";
  }
}

function handleLaneSummaryComplete(laneId: number, body: LooseObj): void {
  laneSummaryPending.delete(laneId);
  applyLaneSummary(laneId, body);
  clearSummariesBundleCache();
  void reloadLanesFromServer();
}

function handleLaneSummaryError(laneId: number, err: unknown): void {
  if (isTransientFetchError(err)) {
    laneSummaryErrors.delete(laneId);
    laneSummaryPending.add(laneId);
    if (!laneSummaryWatching.has(laneId)) {
      watchLaneSummaryCompletion(laneId);
    }
    reloadFromStore();
    return;
  }
  laneSummaryPending.delete(laneId);
  const msg = err instanceof Error ? err.message : String(err);
  laneSummaryErrors.set(laneId, msg);
  console.error(err);
  reloadFromStore();
}

function watchLaneSummaryCompletion(laneId: number): void {
  laneSummaryPending.add(laneId);
  if (laneSummaryWatching.has(laneId)) return;
  laneSummaryWatching.add(laneId);
  void (async () => {
    try {
      const body = await waitForLaneSummary(laneId);
      handleLaneSummaryComplete(laneId, body);
    } catch (err) {
      handleLaneSummaryError(laneId, err);
    } finally {
      laneSummaryWatching.delete(laneId);
      resetLaneRefreshButton(laneId);
    }
  })();
}

/** Re-attach pending UI after navigation/refresh while server jobs keep running. */
export async function syncLaneSummaryJobsFromServer(): Promise<void> {
  const data = getCurrentData();
  if (!data) return;
  const lanes = lanesForCurrentView(data);
  if (!lanes.length) return;

  const restored: number[] = [];
  const reconciled: number[] = [];
  await Promise.all(
    lanes.map(async (lane) => {
      if (laneSummaryWatching.has(lane.id)) {
        if (!laneSummaryPending.has(lane.id)) {
          laneSummaryPending.add(lane.id);
          reconciled.push(lane.id);
        }
        return;
      }
      if (laneSummaryPending.has(lane.id)) return;
      try {
        const body = await fetchLaneSummaryStatusResilient(lane.id);
        if (body.pending === true) {
          laneSummaryErrors.delete(lane.id);
          laneSummaryPending.add(lane.id);
          restored.push(lane.id);
          watchLaneSummaryCompletion(lane.id);
        }
      } catch (err) {
        if (isTransientFetchError(err)) {
          laneSummaryErrors.delete(lane.id);
          laneSummaryPending.add(lane.id);
          restored.push(lane.id);
          watchLaneSummaryCompletion(lane.id);
        }
      }
    }),
  );
  if (restored.length || reconciled.length) {
    renderLanesList();
  }
}

async function persistLaneArchive(laneId: number, archived: boolean): Promise<void> {
  const res = await fetch("/api/lanes/archive", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ lane_id: laneId, archived }),
  });
  const body = (await res.json().catch(() => ({}))) as LooseObj;
  if (!res.ok) throw new Error(str(body.error) || `Archive lane failed (${res.status})`);
}

async function persistLaneDelete(laneId: number): Promise<void> {
  const res = await fetch("/api/lanes/delete", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ lane_id: laneId }),
  });
  const body = (await res.json().catch(() => ({}))) as LooseObj;
  if (!res.ok) throw new Error(str(body.error) || `Delete lane failed (${res.status})`);
}

function isLaneUi(target: EventTarget | null): boolean {
  return !!(
    target instanceof Element &&
    target.closest(".view-lanes, .dashboard-lanes-section")
  );
}

function showLaneCreateError(message: string): void {
  const errEl = document.getElementById("lane-create-error");
  if (!errEl) return;
  if (!message) {
    errEl.textContent = "";
    errEl.hidden = true;
    return;
  }
  errEl.textContent = message;
  errEl.hidden = false;
}

async function reloadLanesFromServer(): Promise<void> {
  clearSummariesBundleCache();
  try {
    const { data, label } = await loadLatestBundle();
    setBundle(normalizeBundle(data), label);
  } catch {
    const data = getCurrentData();
    if (data) setBundle(data, getCurrentSourceLabel());
  }
  renderLanesList();
}

function reloadFromStore(): void {
  const data = getCurrentData();
  if (data) {
    setBundle(data, getCurrentSourceLabel());
    renderLanesList();
  }
}

export function mountLanesPage(root: HTMLElement): void {
  root.innerHTML = PAGE_HTML;
}

export async function renderLanesPage(): Promise<void> {
  const data = getCurrentData();
  if (!data) return;

  await syncLaneSummaryJobsFromServer();
  renderLanesList();
}

export function bindLanesInteractions(): void {
  if (interactionsBound) return;
  interactionsBound = true;

  document.addEventListener("click", (ev) => {
    const target = ev.target as HTMLElement | null;
    if (!target || !isLaneUi(target)) return;

    if (target.closest("#lanes-show-archived-btn")) {
      showArchivedLanes = !showArchivedLanes;
      assignLaneId = null;
      activeLaneTabId = null;
      renderLanesList();
      return;
    }

    if (target.closest("#create-lane-btn")) {
      const form = document.getElementById("create-lane-form");
      const btn = document.getElementById("create-lane-btn");
      form?.removeAttribute("hidden");
      btn?.setAttribute("hidden", "");
      showLaneCreateError("");
      document.getElementById("lane-name-input")?.focus();
      return;
    }

    if (target.closest("#create-lane-cancel")) {
      const form = document.getElementById("create-lane-form") as HTMLFormElement | null;
      const btn = document.getElementById("create-lane-btn");
      form?.reset();
      form?.setAttribute("hidden", "");
      btn?.removeAttribute("hidden");
      showLaneCreateError("");
      return;
    }

    const tabBtn = target.closest(".lane-tab") as HTMLButtonElement | null;
    if (tabBtn) {
      const laneId = Number(tabBtn.dataset.laneId) || 0;
      if (!laneId || laneId === activeLaneTabId) return;
      activeLaneTabId = laneId;
      assignLaneId = null;
      renderLanesList();
      return;
    }

    const editBtn = target.closest(".lane-edit-threads-btn") as HTMLButtonElement | null;
    if (editBtn) {
      const laneId = Number(editBtn.dataset.laneId) || 0;
      assignLaneId = assignLaneId === laneId ? null : laneId;
      renderLanesList();
      return;
    }

    const refreshBtn = target.closest(".lane-refresh-summary-btn") as HTMLButtonElement | null;
    if (refreshBtn && !refreshBtn.disabled) {
      const laneId = Number(refreshBtn.dataset.laneId) || 0;
      if (!laneId) return;
      laneSummaryErrors.delete(laneId);
      laneSummaryPending.add(laneId);
      refreshBtn.disabled = true;
      refreshBtn.textContent = "Refreshing…";
      reloadFromStore();
      void (async () => {
        try {
          const body = await startLaneSummaryJob(laneId, true);
          if (body.pending) {
            watchLaneSummaryCompletion(laneId);
            return;
          }
          if (!laneSummaryHasContent(body)) {
            throw new Error(str(body.error) || "Lane summary returned no content");
          }
          handleLaneSummaryComplete(laneId, body);
          resetLaneRefreshButton(laneId);
        } catch (err) {
          handleLaneSummaryError(laneId, err);
          resetLaneRefreshButton(laneId);
        }
      })();
      return;
    }

    const archiveBtn = target.closest(".lane-archive-btn") as HTMLButtonElement | null;
    if (archiveBtn) {
      const laneId = Number(archiveBtn.dataset.laneId) || 0;
      const laneName = str(archiveBtn.dataset.laneName) || "this lane";
      const archived = archiveBtn.dataset.archived === "true";
      if (!laneId) return;
      const action = archived ? "Unarchive" : "Archive";
      if (!window.confirm(`${action} lane "${laneName}"?`)) return;
      archiveBtn.disabled = true;
      void (async () => {
        applyLaneArchived(laneId, !archived);
        if (assignLaneId === laneId) assignLaneId = null;
        if (activeLaneTabId === laneId) activeLaneTabId = null;
        renderLanesList();
        try {
          await persistLaneArchive(laneId, !archived);
          reloadFromStore();
        } catch (err) {
          console.error(err);
          try {
            const { data, label } = await loadLatestBundle();
            setBundle(normalizeBundle(data), label);
            renderLanesList();
          } catch {
            /* keep optimistic state; user can refresh */
          }
        }
      })();
      return;
    }

    const deleteBtn = target.closest(".lane-delete-btn") as HTMLButtonElement | null;
    if (deleteBtn) {
      const laneId = Number(deleteBtn.dataset.laneId) || 0;
      const laneName = str(deleteBtn.dataset.laneName) || "this lane";
      if (!laneId) return;
      if (!window.confirm(`Delete lane "${laneName}"? This cannot be undone.`)) return;
      deleteBtn.disabled = true;
      void (async () => {
        applyLaneRemoved(laneId);
        if (assignLaneId === laneId) assignLaneId = null;
        if (activeLaneTabId === laneId) activeLaneTabId = null;
        renderLanesList();
        try {
          await persistLaneDelete(laneId);
          reloadFromStore();
        } catch (err) {
          console.error(err);
          try {
            const { data, label } = await loadLatestBundle();
            setBundle(normalizeBundle(data), label);
            renderLanesList();
          } catch {
            /* keep optimistic state cleared; user can refresh */
          }
        }
      })();
      return;
    }
  });

  document.addEventListener("submit", (ev) => {
    const form = (ev.target as HTMLElement | null)?.closest("#create-lane-form");
    if (!form || !isLaneUi(form)) return;
    ev.preventDefault();
    void (async () => {
      const input = document.getElementById("lane-name-input") as HTMLInputElement | null;
      const name = input?.value.trim() ?? "";
      if (!name) return;
      const submitBtn = form.querySelector('button[type="submit"]') as HTMLButtonElement | null;
      if (submitBtn) submitBtn.disabled = true;
      showLaneCreateError("");
      try {
        const lane = await persistLaneCreate(name);
        applyLaneCreated(lane);
        assignLaneId = lane.id;
        activeLaneTabId = lane.id;
        form.setAttribute("hidden", "");
        document.getElementById("create-lane-btn")?.removeAttribute("hidden");
        (form as HTMLFormElement).reset();
        await reloadLanesFromServer();
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        showLaneCreateError(msg);
        console.error(err);
      } finally {
        if (submitBtn) submitBtn.disabled = false;
      }
    })();
  });

  document.addEventListener("change", (ev) => {
    const sortSelect = (ev.target as HTMLElement | null)?.closest(
      "#lanes-sort",
    ) as HTMLSelectElement | null;
    if (sortSelect && isLaneUi(sortSelect)) {
      const mode = sortSelect.value;
      if (isLaneSortMode(mode)) {
        setLaneSortMode(mode);
        renderLanesList();
      }
      return;
    }

    const checkbox = (ev.target as HTMLElement | null)?.closest(
      ".lane-thread-checkbox",
    ) as HTMLInputElement | null;
    if (!checkbox || !isLaneUi(checkbox)) return;
    const laneId = Number(checkbox.dataset.laneId) || 0;
    const threadId = str(checkbox.dataset.threadId);
    if (!laneId || !threadId) return;
    void (async () => {
      const inLane = checkbox.checked;
      applyLaneThreadMembership(laneId, threadId, inLane);
      try {
        await persistLaneThread(laneId, threadId, inLane);
        clearSummariesBundleCache();
        reloadFromStore();
      } catch (err) {
        applyLaneThreadMembership(laneId, threadId, !inLane);
        checkbox.checked = !inLane;
        console.error(err);
      }
    })();
  });
}
