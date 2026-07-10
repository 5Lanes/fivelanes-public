import type { LooseObj } from "./shared/types.js";
import { formatDate, str } from "./shared/utils.js";

export async function fetchPipelineStatus(): Promise<LooseObj | null> {
  try {
    const res = await fetch("/api/pipeline/status", { credentials: "same-origin" });
    if (!res.ok) return null;
    return (await res.json()) as LooseObj;
  } catch {
    return null;
  }
}

/** Last completed pipeline refresh time for UI labels (not in-progress partial writes). */
export function lastPipelineRefreshTime(status: LooseObj): string | null {
  const last = (status.last_run ?? {}) as LooseObj;
  const running = Boolean(status.running);

  if (running) {
    const prev = str(last.last_completed_at);
    return prev ? formatDate(prev) : null;
  }

  if (last.ok === false) {
    const prev = str(last.last_completed_at);
    return prev ? formatDate(prev) : null;
  }

  const finished = str(last.finished_at);
  return finished ? formatDate(finished) : null;
}

function triggerLabel(trigger: string): string {
  if (trigger === "manual") return "manual";
  if (trigger === "scheduler") return "scheduled";
  return trigger || "unknown";
}

export function pipelineRunMetaText(status: LooseObj): string {
  if (Boolean(status.running)) {
    const started = str(status.started_at);
    const base = started
      ? `Pipeline running since ${formatDate(started)}…`
      : "Pipeline running…";
    if (status.stalled) {
      const idleMin = Math.round(Number(status.idle_sec ?? 0) / 60);
      const detail = str(status.detail);
      const detailBit = detail ? ` (${detail})` : "";
      return `${base} stuck${detailBit} — no progress for ${idleMin}m`;
    }
    return base;
  }

  const last = (status.last_run ?? {}) as LooseObj;
  const finishedAt = str(last.finished_at);
  if (!finishedAt) return "";

  const label = triggerLabel(str(last.trigger));
  const backend = str(last.backend);
  const backendBit = backend ? ` · ${backend}` : "";

  if (last.ok === false) {
    const err = str(last.error);
    const errBit = err ? ` — ${err}` : "";
    return `Last pipeline run failed ${formatDate(finishedAt)} (${label}${backendBit})${errBit}`;
  }

  return `Last pipeline run: ${formatDate(finishedAt)} (${label}${backendBit})`;
}

export async function refreshPipelineRunMeta(
  runMetaEl: HTMLParagraphElement | null,
): Promise<void> {
  if (!runMetaEl) return;
  try {
    const res = await fetch("/api/pipeline/status", { credentials: "same-origin" });
    if (!res.ok) return;
    const status = (await res.json()) as LooseObj;
    const text = pipelineRunMetaText(status);
    runMetaEl.textContent = text;
    runMetaEl.hidden = !text;
    runMetaEl.dataset.kind = status.stalled ? "warn" : "";
  } catch {
    /* offline / server down */
  }
}
