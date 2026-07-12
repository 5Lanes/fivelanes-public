import type { LooseObj } from "./types.js";
import { escapeHtml } from "./utils.js";

const WRAP_ID = "dashboard-briefing-wrap";
const SESSION_DISMISS_KEY = "fivelanes_digest_briefing_dismissed_v1";

interface DigestPlan extends LooseObj {
  action: string;
  by_when: string;
}

interface DigestMeeting extends LooseObj {
  summary: string;
  start_iso: string;
}

interface DigestLane extends LooseObj {
  name: string;
  summary: string;
}

interface DigestPayload {
  ok: boolean;
  narrative: string;
  overdue_plans: DigestPlan[];
  due_soon_plans: DigestPlan[];
  upcoming_meetings: DigestMeeting[];
  active_lanes: DigestLane[];
  generated_at: string;
}

async function fetchDigest(): Promise<DigestPayload | null> {
  try {
    const res = await fetch("/api/digest/latest");
    if (!res.ok) return null;
    const body = (await res.json().catch(() => null)) as DigestPayload | null;
    return body && body.ok ? body : null;
  } catch {
    return null;
  }
}

function listHtml(items: string[], heading: string): string {
  if (!items.length) return "";
  const lis = items.map((item) => `<li>${escapeHtml(item)}</li>`).join("");
  return `<div class="dashboard-briefing-group">
    <h3 class="dashboard-briefing-subhead">${escapeHtml(heading)}</h3>
    <ul class="dashboard-briefing-list">${lis}</ul>
  </div>`;
}

export async function refreshDigestBriefing(): Promise<void> {
  const wrap = document.getElementById(WRAP_ID);
  if (!wrap) return;

  const digest = await fetchDigest();
  const hasContent =
    !!digest &&
    (digest.narrative.trim() ||
      digest.overdue_plans.length ||
      digest.due_soon_plans.length ||
      digest.upcoming_meetings.length ||
      digest.active_lanes.length);
  if (!digest || !hasContent) {
    wrap.hidden = true;
    wrap.innerHTML = "";
    return;
  }

  const fingerprint = digest.generated_at;
  if (sessionStorage.getItem(SESSION_DISMISS_KEY) === fingerprint) {
    wrap.hidden = true;
    return;
  }

  const overdueItems = digest.overdue_plans.map((p) => `${p.action} (due ${p.by_when})`);
  const dueSoonItems = digest.due_soon_plans.map((p) => `${p.action} (due ${p.by_when})`);
  const meetingItems = digest.upcoming_meetings
    .slice(0, 6)
    .map((m) => `${m.summary}${m.start_iso ? ` — ${m.start_iso}` : ""}`);
  const laneItems = digest.active_lanes.map((l) => `${l.name}: ${l.summary}`);

  wrap.hidden = false;
  wrap.innerHTML = `<aside class="dashboard-briefing" role="status" aria-live="polite">
    <div class="dashboard-briefing-head">
      <p class="dashboard-briefing-narrative">${escapeHtml(digest.narrative)}</p>
      <button type="button" class="dashboard-briefing-dismiss" aria-label="Dismiss">×</button>
    </div>
    ${listHtml(overdueItems, "Overdue")}
    ${listHtml(dueSoonItems, "Due soon")}
    ${listHtml(meetingItems, "Upcoming meetings")}
    ${listHtml(laneItems, "Recently active lanes")}
  </aside>`;

  wrap.querySelector(".dashboard-briefing-dismiss")?.addEventListener("click", () => {
    sessionStorage.setItem(SESSION_DISMISS_KEY, fingerprint);
    wrap.hidden = true;
  });
}
