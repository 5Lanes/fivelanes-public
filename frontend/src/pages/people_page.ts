import {
  partitionThreadsBySnooze,
  listSection,
  threadEmailSubject,
} from "../shared/thread_domain.js";
import {
  applyPersonCreated,
  applyPersonSummary,
  applyPersonThreadMembership,
  getCurrentData,
  getCurrentSourceLabel,
  getCurrentThreads,
  getPeople,
  getPersonSummary,
  getPersonThreadIds,
  setBundle,
} from "../shared/summaries_store.js";
import { escapeHtml, str } from "../shared/utils.js";
import type { LooseObj, PersonSummaryView, PersonView } from "../shared/types.js";

const PAGE_HTML = `
<div class="view-people">
  <div class="people-toolbar">
    <button type="button" class="create-person-btn" id="create-person-btn">Create person</button>
  </div>
  <form class="create-person-form" id="create-person-form" hidden>
    <input type="text" name="person-name" id="person-name-input" placeholder="Person name" required />
    <button type="submit">Create</button>
    <button type="button" class="create-person-cancel" id="create-person-cancel">Cancel</button>
  </form>
  <div id="people-list" class="people-list"></div>
</div>`;

let interactionsBound = false;
let assignPersonId: number | null = null;

function trackingThreads() {
  const { active, snoozed } = partitionThreadsBySnooze(getCurrentThreads());
  return [...active, ...snoozed];
}

function threadPickerHtml(personId: number, selectedIds: Set<string>): string {
  const threads = trackingThreads();
  if (!threads.length) {
    return `<p class="person-thread-picker-empty">No active or snoozed threads to assign.</p>`;
  }
  const rows = threads
    .map((thread) => {
      const checked = selectedIds.has(thread.id) ? " checked" : "";
      const subject = threadEmailSubject(thread);
      return `<label class="person-thread-option">
        <input type="checkbox" class="person-thread-checkbox" data-person-id="${personId}" data-thread-id="${escapeHtml(thread.id)}"${checked} />
        <span>${escapeHtml(subject)}</span>
      </label>`;
    })
    .join("");
  return `<div class="person-thread-picker">
    <p class="person-thread-picker-title">Assign threads by email subject</p>
    <div class="person-thread-options">${rows}</div>
  </div>`;
}

function personSummaryHtml(summary: PersonSummaryView | null): string {
  if (!summary) {
    return `<p class="person-summary-empty">No summary yet. Assign threads and refresh to generate one.</p>`;
  }
  const tone = summary.tone_overview.trim();
  const updated = summary.updated_at.trim();
  const metaParts: string[] = [];
  if (tone) metaParts.push(escapeHtml(tone));
  if (updated) metaParts.push(`Updated ${escapeHtml(updated.slice(0, 10))}`);
  const meta = metaParts.length
    ? `<p class="person-summary-meta">${metaParts.join(" · ")}</p>`
    : "";
  const body = summary.summary.trim()
    ? `<p class="person-summary-text">${escapeHtml(summary.summary)}</p>`
    : "";
  return `<div class="person-summary">
    ${meta}
    ${body}
    ${listSection("Highlights", summary.highlights)}
    ${listSection("Current priorities", summary.current_priorities)}
    ${listSection("Waiting on others", summary.waiting_on_others)}
  </div>`;
}

function personCardHtml(
  person: PersonView,
  threadIds: string[],
  summary: PersonSummaryView | null,
  expanded: boolean,
): string {
  const selected = new Set(threadIds);
  const threadLabels = threadIds
    .map((tid) => {
      const thread = getCurrentThreads().find((t) => t.id === tid);
      if (!thread) return "";
      return `<li>${escapeHtml(threadEmailSubject(thread))}</li>`;
    })
    .filter(Boolean)
    .join("");
  const threadsBlock = threadLabels
    ? `<ul class="person-assigned-threads">${threadLabels}</ul>`
    : `<p class="person-empty-threads">No threads yet.</p>`;
  const picker = expanded ? threadPickerHtml(person.id, selected) : "";
  return `<article class="user-person-card" data-person-id="${person.id}">
    <header class="user-person-header">
      <h2>${escapeHtml(person.name)}</h2>
      <span class="person-count-pill">${threadIds.length} thread${threadIds.length === 1 ? "" : "s"}</span>
    </header>
    ${personSummaryHtml(summary)}
    ${threadsBlock}
    ${picker}
    <div class="user-person-actions">
      <button type="button" class="person-refresh-summary-btn" data-person-id="${person.id}"${threadIds.length ? "" : " disabled"}>
        Refresh summary
      </button>
      <button type="button" class="person-edit-threads-btn" data-person-id="${person.id}">
        ${expanded ? "Done" : threadIds.length ? "Edit threads" : "Assign threads"}
      </button>
    </div>
  </article>`;
}

function renderPeopleList(): void {
  const listEl = document.getElementById("people-list");
  const data = getCurrentData();
  if (!listEl || !data) return;

  const people = getPeople(data);
  if (!people.length) {
    listEl.innerHTML = `<p class="people-empty">No people yet. Create one to assign threads.</p>`;
    return;
  }

  listEl.innerHTML = people
    .map((person) => {
      const threadIds = getPersonThreadIds(data, person.id);
      const summary = getPersonSummary(data, person.id);
      const expanded = assignPersonId === person.id;
      return personCardHtml(person, threadIds, summary, expanded);
    })
    .join("");
}

async function persistPersonCreate(name: string): Promise<PersonView> {
  const res = await fetch("/api/people/create", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  const body = (await res.json().catch(() => ({}))) as LooseObj;
  if (!res.ok) throw new Error(str(body.error) || `Create person failed (${res.status})`);
  const personRaw = body.person as LooseObj;
  return {
    id: Number(personRaw.id) || 0,
    name: str(personRaw.name) || name,
    created_at: str(personRaw.created_at),
    updated_at: str(personRaw.updated_at),
  };
}

async function persistPersonThread(personId: number, threadId: string, assigned: boolean): Promise<void> {
  const path = assigned ? "/api/people/add-thread" : "/api/people/remove-thread";
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ person_id: personId, thread_id: threadId }),
  });
  const body = (await res.json().catch(() => ({}))) as LooseObj;
  if (!res.ok) throw new Error(str(body.error) || `Person update failed (${res.status})`);
}

async function persistPersonSummary(personId: number, force = false): Promise<LooseObj> {
  const res = await fetch("/api/people/summary", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ person_id: personId, force }),
  });
  const body = (await res.json().catch(() => ({}))) as LooseObj;
  if (!res.ok) throw new Error(str(body.error) || `Person summary failed (${res.status})`);
  return body;
}

function reloadFromStore(): void {
  const data = getCurrentData();
  if (data) {
    setBundle(data, getCurrentSourceLabel());
    void renderPeoplePage();
  }
}

export function mountPeoplePage(root: HTMLElement): void {
  root.innerHTML = PAGE_HTML;
}

export async function renderPeoplePage(): Promise<void> {
  const data = getCurrentData();
  if (!data) return;

  renderPeopleList();
}

export function bindPeopleInteractions(): void {
  if (interactionsBound) return;
  interactionsBound = true;

  document.addEventListener("click", (ev) => {
    const target = ev.target as HTMLElement | null;
    if (!target) return;
    if (!document.getElementById("page-root")?.contains(target)) return;

    if (target.id === "create-person-btn") {
      const form = document.getElementById("create-person-form");
      const btn = document.getElementById("create-person-btn");
      form?.removeAttribute("hidden");
      btn?.setAttribute("hidden", "");
      document.getElementById("person-name-input")?.focus();
      return;
    }

    if (target.id === "create-person-cancel") {
      const form = document.getElementById("create-person-form") as HTMLFormElement | null;
      const btn = document.getElementById("create-person-btn");
      form?.reset();
      form?.setAttribute("hidden", "");
      btn?.removeAttribute("hidden");
      return;
    }

    const editBtn = target.closest(".person-edit-threads-btn") as HTMLButtonElement | null;
    if (editBtn) {
      const personId = Number(editBtn.dataset.personId) || 0;
      assignPersonId = assignPersonId === personId ? null : personId;
      renderPeopleList();
      return;
    }

    const refreshBtn = target.closest(".person-refresh-summary-btn") as HTMLButtonElement | null;
    if (refreshBtn && !refreshBtn.disabled) {
      const personId = Number(refreshBtn.dataset.personId) || 0;
      if (!personId) return;
      refreshBtn.disabled = true;
      refreshBtn.textContent = "Refreshing…";
      void (async () => {
        try {
          const body = await persistPersonSummary(personId, true);
          applyPersonSummary(personId, body);
          reloadFromStore();
        } catch (err) {
          console.error(err);
          refreshBtn.disabled = false;
          refreshBtn.textContent = "Refresh summary";
        }
      })();
      return;
    }
  });

  document.addEventListener("submit", (ev) => {
    const form = (ev.target as HTMLElement | null)?.closest("#create-person-form");
    if (!form) return;
    ev.preventDefault();
    void (async () => {
      const input = document.getElementById("person-name-input") as HTMLInputElement | null;
      const name = input?.value.trim() ?? "";
      if (!name) return;
      try {
        const person = await persistPersonCreate(name);
        applyPersonCreated(person);
        assignPersonId = person.id;
        form.setAttribute("hidden", "");
        document.getElementById("create-person-btn")?.removeAttribute("hidden");
        (form as HTMLFormElement).reset();
        reloadFromStore();
      } catch (err) {
        console.error(err);
      }
    })();
  });

  document.addEventListener("change", (ev) => {
    const checkbox = (ev.target as HTMLElement | null)?.closest(
      ".person-thread-checkbox",
    ) as HTMLInputElement | null;
    if (!checkbox) return;
    const personId = Number(checkbox.dataset.personId) || 0;
    const threadId = str(checkbox.dataset.threadId);
    if (!personId || !threadId) return;
    void (async () => {
      const assigned = checkbox.checked;
      applyPersonThreadMembership(personId, threadId, assigned);
      try {
        await persistPersonThread(personId, threadId, assigned);
        reloadFromStore();
      } catch (err) {
        applyPersonThreadMembership(personId, threadId, !assigned);
        checkbox.checked = !assigned;
        console.error(err);
      }
    })();
  });
}
