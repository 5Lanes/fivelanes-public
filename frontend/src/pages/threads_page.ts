import {
  applySavedThreadDraft,
  applyThreadSummary,
  clearSummariesBundleCache,
  getCurrentData,
  getCurrentSourceLabel,
  getCurrentThreads,
  setBundle,
  threadLaneIds,
  threadTrackPath,
} from "../shared/summaries_store.js";
import { syncLaneSummaryJobsFromServer } from "./lanes_page.js";
import {
  counterpartyAvailabilityForSummary,
  counterpartyAvailabilitySectionHtml,
  formatDraftReplyMarkdown,
  formatChatSenderLabel,
  latestUpdatesForThread,
  listSection,
  ownerNextStepsForThread,
  messageSourceDetailsHtml,
  nextStepsSectionHtml,
  partitionThreadsBySnooze,
  pendingMessageCountForThread,
  pendingMessagePillHtml,
  shouldShowThreadMessageBlocks,
  threadIsEmail,
  threadIsLinkedin,
  threadIsMeetRecording,
  threadIsSlack,
  threadIsText,
  threadLabel,
  threadMessagesForDisplay,
  threadMessagesForReply,
  threadSummaryErrorHtml,
  threadSummaryForDisplay,
} from "../shared/thread_domain.js";
import type { LooseObj, ThreadView } from "../shared/types.js";
import { escapeHtml, formatDate, formatRecipients, str, toneClass } from "../shared/utils.js";
import { applyNavFeatureVisibility, isFeatureEnabled } from "../shared/features.js";
import { sourcePillHtml, threadChannelForThread, type SourceChannel } from "../shared/source_ui.js";

const PAGE_HTML = `
<div class="dashboard-layout dashboard-layout--threads">
  <aside class="thread-nav" id="thread-nav">
    <h2>Threads</h2>
    <ul id="thread-nav-list"></ul>
  </aside>
  <div class="main-panel">
    <button type="button" class="thread-mobile-back" id="thread-mobile-back" aria-label="Back to thread list">← Threads</button>
    <div id="lanes" class="lanes-grid" hidden></div>
    <div id="cards" class="cards"></div>
  </div>
</div>`;

function isMobileThreadsLayout(): boolean {
  return window.matchMedia("(max-width: 640px)").matches;
}

function threadsLayoutEl(): HTMLElement | null {
  return document.querySelector(".dashboard-layout--threads");
}

function closeMobileThreadDetail(): void {
  threadsLayoutEl()?.classList.remove("thread-detail-open");
  document.querySelectorAll("article.card.thread-card-active").forEach((card) => {
    card.classList.remove("thread-card-active");
  });
}

function openMobileThreadDetail(threadId: string): void {
  if (!isMobileThreadsLayout()) {
    document.getElementById(`thread-${threadId}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
    return;
  }
  const layout = threadsLayoutEl();
  if (!layout) return;
  document.querySelectorAll("article.card").forEach((card) => {
    card.classList.toggle("thread-card-active", card.id === `thread-${threadId}`);
  });
  layout.classList.add("thread-detail-open");
}

let threadViewMode: "active" | "snoozed" | "removed" = "active";
let threadChannelFilter: "all" | "text" | "slack" | "linkedin" | "meet" | "email" = "all";
const SOURCE_FILTER_DEFS: Array<{ id: SourceChannel; label: string; feature?: string }> = [
  { id: "email", label: "Email" },
  { id: "text", label: "Text", feature: "texts" },
  { id: "slack", label: "Slack", feature: "slack" },
  { id: "linkedin", label: "LinkedIn", feature: "linkedin" },
  { id: "meet", label: "Meet", feature: "meet_recordings" },
];
let threadSourceFilters = new Set<SourceChannel>(SOURCE_FILTER_DEFS.map((s) => s.id));
let threadAssignmentFilter: "all" | "unassigned" = "all";
let threadSortMode: "recent-updates" | "newest" | "lane" = "recent-updates";
let dashboardToolbarBound = false;
let navObserver: IntersectionObserver | null = null;
let interactionsBound = false;
let cardsRenderToken = 0;
const INITIAL_CARD_BATCH = 10;
const CARD_BATCH_SIZE = 10;

function cardsEl(): HTMLDivElement | null {
  return (document.getElementById("cards") ||
    document.getElementById("dashboard-threads-cards")) as HTMLDivElement | null;
}

function navListEl(): HTMLUListElement {
  return document.getElementById("thread-nav-list") as HTMLUListElement;
}

async function requestEmailReplyDraft(
  threadId: string,
  responseIntent: string,
  threadSubject: string,
): Promise<LooseObj> {
  const thread = getCurrentThreads().find((t) => t.id === threadId);
  if (!thread) throw new Error("Thread not found.");
  const res = await fetch("/api/claude/email-reply", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      thread_id: threadId,
      response_intent: responseIntent,
      thread_subject: threadSubject,
      messages: threadMessagesForReply(thread),
    }),
  });
  const data = (await res.json()) as LooseObj;
  if (!res.ok || data.ok === false) {
    const msg = str(data.error) || `Request failed (${res.status})`;
    throw new Error(msg);
  }
  return data;
}

function threadUpdatedAt(thread: ThreadView): string {
  const row = thread.messages[0];
  return str(row?.cleaned?.datetime || row?.summary?.datetime);
}

function threadCreatedAt(thread: ThreadView): string {
  const row = thread.messages[thread.messages.length - 1] || thread.messages[0];
  return str(row?.cleaned?.datetime || row?.summary?.datetime);
}

function threadIsAssignedToTrack(data: LooseObj | null, threadId: string): boolean {
  return threadTrackPath(data, threadId) !== null;
}

function enabledSourceFilterDefs(): typeof SOURCE_FILTER_DEFS {
  return SOURCE_FILTER_DEFS.filter((s) => !s.feature || isFeatureEnabled(s.feature));
}

function filterThreadsBySourceSet(threads: ThreadView[]): ThreadView[] {
  return threads.filter((t) => threadSourceFilters.has(threadChannelForThread(t)));
}

function filterThreadsByAssignment(threads: ThreadView[], data: LooseObj | null): ThreadView[] {
  if (threadAssignmentFilter !== "unassigned") return threads;
  return threads.filter((t) => !threadIsAssignedToTrack(data, t.id));
}

function sortDashboardThreads(threads: ThreadView[], data: LooseObj | null): ThreadView[] {
  const copy = [...threads];
  if (threadSortMode === "newest") {
    return copy.sort((a, b) => threadCreatedAt(b).localeCompare(threadCreatedAt(a)));
  }
  if (threadSortMode === "lane") {
    return copy.sort((a, b) => {
      const assignedA = threadIsAssignedToTrack(data, a.id);
      const assignedB = threadIsAssignedToTrack(data, b.id);
      if (assignedA !== assignedB) return assignedA ? -1 : 1;
      const pathA = threadTrackPath(data, a.id) || "";
      const pathB = threadTrackPath(data, b.id) || "";
      if (pathA !== pathB) return pathA.localeCompare(pathB);
      return threadUpdatedAt(b).localeCompare(threadUpdatedAt(a));
    });
  }
  return copy.sort((a, b) => threadUpdatedAt(b).localeCompare(threadUpdatedAt(a)));
}

function sourceFilterCounts(threads: ThreadView[]): Record<SourceChannel, number> {
  const counts: Record<SourceChannel, number> = {
    email: 0,
    text: 0,
    slack: 0,
    linkedin: 0,
    meet: 0,
  };
  for (const thread of threads) {
    counts[threadChannelForThread(thread)] += 1;
  }
  return counts;
}

function updateSourceDropdownLabel(): void {
  const trigger = document.getElementById("thread-source-trigger");
  if (!trigger) return;
  const enabled = enabledSourceFilterDefs();
  const selected = enabled.filter((s) => threadSourceFilters.has(s.id));
  if (!selected.length) trigger.textContent = "No sources";
  else if (selected.length === enabled.length) trigger.textContent = "All sources";
  else if (selected.length === 1) trigger.textContent = selected[0].label;
  else trigger.textContent = `${selected.length} sources`;
}

function positionSourceDropdownPanel(): void {
  const trigger = document.getElementById("thread-source-trigger");
  const panel = document.getElementById("thread-source-panel");
  if (!trigger || !panel) return;
  const rect = trigger.getBoundingClientRect();
  panel.style.top = `${rect.bottom + 4}px`;
  panel.style.left = `${rect.left}px`;
}

function setSourceDropdownOpen(open: boolean): void {
  const panel = document.getElementById("thread-source-panel");
  const trigger = document.getElementById("thread-source-trigger");
  if (!panel || !trigger) return;
  panel.hidden = !open;
  trigger.setAttribute("aria-expanded", open ? "true" : "false");
  if (open) positionSourceDropdownPanel();
}

function syncThreadSourceFiltersFromCheckboxes(): void {
  threadSourceFilters = new Set();
  document.querySelectorAll<HTMLInputElement>("#thread-source-panel input[data-source-filter]").forEach((input) => {
    if (input.disabled) return;
    if (input.checked) threadSourceFilters.add(input.dataset.sourceFilter as SourceChannel);
  });
}

function filterThreadsByChannel(threads: ThreadView[]): ThreadView[] {
  if (threadChannelFilter === "all") return threads;
  if (threadChannelFilter === "text") return threads.filter(threadIsText);
  if (threadChannelFilter === "slack") return threads.filter(threadIsSlack);
  if (threadChannelFilter === "linkedin") return threads.filter(threadIsLinkedin);
  if (threadChannelFilter === "meet") return threads.filter(threadIsMeetRecording);
  return threads.filter(threadIsEmail);
}

function channelFilterEmptyMessage(): string {
  if (threadChannelFilter === "text") return "No text threads in this view.";
  if (threadChannelFilter === "slack") return "No Slack threads in this view.";
  if (threadChannelFilter === "linkedin") return "No LinkedIn threads in this view.";
  if (threadChannelFilter === "meet") return "No Meet recording threads in this view.";
  return "No email threads in this view.";
}

function threadNavItemLabel(thread: ThreadView, data: LooseObj | null): string {
  const pendingCount = pendingMessageCountForThread(thread, data);
  const label = threadLabel(thread);
  return pendingCount > 0 ? `${label} (${pendingCount} pending)` : label;
}

function threadNavChannelBadgeHtml(thread: ThreadView): string {
  return sourcePillHtml(threadChannelForThread(thread));
}

function buildThreadCard(thread: ThreadView): HTMLElement {
  const data = getCurrentData();
  const primary = thread.messages[0] || { cleaned: null, summary: null };
  const c0 = (primary.cleaned || {}) as LooseObj;
  const s = threadSummaryForDisplay(thread);
  const sourceAccount = str(data?.source_account);
  const displayMessages = threadMessagesForDisplay(thread, sourceAccount);
  const latestDisplay = displayMessages[0] || primary;
  const cLatest = (latestDisplay.cleaned || {}) as LooseObj;
  const dt = str(cLatest.datetime || s.datetime);
  const tone = str(s.tone);
  const title = str(cLatest.subject) || str(c0.subject) || "(No subject)";
  const label = threadLabel(thread);
  const nMsg = thread.messages.length;
  const pendingCount = pendingMessageCountForThread(thread, data);
  const isText = threadIsText(thread);
  const isSlack = threadIsSlack(thread);
  const isLinkedin = threadIsLinkedin(thread);
  const isMeet = threadIsMeetRecording(thread);
  const updates = latestUpdatesForThread(thread);
  const nextSteps = ownerNextStepsForThread(thread);
  const counterpartySlots = counterpartyAvailabilityForSummary(s);
  const showMessageBlocks = shouldShowThreadMessageBlocks(thread, displayMessages);
  const messagesHtml = showMessageBlocks
    ? `<div class="thread-messages">${displayMessages
        .map((row) => {
          const c = row.cleaned || {};
          const msgDt = str(c.datetime || row.summary?.datetime);
          const subj = str(c.subject) || "(No subject)";
          const fromLine = str(c.sender)
            ? `<div class="meta"><strong>From</strong> ${escapeHtml(
                isText || isSlack || isLinkedin ? formatChatSenderLabel(str(c.sender)) : str(c.sender),
              )}</div>`
            : "";
          const rec = formatRecipients(c.recipients);
          const recLine = rec
            ? `<div class="meta"><strong>Recipients</strong> ${escapeHtml(rec)}</div>`
            : "";
          return `<div class="message-block"><div class="card-top"><time datetime="${escapeHtml(msgDt)}">${formatDate(msgDt)}</time></div><h4 class="msg-subject">${escapeHtml(
            subj,
          )}</h4>${fromLine}${recLine}${messageSourceDetailsHtml(c)}</div>`;
        })
        .join("")}</div>`
    : "";
  const drafts = (data?.thread_drafts || {}) as LooseObj;
  const savedDraft = drafts[thread.id] as LooseObj | undefined;
  const savedIntent = savedDraft ? str(savedDraft.response_intent) : "";
  const savedMd = savedDraft ? str(savedDraft.markdown) : "";
  const showSavedOut = Boolean(savedMd);
  const channelPill = sourcePillHtml(threadChannelForThread(thread));
  const art = document.createElement("article");
  art.className = "card";
  art.id = `thread-${thread.id}`;
  art.innerHTML =
    `<div class="card-top"><time datetime="${escapeHtml(dt)}">${formatDate(dt)}</time>${
      tone ? `<span class="tone ${toneClass(tone)}">${escapeHtml(tone)}</span>` : ""
    }<span class="count-pill">${nMsg} msg${nMsg > 1 ? " (thread)" : ""}</span>${pendingMessagePillHtml(pendingCount)}${channelPill}` +
    `<div class="card-actions">` +
    `<button type="button" class="create-plan-btn" data-add-plan-thread-id="${escapeHtml(thread.id)}">Create a plan</button>` +
    `<button type="button" class="add-to-lane-btn" data-add-to-lane-thread-id="${escapeHtml(thread.id)}">Add to Lane</button>` +
    `<button type="button" class="thread-refresh-summary-btn" data-refresh-thread-id="${escapeHtml(thread.id)}">Refresh summary</button>` +
    `<button type="button" class="draft-reply-toggle" data-draft-thread-id="${escapeHtml(thread.id)}">Draft reply</button>` +
    `<button type="button" class="snooze-btn" data-snooze-thread-id="${escapeHtml(thread.id)}">${
      Number(s.snoozed || 0) === 1 ? "Unsnooze" : "Snooze"
    }</button>` +
    `<button type="button" class="remove-thread-btn" data-remove-thread-id="${escapeHtml(thread.id)}">${
      Number(s.snoozed || 0) === 2 ? "Unremove" : "Remove tracking"
    }</button>` +
    `</div></div>` +
    `<h3>${escapeHtml(title)}</h3><p class="thread-label">${escapeHtml(label)}</p>` +
    `<div class="draft-reply-panel" hidden>` +
    `<p class="draft-reply-hint">What should this reply communicate? (Required — keeps the draft aligned with what you want.)</p>` +
    `<textarea class="draft-intent-input" rows="2" autocomplete="off" placeholder="e.g. I want to meet next week · interested, need more information · don't want to meet">${escapeHtml(savedIntent)}</textarea>` +
    `<div class="draft-reply-actions">` +
    `<button type="button" class="draft-generate-btn" data-draft-thread-id="${escapeHtml(thread.id)}">Generate</button>` +
    `</div>` +
    `<p class="draft-reply-error" hidden></p>` +
    `<label class="draft-output-label">Markdown — copy below</label>` +
    `<textarea class="draft-markdown-output" readonly ${showSavedOut ? "" : "hidden"} rows="12" spellcheck="false">${escapeHtml(savedMd)}</textarea>` +
    `</div>` +
    (str(cLatest.sender)
      ? `<div class="meta"><strong>${nMsg > 1 ? "Latest from" : "From"}</strong> ${escapeHtml(
          isText || isSlack || isLinkedin ? formatChatSenderLabel(str(cLatest.sender)) : str(cLatest.sender),
        )}</div>`
      : "") +
    threadSummaryErrorHtml(s) +
    listSection("Latest updates", updates.length ? updates : s.latest_updates, counterpartySlots) +
    counterpartyAvailabilitySectionHtml(counterpartySlots) +
    nextStepsSectionHtml(nextSteps, counterpartySlots) +
    messagesHtml;
  return art;
}

function renderCards(threads: ThreadView[]): void {
  const token = ++cardsRenderToken;
  const el = cardsEl();
  if (!el) return;
  el.innerHTML = "";
  if (!threads.length) {
    el.innerHTML = `<p class="empty-state">${channelFilterEmptyMessage()}</p>`;
    return;
  }

  let index = 0;
  const renderBatch = (batchSize: number): void => {
    if (token !== cardsRenderToken) return;
    const end = Math.min(index + batchSize, threads.length);
    for (; index < end; index++) {
      const card = buildThreadCard(threads[index]);
      el.appendChild(card);
      observeThreadCard(card);
    }
    if (index < threads.length) scheduleRemainingCards();
  };

  const scheduleRemainingCards = (): void => {
    if (token !== cardsRenderToken) return;
    const run = () => renderBatch(CARD_BATCH_SIZE);
    if (typeof requestIdleCallback === "function") {
      requestIdleCallback(run, { timeout: 200 });
    } else {
      setTimeout(run, 0);
    }
  };

  renderBatch(INITIAL_CARD_BATCH);
}

function renderNav(
  threads: ThreadView[],
  snoozedCount: number,
  removedCount: number,
  channelCounts: {
    all: number;
    text: number;
    slack: number;
    linkedin: number;
    meet: number;
    email: number;
  },
): void {
  const list = navListEl();
  list.innerHTML = "";
  const modeLi = document.createElement("li");
  modeLi.className = "thread-nav-modes";
  const activeBtn = document.createElement("button");
  activeBtn.type = "button";
  activeBtn.className = "nav-mode-btn";
  activeBtn.textContent = "Active";
  activeBtn.classList.toggle("active", threadViewMode === "active");
  activeBtn.addEventListener("click", () => {
    threadViewMode = "active";
    void renderThreadsPage();
  });
  modeLi.appendChild(activeBtn);
  const snoozedBtn = document.createElement("button");
  snoozedBtn.type = "button";
  snoozedBtn.className = "nav-mode-btn";
  snoozedBtn.textContent = `Snoozed (${snoozedCount})`;
  snoozedBtn.classList.toggle("active", threadViewMode === "snoozed");
  snoozedBtn.disabled = snoozedCount === 0;
  snoozedBtn.title = snoozedCount === 0 ? "No snoozed threads" : "Show snoozed threads";
  snoozedBtn.addEventListener("click", () => {
    if (snoozedCount === 0) return;
    threadViewMode = "snoozed";
    void renderThreadsPage();
  });
  modeLi.appendChild(snoozedBtn);
  const removedBtn = document.createElement("button");
  removedBtn.type = "button";
  removedBtn.className = "nav-mode-btn";
  removedBtn.textContent = `Removed (${removedCount})`;
  removedBtn.classList.toggle("active", threadViewMode === "removed");
  removedBtn.disabled = removedCount === 0;
  removedBtn.title = removedCount === 0 ? "No removed threads" : "Show removed threads";
  removedBtn.addEventListener("click", () => {
    if (removedCount === 0) return;
    threadViewMode = "removed";
    void renderThreadsPage();
  });
  modeLi.appendChild(removedBtn);
  list.appendChild(modeLi);

  const channelLi = document.createElement("li");
  channelLi.className = "thread-nav-channels";
  const channelOptions: Array<{
    id: typeof threadChannelFilter;
    label: string;
    count: number;
  }> = [{ id: "all", label: "All", count: channelCounts.all }];
  if (isFeatureEnabled("texts")) {
    channelOptions.push({ id: "text", label: "Texts", count: channelCounts.text });
  }
  if (isFeatureEnabled("slack")) {
    channelOptions.push({ id: "slack", label: "Slack", count: channelCounts.slack });
  }
  if (isFeatureEnabled("linkedin")) {
    channelOptions.push({ id: "linkedin", label: "LinkedIn", count: channelCounts.linkedin });
  }
  if (isFeatureEnabled("meet_recordings")) {
    channelOptions.push({ id: "meet", label: "Meet notes", count: channelCounts.meet });
  }
  channelOptions.push({ id: "email", label: "Emails", count: channelCounts.email });
  for (const { id, label, count } of channelOptions) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "nav-mode-btn nav-channel-btn";
    btn.textContent = count > 0 ? `${label} (${count})` : label;
    btn.classList.toggle("active", threadChannelFilter === id);
    btn.disabled = id !== "all" && count === 0;
    btn.title =
      count === 0 && id !== "all"
        ? `No ${label.toLowerCase()} in this view`
        : `Show ${label.toLowerCase()} only`;
    btn.addEventListener("click", () => {
      if (id !== "all" && count === 0) return;
      threadChannelFilter = id;
      void renderThreadsPage();
    });
    channelLi.appendChild(btn);
  }
  list.appendChild(channelLi);

  const data = getCurrentData();
  for (const thread of threads) {
    const li = document.createElement("li");
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "thread-nav-thread-btn";
    btn.dataset.threadId = thread.id;

    const labelSpan = document.createElement("span");
    labelSpan.className = "thread-nav-thread-label";
    labelSpan.textContent = threadNavItemLabel(thread, data);
    btn.appendChild(labelSpan);

    const badgeWrap = document.createElement("span");
    badgeWrap.innerHTML = threadNavChannelBadgeHtml(thread);
    const badge = badgeWrap.firstElementChild;
    if (badge) btn.appendChild(badge);

    btn.addEventListener("click", () => {
      document.querySelectorAll("#thread-nav button[data-thread-id]").forEach((el) => el.classList.remove("active"));
      btn.classList.add("active");
      openMobileThreadDetail(thread.id);
    });
    li.appendChild(btn);
    list.appendChild(li);
  }
}

function ensureNavObserver(): void {
  if (navObserver) return;
  navObserver = new IntersectionObserver(
    (entries) => {
      let topVisible: string | null = null;
      let bestY = Number.POSITIVE_INFINITY;
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        const el = entry.target as HTMLElement;
        const y = Math.abs(el.getBoundingClientRect().top);
        if (y < bestY) {
          bestY = y;
          topVisible = el.id.replace(/^thread-/, "");
        }
      }
      if (!topVisible) return;
      document.querySelectorAll<HTMLButtonElement>("#thread-nav button[data-thread-id]").forEach((btn) => {
        btn.classList.toggle("active", btn.dataset.threadId === topVisible);
      });
    },
    { root: null, rootMargin: "0px 0px -70% 0px", threshold: [0.1, 0.5, 1] },
  );
}

function observeThreadCard(card: HTMLElement): void {
  ensureNavObserver();
  navObserver?.observe(card);
}

function bindScrollNavHighlight(): void {
  if (navObserver) {
    navObserver.disconnect();
    navObserver = null;
  }
  const cards = Array.from(document.querySelectorAll<HTMLElement>("article.card"));
  if (!cards.length) return;
  ensureNavObserver();
  cards.forEach((c) => navObserver?.observe(c));
}

async function persistThreadSnooze(threadId: string, snoozed: boolean): Promise<void> {
  const res = await fetch("/api/thread-tracking/snooze", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ thread_id: threadId, snoozed: snoozed ? 1 : 0 }),
  });
  if (!res.ok) throw new Error(`Snooze save failed (${res.status})`);
}

async function persistThreadRemove(threadId: string): Promise<void> {
  const res = await fetch("/api/thread-tracking/remove", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ thread_id: threadId }),
  });
  if (!res.ok) throw new Error(`Remove tracking failed (${res.status})`);
}

async function persistThreadUnremove(threadId: string): Promise<void> {
  const res = await fetch("/api/thread-tracking/snooze", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ thread_id: threadId, snoozed: 0 }),
  });
  if (!res.ok) throw new Error(`Unremove failed (${res.status})`);
}

async function persistThreadSummary(threadId: string): Promise<LooseObj> {
  const res = await fetch("/api/threads/summary", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ thread_id: threadId }),
  });
  const body = (await res.json().catch(() => ({}))) as LooseObj;
  if (!res.ok || body.ok === false) {
    throw new Error(str(body.error) || str(body.api_error) || `Thread summary failed (${res.status})`);
  }
  return body;
}

function reloadFromStore(): void {
  const data = getCurrentData();
  if (data) {
    setBundle(data, getCurrentSourceLabel());
    if (document.getElementById("dashboard-threads-root")) {
      void renderDashboardThreadsInline();
    } else {
      void renderThreadsPage();
    }
  }
}

function focusThreadFromQuery(visible: ThreadView[]): void {
  const threadId = new URLSearchParams(location.search).get("thread")?.trim() ?? "";
  if (!threadId) return;
  if (!visible.some((t) => t.id === threadId)) return;
  const card = document.getElementById(`thread-${threadId}`);
  if (!card) return;
  card.scrollIntoView({ behavior: "smooth", block: "start" });
  openMobileThreadDetail(threadId);
  document.querySelectorAll<HTMLButtonElement>("#thread-nav button[data-thread-id]").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.threadId === threadId);
  });
}

export function mountThreadsPage(root: HTMLElement): void {
  root.innerHTML = PAGE_HTML;
}

function ensureDashboardThreadsShell(): void {
  const root = document.getElementById("dashboard-threads-root");
  if (!root || root.dataset.mounted === "1") return;
  root.innerHTML = `
    <div class="thread-toolbar" id="dashboard-thread-toolbar"></div>
    <p class="thread-empty" id="thread-empty" hidden>No threads match these filters.</p>
    <div id="dashboard-threads-cards" class="cards dashboard-threads-cards"></div>`;
  root.dataset.mounted = "1";
  bindDashboardToolbarInteractions();
}

function bindDashboardToolbarInteractions(): void {
  if (dashboardToolbarBound) return;
  dashboardToolbarBound = true;

  document.addEventListener("click", (ev) => {
    const target = ev.target as HTMLElement;
    const toolbar = document.getElementById("dashboard-thread-toolbar");
    if (!toolbar?.contains(target) && !document.getElementById("thread-source-panel")?.contains(target)) {
      setSourceDropdownOpen(false);
    }
  });

  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") setSourceDropdownOpen(false);
  });

  window.addEventListener(
    "scroll",
    () => {
      const panel = document.getElementById("thread-source-panel");
      if (panel && !panel.hidden) positionSourceDropdownPanel();
    },
    true,
  );
  window.addEventListener("resize", () => {
    const panel = document.getElementById("thread-source-panel");
    if (panel && !panel.hidden) positionSourceDropdownPanel();
  });

  document.getElementById("dashboard-threads-root")?.addEventListener("click", (ev) => {
    const target = ev.target as HTMLElement;
    const toolbar = document.getElementById("dashboard-thread-toolbar");
    if (!toolbar) return;

    const modeBtn = target.closest("[data-thread-mode]") as HTMLButtonElement | null;
    if (modeBtn && toolbar.contains(modeBtn)) {
      const mode = modeBtn.dataset.threadMode as typeof threadViewMode;
      if (!mode || modeBtn.disabled) return;
      threadViewMode = mode;
      void renderDashboardThreadsInline();
      return;
    }

    const assignmentBtn = target.closest("[data-thread-assignment]") as HTMLButtonElement | null;
    if (assignmentBtn && toolbar.contains(assignmentBtn)) {
      const assignment = assignmentBtn.dataset.threadAssignment as typeof threadAssignmentFilter;
      if (!assignment || assignmentBtn.disabled) return;
      threadAssignmentFilter = assignment;
      void renderDashboardThreadsInline();
      return;
    }

    if (target.closest("#thread-source-trigger")) {
      ev.stopPropagation();
      const panel = document.getElementById("thread-source-panel");
      setSourceDropdownOpen(Boolean(panel?.hidden));
      return;
    }

    if (target.closest("#thread-source-select-all")) {
      document.querySelectorAll<HTMLInputElement>("#thread-source-panel input[data-source-filter]").forEach((input) => {
        if (!input.disabled) input.checked = true;
      });
      syncThreadSourceFiltersFromCheckboxes();
      updateSourceDropdownLabel();
      void renderDashboardThreadsInline();
      return;
    }

    if (target.closest("#thread-source-select-none")) {
      document.querySelectorAll<HTMLInputElement>("#thread-source-panel input[data-source-filter]").forEach((input) => {
        if (!input.disabled) input.checked = false;
      });
      syncThreadSourceFiltersFromCheckboxes();
      updateSourceDropdownLabel();
      void renderDashboardThreadsInline();
    }
  });

  document.getElementById("dashboard-threads-root")?.addEventListener("change", (ev) => {
    const input = (ev.target as HTMLElement).closest("input[data-source-filter]") as HTMLInputElement | null;
    if (!input) return;
    syncThreadSourceFiltersFromCheckboxes();
    updateSourceDropdownLabel();
    void renderDashboardThreadsInline();
  });
}

function renderDashboardThreadsToolbar(
  inboxThreads: ThreadView[],
  snoozedCount: number,
  removedCount: number,
): void {
  const toolbar = document.getElementById("dashboard-thread-toolbar");
  if (!toolbar) return;

  const counts = sourceFilterCounts(inboxThreads);
  let unassignedCount = 0;
  const data = getCurrentData();
  for (const thread of inboxThreads) {
    if (!threadIsAssignedToTrack(data, thread.id)) unassignedCount += 1;
  }

  if (threadAssignmentFilter === "unassigned" && unassignedCount === 0) {
    threadAssignmentFilter = "all";
  }

  const modeButtons = [
    { id: "active" as const, label: "Active" },
    { id: "snoozed" as const, label: `Snoozed (${snoozedCount})`, disabled: snoozedCount === 0 },
    { id: "removed" as const, label: `Removed (${removedCount})`, disabled: removedCount === 0 },
  ]
    .map(
      ({ id, label, disabled }) =>
        `<button type="button" class="nav-mode-btn${threadViewMode === id ? " active" : ""}" data-thread-mode="${id}"${disabled ? " disabled" : ""}>${escapeHtml(label)}</button>`,
    )
    .join("");

  const sourceOptions = enabledSourceFilterDefs()
    .map(({ id, label }) => {
      const count = counts[id];
      const disabled = count === 0;
      const checked = !disabled && threadSourceFilters.has(id);
      if (disabled) threadSourceFilters.delete(id);
      else if (checked) threadSourceFilters.add(id);
      return `<label class="thread-source-dropdown-option${disabled ? " is-disabled" : ""}"><input type="checkbox" data-source-filter="${id}"${checked ? " checked" : ""}${disabled ? " disabled" : ""} />${sourcePillHtml(id, label)}</label>`;
    })
    .join("");

  const assignmentAllLabel =
    inboxThreads.length > 0 ? `All (${inboxThreads.length})` : "All";
  const assignmentUnassignedLabel =
    unassignedCount > 0 ? `Unassigned (${unassignedCount})` : "Unassigned";

  toolbar.innerHTML = `
    <div class="thread-control-group">
      <span class="thread-control-label" id="thread-inbox-label">Show</span>
      <div class="thread-segmented" role="group" aria-labelledby="thread-inbox-label">${modeButtons}</div>
    </div>
    <div class="thread-control-group">
      <span class="thread-control-label" id="thread-source-label">Source</span>
      <div class="thread-source-dropdown" id="thread-source-dropdown">
        <button type="button" class="thread-source-dropdown-trigger" id="thread-source-trigger" aria-haspopup="true" aria-expanded="false" aria-controls="thread-source-panel">All sources</button>
        <div class="thread-source-dropdown-panel" id="thread-source-panel" hidden>
          <div class="thread-source-dropdown-actions">
            <button type="button" id="thread-source-select-all">All</button>
            <button type="button" id="thread-source-select-none">None</button>
          </div>
          ${sourceOptions}
        </div>
      </div>
    </div>
    <div class="thread-control-group">
      <span class="thread-control-label" id="thread-assignment-label">Assignment</span>
      <div class="thread-segmented" role="group" aria-labelledby="thread-assignment-label">
        <button type="button" class="nav-mode-btn${threadAssignmentFilter === "all" ? " active" : ""}" data-thread-assignment="all">${escapeHtml(assignmentAllLabel)}</button>
        <button type="button" class="nav-mode-btn${threadAssignmentFilter === "unassigned" ? " active" : ""}" data-thread-assignment="unassigned"${unassignedCount === 0 ? " disabled" : ""}>${escapeHtml(assignmentUnassignedLabel)}</button>
      </div>
    </div>
    <div class="thread-control-group">
      <label class="thread-control-label" for="thread-sort">Sort</label>
      <select class="thread-sort-select" id="thread-sort" aria-label="Sort threads">
        <option value="recent-updates"${threadSortMode === "recent-updates" ? " selected" : ""}>Recent updates</option>
        <option value="newest"${threadSortMode === "newest" ? " selected" : ""}>Newest added</option>
        <option value="lane"${threadSortMode === "lane" ? " selected" : ""}>By lane</option>
      </select>
    </div>`;

  updateSourceDropdownLabel();
  document.getElementById("thread-sort")?.addEventListener("change", (ev) => {
    const value = (ev.target as HTMLSelectElement).value;
    if (value === "recent-updates" || value === "newest" || value === "lane") {
      threadSortMode = value;
      void renderDashboardThreadsInline();
    }
  });
}

export async function renderDashboardThreadsInline(): Promise<void> {
  const data = getCurrentData();
  if (!data) return;
  ensureDashboardThreadsShell();

  const { active, snoozed, removed, snoozedCount, removedCount } = partitionThreadsBySnooze(getCurrentThreads());
  const bySnooze = threadViewMode === "snoozed" ? snoozed : threadViewMode === "removed" ? removed : active;

  renderDashboardThreadsToolbar(bySnooze, snoozedCount, removedCount);

  let visible = filterThreadsBySourceSet(bySnooze);
  visible = filterThreadsByAssignment(visible, data);
  visible = sortDashboardThreads(visible, data);

  const emptyEl = document.getElementById("thread-empty");
  const cardsEl = document.getElementById("dashboard-threads-cards");
  emptyEl?.classList.toggle("is-visible", visible.length === 0);
  emptyEl?.toggleAttribute("hidden", visible.length > 0);
  cardsEl?.toggleAttribute("hidden", visible.length === 0);

  renderCards(visible);
}

export async function renderThreadsPage(): Promise<void> {
  const data = getCurrentData();
  if (!data) return;
  applyNavFeatureVisibility();

  const { active, snoozed, removed, snoozedCount, removedCount } = partitionThreadsBySnooze(getCurrentThreads());
  const bySnooze = threadViewMode === "snoozed" ? snoozed : threadViewMode === "removed" ? removed : active;
  const channelCounts = {
    all: bySnooze.length,
    text: bySnooze.filter(threadIsText).length,
    slack: bySnooze.filter(threadIsSlack).length,
    linkedin: bySnooze.filter(threadIsLinkedin).length,
    meet: bySnooze.filter(threadIsMeetRecording).length,
    email: bySnooze.filter(threadIsEmail).length,
  };
  if (threadChannelFilter === "text" && (!isFeatureEnabled("texts") || channelCounts.text === 0)) {
    threadChannelFilter = "all";
  } else if (threadChannelFilter === "slack" && (!isFeatureEnabled("slack") || channelCounts.slack === 0)) {
    threadChannelFilter = "all";
  } else if (
    threadChannelFilter === "linkedin" &&
    (!isFeatureEnabled("linkedin") || channelCounts.linkedin === 0)
  ) {
    threadChannelFilter = "all";
  } else if (
    threadChannelFilter === "meet" &&
    (!isFeatureEnabled("meet_recordings") || channelCounts.meet === 0)
  ) {
    threadChannelFilter = "all";
  } else if (threadChannelFilter === "email" && channelCounts.email === 0) threadChannelFilter = "all";
  const visible = filterThreadsByChannel(bySnooze);

  closeMobileThreadDetail();
  renderCards(visible);
  renderNav(visible, snoozedCount, removedCount, channelCounts);
  bindScrollNavHighlight();
  focusThreadFromQuery(visible);
}

export function bindThreadsInteractions(): void {
  if (interactionsBound) return;
  interactionsBound = true;

  document.getElementById("thread-mobile-back")?.addEventListener("click", () => {
    closeMobileThreadDetail();
  });

  document.addEventListener("click", (ev) => {
    const target = ev.target as HTMLElement | null;
    if (!target) return;
    if (!document.getElementById("page-root")?.contains(target)) return;

    const addPlanBtn = target.closest("button.create-plan-btn") as HTMLButtonElement | null;
    if (addPlanBtn) {
      const threadId = str(addPlanBtn.dataset.addPlanThreadId);
      if (!threadId) return;
      void (async () => {
        const { openDashboardAddPlanForThread } = await import("./dashboard_page.js");
        await openDashboardAddPlanForThread(threadId);
        const url = new URL(location.href);
        url.pathname = "/dashboard";
        url.searchParams.set("thread", threadId);
        url.hash = "schedule-plans";
        history.pushState(null, "", `${url.pathname}${url.search}${url.hash}`);
        const el = document.getElementById(`thread-${threadId}`);
        el?.scrollIntoView({ behavior: "smooth", block: "start" });
        el?.classList.add("is-focused");
        if (el) setTimeout(() => el.classList.remove("is-focused"), 2000);
      })();
      return;
    }

    const addLaneBtn = target.closest("button.add-to-lane-btn") as HTMLButtonElement | null;
    if (addLaneBtn) {
      const threadId = str(addLaneBtn.dataset.addToLaneThreadId);
      if (!threadId) return;
      const thread = getCurrentThreads().find((t) => t.id === threadId);
      if (!thread) return;
      void (async () => {
        const { openAddToLaneModal } = await import("../add_to_lane_ui.js");
        openAddToLaneModal(thread);
      })();
      return;
    }

    const draftToggle = target.closest("button.draft-reply-toggle") as HTMLButtonElement | null;
    if (draftToggle) {
      const card = draftToggle.closest("article.card");
      const panel = card?.querySelector(".draft-reply-panel") as HTMLElement | null;
      if (panel) {
        panel.hidden = !panel.hidden;
        if (!panel.hidden) panel.querySelector<HTMLTextAreaElement>(".draft-intent-input")?.focus();
      }
      return;
    }

    const draftGen = target.closest("button.draft-generate-btn") as HTMLButtonElement | null;
    if (draftGen) {
      void (async () => {
        const threadId = str(draftGen.dataset.draftThreadId);
        const card = draftGen.closest("article.card");
        const intentEl = card?.querySelector<HTMLTextAreaElement>(".draft-intent-input");
        const intent = intentEl?.value.trim() ?? "";
        const outEl = card?.querySelector<HTMLTextAreaElement>(".draft-markdown-output");
        const errEl = card?.querySelector(".draft-reply-error") as HTMLElement | null;
        if (!threadId || !outEl) return;
        if (!intent) {
          if (errEl) {
            errEl.textContent = "Add what the reply should say (required).";
            errEl.hidden = false;
          }
          return;
        }
        if (errEl) errEl.hidden = true;
        draftGen.disabled = true;
        const thread = getCurrentThreads().find((t) => t.id === threadId);
        const primary = thread?.messages[0];
        const c0 = (primary?.cleaned || {}) as LooseObj;
        const subj = str(c0.subject);
        try {
          const payload = await requestEmailReplyDraft(threadId, intent, subj);
          const markdown = str(payload.markdown) || formatDraftReplyMarkdown(payload);
          outEl.value = markdown;
          outEl.hidden = false;
          applySavedThreadDraft(threadId, payload, intent);
        } catch (e) {
          const msg = e instanceof Error ? e.message : String(e);
          outEl.value = ["## Draft reply", "", `**Error:** ${msg}`, ""].join("\n");
          outEl.hidden = false;
        } finally {
          draftGen.disabled = false;
        }
      })();
      return;
    }

    const refreshBtn = target.closest("button.thread-refresh-summary-btn") as HTMLButtonElement | null;
    if (refreshBtn && !refreshBtn.disabled) {
      const threadId = str(refreshBtn.dataset.refreshThreadId);
      if (!threadId) return;
      refreshBtn.disabled = true;
      refreshBtn.textContent = "Refreshing…";
      void (async () => {
        try {
          const body = await persistThreadSummary(threadId);
          const summary = body.thread_summary;
          if (summary && typeof summary === "object") {
            applyThreadSummary(threadId, summary as LooseObj);
          }
          clearSummariesBundleCache();
          reloadFromStore();
          if (threadLaneIds(getCurrentData(), threadId).length) {
            void syncLaneSummaryJobsFromServer();
          }
        } catch (err) {
          console.error(err);
          refreshBtn.disabled = false;
          refreshBtn.textContent = "Refresh summary";
        }
      })();
      return;
    }

    const removeBtn = target.closest("button[data-remove-thread-id]") as HTMLButtonElement | null;
    if (removeBtn) {
      const threadId = str(removeBtn.dataset.removeThreadId);
      if (!threadId) return;
      const thread = getCurrentThreads().find((t) => t.id === threadId);
      if (!thread) return;
      const primary = thread.messages[0] || { cleaned: null, summary: null };
      if (!primary.summary) primary.summary = {};
      const currentlyRemoved = Number((primary.summary as LooseObj).snoozed || 0) === 2;
      (primary.summary as LooseObj).snoozed = currentlyRemoved ? 0 : 2;
      reloadFromStore();
      (currentlyRemoved ? persistThreadUnremove(threadId) : persistThreadRemove(threadId))
        .then(() => clearSummariesBundleCache())
        .catch((err) => console.error(err));
      return;
    }

    const button = target.closest("button[data-snooze-thread-id]") as HTMLButtonElement | null;
    if (!button) return;
    const threadId = str(button.dataset.snoozeThreadId);
    if (!threadId) return;
    const thread = getCurrentThreads().find((t) => t.id === threadId);
    if (!thread) return;
    const primary = thread.messages[0] || { cleaned: null, summary: null };
    if (!primary.summary) primary.summary = {};
    const currentlySnoozed = Number((primary.summary as LooseObj).snoozed || 0) === 1;
    (primary.summary as LooseObj).snoozed = currentlySnoozed ? 0 : 1;
    reloadFromStore();
    persistThreadSnooze(threadId, !currentlySnoozed)
      .then(() => clearSummariesBundleCache())
      .catch((err) => console.error(err));
  });
}
