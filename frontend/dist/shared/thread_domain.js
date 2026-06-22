import { arr, escapeHtml, recipientsContainAddress, str } from "./utils.js";
import { otherPartyOwesRe } from "./owner_config.js";
import { counterpartySlotHighlightHtml, highlightMentionsHtml, } from "./availability_windows.js";
import { formatCounterpartySlotLabel } from "./structured_slot_mentions.js";
import { counterpartySlotsFromText } from "./slot_mentions.js";
let displaySourceAccount = "";
/** Inbox address used to skip Fivelanes delivery shells when picking thread summaries. */
export function setDisplaySourceAccount(account) {
    displaySourceAccount = account.trim().toLowerCase();
}
export function pendingMessageCountForThread(thread, data) {
    if (!data || !data.pending_message_counts || typeof data.pending_message_counts !== "object") {
        return 0;
    }
    const counts = data.pending_message_counts;
    const n = Number(counts[thread.id] || 0);
    return Number.isFinite(n) && n > 0 ? Math.floor(n) : 0;
}
export function pendingMessagePillHtml(count) {
    if (count <= 0)
        return "";
    const label = count === 1 ? "1 pending" : `${count} pending`;
    return `<span class="count-pill pending-pill" title="Waiting for pipeline to segment and summarize">${escapeHtml(label)}</span>`;
}
export function mergeRows(data) {
    const cleaned = Array.isArray(data.cleaned) ? data.cleaned : [];
    const summaries = Array.isArray(data.summary) ? data.summary : [];
    const byId = new Map();
    for (const c of cleaned) {
        const id = str(c.source_id);
        if (id)
            byId.set(id, { cleaned: c, summary: null });
    }
    for (const s of summaries) {
        const id = str(s.source_id);
        if (!id)
            continue;
        const row = byId.get(id) || { cleaned: null, summary: null };
        row.summary = s;
        byId.set(id, row);
    }
    const byThread = new Map();
    for (const row of byId.values()) {
        const tid = str(row.cleaned?.thread_id || row.summary?.thread_id).trim();
        const key = tid || `_orphan_${str(row.cleaned?.source_id || row.summary?.source_id) || "unknown"}`;
        if (!byThread.has(key))
            byThread.set(key, []);
        byThread.get(key).push(row);
    }
    const threads = Array.from(byThread.entries()).map(([id, rows]) => ({
        id,
        messages: rows.sort((a, b) => str(b.cleaned?.datetime || b.summary?.datetime).localeCompare(str(a.cleaned?.datetime || a.summary?.datetime))),
    }));
    return threads.sort((a, b) => str(b.messages[0]?.cleaned?.datetime || b.messages[0]?.summary?.datetime).localeCompare(str(a.messages[0]?.cleaned?.datetime || a.messages[0]?.summary?.datetime)));
}
/** Newest-first message rows for LLM prompts (Message 1 = most recent). */
export function threadMessagesForReply(thread) {
    return thread.messages.map((row) => {
        const c = (row.cleaned || {});
        const s = (row.summary || {});
        const content = str(c.cleaned_content) || str(c.raw_text) || "";
        return {
            datetime: str(c.datetime || s.datetime),
            sender: str(c.sender || c.forwarded_from),
            recipients: str(c.recipients),
            subject: str(c.subject || s.subject),
            content,
        };
    });
}
/** True when the message is a Fivelanes inbox delivery (forward/Cc shell). */
export function messageIsToSourceAccount(row, sourceAccount) {
    const inbox = sourceAccount.trim().toLowerCase();
    if (!inbox)
        return false;
    const c = (row.cleaned || {});
    const s = (row.summary || {});
    return (recipientsContainAddress(c.recipients, inbox) ||
        recipientsContainAddress(s.recipients, inbox));
}
/** Newest-first thread messages for the UI, excluding inbox delivery shells. */
export function threadMessagesForDisplay(thread, sourceAccount) {
    const inbox = (sourceAccount || "").trim().toLowerCase();
    if (!inbox || thread.id.startsWith("text:")) {
        return [...thread.messages];
    }
    return thread.messages.filter((row) => !messageIsToSourceAccount(row, inbox));
}
export function formatDraftReplyMarkdown(payload) {
    const body = str(payload.reply_body);
    const rationale = str(payload.rationale);
    const rawText = str(payload.raw_text);
    const oq = arr(payload.open_questions)
        .map((x) => String(x).trim())
        .filter(Boolean);
    const lines = ["## Draft reply", ""];
    if (body) {
        lines.push(body, "");
    }
    else if (rawText) {
        lines.push("```", rawText, "```", "");
    }
    else {
        lines.push("_(No reply body returned.)_", "");
    }
    lines.push("---", "");
    if (rationale)
        lines.push(`**Note:** ${rationale}`, "");
    if (oq.length) {
        lines.push("**Double-check before sending:**", "");
        for (const q of oq)
            lines.push(`- ${q}`);
        lines.push("");
    }
    return lines.join("\n").trimEnd();
}
function summaryHasDisplayContent(s) {
    if (arr(s.latest_updates).some((x) => String(x).trim()))
        return true;
    if (ownerOwnedNextSteps(s).length)
        return true;
    if (str(s.raw_text).trim())
        return true;
    if (str(s.latest_status).trim())
        return true;
    if (arr(s.pending_items).some((x) => String(x).trim()))
        return true;
    return false;
}
export function threadSummaryForDisplay(thread) {
    const rows = threadMessagesForDisplay(thread, displaySourceAccount);
    for (const row of rows) {
        const s = (row.summary || {});
        if (summaryHasDisplayContent(s))
            return s;
    }
    return (rows[0]?.summary || thread.messages[0]?.summary || {});
}
export function threadIsText(thread) {
    const primary = thread.messages[0] || { cleaned: null, summary: null };
    const c0 = (primary.cleaned || {});
    const s = threadSummaryForDisplay(thread);
    return str(s.channel || c0.channel) === "text" || thread.id.startsWith("text:");
}
export function threadLabel(thread) {
    const p = thread.messages[0] || { cleaned: null, summary: null };
    const s = threadSummaryForDisplay(thread);
    return str(s.suggested_thread_label).trim() || str(p.cleaned?.subject).trim() || "(No subject)";
}
export function threadEmailSubject(thread) {
    for (const row of thread.messages) {
        const subj = str(row.cleaned?.subject || row.summary?.subject).trim();
        if (subj)
            return subj;
    }
    return "(No subject)";
}
function normalizeStepType(raw) {
    const t = raw.trim().toLowerCase();
    if (t === "response required" || t === "response_required")
        return "response required";
    if (t === "follow up needed" || t === "follow_up_needed" || t === "follow-up needed")
        return "follow up needed";
    return "";
}
function humanizeAction(action) {
    const a = action.trim();
    if (/^[a-z][a-z0-9]*(_[a-z0-9]+)+$/.test(a)) {
        return a.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
    }
    return a;
}
function parseNextSteps(summary) {
    const out = [];
    for (const raw of arr(summary.next_steps)) {
        if (raw && typeof raw === "object") {
            const o = raw;
            const action = humanizeAction(str(o.action).trim());
            if (!action)
                continue;
            out.push({
                type: normalizeStepType(str(o.type)),
                action,
                by_when: str(o.by_when).trim(),
            });
            continue;
        }
        const line = humanizeAction(String(raw).trim());
        if (line)
            out.push({ type: "response required", action: line, by_when: "" });
    }
    return out;
}
const PASSIVE_SNAKE_ACTIONS = new Set([
    "await_call",
    "await_response",
    "await_reply",
    "await_meeting",
    "wait_for_call",
    "wait_for_response",
    "wait_for_reply",
]);
const PASSIVE_WAIT_RE = /^(?:wait(?:ing)?\s+(?:for|on)\b|ball\s+is\s+with\b|pending\s+from\b|await(?:ing)?\b|await_|wait_for_)/i;
function isPassiveWaitAction(action) {
    const a = action.trim();
    if (!a)
        return true;
    const key = a.toLowerCase().replace(/-/g, "_");
    if (PASSIVE_SNAKE_ACTIONS.has(key))
        return true;
    if (PASSIVE_WAIT_RE.test(a))
        return true;
    if (otherPartyOwesRe().test(a))
        return true;
    return false;
}
function ownerOwnedNextSteps(summary) {
    return parseNextSteps(summary).filter((step) => !isPassiveWaitAction(step.action));
}
function formatNextStepLine(step) {
    const label = step.type === "follow up needed" ? "Follow up needed" : "Response required";
    const when = step.by_when ? ` — by ${step.by_when}` : "";
    return `${label}: ${step.action}${when}`;
}
function summaryFallbackProse(summary) {
    const raw = str(summary.raw_text).trim();
    if (!raw)
        return "";
    const oneLine = raw.replace(/\s+/g, " ").trim();
    return oneLine.length > 800 ? `${oneLine.slice(0, 797)}…` : oneLine;
}
export function latestUpdatesForThread(t) {
    const s = threadSummaryForDisplay(t);
    const out = [];
    const seen = new Set();
    for (const raw of arr(s.latest_updates)) {
        const line = String(raw).trim();
        if (!line || seen.has(line))
            continue;
        seen.add(line);
        out.push(line);
    }
    const legacyStatus = str(s.latest_status).trim();
    if (!out.length && legacyStatus)
        out.push(legacyStatus);
    for (const raw of arr(s.pending_items)) {
        const line = String(raw).trim();
        if (!line || seen.has(line))
            continue;
        seen.add(line);
        out.push(line);
    }
    const fallback = summaryFallbackProse(s);
    if (!out.length && fallback)
        out.push(fallback);
    return out;
}
export function threadSummaryErrorHtml(summary) {
    const err = str(summary.summary_api_error).trim();
    if (!err)
        return "";
    return `<div class="section summary-error"><h4>Summary issue</h4><p>${escapeHtml(err)}</p></div>`;
}
export function responseRequiredForThread(t) {
    const s = threadSummaryForDisplay(t);
    return ownerOwnedNextSteps(s)
        .filter((step) => step.type !== "follow up needed")
        .map(formatNextStepLine);
}
export function followUpNeededForThread(t) {
    const s = threadSummaryForDisplay(t);
    return ownerOwnedNextSteps(s)
        .filter((step) => step.type === "follow up needed")
        .map(formatNextStepLine);
}
function counterpartySlotKey(slot) {
    return `${slot.date}|${slot.start}|${slot.end}`;
}
function counterpartySlotsFromSummaryField(summary) {
    const out = [];
    for (const raw of arr(summary.counterparty_availability)) {
        if (!raw || typeof raw !== "object")
            continue;
        const row = raw;
        const date = str(row.date).trim();
        const start = str(row.start).trim();
        const end = str(row.end).trim();
        if (!date || !start || !end)
            continue;
        const slot = { date, start, end };
        const party = str(row.party).trim();
        const label = str(row.label).trim();
        if (party)
            slot.party = party;
        if (label)
            slot.label = label;
        out.push(slot);
    }
    return out;
}
export function counterpartyAvailabilityForSummary(summary) {
    const out = counterpartySlotsFromSummaryField(summary);
    const seen = new Set(out.map(counterpartySlotKey));
    const proseSources = [
        ...arr(summary.latest_updates).map(String),
        ...arr(summary.next_steps).flatMap((raw) => {
            if (raw && typeof raw === "object")
                return [str(raw.action)];
            return [String(raw ?? "")];
        }),
    ];
    for (const line of proseSources) {
        for (const inferred of counterpartySlotsFromText(line)) {
            const key = counterpartySlotKey(inferred);
            if (seen.has(key))
                continue;
            seen.add(key);
            out.push(inferred);
        }
    }
    return out;
}
export function counterpartyAvailabilitySectionHtml(slots) {
    if (!slots.length)
        return "";
    const rows = slots
        .map((slot) => {
        const display = formatCounterpartySlotLabel(slot);
        const party = slot.party ? ` <span class="counterparty-slot-party">${escapeHtml(slot.party)}</span>` : "";
        const note = slot.label ? ` <span class="counterparty-slot-note">${escapeHtml(slot.label)}</span>` : "";
        return `<li>${counterpartySlotHighlightHtml(slot, display)}${party}${note}</li>`;
    })
        .join("");
    return `<div class="section"><h4>Their availability</h4><ul class="counterparty-slots">${rows}</ul></div>`;
}
export function nextStepsSectionHtml(steps, structuredSlots = []) {
    if (!steps.length)
        return "";
    const rows = steps
        .map((step) => {
        const isFollowUp = step.type === "follow up needed";
        const label = isFollowUp ? "Follow up needed" : "Response required";
        const typeClass = isFollowUp ? "next-step-type follow-up" : "next-step-type";
        const when = step.by_when ? ` <span class="next-step-when">(${escapeHtml(step.by_when)})</span>` : "";
        return `<li><span class="${typeClass}">${escapeHtml(label)}</span> ${highlightMentionsHtml(step.action, structuredSlots)}${when}</li>`;
    })
        .join("");
    return `<div class="section"><h4>Next steps</h4><ul class="next-steps">${rows}</ul></div>`;
}
function parseNextStepsForDisplay(summary) {
    return ownerOwnedNextSteps(summary);
}
export function ownerNextStepsForThread(t) {
    return parseNextStepsForDisplay(threadSummaryForDisplay(t));
}
export function partitionThreadsBySnooze(threads) {
    const active = [];
    const snoozed = [];
    const removed = [];
    let snoozedCount = 0;
    let removedCount = 0;
    for (const thread of threads) {
        const primary = thread.messages[0] || { cleaned: null, summary: null };
        const s = (primary.summary || {});
        const state = Number(s.snoozed || 0);
        if (state === 2) {
            removedCount += 1;
            removed.push(thread);
            continue;
        }
        const isSnoozed = state === 1;
        if (isSnoozed) {
            snoozedCount += 1;
            snoozed.push(thread);
            continue;
        }
        active.push(thread);
    }
    return { active, snoozed, removed, snoozedCount, removedCount };
}
export function listSection(title, items, structuredSlots = []) {
    const rows = arr(items)
        .map((x) => `<li>${highlightMentionsHtml(String(x ?? ""), structuredSlots)}</li>`)
        .join("");
    return rows ? `<div class="section"><h4>${escapeHtml(title)}</h4><ul>${rows}</ul></div>` : "";
}
export function renderMentionAwareText(text, structuredSlots = []) {
    return highlightMentionsHtml(text, structuredSlots);
}
export function threadSummaryTextFragments(thread) {
    const summary = threadSummaryForDisplay(thread);
    const out = [];
    for (const line of latestUpdatesForThread(thread))
        out.push(line);
    for (const step of ownerOwnedNextSteps(summary))
        out.push(step.action);
    return out;
}
/** True when the thread card should list per-message blocks (not only header meta). */
export function shouldShowThreadMessageBlocks(thread, displayMessages) {
    if (displayMessages.length > 1)
        return true;
    return displayMessages.length === 1 && thread.messages.length > 1;
}
export function messageSourceDetailsHtml(c) {
    if (!c)
        return "";
    const cleanedBody = str(c.cleaned_content);
    const rawBody = str(c.raw_text);
    const quoted = str(c.quoted_reply);
    const sig = str(c.signature);
    const err = str(c.api_error);
    if (!cleanedBody && !rawBody && !quoted && !sig && !err)
        return "";
    let inner = "";
    if (cleanedBody)
        inner += `<h4>Cleaned body</h4><pre class="pre">${escapeHtml(cleanedBody)}</pre>`;
    else if (rawBody)
        inner += `<h4>Source text</h4><pre class="pre">${escapeHtml(rawBody)}</pre>`;
    if (quoted)
        inner += `<h4>Quoted thread</h4><pre class="pre">${escapeHtml(quoted)}</pre>`;
    if (sig)
        inner += `<h4>Signature</h4><pre class="pre">${escapeHtml(sig)}</pre>`;
    if (err)
        inner += `<h4>API error</h4><pre class="pre">${escapeHtml(err)}</pre>`;
    return `<details class="body-detail"><summary>Message source text</summary>${inner}</details>`;
}
