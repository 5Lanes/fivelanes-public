/**
 * Shared meeting-prep UI: API call, cache keys, modal, and HTML formatting.
 */

import { meetingDedupeKey, type MeetingRow } from "./meetings_panel.js";
import type { ThreadMatchContext } from "./thread_meeting_match.js";
import { formatTimeRangeInTz } from "./shared/time_ui.js";
import type { LooseObj, ThreadView } from "./shared/types.js";
import { escapeHtml, threadPageHref } from "./shared/utils.js";

function strField(v: unknown): string {
  return typeof v === "string" ? v : "";
}

export function threadMessagesForPrep(
  thread: ThreadView,
): Array<{ datetime: string; sender: string; recipients: string; content: string }> {
  return thread.messages.map((row) => {
    const c = (row.cleaned || {}) as LooseObj;
    const s = (row.summary || {}) as LooseObj;
    const content = strField(c.cleaned_content) || strField(c.raw_text) || "";
    return {
      datetime: strField(c.datetime || s.datetime),
      sender: strField(c.sender || c.forwarded_from),
      recipients: strField(c.recipients),
      content,
    };
  });
}

export function meetingPrepCacheKey(meeting: MeetingRow, threadId: string): string {
  return `${meetingDedupeKey(meeting)}|${threadId}`;
}

export function prepFieldsFromPayload(payload: LooseObj): LooseObj {
  return {
    prep_summary: payload.prep_summary,
    talking_points: payload.talking_points,
    open_loops: payload.open_loops,
    suggested_opener: payload.suggested_opener,
    open_questions: payload.open_questions,
  };
}

export function formatPrepPayloadHtml(payload: LooseObj): string {
  const summary = strField(payload.prep_summary).trim();
  const opener = strField(payload.suggested_opener).trim();
  const points = Array.isArray(payload.talking_points)
    ? payload.talking_points.map((x) => strField(x).trim()).filter(Boolean)
    : [];
  const loops = Array.isArray(payload.open_loops)
    ? payload.open_loops.map((x) => strField(x).trim()).filter(Boolean)
    : [];
  const questions = Array.isArray(payload.open_questions)
    ? payload.open_questions.map((x) => strField(x).trim()).filter(Boolean)
    : [];
  const chunks: string[] = [];
  if (summary) chunks.push(`<p class="meeting-prep-summary">${escapeHtml(summary)}</p>`);
  if (opener) {
    chunks.push(
      `<p class="meeting-prep-opener"><strong>Suggested opener:</strong> ${escapeHtml(opener)}</p>`,
    );
  }
  if (points.length) {
    chunks.push(
      `<h4 class="meeting-prep-subhead">Talking points</h4><ul>${points.map((p) => `<li>${escapeHtml(p)}</li>`).join("")}</ul>`,
    );
  }
  if (loops.length) {
    chunks.push(
      `<h4 class="meeting-prep-subhead">Open loops</h4><ul>${loops.map((p) => `<li>${escapeHtml(p)}</li>`).join("")}</ul>`,
    );
  }
  if (questions.length) {
    chunks.push(
      `<h4 class="meeting-prep-subhead">Check before the meeting</h4><ul>${questions.map((p) => `<li>${escapeHtml(p)}</li>`).join("")}</ul>`,
    );
  }
  return chunks.length
    ? chunks.join("")
    : `<p class="meeting-prep-error">No prep content returned.</p>`;
}

export async function requestMeetingPrep(
  thread: ThreadView,
  meeting: MeetingRow,
  threadLabelText: string,
  force = false,
): Promise<LooseObj> {
  const res = await fetch("/api/meeting-prep", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      thread_id: thread.id,
      thread_label: threadLabelText,
      meeting_title: meeting.summary,
      meeting_start: meeting.start_iso,
      meeting_end: meeting.end_iso,
      meeting_location: meeting.location,
      meeting_attendees: meeting.attendees.join(", "),
      messages: threadMessagesForPrep(thread),
      force,
    }),
  });
  const data = (await res.json()) as LooseObj;
  if (!res.ok || data.ok === false) {
    throw new Error(strField(data.error) || `Request failed (${res.status})`);
  }
  return data;
}

type MeetingPrepContext = {
  meeting: MeetingRow;
  thread: ThreadMatchContext;
};

const prepContexts = new Map<string, MeetingPrepContext>();
const inflightPreps = new Map<string, Promise<LooseObj>>();
let dialogEl: HTMLDialogElement | null = null;
let meetingPrepsCache: LooseObj = {};
let onPrepSavedCallback: ((cacheKey: string, prep: LooseObj) => void) | undefined;
let getThreadsByIdFn: (() => Map<string, ThreadView>) | undefined;
let prepBound = false;
let activeContext: MeetingPrepContext | null = null;
let activeTz = "America/New_York";

export function clearMeetingPrepContexts(): void {
  prepContexts.clear();
}

export function registerMeetingPrepContext(meeting: MeetingRow, thread: ThreadMatchContext): string {
  const key = meetingPrepCacheKey(meeting, thread.threadId);
  prepContexts.set(key, { meeting, thread });
  return key;
}

export function meetingPrepLinkHtml(
  label: string,
  meeting: MeetingRow,
  thread: ThreadMatchContext,
  className = "meet-track-link",
): string {
  const key = registerMeetingPrepContext(meeting, thread);
  return `<a class="${className}" href="#" data-meeting-prep="${escapeHtml(key)}">${escapeHtml(label)}</a>`;
}

export function configureMeetingPrep(opts: {
  meetingPreps?: LooseObj;
  onMeetingPrepSaved?: (cacheKey: string, prep: LooseObj) => void;
  getThreadsById?: () => Map<string, ThreadView>;
  timezone?: string;
}): void {
  meetingPrepsCache = opts.meetingPreps && typeof opts.meetingPreps === "object" ? opts.meetingPreps : {};
  onPrepSavedCallback = opts.onMeetingPrepSaved;
  getThreadsByIdFn = opts.getThreadsById;
  if (opts.timezone) activeTz = opts.timezone;
}

function ensureMeetingPrepDialog(): HTMLDialogElement {
  if (dialogEl) return dialogEl;

  const dialog = document.createElement("dialog");
  dialog.id = "meeting-prep-dialog";
  dialog.className = "meeting-prep-dialog";
  dialog.innerHTML = `
    <div class="meeting-prep-dialog-inner">
      <header class="meeting-prep-dialog-head">
        <div>
          <h2 id="meeting-prep-title"></h2>
          <p id="meeting-prep-meta" class="meeting-prep-meta"></p>
        </div>
        <button type="button" class="meeting-prep-close" aria-label="Close">×</button>
      </header>
      <div id="meeting-prep-body" class="meeting-prep-body"></div>
      <footer class="meeting-prep-dialog-foot">
        <a id="meeting-prep-thread-link" class="meeting-prep-thread-link" href="#">View thread</a>
        <button type="button" id="meeting-prep-refresh" class="meeting-prep-btn">Refresh</button>
      </footer>
    </div>`;
  document.body.appendChild(dialog);

  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
  dialog.querySelector(".meeting-prep-close")?.addEventListener("click", () => dialog.close());
  dialog.querySelector("#meeting-prep-refresh")?.addEventListener("click", () => {
    if (activeContext) void loadMeetingPrepIntoModal(activeContext, true);
  });

  dialogEl = dialog;
  return dialog;
}

async function fetchAndCachePrep(ctx: MeetingPrepContext, force = false): Promise<LooseObj> {
  const cacheKey = meetingPrepCacheKey(ctx.meeting, ctx.thread.threadId);
  if (!force) {
    const cached = meetingPrepsCache[cacheKey] as LooseObj | undefined;
    if (cached && typeof cached === "object") return cached;
    const inflight = inflightPreps.get(cacheKey);
    if (inflight) return inflight;
  }

  const threadsById = getThreadsByIdFn?.() ?? new Map<string, ThreadView>();
  const thread = threadsById.get(ctx.thread.threadId);
  if (!thread) throw new Error("Thread not found in loaded bundle.");

  const promise = (async () => {
    const payload = await requestMeetingPrep(thread, ctx.meeting, ctx.thread.label, force);
    const prep = prepFieldsFromPayload(payload);
    meetingPrepsCache[cacheKey] = prep;
    onPrepSavedCallback?.(cacheKey, prep);
    return prep;
  })().finally(() => {
    inflightPreps.delete(cacheKey);
  });

  inflightPreps.set(cacheKey, promise);
  return promise;
}

async function loadMeetingPrepIntoModal(ctx: MeetingPrepContext, force = false): Promise<void> {
  const dialog = ensureMeetingPrepDialog();
  const body = dialog.querySelector("#meeting-prep-body") as HTMLElement | null;
  const refreshBtn = dialog.querySelector("#meeting-prep-refresh") as HTMLButtonElement | null;
  if (!body) return;

  refreshBtn?.setAttribute("disabled", "");
  body.innerHTML = `<p class="meeting-prep-loading">Preparing from email thread…</p>`;
  try {
    const prep = await fetchAndCachePrep(ctx, force);
    body.innerHTML = formatPrepPayloadHtml(prep);
  } catch (e) {
    body.innerHTML = `<p class="meeting-prep-error">${escapeHtml(e instanceof Error ? e.message : String(e))}</p>`;
  } finally {
    refreshBtn?.removeAttribute("disabled");
  }
}

export async function openMeetingPrepModal(ctx: MeetingPrepContext): Promise<void> {
  activeContext = ctx;
  const dialog = ensureMeetingPrepDialog();
  const titleEl = dialog.querySelector("#meeting-prep-title");
  const metaEl = dialog.querySelector("#meeting-prep-meta");
  const threadLink = dialog.querySelector("#meeting-prep-thread-link") as HTMLAnchorElement | null;
  const body = dialog.querySelector("#meeting-prep-body") as HTMLElement | null;

  if (titleEl) titleEl.textContent = ctx.meeting.summary || "Meeting prep";
  if (metaEl) {
    const timeLine = formatTimeRangeInTz(ctx.meeting.start_iso, ctx.meeting.end_iso, activeTz);
    metaEl.textContent = `${timeLine} · ${ctx.thread.label}`;
  }
  if (threadLink) {
    threadLink.href = threadPageHref(ctx.thread.threadId);
    threadLink.textContent = "View thread";
  }
  if (body) body.innerHTML = `<p class="meeting-prep-loading">Preparing from email thread…</p>`;

  if (!dialog.open) dialog.showModal();
  await loadMeetingPrepIntoModal(ctx);
}

export function bindMeetingPrepInteractions(): void {
  if (prepBound) return;
  prepBound = true;

  document.addEventListener("click", (ev) => {
    const link = (ev.target as HTMLElement).closest<HTMLElement>("[data-meeting-prep]");
    if (!link) return;
    ev.preventDefault();
    const key = link.dataset.meetingPrep?.trim();
    if (!key) return;
    const ctx = prepContexts.get(key);
    if (!ctx) return;
    void openMeetingPrepModal(ctx);
  });
}
