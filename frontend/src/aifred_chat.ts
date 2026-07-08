/**
 * "Ask AIFred" — a persisting chat popup for questions about tracked threads, lanes, and plans.
 *
 * Mounted once at bootstrap (outside #page-root) so it survives client-side navigation between
 * pages. History is kept in localStorage until the user explicitly clears it.
 */

import { escapeHtml, str } from "./shared/utils.js";

type ChatRole = "user" | "assistant";

type ChatTurn = {
  role: ChatRole;
  content: string;
  thinking?: string;
};

const HISTORY_KEY = "aifred_chat_history";
const MAX_HISTORY_TURNS = 40;
const MAX_HISTORY_SENT = 20;

let panelEl: HTMLDivElement | null = null;
let toggleBtn: HTMLButtonElement | null = null;
let messagesEl: HTMLDivElement | null = null;
let formEl: HTMLFormElement | null = null;
let inputEl: HTMLTextAreaElement | null = null;
let sendBtn: HTMLButtonElement | null = null;
let sending = false;

function loadHistory(): ChatTurn[] {
  try {
    const raw = localStorage.getItem(HISTORY_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter((t): t is ChatTurn => t && (t.role === "user" || t.role === "assistant") && typeof t.content === "string")
      .map((t) => ({
        role: t.role,
        content: t.content,
        ...(typeof t.thinking === "string" && t.thinking ? { thinking: t.thinking } : {}),
      }))
      .slice(-MAX_HISTORY_TURNS);
  } catch {
    return [];
  }
}

function saveHistory(history: ChatTurn[]): void {
  try {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(history.slice(-MAX_HISTORY_TURNS)));
  } catch {
    // Storage full/unavailable — history just won't persist across reloads.
  }
}

function renderMessages(history: ChatTurn[]): void {
  if (!messagesEl) return;
  if (!history.length) {
    messagesEl.innerHTML =
      '<p class="aifred-chat-empty">Ask AIFred about the latest thread statuses, who owes a response, or upcoming follow-ups.</p>';
  } else {
    messagesEl.innerHTML = history
      .map((turn) => {
        const thinkingBlock = turn.thinking
          ? `<details class="aifred-chat-thinking"><summary>Thinking</summary><div class="aifred-chat-thinking-body">${escapeHtml(turn.thinking)}</div></details>`
          : "";
        return `<div class="aifred-chat-msg aifred-chat-msg-${turn.role}">${thinkingBlock}<div class="aifred-chat-bubble">${escapeHtml(turn.content)}</div></div>`;
      })
      .join("");
  }
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function setSending(next: boolean): void {
  sending = next;
  if (inputEl) inputEl.disabled = next;
  if (sendBtn) sendBtn.disabled = next;
}

async function handleSubmit(event: SubmitEvent): Promise<void> {
  event.preventDefault();
  if (sending || !inputEl) return;
  const question = inputEl.value.trim();
  if (!question) return;

  const history = loadHistory();
  history.push({ role: "user", content: question });
  saveHistory(history);
  renderMessages(history);
  inputEl.value = "";
  setSending(true);

  try {
    const res = await fetch("/api/aifred/ask", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question,
        chat_history: history.slice(-MAX_HISTORY_SENT),
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data || data.ok === false) {
      throw new Error(str(data && data.error) || `Request failed (${res.status})`);
    }
    const answer = str(data.answer).trim() || "AIFred didn't return an answer — try again.";
    const thinking = str(data.thinking).trim();
    const updated = loadHistory();
    updated.push({ role: "assistant", content: answer, ...(thinking ? { thinking } : {}) });
    saveHistory(updated);
    renderMessages(updated);
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    const updated = loadHistory();
    updated.push({ role: "assistant", content: `Something went wrong: ${message}` });
    saveHistory(updated);
    renderMessages(updated);
  } finally {
    setSending(false);
    inputEl?.focus();
  }
}

function togglePanel(open?: boolean): void {
  if (!panelEl || !toggleBtn) return;
  const next = open ?? panelEl.hidden;
  panelEl.hidden = !next;
  toggleBtn.setAttribute("aria-expanded", String(next));
  if (next) {
    renderMessages(loadHistory());
    inputEl?.focus();
  }
}

function clearHistory(): void {
  saveHistory([]);
  renderMessages([]);
}

export function mountAifredChat(): void {
  if (document.getElementById("aifred-chat-panel")) return;

  toggleBtn = document.createElement("button");
  toggleBtn.type = "button";
  toggleBtn.id = "aifred-chat-toggle";
  toggleBtn.className = "aifred-chat-toggle";
  toggleBtn.setAttribute("aria-haspopup", "dialog");
  toggleBtn.setAttribute("aria-controls", "aifred-chat-panel");
  toggleBtn.setAttribute("aria-expanded", "false");
  toggleBtn.innerHTML = '<span class="aifred-chat-toggle-label">Ask AIFred</span>';
  toggleBtn.addEventListener("click", () => togglePanel());

  const panel = document.createElement("div");
  panel.id = "aifred-chat-panel";
  panel.className = "aifred-chat-panel";
  panel.hidden = true;
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-label", "Ask AIFred");
  panel.innerHTML = `
    <div class="aifred-chat-header">
      <strong>Ask AIFred</strong>
      <div class="aifred-chat-header-actions">
        <button type="button" class="aifred-chat-clear">Clear chat</button>
        <button type="button" class="aifred-chat-close" aria-label="Close">&times;</button>
      </div>
    </div>
    <div class="aifred-chat-messages" id="aifred-chat-messages"></div>
    <form class="aifred-chat-form" id="aifred-chat-form">
      <textarea
        class="aifred-chat-input"
        id="aifred-chat-input"
        rows="1"
        placeholder="What needs my response this week?"
      ></textarea>
      <button type="submit" class="aifred-chat-send" id="aifred-chat-send">Send</button>
    </form>
  `;

  document.body.appendChild(toggleBtn);
  document.body.appendChild(panel);

  panelEl = panel;
  messagesEl = panel.querySelector<HTMLDivElement>("#aifred-chat-messages");
  formEl = panel.querySelector<HTMLFormElement>("#aifred-chat-form");
  inputEl = panel.querySelector<HTMLTextAreaElement>("#aifred-chat-input");
  sendBtn = panel.querySelector<HTMLButtonElement>("#aifred-chat-send");

  formEl?.addEventListener("submit", (event) => void handleSubmit(event));
  inputEl?.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      formEl?.requestSubmit();
    }
  });
  panel.querySelector(".aifred-chat-close")?.addEventListener("click", () => togglePanel(false));
  panel.querySelector(".aifred-chat-clear")?.addEventListener("click", () => clearHistory());

  renderMessages(loadHistory());
}
