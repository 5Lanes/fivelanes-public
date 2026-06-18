import { refreshMeetingsPanel } from "../meetings_panel.js";
import { refreshPipelineRunMeta } from "../pipeline_run_meta.js";

const PAGE_HTML = `
<div class="view-meetings">
  <p class="meetings-meta" id="meetings-meta">Loading meetings…</p>
  <div id="meetings-agenda" class="meetings-agenda"></div>
</div>`;

export function mountMeetingsPage(root: HTMLElement): void {
  root.innerHTML = PAGE_HTML;
}

export async function renderMeetingsPage(runMetaEl: HTMLParagraphElement): Promise<void> {
  void refreshPipelineRunMeta(runMetaEl);

  const metaEl = document.getElementById("meetings-meta");
  const agendaEl = document.getElementById("meetings-agenda");
  if (!metaEl || !agendaEl) return;

  await refreshMeetingsPanel(metaEl, agendaEl);
}
