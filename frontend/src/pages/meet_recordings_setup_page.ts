import { clearSummariesBundleCache } from "../shared/summaries_store.js";
import { escapeHtml } from "../shared/utils.js";

type CatalogRow = {
  id: string;
  document_key: string;
  name?: string;
  label: string;
  doc_date?: string;
  created_time?: string;
  modified_time?: string;
};

const PAGE_HTML = `
<div class="view-texts-setup view-meet-recordings-setup">
  <header class="texts-setup-header">
    <h2>Meet recordings</h2>
    <p class="texts-setup-lead">
      Pull Google Docs for Meet / Gemini notes (names and dates), then choose which to import.
      Only the conversation-summary tab is imported — not the full transcript.
      Tracked notes appear on <a href="/threads">Threads</a>.
    </p>
  </header>

  <section class="texts-setup-card" aria-labelledby="meet-pull-heading">
    <h3 id="meet-pull-heading">Pull from Google Drive</h3>
    <p class="texts-setup-hint">Uses connected Google OAuth accounts (Drive + Docs readonly scopes).</p>
    <div class="texts-setup-actions">
      <button type="button" class="texts-save-tracked-btn" id="meet-pull-btn">Pull doc names</button>
    </div>
    <p id="meet-pull-status" class="texts-status" hidden></p>
  </section>

  <section class="texts-setup-card" aria-labelledby="meet-select-heading">
    <h3 id="meet-select-heading">Choose recordings to import</h3>
    <p class="texts-setup-hint" id="meet-dir-hint"></p>
    <div class="texts-conversation-toolbar">
      <button type="button" class="texts-select-all-btn" id="meet-select-all-btn">Select all</button>
      <button type="button" class="texts-select-none-btn" id="meet-select-none-btn">Select none</button>
      <span id="meet-selection-count" class="texts-selection-count"></span>
    </div>
    <div id="meet-document-list" class="texts-conversation-list"></div>
    <div class="texts-setup-actions">
      <button type="button" class="texts-save-tracked-btn" id="meet-save-tracked-btn">Save tracking</button>
      <button type="button" class="texts-summarize-btn" id="meet-summarize-btn">Generate summaries</button>
    </div>
    <p id="meet-save-status" class="texts-status" hidden></p>
  </section>
</div>`;

let interactionsBound = false;
let catalogRows: CatalogRow[] = [];
let trackedKeys = new Set<string>();

function setStatus(
  el: HTMLElement | null,
  message: string,
  kind: "ok" | "error" | "info",
): void {
  if (!el) return;
  el.hidden = !message;
  el.textContent = message;
  el.classList.remove("texts-status-ok", "texts-status-error", "texts-status-info");
  el.classList.add(
    kind === "ok" ? "texts-status-ok" : kind === "error" ? "texts-status-error" : "texts-status-info",
  );
}

function selectedKeys(): string[] {
  return Array.from(document.querySelectorAll<HTMLInputElement>(".meet-document-checkbox:checked"))
    .map((el) => el.dataset.documentKey || "")
    .filter(Boolean);
}

function updateSelectionUi(): void {
  const countEl = document.getElementById("meet-selection-count");
  const selected = selectedKeys();
  if (countEl) {
    countEl.textContent =
      selected.length === 0 ? "None selected" : `${selected.length} selected for import`;
  }
}

function formatDocDate(raw: string | undefined): string {
  const value = (raw || "").trim();
  if (!value) return "";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  return d.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function optionLabel(row: CatalogRow): string {
  const base = row.label || row.name || row.document_key;
  const when = formatDocDate(row.doc_date || row.created_time || row.modified_time);
  return when ? `${base} · ${when}` : base;
}

function renderDocumentList(): void {
  const listEl = document.getElementById("meet-document-list");
  if (!listEl) return;

  if (!catalogRows.length) {
    listEl.innerHTML =
      '<p class="texts-conversation-empty">No Meet recording docs yet. Click <strong>Pull doc names</strong> above.</p>';
    updateSelectionUi();
    return;
  }

  listEl.innerHTML = catalogRows
    .map((row) => {
      const key = row.document_key || row.id;
      const checked = trackedKeys.has(key);
      return `<label class="texts-conversation-option">
        <input
          type="checkbox"
          class="meet-document-checkbox"
          data-document-key="${escapeHtml(key)}"
          ${checked ? "checked" : ""}
        />
        <span>${escapeHtml(optionLabel(row))}</span>
      </label>`;
    })
    .join("");

  listEl.querySelectorAll(".meet-document-checkbox").forEach((box) => {
    box.addEventListener("change", updateSelectionUi);
  });
  updateSelectionUi();
}

async function fetchCatalog(): Promise<void> {
  const res = await fetch("/api/meet-recordings/catalog");
  const data = (await res.json()) as {
    ok?: boolean;
    error?: string;
    meet_recordings_dir?: string;
    catalog?: CatalogRow[];
    tracked?: string[];
  };
  if (!res.ok) {
    throw new Error(data.error || `HTTP ${res.status}`);
  }
  const hint = document.getElementById("meet-dir-hint");
  if (hint && data.meet_recordings_dir) {
    hint.textContent = `Catalog in ${data.meet_recordings_dir}`;
  }
  catalogRows = Array.isArray(data.catalog) ? data.catalog : [];
  trackedKeys = new Set(Array.isArray(data.tracked) ? data.tracked : []);
}

export function mountMeetRecordingsSetupPage(root: HTMLElement): void {
  root.innerHTML = PAGE_HTML;
}

export async function renderMeetRecordingsSetupPage(): Promise<void> {
  const saveStatus = document.getElementById("meet-save-status");
  try {
    await fetchCatalog();
    renderDocumentList();
    setStatus(saveStatus, "", "info");
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    setStatus(saveStatus, `Could not load catalog: ${msg}`, "error");
  }
}

export function bindMeetRecordingsSetupInteractions(): void {
  if (interactionsBound) return;
  interactionsBound = true;

  const pullBtn = document.getElementById("meet-pull-btn") as HTMLButtonElement | null;
  const pullStatus = document.getElementById("meet-pull-status");
  const saveBtn = document.getElementById("meet-save-tracked-btn");
  const summarizeBtn = document.getElementById("meet-summarize-btn") as HTMLButtonElement | null;
  const saveStatus = document.getElementById("meet-save-status");
  const selectAllBtn = document.getElementById("meet-select-all-btn");
  const selectNoneBtn = document.getElementById("meet-select-none-btn");

  pullBtn?.addEventListener("click", async () => {
    setStatus(pullStatus, "Pulling Meet recording doc names from Drive…", "info");
    if (pullBtn) pullBtn.disabled = true;
    try {
      const res = await fetch("/api/meet-recordings/pull", { method: "POST" });
      const data = (await res.json()) as {
        ok?: boolean;
        error?: string;
        doc_count?: number;
      };
      if (!res.ok) {
        setStatus(pullStatus, data.error || `HTTP ${res.status}`, "error");
        return;
      }
      await fetchCatalog();
      renderDocumentList();
      setStatus(
        pullStatus,
        `Found ${data.doc_count ?? 0} Meet recording doc(s). Select which to import below.`,
        "ok",
      );
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setStatus(pullStatus, msg, "error");
    } finally {
      if (pullBtn) pullBtn.disabled = false;
    }
  });

  selectAllBtn?.addEventListener("click", () => {
    document.querySelectorAll<HTMLInputElement>(".meet-document-checkbox").forEach((box) => {
      box.checked = true;
    });
    updateSelectionUi();
  });

  selectNoneBtn?.addEventListener("click", () => {
    document.querySelectorAll<HTMLInputElement>(".meet-document-checkbox").forEach((box) => {
      box.checked = false;
    });
    updateSelectionUi();
  });

  summarizeBtn?.addEventListener("click", async () => {
    setStatus(saveStatus, "Generating summaries for tracked recordings…", "info");
    summarizeBtn.disabled = true;
    try {
      const res = await fetch("/api/meet-recordings/summarize", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ force: true }),
      });
      const data = (await res.json()) as {
        ok?: boolean;
        error?: string;
        summarized?: number;
        skipped?: number;
        errors?: Array<{ document_key?: string; error?: string }>;
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
          const key = e.document_key ? `${e.document_key}: ` : "";
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
    setStatus(saveStatus, "Importing selected summaries…", "info");
    try {
      const res = await fetch("/api/meet-recordings/track", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ document_keys: keys }),
      });
      const data = (await res.json()) as {
        ok?: boolean;
        error?: string;
        tracked_count?: number;
        imported?: number;
        errors?: Array<{ document_key?: string; error?: string }>;
      };
      if (!res.ok) {
        setStatus(saveStatus, data.error || `HTTP ${res.status}`, "error");
        return;
      }
      trackedKeys = new Set(keys);
      await fetchCatalog();
      renderDocumentList();
      clearSummariesBundleCache();
      const errs = Array.isArray(data.errors) ? data.errors : [];
      const errDetail = errs
        .map((e) => `${e.document_key || "?"}: ${e.error || "failed"}`)
        .join(" · ");
      setStatus(
        saveStatus,
        `Tracking ${data.tracked_count ?? keys.length} recording(s)` +
          (data.imported ? ` (imported ${data.imported} summary tab(s))` : "") +
          `. Summaries are generating in the background — refresh Threads in a minute.` +
          (errDetail ? ` Errors: ${errDetail}` : ""),
        errs.length && !(data.tracked_count ?? 0) ? "error" : "ok",
      );
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setStatus(saveStatus, msg, "error");
    }
  });
}
