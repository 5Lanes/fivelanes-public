import type { LooseObj } from "./shared/types.js";
import { formatDate, str } from "./shared/utils.js";

function triggerLabel(trigger: string): string {
  if (trigger === "manual") return "manual";
  if (trigger === "scheduler") return "scheduled";
  return trigger || "unknown";
}

export function pipelineRunMetaText(status: LooseObj): string {
  if (Boolean(status.running)) {
    const started = str(status.started_at);
    return started
      ? `Pipeline running since ${formatDate(started)}…`
      : "Pipeline running…";
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
  } catch {
    /* offline / server down */
  }
}
