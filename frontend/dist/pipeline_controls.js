import { refreshPipelineRunMeta } from "./pipeline_run_meta.js";
import { clearSummariesBundleCache, loadLatestBundle, setBundle, } from "./shared/summaries_store.js";
import { str } from "./shared/utils.js";
const runMetaEl = document.getElementById("run-meta");
const runBtn = document.getElementById("run-fivelanes-btn");
const statusEl = document.getElementById("pipeline-status");
const backendSwitch = document.getElementById("backend-switch");
const backendLabel = document.getElementById("backend-label");
let pollTimer = null;
let onRunComplete = null;
function backendDisplayName(backend) {
    return backend === "claude" ? "Claude" : "Llama";
}
function setBackendLabel(backend) {
    const name = backendDisplayName(backend);
    if (backendLabel) {
        backendLabel.textContent = `Backend: ${name}`;
    }
    backendSwitch?.setAttribute("aria-label", `LLM backend, ${name} selected`);
}
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
    runBtn.textContent = running ? "Running…" : "Run fivelanes";
    runBtn.setAttribute("aria-busy", running ? "true" : "false");
}
function setBackendSwitchDisabled(disabled) {
    backendSwitch?.querySelectorAll("[data-backend]").forEach((btn) => {
        btn.disabled = disabled;
    });
}
function updateBackendSwitch(backend) {
    setBackendLabel(backend);
    backendSwitch?.querySelectorAll("[data-backend]").forEach((btn) => {
        const active = btn.dataset.backend === backend;
        btn.classList.toggle("active", active);
        btn.setAttribute("aria-pressed", active ? "true" : "false");
        btn.setAttribute("aria-current", active ? "true" : "false");
    });
}
function syncBackendFromStatus(status) {
    const raw = str(status.backend).toLowerCase();
    if (raw === "claude" || raw === "llama") {
        updateBackendSwitch(raw);
    }
}
async function fetchConfig() {
    const res = await fetch("/api/config", { credentials: "same-origin" });
    if (!res.ok)
        throw new Error(`Config load failed (${res.status})`);
    const data = (await res.json());
    const backend = str(data.backend).toLowerCase();
    if (backend !== "claude" && backend !== "llama") {
        throw new Error(`Unexpected backend: ${backend || "(empty)"}`);
    }
    return backend;
}
async function setBackend(backend) {
    const res = await fetch("/api/config/backend", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ backend }),
    });
    const data = (await res.json().catch(() => ({})));
    if (!res.ok) {
        throw new Error(str(data.error) || `Backend update failed (${res.status})`);
    }
    updateBackendSwitch(backend);
}
async function fetchPipelineStatus() {
    const res = await fetch("/api/pipeline/status", { credentials: "same-origin" });
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
    const { data, label } = await loadLatestBundle();
    setBundle(data, label);
    onRunComplete?.();
}
async function pollPipelineStatus() {
    try {
        const status = await fetchPipelineStatus();
        syncBackendFromStatus(status);
        void refreshPipelineRunMeta(runMetaEl);
        const running = Boolean(status.running);
        setRunButtonRunning(running);
        setBackendSwitchDisabled(running);
        if (running) {
            setStatus("Fivelanes is running…");
            return;
        }
        stopPolling();
        const err = str(status.error);
        if (err) {
            setStatus(`Run failed: ${err}`, "error");
            return;
        }
        setStatus("Run finished. Refreshing data…");
        try {
            await refreshAfterRun();
            setStatus("Run finished.");
        }
        catch (refreshErr) {
            const msg = refreshErr instanceof Error ? refreshErr.message : String(refreshErr);
            setStatus(`Run finished, but refresh failed: ${msg}`, "error");
        }
    }
    catch (err) {
        stopPolling();
        setRunButtonRunning(false);
        setBackendSwitchDisabled(false);
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
    setBackendSwitchDisabled(true);
    setStatus("Starting fivelanes…");
    const res = await fetch("/api/pipeline/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: "{}",
    });
    const data = (await res.json().catch(() => ({})));
    if (res.status === 409) {
        setStatus("A run is already in progress.");
        startPolling();
        return;
    }
    if (!res.ok) {
        setRunButtonRunning(false);
        setBackendSwitchDisabled(false);
        setStatus(str(data.error) || `Run failed (${res.status})`, "error");
        return;
    }
    startPolling();
}
export function bindPipelineControls(onComplete) {
    onRunComplete = onComplete ?? null;
    runBtn?.addEventListener("click", () => {
        void startPipelineRun();
    });
    backendSwitch?.querySelectorAll("[data-backend]").forEach((btn) => {
        btn.addEventListener("click", () => {
            const backend = btn.dataset.backend;
            if (!backend || btn.classList.contains("active") || btn.disabled)
                return;
            void (async () => {
                try {
                    setBackendSwitchDisabled(true);
                    await setBackend(backend);
                }
                catch (err) {
                    const msg = err instanceof Error ? err.message : String(err);
                    setStatus(msg, "error");
                }
                finally {
                    if (!runBtn?.disabled)
                        setBackendSwitchDisabled(false);
                }
            })();
        });
    });
    void (async () => {
        try {
            const backend = await fetchConfig();
            updateBackendSwitch(backend);
            const status = await fetchPipelineStatus();
            void refreshPipelineRunMeta(runMetaEl);
            if (status.running) {
                setRunButtonRunning(true);
                setBackendSwitchDisabled(true);
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
