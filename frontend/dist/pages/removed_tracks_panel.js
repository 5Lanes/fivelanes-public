import { clearSummariesBundleCache, getBundleMutationGeneration, getCurrentData, getLanes, loadLatestBundle, normalizeBundle, setBundleFromNetwork, } from "../shared/summaries_store.js";
import { escapeHtml } from "../shared/utils.js";
const PANEL_HTML = `
<div class="removed-tracks-panel">
  <p class="removed-tracks-subtitle">Tracks removed from the onebox stop being checked for new messages. Restore one to bring it back.</p>
  <div id="removed-tracks-list" class="removed-tracks-list"></div>
</div>`;
let interactionsBound = false;
function removedLanes(data) {
    return getLanes(data).filter((lane) => lane.removed);
}
function rowHtml(lane) {
    return `<div class="removed-track-row">
    <span class="removed-track-name">${escapeHtml(lane.name)}</span>
    <button type="button" class="btn btn--default removed-track-restore-btn" data-lane-id="${lane.id}">Restore</button>
  </div>`;
}
function renderList() {
    const el = document.getElementById("removed-tracks-list");
    if (!el)
        return;
    const lanes = removedLanes(getCurrentData());
    el.innerHTML = lanes.length
        ? lanes.map(rowHtml).join("")
        : `<p class="removed-tracks-empty">No removed tracks.</p>`;
}
async function restoreLane(laneId) {
    const res = await fetch("/api/lanes/archive", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ lane_id: laneId, archived: false }),
    });
    if (!res.ok)
        throw new Error(`Restore failed (${res.status})`);
}
export function mountRemovedTracksPanel(root) {
    root.innerHTML = PANEL_HTML;
}
export async function renderRemovedTracksPanel() {
    renderList();
}
export function bindRemovedTracksInteractions() {
    if (interactionsBound)
        return;
    interactionsBound = true;
    document.addEventListener("click", (ev) => {
        const target = ev.target;
        const btn = target?.closest(".removed-track-restore-btn");
        if (!btn)
            return;
        const laneId = Number(btn.dataset.laneId) || 0;
        if (!laneId)
            return;
        btn.disabled = true;
        void (async () => {
            try {
                await restoreLane(laneId);
                clearSummariesBundleCache();
                const mutationGenAtFetch = getBundleMutationGeneration();
                const { data, label } = await loadLatestBundle();
                setBundleFromNetwork(normalizeBundle(data), label, mutationGenAtFetch);
                renderList();
            }
            catch (err) {
                console.error(err);
                btn.disabled = false;
            }
        })();
    });
}
