import { clearSummariesBundleCache } from "../shared/summaries_store.js";
import { escapeHtml } from "../shared/utils.js";
const PAGE_HTML = `
<div class="view-texts-setup view-linkedin-setup">
  <header class="texts-setup-header">
    <h2>LinkedIn threads</h2>
    <p class="texts-setup-lead">
      Pull fresh messages for selected conversations, then choose which threads to track on the
      <a href="/onebox">onebox</a>.
    </p>
  </header>

  <section class="texts-setup-card">
    <p class="texts-setup-hint">
      Pull is manual — the scheduled pipeline only summarizes existing messages.
    </p>
    <div class="texts-setup-actions">
      <button type="button" class="texts-save-tracked-btn" id="linkedin-pull-btn">Pull tracked conversations</button>
    </div>
    <p id="linkedin-pull-status" class="texts-status" hidden></p>
  </section>

  <section class="texts-setup-card" aria-labelledby="linkedin-select-heading">
    <h3 id="linkedin-select-heading">Choose threads to track</h3>
    <div class="texts-conversation-toolbar">
      <button type="button" class="texts-select-all-btn" id="linkedin-select-all-btn">Select all</button>
      <button type="button" class="texts-select-none-btn" id="linkedin-select-none-btn">Select none</button>
      <span id="linkedin-selection-count" class="texts-selection-count"></span>
    </div>
    <div id="linkedin-conversation-list" class="texts-conversation-list"></div>
    <div class="texts-setup-actions">
      <button type="button" class="texts-save-tracked-btn" id="linkedin-save-tracked-btn">Save tracking</button>
      <button type="button" class="texts-summarize-btn" id="linkedin-summarize-btn">Generate summaries</button>
    </div>
    <p id="linkedin-save-status" class="texts-status" hidden></p>
  </section>
</div>`;
let interactionsBound = false;
let catalogRows = [];
let trackedKeys = new Set();
function setStatus(el, message, kind) {
    if (!el)
        return;
    el.hidden = !message;
    el.textContent = message;
    el.classList.remove("texts-status-ok", "texts-status-error", "texts-status-info");
    el.classList.add(kind === "ok" ? "texts-status-ok" : kind === "error" ? "texts-status-error" : "texts-status-info");
}
function selectedKeys() {
    return Array.from(document.querySelectorAll(".linkedin-conversation-checkbox:checked"))
        .map((el) => el.dataset.conversationKey || "")
        .filter(Boolean);
}
function updateSelectionUi() {
    const countEl = document.getElementById("linkedin-selection-count");
    const selected = selectedKeys();
    if (countEl) {
        countEl.textContent =
            selected.length === 0 ? "None selected" : `${selected.length} selected for tracking`;
    }
}
function optionLabel(row) {
    const base = row.label || row.conversation_key;
    const count = row.message_count === 1
        ? "1 message"
        : row.message_count != null
            ? `${row.message_count} messages`
            : "";
    const when = row.last_message_at ? ` · ${row.last_message_at}` : "";
    const svc = row.service ? ` · ${row.service}` : "";
    return count ? `${base}${svc} (${count}${when})` : `${base}${svc}`;
}
function renderConversationList() {
    const listEl = document.getElementById("linkedin-conversation-list");
    if (!listEl)
        return;
    if (!catalogRows.length) {
        listEl.innerHTML =
            '<p class="texts-conversation-empty">No conversations found in <code>linkedin-messages/messages.csv</code>.</p>';
        updateSelectionUi();
        return;
    }
    listEl.innerHTML = catalogRows
        .map((row) => {
        const key = row.conversation_key || row.id;
        const checked = trackedKeys.has(key);
        return `<label class="texts-conversation-option">
        <input
          type="checkbox"
          class="linkedin-conversation-checkbox"
          data-conversation-key="${escapeHtml(key)}"
          ${checked ? "checked" : ""}
        />
        <span>${escapeHtml(optionLabel(row))}</span>
      </label>`;
    })
        .join("");
    listEl.querySelectorAll(".linkedin-conversation-checkbox").forEach((box) => {
        box.addEventListener("change", updateSelectionUi);
    });
    updateSelectionUi();
}
async function fetchCatalog() {
    const res = await fetch("/api/linkedin/catalog");
    const data = (await res.json());
    if (!res.ok) {
        throw new Error(data.error || `HTTP ${res.status}`);
    }
    catalogRows = Array.isArray(data.catalog) ? data.catalog : [];
    trackedKeys = new Set(Array.isArray(data.tracked) ? data.tracked : []);
}
export function mountLinkedinSetupPage(root) {
    root.innerHTML = PAGE_HTML;
}
export async function renderLinkedinSetupPage() {
    const saveStatus = document.getElementById("linkedin-save-status");
    try {
        await fetchCatalog();
        renderConversationList();
        setStatus(saveStatus, "", "info");
    }
    catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        setStatus(saveStatus, `Could not load conversations: ${msg}`, "error");
    }
}
export function bindLinkedinSetupInteractions() {
    if (interactionsBound)
        return;
    interactionsBound = true;
    const pullBtn = document.getElementById("linkedin-pull-btn");
    const pullStatus = document.getElementById("linkedin-pull-status");
    const saveBtn = document.getElementById("linkedin-save-tracked-btn");
    const summarizeBtn = document.getElementById("linkedin-summarize-btn");
    const saveStatus = document.getElementById("linkedin-save-status");
    const selectAllBtn = document.getElementById("linkedin-select-all-btn");
    const selectNoneBtn = document.getElementById("linkedin-select-none-btn");
    pullBtn?.addEventListener("click", async () => {
        const keys = selectedKeys();
        const useTrackedOnly = keys.length === 0;
        if (useTrackedOnly && trackedKeys.size === 0) {
            setStatus(pullStatus, "Save tracking for at least one conversation first.", "error");
            return;
        }
        setStatus(pullStatus, useTrackedOnly
            ? `Pulling ${trackedKeys.size} tracked conversation(s) from LinkedIn… this may take a few minutes.`
            : `Pulling ${keys.length} selected conversation(s) from LinkedIn… this may take a few minutes.`, "info");
        if (pullBtn)
            pullBtn.disabled = true;
        try {
            const res = await fetch("/api/linkedin/pull", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(useTrackedOnly ? {} : { conversation_keys: keys }),
            });
            const data = (await res.json());
            if (!res.ok) {
                setStatus(pullStatus, data.error || `HTTP ${res.status}`, "error");
                return;
            }
            if (data.skipped) {
                setStatus(pullStatus, "No tracked conversations to pull.", "error");
                return;
            }
            clearSummariesBundleCache();
            await fetchCatalog();
            renderConversationList();
            setStatus(pullStatus, `Pulled ${data.message_count ?? 0} new message(s) from ${data.conversation_count ?? 0} conversation(s). Summaries are updating — refresh Threads shortly.`, "ok");
        }
        catch (err) {
            const msg = err instanceof Error ? err.message : String(err);
            setStatus(pullStatus, msg, "error");
        }
        finally {
            if (pullBtn)
                pullBtn.disabled = false;
        }
    });
    selectAllBtn?.addEventListener("click", () => {
        document.querySelectorAll(".linkedin-conversation-checkbox").forEach((box) => {
            box.checked = true;
        });
        updateSelectionUi();
    });
    selectNoneBtn?.addEventListener("click", () => {
        document.querySelectorAll(".linkedin-conversation-checkbox").forEach((box) => {
            box.checked = false;
        });
        updateSelectionUi();
    });
    summarizeBtn?.addEventListener("click", async () => {
        setStatus(saveStatus, "Generating summaries for tracked threads…", "info");
        summarizeBtn.disabled = true;
        try {
            const res = await fetch("/api/linkedin/summarize", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ force: true }),
            });
            const data = (await res.json());
            if (!res.ok) {
                setStatus(saveStatus, data.error || `HTTP ${res.status}`, "error");
                return;
            }
            clearSummariesBundleCache();
            const errs = Array.isArray(data.errors) ? data.errors : [];
            const errCount = errs.length;
            const errDetail = errs
                .map((e) => {
                const key = e.conversation_key ? `${e.conversation_key}: ` : "";
                return `${key}${e.error || "unknown error"}`;
            })
                .join(" · ");
            setStatus(saveStatus, `Done — ${data.summarized ?? 0} summarized, ${data.skipped ?? 0} skipped${errCount ? `, ${errCount} error(s)` : ""}${errDetail ? ` — ${errDetail}` : ""}. Refresh Threads.`, errCount && !data.summarized ? "error" : "ok");
        }
        catch (err) {
            const msg = err instanceof Error ? err.message : String(err);
            setStatus(saveStatus, msg, "error");
        }
        finally {
            summarizeBtn.disabled = false;
        }
    });
    saveBtn?.addEventListener("click", async () => {
        const keys = selectedKeys();
        setStatus(saveStatus, "Saving…", "info");
        try {
            const res = await fetch("/api/linkedin/track", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ conversation_keys: keys }),
            });
            const data = (await res.json());
            if (!res.ok) {
                setStatus(saveStatus, data.error || `HTTP ${res.status}`, "error");
                return;
            }
            trackedKeys = new Set(keys);
            clearSummariesBundleCache();
            setStatus(saveStatus, `Tracking ${data.tracked_count ?? keys.length} thread(s). Summaries are generating in the background — refresh Threads in a minute.`, "ok");
        }
        catch (err) {
            const msg = err instanceof Error ? err.message : String(err);
            setStatus(saveStatus, msg, "error");
        }
    });
}
