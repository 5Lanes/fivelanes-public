import { clearSummariesBundleCache } from "../shared/summaries_store.js";
import { escapeHtml } from "../shared/utils.js";

type CatalogRow = {
  id: string;
  conversation_key: string;
  label: string;
  service?: string;
  message_count?: number;
  last_message_at?: string;
};

const PAGE_HTML = `
<div class="view-texts-setup view-linkedin-setup">
  <header class="texts-setup-header">
    <h2>LinkedIn threads</h2>
    <p class="texts-setup-lead">
      Conversations are loaded from your data directory's <code>linkedin-messages/messages.csv</code> file (LinkedIn data export format).
      Select which threads to track; tracked threads appear on <a href="/threads">Threads</a>.
    </p>
  </header>

  <section class="texts-setup-card" aria-labelledby="linkedin-select-heading">
    <h3 id="linkedin-select-heading">Choose threads to track</h3>
    <p class="texts-setup-hint" id="linkedin-dir-hint"></p>
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
let catalogRows: CatalogRow[] = [];
let trackedKeys = new Set<string>();

function setStatus(el: HTMLElement | null, message: string, kind: "ok" | "error" | "info"): void {
  if (!el) return;
  el.hidden = !message;
  el.textContent = message;
  el.classList.remove("texts-status-ok", "texts-status-error", "texts-status-info");
  el.classList.add(
    kind === "ok" ? "texts-status-ok" : kind === "error" ? "texts-status-error" : "texts-status-info",
  );
}

function selectedKeys(): string[] {
  return Array.from(document.querySelectorAll<HTMLInputElement>(".linkedin-conversation-checkbox:checked"))
    .map((el) => el.dataset.conversationKey || "")
    .filter(Boolean);
}

function updateSelectionUi(): void {
  const countEl = document.getElementById("linkedin-selection-count");
  const selected = selectedKeys();
  if (countEl) {
    countEl.textContent =
      selected.length === 0 ? "None selected" : `${selected.length} selected for tracking`;
  }
}

function optionLabel(row: CatalogRow): string {
  const base = row.label || row.conversation_key;
  const count =
    row.message_count === 1
      ? "1 message"
      : row.message_count != null
        ? `${row.message_count} messages`
        : "";
  const when = row.last_message_at ? ` · ${row.last_message_at}` : "";
  const svc = row.service ? ` · ${row.service}` : "";
  return count ? `${base}${svc} (${count}${when})` : `${base}${svc}`;
}

function renderConversationList(): void {
  const listEl = document.getElementById("linkedin-conversation-list");
  if (!listEl) return;

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

async function fetchCatalog(): Promise<void> {
  const res = await fetch("/api/linkedin/catalog");
  const data = (await res.json()) as {
    ok?: boolean;
    error?: string;
    linkedin_messages_dir?: string;
    catalog?: CatalogRow[];
    tracked?: string[];
  };
  if (!res.ok) {
    throw new Error(data.error || `HTTP ${res.status}`);
  }
  const hint = document.getElementById("linkedin-dir-hint");
  if (hint && data.linkedin_messages_dir) {
    hint.textContent = `Reading from ${data.linkedin_messages_dir}`;
  }
  catalogRows = Array.isArray(data.catalog) ? data.catalog : [];
  trackedKeys = new Set(Array.isArray(data.tracked) ? data.tracked : []);
}

export function mountLinkedinSetupPage(root: HTMLElement): void {
  root.innerHTML = PAGE_HTML;
}

export async function renderLinkedinSetupPage(): Promise<void> {
  const saveStatus = document.getElementById("linkedin-save-status");
  try {
    await fetchCatalog();
    renderConversationList();
    setStatus(saveStatus, "", "info");
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    setStatus(saveStatus, `Could not load conversations: ${msg}`, "error");
  }
}

export function bindLinkedinSetupInteractions(): void {
  if (interactionsBound) return;
  interactionsBound = true;

  const saveBtn = document.getElementById("linkedin-save-tracked-btn");
  const summarizeBtn = document.getElementById("linkedin-summarize-btn") as HTMLButtonElement | null;
  const saveStatus = document.getElementById("linkedin-save-status");
  const selectAllBtn = document.getElementById("linkedin-select-all-btn");
  const selectNoneBtn = document.getElementById("linkedin-select-none-btn");

  selectAllBtn?.addEventListener("click", () => {
    document.querySelectorAll<HTMLInputElement>(".linkedin-conversation-checkbox").forEach((box) => {
      box.checked = true;
    });
    updateSelectionUi();
  });

  selectNoneBtn?.addEventListener("click", () => {
    document.querySelectorAll<HTMLInputElement>(".linkedin-conversation-checkbox").forEach((box) => {
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
      const data = (await res.json()) as {
        ok?: boolean;
        error?: string;
        summarized?: number;
        skipped?: number;
        errors?: Array<{ conversation_key?: string; error?: string }>;
      };
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
      setStatus(
        saveStatus,
        `Done — ${data.summarized ?? 0} summarized, ${data.skipped ?? 0} skipped${
          errCount ? `, ${errCount} error(s)` : ""
        }${errDetail ? ` — ${errDetail}` : ""}. Refresh Threads.`,
        errCount && !data.summarized ? "error" : "ok",
      );
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setStatus(saveStatus, msg, "error");
    } finally {
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
      const data = (await res.json()) as { ok?: boolean; error?: string; tracked_count?: number };
      if (!res.ok) {
        setStatus(saveStatus, data.error || `HTTP ${res.status}`, "error");
        return;
      }
      trackedKeys = new Set(keys);
      clearSummariesBundleCache();
      setStatus(
        saveStatus,
        `Tracking ${data.tracked_count ?? keys.length} thread(s). Summaries are generating in the background — refresh Threads in a minute.`,
        "ok",
      );
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setStatus(saveStatus, msg, "error");
    }
  });
}
