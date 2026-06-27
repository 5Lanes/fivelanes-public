import { clearSummariesBundleCache } from "../shared/summaries_store.js";
import { escapeHtml } from "../shared/utils.js";
const PAGE_HTML = `
<div class="view-texts-setup view-slack-setup">
  <header class="texts-setup-header">
    <h2>Slack DMs</h2>
    <p class="texts-setup-lead">
      Pull your 1:1 Slack DMs, then choose which conversations to track.
      Tracked threads appear on <a href="/threads">Threads</a>.
    </p>
  </header>

  <section class="texts-setup-card" aria-labelledby="slack-pull-heading">
    <h3 id="slack-pull-heading">Pull from Slack</h3>
    <p class="texts-setup-hint">Uses <code>SLACK_USER_TOKEN</code> from your data <code>.env</code>.</p>
    <div class="texts-setup-actions">
      <button type="button" class="texts-save-tracked-btn" id="slack-pull-btn">Pull DMs from Slack</button>
    </div>
    <p id="slack-pull-status" class="texts-status" hidden></p>
  </section>

  <section class="texts-setup-card" aria-labelledby="slack-select-heading">
    <h3 id="slack-select-heading">Choose DMs to track</h3>
    <p class="texts-setup-hint" id="slack-dir-hint"></p>
    <div class="texts-conversation-toolbar">
      <button type="button" class="texts-select-all-btn" id="slack-select-all-btn">Select all</button>
      <button type="button" class="texts-select-none-btn" id="slack-select-none-btn">Select none</button>
      <span id="slack-selection-count" class="texts-selection-count"></span>
    </div>
    <div id="slack-conversation-list" class="texts-conversation-list"></div>
    <div class="texts-setup-actions">
      <button type="button" class="texts-save-tracked-btn" id="slack-save-tracked-btn">Save tracking</button>
      <button type="button" class="texts-summarize-btn" id="slack-summarize-btn">Generate summaries</button>
    </div>
    <p id="slack-save-status" class="texts-status" hidden></p>
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
    return Array.from(document.querySelectorAll(".slack-conversation-checkbox:checked"))
        .map((el) => el.dataset.conversationKey || "")
        .filter(Boolean);
}
function updateSelectionUi() {
    const countEl = document.getElementById("slack-selection-count");
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
    const listEl = document.getElementById("slack-conversation-list");
    if (!listEl)
        return;
    if (!catalogRows.length) {
        listEl.innerHTML =
            '<p class="texts-conversation-empty">No Slack DMs yet. Click <strong>Pull DMs from Slack</strong> above.</p>';
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
          class="slack-conversation-checkbox"
          data-conversation-key="${escapeHtml(key)}"
          ${checked ? "checked" : ""}
        />
        <span>${escapeHtml(optionLabel(row))}</span>
      </label>`;
    })
        .join("");
    listEl.querySelectorAll(".slack-conversation-checkbox").forEach((box) => {
        box.addEventListener("change", updateSelectionUi);
    });
    updateSelectionUi();
}
async function fetchCatalog() {
    const res = await fetch("/api/slack/catalog");
    const data = (await res.json());
    if (!res.ok) {
        throw new Error(data.error || `HTTP ${res.status}`);
    }
    const hint = document.getElementById("slack-dir-hint");
    if (hint && data.slack_dms_dir) {
        hint.textContent = `Reading from ${data.slack_dms_dir}`;
    }
    catalogRows = Array.isArray(data.catalog) ? data.catalog : [];
    trackedKeys = new Set(Array.isArray(data.tracked) ? data.tracked : []);
}
export function mountSlackSetupPage(root) {
    root.innerHTML = PAGE_HTML;
}
export async function renderSlackSetupPage() {
    const saveStatus = document.getElementById("slack-save-status");
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
export function bindSlackSetupInteractions() {
    if (interactionsBound)
        return;
    interactionsBound = true;
    const pullBtn = document.getElementById("slack-pull-btn");
    const pullStatus = document.getElementById("slack-pull-status");
    const saveBtn = document.getElementById("slack-save-tracked-btn");
    const summarizeBtn = document.getElementById("slack-summarize-btn");
    const saveStatus = document.getElementById("slack-save-status");
    const selectAllBtn = document.getElementById("slack-select-all-btn");
    const selectNoneBtn = document.getElementById("slack-select-none-btn");
    pullBtn?.addEventListener("click", async () => {
        setStatus(pullStatus, "Pulling DMs from Slack… this may take a minute.", "info");
        if (pullBtn)
            pullBtn.disabled = true;
        try {
            const res = await fetch("/api/slack/pull", { method: "POST" });
            const data = (await res.json());
            if (!res.ok) {
                setStatus(pullStatus, data.error || `HTTP ${res.status}`, "error");
                return;
            }
            await fetchCatalog();
            renderConversationList();
            setStatus(pullStatus, `Pulled ${data.dm_count ?? 0} DM(s), ${data.message_count ?? 0} message(s).`, "ok");
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
        document.querySelectorAll(".slack-conversation-checkbox").forEach((box) => {
            box.checked = true;
        });
        updateSelectionUi();
    });
    selectNoneBtn?.addEventListener("click", () => {
        document.querySelectorAll(".slack-conversation-checkbox").forEach((box) => {
            box.checked = false;
        });
        updateSelectionUi();
    });
    summarizeBtn?.addEventListener("click", async () => {
        setStatus(saveStatus, "Generating summaries for tracked threads…", "info");
        summarizeBtn.disabled = true;
        try {
            const res = await fetch("/api/slack/summarize", {
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
            const res = await fetch("/api/slack/track", {
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
