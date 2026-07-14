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
<div class="view-texts-setup">
  <header class="texts-setup-header">
    <h2>Text threads</h2>
    <p class="texts-setup-lead">
      Conversations are loaded from your data directory's <code>conversations/</code> folder (JSON export format).
      Select which threads to track; tracked threads appear on the <a href="/onebox">onebox</a>.
    </p>
  </header>

  <section class="texts-setup-card" aria-labelledby="texts-select-heading">
    <h3 id="texts-select-heading">Choose threads to track</h3>
    <p class="texts-setup-hint" id="texts-dir-hint"></p>
    <div class="texts-conversation-toolbar">
      <button type="button" class="texts-select-all-btn" id="texts-select-all-btn">Select all</button>
      <button type="button" class="texts-select-none-btn" id="texts-select-none-btn">Select none</button>
      <span id="texts-selection-count" class="texts-selection-count"></span>
    </div>
    <div id="texts-conversation-list" class="texts-conversation-list"></div>
    <div class="texts-setup-actions">
      <button type="button" class="texts-save-tracked-btn" id="texts-save-tracked-btn">Save tracking</button>
      <button type="button" class="texts-summarize-btn" id="texts-summarize-btn">Generate summaries</button>
    </div>
    <p id="texts-save-status" class="texts-status" hidden></p>
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
  return Array.from(document.querySelectorAll<HTMLInputElement>(".texts-conversation-checkbox:checked"))
    .map((el) => el.dataset.conversationKey || "")
    .filter(Boolean);
}

function updateSelectionUi(): void {
  const countEl = document.getElementById("texts-selection-count");
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
  const listEl = document.getElementById("texts-conversation-list");
  if (!listEl) return;

  if (!catalogRows.length) {
    listEl.innerHTML =
      '<p class="texts-conversation-empty">No <code>*.json</code> files found under <code>conversations/</code>.</p>';
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
          class="texts-conversation-checkbox"
          data-conversation-key="${escapeHtml(key)}"
          ${checked ? "checked" : ""}
        />
        <span>${escapeHtml(optionLabel(row))}</span>
      </label>`;
    })
    .join("");

  listEl.querySelectorAll(".texts-conversation-checkbox").forEach((box) => {
    box.addEventListener("change", updateSelectionUi);
  });
  updateSelectionUi();
}

async function fetchCatalog(): Promise<void> {
  const res = await fetch("/api/texts/catalog");
  const data = (await res.json()) as {
    ok?: boolean;
    error?: string;
    conversations_dir?: string;
    catalog?: CatalogRow[];
    tracked?: string[];
  };
  if (!res.ok) {
    throw new Error(data.error || `HTTP ${res.status}`);
  }
  const hint = document.getElementById("texts-dir-hint");
  if (hint && data.conversations_dir) {
    hint.textContent = `Reading from ${data.conversations_dir}`;
  }
  catalogRows = Array.isArray(data.catalog) ? data.catalog : [];
  trackedKeys = new Set(Array.isArray(data.tracked) ? data.tracked : []);
}

export function mountTextsSetupPage(root: HTMLElement): void {
  root.innerHTML = PAGE_HTML;
}

export async function renderTextsSetupPage(): Promise<void> {
  const saveStatus = document.getElementById("texts-save-status");
  try {
    await fetchCatalog();
    renderConversationList();
    setStatus(saveStatus, "", "info");
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    setStatus(saveStatus, `Could not load conversations: ${msg}`, "error");
  }
}

export function bindTextsSetupInteractions(): void {
  if (interactionsBound) return;
  interactionsBound = true;

  const saveBtn = document.getElementById("texts-save-tracked-btn");
  const summarizeBtn = document.getElementById("texts-summarize-btn") as HTMLButtonElement | null;
  const saveStatus = document.getElementById("texts-save-status");
  const selectAllBtn = document.getElementById("texts-select-all-btn");
  const selectNoneBtn = document.getElementById("texts-select-none-btn");

  selectAllBtn?.addEventListener("click", () => {
    document.querySelectorAll<HTMLInputElement>(".texts-conversation-checkbox").forEach((box) => {
      box.checked = true;
    });
    updateSelectionUi();
  });

  selectNoneBtn?.addEventListener("click", () => {
    document.querySelectorAll<HTMLInputElement>(".texts-conversation-checkbox").forEach((box) => {
      box.checked = false;
    });
    updateSelectionUi();
  });

  summarizeBtn?.addEventListener("click", async () => {
    setStatus(saveStatus, "Generating summaries for tracked threads…", "info");
    summarizeBtn.disabled = true;
    try {
      const res = await fetch("/api/texts/summarize", {
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
      const res = await fetch("/api/texts/track", {
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
