import { formatDate, str } from "./shared/utils.js";
export async function fetchPipelineStatus() {
    try {
        const res = await fetch("/api/pipeline/inbox-pull-status", { credentials: "same-origin" });
        if (!res.ok)
            return null;
        return (await res.json());
    }
    catch {
        return null;
    }
}
/** Last completed inbox pull time for UI labels (not in-progress partial writes). */
export function lastPipelineRefreshTime(status) {
    const last = (status.last_run ?? {});
    const finished = str(last.finished_at);
    return finished ? formatDate(finished) : null;
}
function triggerLabel(trigger) {
    if (trigger === "manual")
        return "manual";
    if (trigger === "scheduler")
        return "scheduled";
    return trigger || "unknown";
}
export function pipelineRunMetaText(status) {
    if (Boolean(status.running)) {
        const started = str(status.started_at);
        return started ? `Inbox pull running since ${formatDate(started)}…` : "Inbox pull running…";
    }
    const last = (status.last_run ?? {});
    const finishedAt = str(last.finished_at);
    if (!finishedAt)
        return "";
    const label = triggerLabel(str(last.trigger));
    if (last.ok === false) {
        const err = str(last.error);
        const errBit = err ? ` — ${err}` : "";
        return `Last inbox pull failed ${formatDate(finishedAt)} (${label})${errBit}`;
    }
    return `Last inbox pull: ${formatDate(finishedAt)} (${label})`;
}
export async function refreshPipelineRunMeta(runMetaEl) {
    if (!runMetaEl)
        return;
    try {
        const res = await fetch("/api/pipeline/inbox-pull-status", { credentials: "same-origin" });
        if (!res.ok)
            return;
        const status = (await res.json());
        const text = pipelineRunMetaText(status);
        runMetaEl.textContent = text;
        runMetaEl.hidden = !text;
        runMetaEl.dataset.kind = "";
    }
    catch {
        /* offline / server down */
    }
}
