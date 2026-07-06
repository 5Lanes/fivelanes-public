/**
 * Modal for assigning a thread to a lane (area) and track, or creating new ones.
 */
import { applyLaneAreaCreated, applyLaneCreated, applyLaneThreadMembership, getCurrentData, getLaneAreas, getLanes, } from "./shared/summaries_store.js";
import { threadEmailSubject, threadLabel } from "./shared/thread_domain.js";
import { escapeHtml, str } from "./shared/utils.js";
const LANE_AREA_COLORS = 6;
let dialogEl = null;
let activeThreadId = "";
function activeTracks(data) {
    return getLanes(data).filter((lane) => !lane.archived);
}
function areaOptions(data) {
    const areas = getLaneAreas(data).map((a) => ({ id: a.id, name: a.name }));
    const hasUnassigned = activeTracks(data).some((lane) => lane.area_id == null);
    if (hasUnassigned)
        areas.push({ id: 0, name: "Unassigned" });
    return areas;
}
function tracksForArea(data, areaId) {
    const lanes = activeTracks(data);
    if (areaId <= 0)
        return lanes.filter((lane) => lane.area_id == null);
    return lanes.filter((lane) => lane.area_id === areaId);
}
function laneIdForThread(data, threadId) {
    const memberships = data.lane_threads;
    if (!memberships || typeof memberships !== "object")
        return null;
    for (const [laneKey, ids] of Object.entries(memberships)) {
        if (!Array.isArray(ids) || !ids.map((id) => str(id)).includes(threadId))
            continue;
        return Number(laneKey) || null;
    }
    return null;
}
function initialAreaId(data, lane) {
    const options = areaOptions(data);
    if (!options.length)
        return -1;
    if (lane?.area_id != null)
        return lane.area_id;
    if (lane)
        return 0;
    return options[0]?.id ?? -1;
}
function nextLaneColorIndex(data) {
    return getLaneAreas(data).length % LANE_AREA_COLORS;
}
async function persistLaneAreaCreate(name, colorIndex) {
    const res = await fetch("/api/lane-areas", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, color_index: colorIndex }),
    });
    const body = (await res.json().catch(() => ({})));
    if (!res.ok)
        throw new Error(str(body.error) || `Create lane failed (${res.status})`);
    const areaRaw = body.lane_area;
    return {
        id: Number(areaRaw.id) || 0,
        name: str(areaRaw.name) || name,
        color_index: Number(areaRaw.color_index) || colorIndex,
        sort_order: Number(areaRaw.sort_order) || 0,
        created_at: str(areaRaw.created_at),
        updated_at: str(areaRaw.updated_at),
    };
}
async function persistLaneCreate(name, areaId) {
    const payload = { name };
    if (areaId != null && areaId > 0)
        payload.area_id = areaId;
    const res = await fetch("/api/lanes/create", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
    });
    const body = (await res.json().catch(() => ({})));
    if (!res.ok)
        throw new Error(str(body.error) || `Create track failed (${res.status})`);
    const laneRaw = body.lane;
    return {
        id: Number(laneRaw.id) || 0,
        name: str(laneRaw.name) || name,
        created_at: str(laneRaw.created_at),
        updated_at: str(laneRaw.updated_at),
        archived: Boolean(laneRaw.archived),
        area_id: laneRaw.area_id == null ? null : Number(laneRaw.area_id) || null,
    };
}
async function persistLaneThread(laneId, threadId) {
    const res = await fetch("/api/lanes/add-thread", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ lane_id: laneId, thread_id: threadId }),
    });
    const body = (await res.json().catch(() => ({})));
    if (!res.ok)
        throw new Error(str(body.error) || `Add to track failed (${res.status})`);
}
function setError(message) {
    const errEl = dialogEl?.querySelector(".add-to-lane-error");
    if (!errEl)
        return;
    if (message) {
        errEl.textContent = message;
        errEl.hidden = false;
    }
    else {
        errEl.textContent = "";
        errEl.hidden = true;
    }
}
function isCreatingNewLane() {
    return Boolean(dialogEl?.querySelector("#add-to-lane-new-lane-check")?.checked);
}
function populateTrackSelect(areaId, selectedTrackId = "") {
    const data = getCurrentData();
    const select = dialogEl?.querySelector("#add-to-lane-track-select");
    if (!data || !select)
        return;
    const tracks = areaId === -1 ? activeTracks(data) : tracksForArea(data, areaId);
    if (!tracks.length) {
        select.innerHTML = `<option value="">No tracks yet — create one below</option>`;
        select.value = "";
        return;
    }
    select.innerHTML = tracks
        .map((track) => `<option value="${track.id}"${String(track.id) === selectedTrackId ? " selected" : ""}>${escapeHtml(track.name)}</option>`)
        .join("");
}
function populateAreaSelect(selectedAreaId) {
    const data = getCurrentData();
    const areaField = dialogEl?.querySelector("#add-to-lane-area-field");
    const select = dialogEl?.querySelector("#add-to-lane-area-select");
    if (!data || !select || !areaField)
        return;
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
        .map((area) => `<option value="${area.id}"${area.id === selectedAreaId ? " selected" : ""}>${escapeHtml(area.name)}</option>`)
        .join("");
    populateTrackSelect(selectedAreaId);
}
function syncNewLaneMode() {
    const createNewLane = dialogEl?.querySelector("#add-to-lane-new-lane-check");
    const areaField = dialogEl?.querySelector("#add-to-lane-area-field");
    const newLaneField = dialogEl?.querySelector("#add-to-lane-new-lane-field");
    const areaSelect = dialogEl?.querySelector("#add-to-lane-area-select");
    const newLaneNameInput = dialogEl?.querySelector("#add-to-lane-new-lane-name");
    const createNewTrack = dialogEl?.querySelector("#add-to-lane-new-track-check");
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
    if (newLaneNameInput)
        newLaneNameInput.required = creating;
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
    if (createNewTrack)
        createNewTrack.disabled = false;
    const areaId = Number(areaSelect?.value) || 0;
    populateTrackSelect(hasRealAreas ? areaId : -1);
    syncNewTrackMode();
}
function syncNewTrackMode() {
    const createNew = dialogEl?.querySelector("#add-to-lane-new-track-check");
    const trackField = dialogEl?.querySelector("#add-to-lane-track-field");
    const newTrackField = dialogEl?.querySelector("#add-to-lane-new-track-field");
    const trackSelect = dialogEl?.querySelector("#add-to-lane-track-select");
    const newNameInput = dialogEl?.querySelector("#add-to-lane-new-track-name");
    const creating = Boolean(createNew?.checked);
    trackField?.toggleAttribute("hidden", creating);
    newTrackField?.toggleAttribute("hidden", !creating);
    if (trackSelect) {
        trackSelect.disabled = creating;
        trackSelect.required = !creating;
    }
    if (newNameInput) {
        newNameInput.required = creating;
        if (creating && !isCreatingNewLane())
            newNameInput.focus();
    }
}
function ensureDialog() {
    if (dialogEl)
        return dialogEl;
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
        if (event.target === dialog)
            dialog.close();
    });
    dialog.querySelector(".add-to-lane-close")?.addEventListener("click", () => dialog.close());
    dialog.querySelector(".add-to-lane-cancel")?.addEventListener("click", () => dialog.close());
    dialog.querySelector("#add-to-lane-new-lane-check")?.addEventListener("change", () => syncNewLaneMode());
    dialog.querySelector("#add-to-lane-new-track-check")?.addEventListener("change", () => syncNewTrackMode());
    dialog.querySelector("#add-to-lane-area-select")?.addEventListener("change", () => {
        const areaId = Number(dialog.querySelector("#add-to-lane-area-select")?.value) || 0;
        populateTrackSelect(areaId);
        const trackSelect = dialog.querySelector("#add-to-lane-track-select");
        const createNewTrack = dialog.querySelector("#add-to-lane-new-track-check");
        if (createNewTrack && !trackSelect?.value)
            createNewTrack.checked = true;
        syncNewTrackMode();
    });
    dialog.querySelector(".add-to-lane-form")?.addEventListener("submit", (event) => {
        event.preventDefault();
        void submitAddToLane();
    });
    dialogEl = dialog;
    return dialog;
}
async function refreshAfterAssign() {
    const { renderLanesList } = await import("./pages/lanes_page.js");
    renderLanesList();
    if (document.getElementById("dashboard-threads-root")) {
        const { renderDashboardThreadsInline } = await import("./pages/threads_page.js");
        await renderDashboardThreadsInline();
    }
}
async function submitAddToLane() {
    const data = getCurrentData();
    const threadId = activeThreadId;
    if (!data || !threadId)
        return;
    const areaField = dialogEl?.querySelector("#add-to-lane-area-field");
    const areaSelect = dialogEl?.querySelector("#add-to-lane-area-select");
    const createNewLane = dialogEl?.querySelector("#add-to-lane-new-lane-check");
    const newLaneNameInput = dialogEl?.querySelector("#add-to-lane-new-lane-name");
    const trackSelect = dialogEl?.querySelector("#add-to-lane-track-select");
    const createNewTrack = dialogEl?.querySelector("#add-to-lane-new-track-check");
    const newTrackNameInput = dialogEl?.querySelector("#add-to-lane-new-track-name");
    const submitBtn = dialogEl?.querySelector("#add-to-lane-submit");
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
    if (submitBtn)
        submitBtn.disabled = true;
    try {
        let resolvedAreaId = selectedAreaId > 0 ? selectedAreaId : null;
        if (creatingLane) {
            const area = await persistLaneAreaCreate(newLaneName, nextLaneColorIndex(getCurrentData() || data));
            if (!area.id)
                throw new Error("Lane was not created.");
            applyLaneAreaCreated(area);
            resolvedAreaId = area.id;
        }
        let laneId = selectedTrackId;
        if (creatingTrack) {
            const lane = await persistLaneCreate(newTrackName, resolvedAreaId);
            if (!lane.id)
                throw new Error("Track was not created.");
            applyLaneCreated(lane);
            laneId = lane.id;
        }
        applyLaneThreadMembership(laneId, threadId, true);
        await persistLaneThread(laneId, threadId);
        dialogEl?.close();
        await refreshAfterAssign();
    }
    catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        setError(msg);
        console.error(err);
    }
    finally {
        if (submitBtn)
            submitBtn.disabled = false;
    }
}
export function openAddToLaneModal(thread) {
    const data = getCurrentData();
    if (!data)
        return;
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
    const areaSelect = dialog.querySelector("#add-to-lane-area-select");
    const createNewLaneCheck = dialog.querySelector("#add-to-lane-new-lane-check");
    const newLaneNameInput = dialog.querySelector("#add-to-lane-new-lane-name");
    const createNewTrackCheck = dialog.querySelector("#add-to-lane-new-track-check");
    const newTrackNameInput = dialog.querySelector("#add-to-lane-new-track-name");
    const hasRealAreas = getLaneAreas(data).length > 0;
    populateAreaSelect(areaId);
    if (areaSelect && areaId >= 0)
        areaSelect.value = String(areaId);
    populateTrackSelect(hasRealAreas ? areaId : -1, existingLaneId ? String(existingLaneId) : "");
    const noLanes = !hasRealAreas;
    if (createNewLaneCheck) {
        createNewLaneCheck.checked = noLanes;
        createNewLaneCheck.disabled = false;
    }
    if (newLaneNameInput)
        newLaneNameInput.value = "";
    const trackSelect = dialog.querySelector("#add-to-lane-track-select");
    const noTracks = !trackSelect?.value;
    if (createNewTrackCheck) {
        createNewTrackCheck.checked = noLanes || noTracks;
        createNewTrackCheck.disabled = false;
    }
    if (newTrackNameInput)
        newTrackNameInput.value = "";
    syncNewLaneMode();
    if (!dialog.open)
        dialog.showModal();
}
