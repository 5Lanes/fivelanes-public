/**
 * Modal for assigning a thread to a lane (area) and track, or creating new ones.
 */

import {
  applyLaneAreaCreated,
  applyLaneCreated,
  applyLaneThreadMembership,
  getCurrentData,
  getLaneAreas,
  getLanes,
} from "./shared/summaries_store.js";
import { threadEmailSubject, threadLabel } from "./shared/thread_domain.js";
import type { LaneAreaView, LaneView, LooseObj, ThreadView } from "./shared/types.js";
import { escapeHtml, str } from "./shared/utils.js";

type AreaOption = { id: number; name: string };

const LANE_AREA_COLORS = 6;

let dialogEl: HTMLDialogElement | null = null;
let activeThreadId = "";

function activeTracks(data: LooseObj): LaneView[] {
  return getLanes(data).filter((lane) => !lane.archived);
}

function areaOptions(data: LooseObj): AreaOption[] {
  const areas = getLaneAreas(data).map((a) => ({ id: a.id, name: a.name }));
  const hasUnassigned = activeTracks(data).some((lane) => lane.area_id == null);
  if (hasUnassigned) areas.push({ id: 0, name: "Unassigned" });
  return areas;
}

function tracksForArea(data: LooseObj, areaId: number): LaneView[] {
  const lanes = activeTracks(data);
  if (areaId <= 0) return lanes.filter((lane) => lane.area_id == null);
  return lanes.filter((lane) => lane.area_id === areaId);
}

function laneIdForThread(data: LooseObj, threadId: string): number | null {
  const memberships = data.lane_threads;
  if (!memberships || typeof memberships !== "object") return null;
  for (const [laneKey, ids] of Object.entries(memberships as LooseObj)) {
    if (!Array.isArray(ids) || !ids.map((id) => str(id)).includes(threadId)) continue;
    return Number(laneKey) || null;
  }
  return null;
}

function initialAreaId(data: LooseObj, lane: LaneView | null): number {
  const options = areaOptions(data);
  if (!options.length) return -1;
  if (lane?.area_id != null) return lane.area_id;
  if (lane) return 0;
  return options[0]?.id ?? -1;
}

function nextLaneColorIndex(data: LooseObj): number {
  return getLaneAreas(data).length % LANE_AREA_COLORS;
}

async function persistLaneAreaCreate(name: string, colorIndex: number): Promise<LaneAreaView> {
  const res = await fetch("/api/lane-areas", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, color_index: colorIndex }),
  });
  const body = (await res.json().catch(() => ({}))) as LooseObj;
  if (!res.ok) throw new Error(str(body.error) || `Create lane failed (${res.status})`);
  const areaRaw = body.lane_area as LooseObj;
  return {
    id: Number(areaRaw.id) || 0,
    name: str(areaRaw.name) || name,
    color_index: Number(areaRaw.color_index) || colorIndex,
    sort_order: Number(areaRaw.sort_order) || 0,
    created_at: str(areaRaw.created_at),
    updated_at: str(areaRaw.updated_at),
  };
}

async function persistLaneCreate(name: string, areaId: number | null): Promise<LaneView> {
  const payload: LooseObj = { name };
  if (areaId != null && areaId > 0) payload.area_id = areaId;
  const res = await fetch("/api/lanes/create", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const body = (await res.json().catch(() => ({}))) as LooseObj;
  if (!res.ok) throw new Error(str(body.error) || `Create track failed (${res.status})`);
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

async function persistLaneThread(laneId: number, threadId: string): Promise<void> {
  const res = await fetch("/api/lanes/add-thread", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ lane_id: laneId, thread_id: threadId }),
  });
  const body = (await res.json().catch(() => ({}))) as LooseObj;
  if (!res.ok) throw new Error(str(body.error) || `Add to track failed (${res.status})`);
}

function setError(message: string): void {
  const errEl = dialogEl?.querySelector(".add-to-lane-error") as HTMLElement | null;
  if (!errEl) return;
  if (message) {
    errEl.textContent = message;
    errEl.hidden = false;
  } else {
    errEl.textContent = "";
    errEl.hidden = true;
  }
}

function isCreatingNewLane(): boolean {
  return Boolean(
    (dialogEl?.querySelector("#add-to-lane-new-lane-check") as HTMLInputElement | null)?.checked,
  );
}

function populateTrackSelect(areaId: number, selectedTrackId = ""): void {
  const data = getCurrentData();
  const select = dialogEl?.querySelector("#add-to-lane-track-select") as HTMLSelectElement | null;
  if (!data || !select) return;
  const tracks = areaId === -1 ? activeTracks(data) : tracksForArea(data, areaId);
  if (!tracks.length) {
    select.innerHTML = `<option value="">No tracks yet — create one below</option>`;
    select.value = "";
    return;
  }
  select.innerHTML = tracks
    .map(
      (track) =>
        `<option value="${track.id}"${String(track.id) === selectedTrackId ? " selected" : ""}>${escapeHtml(track.name)}</option>`,
    )
    .join("");
}

function populateAreaSelect(selectedAreaId: number): void {
  const data = getCurrentData();
  const areaField = dialogEl?.querySelector("#add-to-lane-area-field") as HTMLElement | null;
  const select = dialogEl?.querySelector("#add-to-lane-area-select") as HTMLSelectElement | null;
  if (!data || !select || !areaField) return;
  const options = areaOptions(data);
  const hasRealAreas = getLaneAreas(data).length > 0;
  if (!hasRealAreas) {
    areaField.hidden = true;
    select.required = false;
    populateTrackSelect(-1);
    return;
  }
  if (!options.length) {
    areaField.hidden = true;
    select.required = false;
    populateTrackSelect(-1);
    return;
  }
  areaField.hidden = false;
  select.required = !isCreatingNewLane();
  select.innerHTML = options
    .map(
      (area) =>
        `<option value="${area.id}"${area.id === selectedAreaId ? " selected" : ""}>${escapeHtml(area.name)}</option>`,
    )
    .join("");
  populateTrackSelect(selectedAreaId);
}

function syncNewLaneMode(): void {
  const createNewLane = dialogEl?.querySelector("#add-to-lane-new-lane-check") as HTMLInputElement | null;
  const areaField = dialogEl?.querySelector("#add-to-lane-area-field") as HTMLElement | null;
  const newLaneField = dialogEl?.querySelector("#add-to-lane-new-lane-field") as HTMLElement | null;
  const areaSelect = dialogEl?.querySelector("#add-to-lane-area-select") as HTMLSelectElement | null;
  const newLaneNameInput = dialogEl?.querySelector("#add-to-lane-new-lane-name") as HTMLInputElement | null;
  const createNewTrack = dialogEl?.querySelector("#add-to-lane-new-track-check") as HTMLInputElement | null;
  const creating = Boolean(createNewLane?.checked);
  const data = getCurrentData();
  const hasRealAreas = data ? getLaneAreas(data).length > 0 : false;

  if (hasRealAreas) {
    areaField?.toggleAttribute("hidden", creating);
  }
  newLaneField?.toggleAttribute("hidden", !creating);
  if (areaSelect) {
    areaSelect.disabled = creating;
    areaSelect.required = hasRealAreas && !creating;
  }
  if (newLaneNameInput) newLaneNameInput.required = creating;

  if (creating) {
    if (createNewTrack) {
      createNewTrack.checked = true;
      createNewTrack.disabled = true;
    }
    populateTrackSelect(-1);
    syncNewTrackMode();
    newLaneNameInput?.focus();
    return;
  }

  if (createNewTrack) createNewTrack.disabled = false;
  const areaId = Number(areaSelect?.value) || 0;
  populateTrackSelect(hasRealAreas ? areaId : -1);
  syncNewTrackMode();
}

function syncNewTrackMode(): void {
  const createNew = dialogEl?.querySelector("#add-to-lane-new-track-check") as HTMLInputElement | null;
  const trackField = dialogEl?.querySelector("#add-to-lane-track-field") as HTMLElement | null;
  const newTrackField = dialogEl?.querySelector("#add-to-lane-new-track-field") as HTMLElement | null;
  const trackSelect = dialogEl?.querySelector("#add-to-lane-track-select") as HTMLSelectElement | null;
  const newNameInput = dialogEl?.querySelector("#add-to-lane-new-track-name") as HTMLInputElement | null;
  const creating = Boolean(createNew?.checked);
  trackField?.toggleAttribute("hidden", creating);
  newTrackField?.toggleAttribute("hidden", !creating);
  if (trackSelect) {
    trackSelect.disabled = creating;
    trackSelect.required = !creating;
  }
  if (newNameInput) {
    newNameInput.required = creating;
    if (creating && !isCreatingNewLane()) newNameInput.focus();
  }
}

function ensureDialog(): HTMLDialogElement {
  if (dialogEl) return dialogEl;

  const dialog = document.createElement("dialog");
  dialog.id = "add-to-lane-dialog";
  dialog.className = "add-to-lane-dialog";
  dialog.innerHTML = `
    <form method="dialog" class="add-to-lane-form">
      <header class="add-to-lane-head">
        <div>
          <h2>Add to lane</h2>
          <p class="add-to-lane-thread" id="add-to-lane-thread-label"></p>
        </div>
        <button type="button" class="add-to-lane-close" aria-label="Close">×</button>
      </header>
      <div class="add-to-lane-body">
        <label class="add-to-lane-field" id="add-to-lane-area-field">
          <span>Lane</span>
          <select id="add-to-lane-area-select" required></select>
        </label>
        <label class="add-to-lane-checkbox">
          <input type="checkbox" id="add-to-lane-new-lane-check" />
          <span>Create new lane</span>
        </label>
        <label class="add-to-lane-field" id="add-to-lane-new-lane-field" hidden>
          <span>New lane name</span>
          <input type="text" id="add-to-lane-new-lane-name" placeholder="e.g. Work" autocomplete="off" />
        </label>
        <label class="add-to-lane-field" id="add-to-lane-track-field">
          <span>Track</span>
          <select id="add-to-lane-track-select" required></select>
        </label>
        <label class="add-to-lane-checkbox">
          <input type="checkbox" id="add-to-lane-new-track-check" />
          <span>Create new track</span>
        </label>
        <label class="add-to-lane-field" id="add-to-lane-new-track-field" hidden>
          <span>New track name</span>
          <input type="text" id="add-to-lane-new-track-name" placeholder="e.g. Acme consulting" autocomplete="off" />
        </label>
        <p class="add-to-lane-error" hidden></p>
      </div>
      <footer class="add-to-lane-foot">
        <button type="button" class="add-to-lane-cancel">Cancel</button>
        <button type="submit" class="btn btn--primary" id="add-to-lane-submit">Add to lane</button>
      </footer>
    </form>`;
  document.body.appendChild(dialog);

  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
  dialog.querySelector(".add-to-lane-close")?.addEventListener("click", () => dialog.close());
  dialog.querySelector(".add-to-lane-cancel")?.addEventListener("click", () => dialog.close());
  dialog.querySelector("#add-to-lane-new-lane-check")?.addEventListener("change", () => syncNewLaneMode());
  dialog.querySelector("#add-to-lane-new-track-check")?.addEventListener("change", () => syncNewTrackMode());
  dialog.querySelector("#add-to-lane-area-select")?.addEventListener("change", () => {
    const areaId = Number((dialog.querySelector("#add-to-lane-area-select") as HTMLSelectElement)?.value) || 0;
    populateTrackSelect(areaId);
    const trackSelect = dialog.querySelector("#add-to-lane-track-select") as HTMLSelectElement | null;
    const createNewTrack = dialog.querySelector("#add-to-lane-new-track-check") as HTMLInputElement | null;
    if (createNewTrack && !trackSelect?.value) createNewTrack.checked = true;
    syncNewTrackMode();
  });

  dialog.querySelector(".add-to-lane-form")?.addEventListener("submit", (event) => {
    event.preventDefault();
    void submitAddToLane();
  });

  dialogEl = dialog;
  return dialog;
}

async function refreshAfterAssign(): Promise<void> {
  const { renderLanesList } = await import("./pages/lanes_page.js");
  renderLanesList();
  if (document.getElementById("dashboard-threads-root")) {
    const { renderDashboardThreadsInline } = await import("./pages/threads_page.js");
    await renderDashboardThreadsInline();
  }
}

async function submitAddToLane(): Promise<void> {
  const data = getCurrentData();
  const threadId = activeThreadId;
  if (!data || !threadId) return;

  const areaField = dialogEl?.querySelector("#add-to-lane-area-field") as HTMLElement | null;
  const areaSelect = dialogEl?.querySelector("#add-to-lane-area-select") as HTMLSelectElement | null;
  const createNewLane = dialogEl?.querySelector("#add-to-lane-new-lane-check") as HTMLInputElement | null;
  const newLaneNameInput = dialogEl?.querySelector("#add-to-lane-new-lane-name") as HTMLInputElement | null;
  const trackSelect = dialogEl?.querySelector("#add-to-lane-track-select") as HTMLSelectElement | null;
  const createNewTrack = dialogEl?.querySelector("#add-to-lane-new-track-check") as HTMLInputElement | null;
  const newTrackNameInput = dialogEl?.querySelector("#add-to-lane-new-track-name") as HTMLInputElement | null;
  const submitBtn = dialogEl?.querySelector("#add-to-lane-submit") as HTMLButtonElement | null;

  const creatingLane = Boolean(createNewLane?.checked);
  const creatingTrack = Boolean(createNewTrack?.checked);
  const newLaneName = newLaneNameInput?.value.trim() ?? "";
  const newTrackName = newTrackNameInput?.value.trim() ?? "";
  const selectedAreaId = areaField?.hidden ? -1 : Number(areaSelect?.value) || 0;
  const selectedTrackId = Number(trackSelect?.value) || 0;

  if (creatingLane && !newLaneName) {
    setError("Enter a name for the new lane.");
    newLaneNameInput?.focus();
    return;
  }
  if (creatingTrack && !newTrackName) {
    setError("Enter a name for the new track.");
    newTrackNameInput?.focus();
    return;
  }
  if (!creatingTrack && !selectedTrackId) {
    setError("Select a track or create a new one.");
    return;
  }

  setError("");
  if (submitBtn) submitBtn.disabled = true;

  try {
    let resolvedAreaId: number | null = selectedAreaId > 0 ? selectedAreaId : null;
    if (creatingLane) {
      const area = await persistLaneAreaCreate(newLaneName, nextLaneColorIndex(getCurrentData() || data));
      if (!area.id) throw new Error("Lane was not created.");
      applyLaneAreaCreated(area);
      resolvedAreaId = area.id;
    }

    let laneId = selectedTrackId;
    if (creatingTrack) {
      const lane = await persistLaneCreate(newTrackName, resolvedAreaId);
      if (!lane.id) throw new Error("Track was not created.");
      applyLaneCreated(lane);
      laneId = lane.id;
    }

    applyLaneThreadMembership(laneId, threadId, true);
    await persistLaneThread(laneId, threadId);
    dialogEl?.close();
    await refreshAfterAssign();
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    setError(msg);
    console.error(err);
  } finally {
    if (submitBtn) submitBtn.disabled = false;
  }
}

export function openAddToLaneModal(thread: ThreadView): void {
  const data = getCurrentData();
  if (!data) return;

  const dialog = ensureDialog();
  activeThreadId = thread.id;
  setError("");

  const labelEl = dialog.querySelector("#add-to-lane-thread-label");
  if (labelEl) {
    labelEl.textContent = threadEmailSubject(thread) || threadLabel(thread);
  }

  const existingLaneId = laneIdForThread(data, thread.id);
  const existingLane = existingLaneId
    ? getLanes(data).find((lane) => lane.id === existingLaneId) ?? null
    : null;
  const areaId = initialAreaId(data, existingLane);
  const areaSelect = dialog.querySelector("#add-to-lane-area-select") as HTMLSelectElement | null;
  const createNewLaneCheck = dialog.querySelector("#add-to-lane-new-lane-check") as HTMLInputElement | null;
  const newLaneNameInput = dialog.querySelector("#add-to-lane-new-lane-name") as HTMLInputElement | null;
  const createNewTrackCheck = dialog.querySelector("#add-to-lane-new-track-check") as HTMLInputElement | null;
  const newTrackNameInput = dialog.querySelector("#add-to-lane-new-track-name") as HTMLInputElement | null;
  const hasRealAreas = getLaneAreas(data).length > 0;

  populateAreaSelect(areaId);
  if (areaSelect && areaId >= 0) areaSelect.value = String(areaId);
  populateTrackSelect(hasRealAreas ? areaId : -1, existingLaneId ? String(existingLaneId) : "");

  const noLanes = !hasRealAreas;
  if (createNewLaneCheck) {
    createNewLaneCheck.checked = noLanes;
    createNewLaneCheck.disabled = false;
  }
  if (newLaneNameInput) newLaneNameInput.value = "";

  const trackSelect = dialog.querySelector("#add-to-lane-track-select") as HTMLSelectElement | null;
  const noTracks = !trackSelect?.value;
  if (createNewTrackCheck) {
    createNewTrackCheck.checked = noLanes || noTracks;
    createNewTrackCheck.disabled = false;
  }
  if (newTrackNameInput) newTrackNameInput.value = "";

  syncNewLaneMode();

  if (!dialog.open) dialog.showModal();
}
