import { refreshPipelineRunMeta } from "./pipeline_run_meta.js";
import { setSettingsControlsLocked, syncBackendFromStatus, } from "./settings_panel.js";
import { clearSummariesBundleCache, getBundleMutationGeneration, loadLatestBundle, setBundleFromNetwork, } from "./shared/summaries_store.js";
import { str } from "./shared/utils.js";
const runMetaEl = document.getElementById("run-meta");
const runBtn = document.getElementById("run-fivelanes-btn");
const statusEl = document.getElementById("pipeline-status");
let pollTimer = null;
let onRunComplete = null;
function setStatus(message, kind = "info") {
    if (!statusEl)
        return;
    statusEl.textContent = message;
    statusEl.hidden = !message;
    statusEl.dataset.kind = kind;
}
function setRunButtonRunning(running) {
    if (!runBtn)
        return;
    runBtn.disabled = running;
    const label = runBtn.querySelector(".run-fivelanes-btn-label");
    if (label) {
        label.textContent = running ? "Pulling…" : "Pull onebox";
    }
    else {
        runBtn.textContent = running ? "Pulling…" : "Pull onebox";
    }
    runBtn.setAttribute("aria-busy", running ? "true" : "false");
}
async function fetchPipelineStatus() {
    const res = await fetch("/api/pipeline/inbox-pull-status", { credentials: "same-origin" });
    if (!res.ok)
        throw new Error(`Pipeline status failed (${res.status})`);
    return (await res.json());
}
function stopPolling() {
    if (pollTimer !== null) {
        clearInterval(pollTimer);
        pollTimer = null;
    }
}
async function refreshAfterRun() {
    clearSummariesBundleCache();
    const mutationGenAtFetch = getBundleMutationGeneration();
    const { data, label } = await loadLatestBundle();
    setBundleFromNetwork(data, label, mutationGenAtFetch);
    onRunComplete?.();
}
async function pollPipelineStatus() {
    try {
        const status = await fetchPipelineStatus();
        syncBackendFromStatus(status);
        void refreshPipelineRunMeta(runMetaEl);
        const running = Boolean(status.running);
        setRunButtonRunning(running);
        setSettingsControlsLocked(running);
        if (running) {
            const detail = str(status.detail);
            const detailBit = detail ? ` (${detail})` : "";
            if (status.stalled) {
                const idleMin = Math.round(Number(status.idle_sec ?? 0) / 60);
                setStatus(`Inbox pull appears stuck${detailBit} — no progress for ${idleMin}m.`, "warn");
            }
            else {
                setStatus(`Pulling inbox…${detailBit}`);
            }
            return;
        }
        stopPolling();
        const err = str(status.error);
        if (err) {
            setStatus(`Pull failed: ${err}`, "error");
            return;
        }
        setStatus("Pull finished. Refreshing data…");
        try {
            await refreshAfterRun();
            setStatus("Pull finished.");
        }
        catch (refreshErr) {
            const msg = refreshErr instanceof Error ? refreshErr.message : String(refreshErr);
            setStatus(`Pull finished, but refresh failed: ${msg}`, "error");
        }
    }
    catch (err) {
        stopPolling();
        setRunButtonRunning(false);
        setSettingsControlsLocked(false);
        const msg = err instanceof Error ? err.message : String(err);
        setStatus(msg, "error");
    }
}
function startPolling() {
    stopPolling();
    void pollPipelineStatus();
    pollTimer = setInterval(() => {
        void pollPipelineStatus();
    }, 2000);
}
async function startPipelineRun() {
    setRunButtonRunning(true);
    setSettingsControlsLocked(true);
    setStatus("Starting inbox pull…");
    const res = await fetch("/api/pipeline/run-inbox-pull", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: "{}",
    });
    const data = (await res.json().catch(() => ({})));
    if (res.status === 409) {
        setStatus("A pull is already in progress.");
        startPolling();
        return;
    }
    if (!res.ok) {
        setRunButtonRunning(false);
        setSettingsControlsLocked(false);
        setStatus(str(data.error) || `Pull failed (${res.status})`, "error");
        return;
    }
    startPolling();
}
export function bindPipelineControls(onComplete) {
    onRunComplete = onComplete ?? null;
    runBtn?.addEventListener("click", () => {
        void startPipelineRun();
    });
    void (async () => {
        try {
            const status = await fetchPipelineStatus();
            syncBackendFromStatus(status);
            void refreshPipelineRunMeta(runMetaEl);
            if (status.running) {
                setRunButtonRunning(true);
                setSettingsControlsLocked(true);
                startPolling();
            }
        }
        catch (err) {
            const msg = err instanceof Error ? err.message : String(err);
            setStatus(msg, "error");
        }
    })();
    setInterval(() => {
        if (pollTimer !== null)
            return;
        void refreshPipelineRunMeta(runMetaEl);
    }, 60000);
}
