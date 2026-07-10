import { clearSummariesBundleCache } from "../shared/summaries_store.js";
import { escapeHtml } from "../shared/utils.js";
const PAGE_HTML = `
<div class="view-texts-setup view-calendar-setup">
  <header class="texts-setup-header">
    <h2>Calendar events</h2>
    <p class="texts-setup-lead">
      Choose which calendar events to track as threads. Tracked events appear on the
      <a href="/dashboard">dashboard</a> as their own thread — tracking an event here never
      links or tags it into a lane on its own; adding it to a lane is always a separate,
      explicit action.
    </p>
  </header>

  <section class="texts-setup-card" aria-labelledby="calendar-select-heading">
    <h3 id="calendar-select-heading">Choose events to track</h3>
    <div class="texts-conversation-toolbar">
      <button type="button" class="texts-select-all-btn" id="calendar-select-all-btn">Select all</button>
      <button type="button" class="texts-select-none-btn" id="calendar-select-none-btn">Select none</button>
      <span id="calendar-selection-count" class="texts-selection-count"></span>
    </div>
    <div id="calendar-event-list" class="texts-conversation-list"></div>
    <div class="texts-setup-actions">
      <button type="button" class="texts-save-tracked-btn" id="calendar-save-tracked-btn">Save tracking</button>
      <button type="button" class="texts-summarize-btn" id="calendar-summarize-btn">Generate summaries</button>
    </div>
    <p id="calendar-save-status" class="texts-status" hidden></p>
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
    return Array.from(document.querySelectorAll(".calendar-event-checkbox:checked"))
        .map((el) => el.dataset.dedupeKey || "")
        .filter(Boolean);
}
function updateSelectionUi() {
    const countEl = document.getElementById("calendar-selection-count");
    const selected = selectedKeys();
    if (countEl) {
        countEl.textContent =
            selected.length === 0 ? "None selected" : `${selected.length} selected for tracking`;
    }
}
function formatWhen(raw) {
    const value = (raw || "").trim();
    if (!value)
        return "";
    const d = new Date(value);
    if (Number.isNaN(d.getTime()))
        return value;
    return d.toLocaleString(undefined, {
        year: "numeric",
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
    });
}
function optionLabel(row) {
    const base = row.label || row.dedupe_key;
    const when = formatWhen(row.start_iso);
    return when ? `${base} · ${when}` : base;
}
function renderEventList() {
    const listEl = document.getElementById("calendar-event-list");
    if (!listEl)
        return;
    if (!catalogRows.length) {
        listEl.innerHTML =
            '<p class="texts-conversation-empty">No calendar events found yet.</p>';
        updateSelectionUi();
        return;
    }
    listEl.innerHTML = catalogRows
        .map((row) => {
        const key = row.dedupe_key || row.id;
        const checked = trackedKeys.has(key);
        return `<label class="texts-conversation-option">
        <input
          type="checkbox"
          class="calendar-event-checkbox"
          data-dedupe-key="${escapeHtml(key)}"
          ${checked ? "checked" : ""}
        />
        <span>${escapeHtml(optionLabel(row))}</span>
      </label>`;
    })
        .join("");
    listEl.querySelectorAll(".calendar-event-checkbox").forEach((box) => {
        box.addEventListener("change", updateSelectionUi);
    });
    updateSelectionUi();
}
async function fetchCatalog() {
    const res = await fetch("/api/calendar/catalog");
    const data = (await res.json());
    if (!res.ok) {
        throw new Error(data.error || `HTTP ${res.status}`);
    }
    catalogRows = Array.isArray(data.catalog) ? data.catalog : [];
    trackedKeys = new Set(Array.isArray(data.tracked) ? data.tracked : []);
}
export function mountCalendarSetupPage(root) {
    root.innerHTML = PAGE_HTML;
}
export async function renderCalendarSetupPage() {
    const saveStatus = document.getElementById("calendar-save-status");
    try {
        await fetchCatalog();
        renderEventList();
        setStatus(saveStatus, "", "info");
    }
    catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        setStatus(saveStatus, `Could not load calendar events: ${msg}`, "error");
    }
}
export function bindCalendarSetupInteractions() {
    if (interactionsBound)
        return;
    interactionsBound = true;
    const saveBtn = document.getElementById("calendar-save-tracked-btn");
    const summarizeBtn = document.getElementById("calendar-summarize-btn");
    const saveStatus = document.getElementById("calendar-save-status");
    const selectAllBtn = document.getElementById("calendar-select-all-btn");
    const selectNoneBtn = document.getElementById("calendar-select-none-btn");
    selectAllBtn?.addEventListener("click", () => {
        document.querySelectorAll(".calendar-event-checkbox").forEach((box) => {
            box.checked = true;
        });
        updateSelectionUi();
    });
    selectNoneBtn?.addEventListener("click", () => {
        document.querySelectorAll(".calendar-event-checkbox").forEach((box) => {
            box.checked = false;
        });
        updateSelectionUi();
    });
    summarizeBtn?.addEventListener("click", async () => {
        setStatus(saveStatus, "Generating summaries for tracked events…", "info");
        summarizeBtn.disabled = true;
        try {
            const res = await fetch("/api/calendar/summarize", {
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
                const key = e.dedupe_key ? `${e.dedupe_key}: ` : "";
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
            const res = await fetch("/api/calendar/track", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ dedupe_keys: keys }),
            });
            const data = (await res.json());
            if (!res.ok) {
                setStatus(saveStatus, data.error || `HTTP ${res.status}`, "error");
                return;
            }
            trackedKeys = new Set(keys);
            clearSummariesBundleCache();
            setStatus(saveStatus, `Tracking ${data.tracked_count ?? keys.length} event(s). Summaries are generating in the background — refresh Threads in a minute.`, "ok");
        }
        catch (err) {
            const msg = err instanceof Error ? err.message : String(err);
            setStatus(saveStatus, msg, "error");
        }
    });
}
