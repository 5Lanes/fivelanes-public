import {
  applySavedThreadDraft,
  applyThreadSummary,
  clearSummariesBundleCache,
  getCurrentData,
  getCurrentSourceLabel,
  getCurrentThreads,
  setBundle,
} from "../shared/summaries_store.js";
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
import { ensureAvailabilityDocLoaded } from "../shared/availability_windows.js";
import { applyNavFeatureVisibility, isFeatureEnabled } from "../shared/features.js";
import { refreshAvailabilityPanel } from "../availability_panel.js";

const PAGE_HTML = `
<div class="dashboard-layout dashboard-layout--threads">
  <aside class="thread-nav" id="thread-nav">
    <h2>Threads</h2>
    <ul id="thread-nav-list"></ul>
  </aside>
  <div class="main-panel">
    <div id="lanes" class="lanes-grid" hidden></div>
    <div id="cards" class="cards"></div>
  </div>
  <aside class="availability-rail" id="availability-rail" aria-label="Your availability" data-feature="availability">
    <section id="availability-section" class="availability-section" aria-labelledby="availability-heading" hidden>
      <h2 id="availability-heading" class="availability-section-title">Open slots · next 7 days</h2>
      <p class="availability-meta" id="availability-meta"></p>
      <div id="availability-agenda" class="availability-agenda"></div>
    </section>
  </aside>
</div>`;

let threadViewMode: "active" | "snoozed" | "removed" = "active";
let threadChannelFilter: "all" | "text" | "slack" | "email" = "all";
let navObserver: IntersectionObserver | null = null;
let interactionsBound = false;

function cardsEl(): HTMLDivElement {
  return document.getElementById("cards") as HTMLDivElement;
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

function filterThreadsByChannel(threads: ThreadView[]): ThreadView[] {
  if (threadChannelFilter === "all") return threads;
  if (threadChannelFilter === "text") return threads.filter(threadIsText);
  if (threadChannelFilter === "slack") return threads.filter(threadIsSlack);
  return threads.filter(threadIsEmail);
}

function channelFilterEmptyMessage(): string {
  if (threadChannelFilter === "text") return "No text threads in this view.";
  if (threadChannelFilter === "slack") return "No Slack threads in this view.";
  return "No email threads in this view.";
}

function renderCards(threads: ThreadView[]): void {
  const el = cardsEl();
  el.innerHTML = "";
  if (!threads.length) {
    el.innerHTML = `<p class="empty-state">${channelFilterEmptyMessage()}</p>`;
    return;
  }
  const data = getCurrentData();
  for (const thread of threads) {
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
              ? `<div class="meta"><strong>From</strong> ${escapeHtml(str(c.sender))}</div>`
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
    const art = document.createElement("article");
    art.className = "card";
    art.id = `thread-${thread.id}`;
    art.innerHTML =
      `<div class="card-top"><time datetime="${escapeHtml(dt)}">${formatDate(dt)}</time>${
        tone ? `<span class="tone ${toneClass(tone)}">${escapeHtml(tone)}</span>` : ""
      }<span class="count-pill">${nMsg} msg${nMsg > 1 ? " (thread)" : ""}</span>${pendingMessagePillHtml(pendingCount)}${
        isText ? `<span class="count-pill channel-text">Text</span>` : isSlack ? `<span class="count-pill channel-slack">Slack</span>` : ""
      }` +
      `<div class="card-actions">` +
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
            isText || isSlack ? formatChatSenderLabel(str(cLatest.sender)) : str(cLatest.sender),
          )}</div>`
        : "") +
      threadSummaryErrorHtml(s) +
      listSection("Latest updates", updates.length ? updates : s.latest_updates, counterpartySlots) +
      counterpartyAvailabilitySectionHtml(counterpartySlots) +
      nextStepsSectionHtml(nextSteps, counterpartySlots) +
      messagesHtml;
    el.appendChild(art);
  }
}

function renderNav(
  threads: ThreadView[],
  snoozedCount: number,
  removedCount: number,
  channelCounts: { all: number; text: number; slack: number; email: number },
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
  for (const { id, label, count } of [
    { id: "all" as const, label: "All", count: channelCounts.all },
    { id: "text" as const, label: "Texts", count: channelCounts.text },
    { id: "slack" as const, label: "Slack", count: channelCounts.slack },
    { id: "email" as const, label: "Emails", count: channelCounts.email },
  ]) {
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
    const pendingCount = pendingMessageCountForThread(thread, data);
    btn.textContent =
      pendingCount > 0
        ? `${threadLabel(thread)} (${pendingCount} pending)`
        : threadLabel(thread);
    btn.dataset.threadId = thread.id;
    btn.addEventListener("click", () => {
      document.querySelectorAll("#thread-nav button[data-thread-id]").forEach((el) => el.classList.remove("active"));
      btn.classList.add("active");
      document.getElementById(`thread-${thread.id}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
    li.appendChild(btn);
    list.appendChild(li);
  }
}

function bindScrollNavHighlight(): void {
  if (navObserver) {
    navObserver.disconnect();
    navObserver = null;
  }
  const cards = Array.from(document.querySelectorAll<HTMLElement>("article.card"));
  if (!cards.length) return;
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
    void renderThreadsPage();
  }
}

export function mountThreadsPage(root: HTMLElement): void {
  root.innerHTML = PAGE_HTML;
}

export async function renderThreadsPage(): Promise<void> {
  const data = getCurrentData();
  if (!data) return;
  applyNavFeatureVisibility();
  if (isFeatureEnabled("availability")) {
    await ensureAvailabilityDocLoaded();
  }

  const { active, snoozed, removed, snoozedCount, removedCount } = partitionThreadsBySnooze(getCurrentThreads());
  const bySnooze = threadViewMode === "snoozed" ? snoozed : threadViewMode === "removed" ? removed : active;
  const channelCounts = {
    all: bySnooze.length,
    text: bySnooze.filter(threadIsText).length,
    slack: bySnooze.filter(threadIsSlack).length,
    email: bySnooze.filter(threadIsEmail).length,
  };
  if (threadChannelFilter === "text" && channelCounts.text === 0) threadChannelFilter = "all";
  else if (threadChannelFilter === "slack" && channelCounts.slack === 0) threadChannelFilter = "all";
  else if (threadChannelFilter === "email" && channelCounts.email === 0) threadChannelFilter = "all";
  const visible = filterThreadsByChannel(bySnooze);

  renderCards(visible);
  renderNav(visible, snoozedCount, removedCount, channelCounts);
  bindScrollNavHighlight();
  if (isFeatureEnabled("availability")) {
    await refreshAvailabilityPanel();
  }
}

export function bindThreadsInteractions(): void {
  if (interactionsBound) return;
  interactionsBound = true;

  document.addEventListener("click", (ev) => {
    const target = ev.target as HTMLElement | null;
    if (!target) return;
    if (!document.getElementById("page-root")?.contains(target)) return;

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
