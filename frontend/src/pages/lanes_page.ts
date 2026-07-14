import {
  listSection,
  partitionThreadsBySnooze,
  threadEmailSubject,
} from "../shared/thread_domain.js";
import {
  applyLaneArchived,
  applyLaneAreaAssigned,
  applyLaneCreated,
  applyLaneRemoved,
  applyLaneSummary,
  applyLaneThreadMembership,
  clearSummariesBundleCache,
  getCurrentData,
  getCurrentSourceLabel,
  getCurrentThreads,
  getLaneAreas,
  getLaneSummary,
  getLaneThreadIds,
  getLanes,
  getBundleMutationGeneration,
  loadLatestBundle,
  normalizeBundle,
  setBundle,
  setBundleFromNetwork,
} from "../shared/summaries_store.js";
import { laneAreaColorVar, sourcePillHtml, threadChannelForThread } from "../shared/source_ui.js";
import {
  escapeHtml,
  formatRelativeShort,
  formatSummaryUpdated,
  str,
  threadPageHref,
} from "../shared/utils.js";
import type { LaneAreaView, LaneSummaryView, LaneView, LooseObj, ThreadView } from "../shared/types.js";

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
let activeAreaTabId: number | null = null;
let showArchivedLanes = false;
const expandedTracks = new Set<number>();
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

function areaTabTrackCount(data: LooseObj, areaId: number): number {
  const base = getLanes(data);
  if (areaId === 0) return base.filter((lane) => lane.area_id == null).length;
  return base.filter((lane) => lane.area_id === areaId).length;
}

function trackAreaSelectHtml(lane: LaneView, data: LooseObj): string {
  const areas = getLaneAreas(data);
  if (areas.length <= 1) return "";
  const options = areas
    .map((area) => {
      const selected = lane.area_id === area.id ? " selected" : "";
      return `<option value="${area.id}"${selected}>${escapeHtml(area.name)}</option>`;
    })
    .join("");
  return `<label class="track-area-label">Lane tab
    <select class="track-area-select thread-sort-select" data-lane-id="${lane.id}" aria-label="Move track to lane tab">${options}</select>
  </label>`;
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

function lanePickerThreads(selectedIds: Set<string>): ThreadView[] {
  const tracked = trackingThreads();
  const trackedIds = new Set(tracked.map((t) => t.id));
  const orphans = [...selectedIds].filter((id) => !trackedIds.has(id));
  if (!orphans.length) return tracked;
  const allThreads = getCurrentThreads();
  const extra = orphans.map((id) => {
    const found = allThreads.find((t) => t.id === id);
    if (found) return found;
    return {
      id,
      messages: [{ cleaned: { subject: id }, summary: null }],
    } as ThreadView;
  });
  return [...tracked, ...extra];
}

function threadPickerHtml(laneId: number, selectedIds: Set<string>): string {
  const threads = lanePickerThreads(selectedIds);
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

function channelForHighlight(highlight: string, threads: ThreadView[]): ReturnType<typeof threadChannelForThread> {
  const text = highlight.toLowerCase();
  for (const thread of threads) {
    const subject = threadEmailSubject(thread).toLowerCase();
    if (subject.length >= 4 && (text.includes(subject.slice(0, 24)) || subject.includes(text.slice(0, 24)))) {
      return threadChannelForThread(thread);
    }
  }
  return "email";
}

function highlightsSectionHtml(highlights: string[], threadIds: string[]): string {
  if (!highlights.length) return "";
  const threads = getCurrentThreads().filter((t) => threadIds.includes(t.id));
  const items = highlights
    .map((highlight) => {
      const channel = channelForHighlight(highlight, threads);
      return `<li class="source-highlight" data-source="${channel}">${sourcePillHtml(channel)}${escapeHtml(highlight)}</li>`;
    })
    .join("");
  return `<div class="section"><h4>Highlights</h4><ul class="source-highlight-list">${items}</ul></div>`;
}

function laneSummaryHtml(
  summary: LaneSummaryView | null,
  laneId: number,
  threadIds: string[] = [],
  trackMode = false,
): string {
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
  if (updated) metaParts.push(`Updated ${escapeHtml(formatSummaryUpdated(updated))}`);
  const meta = metaParts.length
    ? `<p class="lane-summary-meta">${metaParts.join(" · ")}</p>`
    : "";
  const body = summary.summary.trim()
    ? `<p class="lane-summary-text">${escapeHtml(summary.summary)}</p>`
    : "";
  return `<div class="lane-summary">
    ${meta}
    ${body}
    ${trackMode ? listSection("Highlights", summary.highlights) : highlightsSectionHtml(summary.highlights, threadIds)}
    ${listSection("Current priorities", summary.current_priorities)}
    ${listSection("Waiting on others", summary.waiting_on_others)}
  </div>`;
}

function laneCardHtml(
  lane: LaneView,
  threadIds: string[],
  summary: LaneSummaryView | null,
  expanded: boolean,
  opts: {
    tabbed?: boolean;
    trackMode?: boolean;
    archivedView?: boolean;
    linkThreads?: boolean;
    expanded?: boolean;
  } = {},
): string {
  const selected = new Set(threadIds);
  const threadLabels = threadIds
    .map((tid) => {
      const thread = getCurrentThreads().find((t) => t.id === tid);
      const label = escapeHtml(thread ? threadEmailSubject(thread) : tid);
      const pill = thread ? sourcePillHtml(threadChannelForThread(thread)) : "";
      const planBtn = thread
        ? `<button type="button" class="create-plan-btn lane-thread-create-plan-btn" data-add-plan-thread-id="${escapeHtml(tid)}">Create a plan</button>`
        : "";
      const removeBtn = `<button type="button" class="lane-thread-remove-btn" data-lane-id="${lane.id}" data-thread-id="${escapeHtml(tid)}" title="Remove from track" aria-label="Remove from track">&times;</button>`;
      if (opts.linkThreads && thread) {
        return `<li><a class="lane-thread-link" href="${escapeHtml(threadPageHref(tid))}">${pill}${label}</a>${planBtn}${removeBtn}</li>`;
      }
      return `<li>${pill}${label}${planBtn}${removeBtn}</li>`;
    })
    .filter(Boolean)
    .join("");
  const threadsBlock = threadLabels
    ? `<ul class="lane-assigned-threads">${threadLabels}</ul>`
    : `<p class="lane-empty-threads">No threads yet.</p>`;
  const picker = expanded ? threadPickerHtml(lane.id, selected) : "";
  const summaryPreview = summary?.summary?.trim()
    ? escapeHtml(summary.summary.trim().slice(0, 120))
    : "No summary yet.";
  const data = getCurrentData();
  const areaSelect =
    opts.trackMode && data ? trackAreaSelectHtml(lane, data) : "";
  const actions = `<div class="item-actions user-lane-actions" role="toolbar" aria-label="Track actions">
      <div class="item-actions-group">
      ${areaSelect}
      <button type="button" class="btn btn--primary lane-refresh-summary-btn" data-lane-id="${lane.id}"${threadIds.length && !laneSummaryPending.has(lane.id) ? "" : " disabled"}>
        ${laneSummaryPending.has(lane.id) ? "Refreshing…" : "Refresh summary"}
      </button>
      <button type="button" class="btn btn--default lane-edit-threads-btn" data-lane-id="${lane.id}">
        ${expanded ? "Done" : threadIds.length ? "Edit threads" : "Add threads"}
      </button>
      </div>
      <div class="item-actions-group">
      <button type="button" class="btn btn--ghost lane-archive-btn" data-lane-id="${lane.id}" data-lane-name="${escapeHtml(lane.name)}" data-archived="${opts.archivedView ? "true" : "false"}" title="${opts.archivedView ? "Restore to dashboard" : "Hide from dashboard"}">
        ${opts.archivedView ? "Unarchive" : "Archive"}
      </button>
      <button type="button" class="btn btn--danger lane-delete-btn" data-lane-id="${lane.id}" data-lane-name="${escapeHtml(lane.name)}" title="Delete this lane">
        Delete lane
      </button>
      </div>
    </div>`;

  if (opts.trackMode) {
    const isExpanded = opts.expanded === true;
    const threadCountLabel = `${threadIds.length} thread${threadIds.length === 1 ? "" : "s"}`;
    const latestAt = laneLatestThreadMessageAt(getCurrentData() || {}, lane.id);
    const relativeUpdated = formatRelativeShort(latestAt || summary?.updated_at || lane.updated_at || lane.created_at);
    return `<li class="track-section${isExpanded ? "" : " is-collapsed"}" data-lane-id="${lane.id}">
      <header class="lane-section-head track-head" data-track-toggle="${lane.id}">
        <span class="lane-collapse-icon" aria-hidden="true"></span>
        <div class="lane-name-wrap">
          <h4 class="track-name">${escapeHtml(lane.name)}</h4>
          <span class="track-summary-preview">${summaryPreview}</span>
        </div>
        <span class="lane-track-count">${threadCountLabel}</span>
        ${relativeUpdated ? `<span class="track-meta-head">${escapeHtml(relativeUpdated)}</span>` : ""}
      </header>
      <div class="track-body">
        ${laneSummaryHtml(summary, lane.id, threadIds, true)}
        ${threadsBlock}
        ${picker}
        ${actions}
      </div>
    </li>`;
  }

  const legacyActions = `<div class="user-lane-actions item-actions">
      ${areaSelect}
      <button type="button" class="lane-refresh-summary-btn" data-lane-id="${lane.id}"${threadIds.length && !laneSummaryPending.has(lane.id) ? "" : " disabled"}>
        ${laneSummaryPending.has(lane.id) ? "Refreshing…" : "Refresh summary"}
      </button>
      <button type="button" class="lane-edit-threads-btn" data-lane-id="${lane.id}">
        ${expanded ? "Done" : threadIds.length ? "Edit threads" : "Add threads"}
      </button>
      <button type="button" class="lane-archive-btn" data-lane-id="${lane.id}" data-lane-name="${escapeHtml(lane.name)}" data-archived="${opts.archivedView ? "true" : "false"}">
        ${opts.archivedView ? "Unarchive" : "Archive"}
      </button>
      <button type="button" class="lane-delete-btn" data-lane-id="${lane.id}" data-lane-name="${escapeHtml(lane.name)}">
        Delete lane
      </button>
    </div>`;

  const header = opts.tabbed
    ? ""
    : `<header class="user-lane-header">
      <h2>${escapeHtml(lane.name)}</h2>
      <span class="lane-count-pill">${threadIds.length} thread${threadIds.length === 1 ? "" : "s"}</span>
    </header>`;
  const tag = opts.tabbed ? "div" : "article";
  const className = opts.tabbed ? "user-lane-panel" : "user-lane-card";
  return `<${tag} class="${className}" data-lane-id="${lane.id}">
    ${header}
    ${laneSummaryHtml(summary, lane.id)}
    ${threadsBlock}
    ${picker}
    ${legacyActions}
  </${tag}>`;
}

function areaTabTracks(data: LooseObj, areaId: number): LaneView[] {
  const base = lanesForCurrentView(data);
  const filtered =
    areaId === 0
      ? base.filter((l) => l.area_id == null)
      : base.filter((l) => l.area_id === areaId);
  return sortLanes(filtered, getLaneSortMode(), data);
}

function renderAreaToolbar(): string {
  return `<div class="thread-toolbar dashboard-lanes-toolbar" id="lanes-toolbar">
    <div class="thread-control-group">
      <span class="thread-control-label" id="track-inbox-label">Show</span>
      <div class="thread-segmented" role="group" aria-labelledby="track-inbox-label">
        <button type="button" class="lanes-show-archived-btn${showArchivedLanes ? "" : " active"}" data-track-inbox="active" aria-pressed="${showArchivedLanes ? "false" : "true"}">Active</button>
        <button type="button" class="lanes-show-archived-btn${showArchivedLanes ? " active" : ""}" data-track-inbox="archived" aria-pressed="${showArchivedLanes ? "true" : "false"}">Archived</button>
      </div>
    </div>
    <div class="thread-control-group">
      <label class="thread-control-label" for="lanes-sort">Sort</label>
      <select id="lanes-sort" class="lanes-sort-select thread-sort-select" aria-label="Sort tracks">
        <option value="updated-desc">Recently updated</option>
        <option value="created-desc">Recently added</option>
      </select>
    </div>
    <div class="thread-control-group">
      <button type="button" class="create-lane-btn" id="create-lane-btn">Create track</button>
    </div>
  </div>`;
}

function renderDashboardLaneAreaTabs(
  listEl: HTMLElement,
  data: NonNullable<ReturnType<typeof getCurrentData>>,
): void {
  const areas = getLaneAreas(data);
  const unassigned = lanesForCurrentView(data).filter((l) => l.area_id == null);
  const tabs: Array<{ id: number; name: string; color_index: number }> = [
    ...areas.map((a) => ({ id: a.id, name: a.name, color_index: a.color_index })),
  ];
  if (unassigned.length) tabs.push({ id: 0, name: "Unassigned", color_index: 0 });

  if (!tabs.length) {
    renderDashboardLanesTabs(listEl, lanesForCurrentView(data), data);
    return;
  }

  const tabIds = new Set(tabs.map((t) => t.id));
  if (activeAreaTabId == null || !tabIds.has(activeAreaTabId)) {
    activeAreaTabId = tabs[0]?.id ?? null;
  }

  const tabButtons = tabs
    .map((tab) => {
      const trackCount = areaTabTrackCount(data, tab.id);
      const active = tab.id === activeAreaTabId;
      const color = laneAreaColorVar(tab.color_index);
      return `<button type="button" class="lane-tab${active ? " is-active" : ""}" role="tab" aria-selected="${active ? "true" : "false"}" id="area-tab-${tab.id}" aria-controls="area-panel-${tab.id}" data-area-id="${tab.id}">
        <span class="lane-tab-color" style="background: ${color};"></span>
        <span class="lane-tab-label">${escapeHtml(tab.name)}</span>
        <span class="lane-tab-count">${trackCount}</span>
      </button>`;
    })
    .join("");

  const panels = tabs
    .map((tab) => {
      const tracks = areaTabTracks(data, tab.id);
      const active = tab.id === activeAreaTabId;
      const trackItems = tracks
        .map((lane) => {
          const threadIds = getLaneThreadIds(data, lane.id);
          const summary = getLaneSummary(data, lane.id);
          const expanded = assignLaneId === lane.id;
          const trackExpanded = expandedTracks.has(lane.id) || expanded;
          return laneCardHtml(lane, threadIds, summary, expanded, {
            trackMode: true,
            archivedView: showArchivedLanes,
            linkThreads: true,
            expanded: trackExpanded,
          });
        })
        .join("");
      const empty = tracks.length
        ? `<ul class="track-stack">${trackItems}</ul>`
        : `<p class="tracks-empty">No tracks in this lane.</p>`;
      return `<div class="lanes-tab-panel${active ? " is-active" : ""}" role="tabpanel" id="area-panel-${tab.id}" aria-labelledby="area-tab-${tab.id}" data-area-id="${tab.id}"${active ? "" : " hidden"}>
        ${active ? renderAreaToolbar() : ""}
        ${empty}
      </div>`;
    })
    .join("");

  listEl.innerHTML = `<div class="lanes-tabs">
    <div class="lanes-tab-bar" role="tablist" aria-label="Lanes">${tabButtons}</div>
    <div class="lanes-tab-panels">${panels}</div>
  </div>`;
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
        ${laneCardHtml(lane, threadIds, summary, expanded, { tabbed: true, archivedView: showArchivedLanes, linkThreads: true })}
      </div>`;
    })
    .join("");

  listEl.innerHTML = `${renderAreaToolbar()}<div class="lanes-tabs">
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
    const emptyMessage = showArchivedLanes
      ? `<p class="lanes-empty">No archived lanes.</p>`
      : `<p class="lanes-empty">No lanes yet. Create one to group threads.</p>`;
    listEl.innerHTML = isDashboardLanesList(listEl)
      ? `${renderAreaToolbar()}${emptyMessage}`
      : emptyMessage;
    activeLaneTabId = null;
    return;
  }

  if (isDashboardLanesList(listEl)) {
    if (getLaneAreas(data).length || getLanes(data).some((l) => l.area_id != null)) {
      renderDashboardLaneAreaTabs(listEl, data);
    } else {
      renderDashboardLanesTabs(listEl, lanes, data);
    }
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

async function persistLaneAssignArea(laneId: number, areaId: number): Promise<void> {
  const res = await fetch("/api/lanes/assign-area", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ lane_id: laneId, area_id: areaId }),
  });
  const body = (await res.json().catch(() => ({}))) as LooseObj;
  if (!res.ok) throw new Error(str(body.error) || `Assign area failed (${res.status})`);
}

async function persistLaneCreate(name: string, areaId?: number | null): Promise<LaneView> {
  const payload: LooseObj = { name };
  if (areaId != null && areaId > 0) payload.area_id = areaId;
  const res = await fetch("/api/lanes/create", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
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
    area_id: laneRaw.area_id == null ? null : Number(laneRaw.area_id) || null,
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
  const mutationGenAtFetch = getBundleMutationGeneration();
  try {
    const { data, label } = await loadLatestBundle();
    setBundleFromNetwork(normalizeBundle(data), label, mutationGenAtFetch);
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

    const addPlanBtn = target.closest("button.create-plan-btn") as HTMLButtonElement | null;
    if (addPlanBtn) {
      const threadId = str(addPlanBtn.dataset.addPlanThreadId);
      if (!threadId) return;
      void (async () => {
        const { openDashboardAddPlanForThread } = await import("../dashboard_schedule_rail.js");
        await openDashboardAddPlanForThread(threadId);
        const url = new URL(location.href);
        url.pathname = "/onebox";
        url.searchParams.set("thread", threadId);
        url.hash = "schedule-plans";
        history.pushState(null, "", `${url.pathname}${url.search}${url.hash}`);
      })();
      return;
    }

    const removeThreadBtn = target.closest(".lane-thread-remove-btn") as HTMLButtonElement | null;
    if (removeThreadBtn) {
      const laneId = Number(removeThreadBtn.dataset.laneId) || 0;
      const threadId = str(removeThreadBtn.dataset.threadId);
      if (!laneId || !threadId) return;
      removeThreadBtn.disabled = true;
      void (async () => {
        applyLaneThreadMembership(laneId, threadId, false);
        renderLanesList();
        try {
          await persistLaneThread(laneId, threadId, false);
          await reloadLanesFromServer();
        } catch (err) {
          applyLaneThreadMembership(laneId, threadId, true);
          renderLanesList();
          console.error(err);
          window.alert(err instanceof Error ? err.message : String(err));
        }
      })();
      return;
    }

    if (target.closest(".lanes-show-archived-btn")) {
      const btn = target.closest(".lanes-show-archived-btn") as HTMLButtonElement;
      showArchivedLanes = btn.dataset.trackInbox
        ? btn.dataset.trackInbox === "archived"
        : !showArchivedLanes;
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

    if (target.closest("#collapse-all-tracks")) {
      const data = getCurrentData();
      if (!data) return;
      expandedTracks.clear();
      renderLanesList();
      return;
    }

    if (target.closest("#expand-all-tracks")) {
      const data = getCurrentData();
      if (!data) return;
      for (const lane of areaTabTracks(data, activeAreaTabId ?? 0)) {
        expandedTracks.add(lane.id);
      }
      renderLanesList();
      return;
    }

    const trackHead = target.closest("[data-track-toggle]") as HTMLElement | null;
    if (trackHead) {
      const laneId = Number(trackHead.dataset.trackToggle) || 0;
      if (!laneId) return;
      if (expandedTracks.has(laneId)) expandedTracks.delete(laneId);
      else expandedTracks.add(laneId);
      renderLanesList();
      return;
    }

    const areaTabBtn = target.closest(".lane-tab[data-area-id]") as HTMLButtonElement | null;
    if (areaTabBtn) {
      const areaId = Number(areaTabBtn.dataset.areaId);
      if (Number.isNaN(areaId) || areaId === activeAreaTabId) return;
      activeAreaTabId = areaId;
      assignLaneId = null;
      renderLanesList();
      return;
    }

    const tabBtn = target.closest(".lane-tab[data-lane-id]") as HTMLButtonElement | null;
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
            const mutationGenAtFetch = getBundleMutationGeneration();
            const { data, label } = await loadLatestBundle();
            if (setBundleFromNetwork(normalizeBundle(data), label, mutationGenAtFetch)) {
              renderLanesList();
            }
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
            const mutationGenAtFetch = getBundleMutationGeneration();
            const { data, label } = await loadLatestBundle();
            if (setBundleFromNetwork(normalizeBundle(data), label, mutationGenAtFetch)) {
              renderLanesList();
            }
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
        const areaId = activeAreaTabId != null && activeAreaTabId > 0 ? activeAreaTabId : null;
        const lane = await persistLaneCreate(name, areaId);
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

    const areaSelect = (ev.target as HTMLElement | null)?.closest(
      ".track-area-select",
    ) as HTMLSelectElement | null;
    if (areaSelect && isLaneUi(areaSelect)) {
      const laneId = Number(areaSelect.dataset.laneId) || 0;
      const areaId = Number(areaSelect.value) || 0;
      if (!laneId || !areaId) return;
      const previous = getLanes(getCurrentData()).find((lane) => lane.id === laneId)?.area_id ?? null;
      if (previous === areaId) return;
      areaSelect.disabled = true;
      void (async () => {
        try {
          applyLaneAreaAssigned(laneId, areaId);
          renderLanesList();
          await persistLaneAssignArea(laneId, areaId);
          await reloadLanesFromServer();
        } catch (err) {
          applyLaneAreaAssigned(laneId, previous);
          renderLanesList();
          console.error(err);
          window.alert(err instanceof Error ? err.message : String(err));
        } finally {
          areaSelect.disabled = false;
        }
      })();
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
      renderLanesList();
      try {
        await persistLaneThread(laneId, threadId, inLane);
        await reloadLanesFromServer();
      } catch (err) {
        applyLaneThreadMembership(laneId, threadId, !inLane);
        checkbox.checked = !inLane;
        renderLanesList();
        console.error(err);
      }
    })();
  });
}
