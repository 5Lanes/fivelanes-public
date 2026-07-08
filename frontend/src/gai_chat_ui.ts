/**
 * GAI chat modal: ask questions about inbox data via POST /api/gai/chat.
 */

import type { LooseObj } from "./shared/types.js";
import { escapeHtml, str } from "./shared/utils.js";

export type ChatTurn = {
  role: "user" | "assistant";
  content: string;
  thinking?: string;
};

type SessionContext = {
  last_person?: string;
};

type StreamEvent = LooseObj & {
  type?: string;
  message?: string;
  stage?: string;
  kind?: string;
  text?: string;
  sql?: string;
  reasoning?: string;
  answer?: string;
  thinking?: string;
  last_person?: string;
  error?: string;
};

type LiveBubble = {
  root: HTMLElement;
  progressEl: HTMLElement;
  streamEl: HTMLElement;
  streamLabelEl: HTMLElement;
  contentEl: HTMLElement;
  thinkingWrapEl: HTMLElement;
  thinkingBodyEl: HTMLElement;
  answerText: string;
  thinkingText: string;
  streamText: string;
  streamStage: string;
};

let dialogEl: HTMLDialogElement | null = null;
let history: ChatTurn[] = [];
let sessionContext: SessionContext = {};
let inflight: Promise<void> | null = null;
let generation = 0;

function ensureGaiChatDialog(): HTMLDialogElement {
  if (dialogEl) return dialogEl;

  const dialog = document.createElement("dialog");
  dialog.id = "gai-chat-dialog";
  dialog.className = "gai-chat-dialog";
  dialog.innerHTML = `
    <div class="gai-chat-dialog-inner">
      <header class="gai-chat-dialog-head">
        <div>
          <h2>Ask Alfred</h2>
        </div>
        <div class="gai-chat-head-actions">
          <button type="button" class="gai-chat-reset" aria-label="Reset conversation" title="Reset conversation">Reset</button>
          <button type="button" class="gai-chat-close" aria-label="Close">×</button>
        </div>
      </header>
      <div id="gai-chat-messages" class="gai-chat-messages" role="log" aria-live="polite"></div>
      <footer class="gai-chat-dialog-foot">
        <form id="gai-chat-form" class="gai-chat-form">
          <textarea
            id="gai-chat-input"
            class="gai-chat-input"
            rows="2"
            placeholder="Ask about your lanes, tracks, threads, etc."
            aria-label="Message"
          ></textarea>
          <button type="submit" id="gai-chat-send" class="gai-chat-send">Send</button>
        </form>
      </footer>
    </div>
  `;
  document.body.appendChild(dialog);

  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
  dialog.querySelector(".gai-chat-close")?.addEventListener("click", () => dialog.close());
  dialog.querySelector(".gai-chat-reset")?.addEventListener("click", () => resetGaiChat());
  dialog.addEventListener("close", () => {
    const input = dialog.querySelector("#gai-chat-input") as HTMLTextAreaElement | null;
    if (input) input.value = "";
  });

  dialogEl = dialog;
  return dialog;
}

function messagesEl(): HTMLElement {
  const dialog = ensureGaiChatDialog();
  return dialog.querySelector("#gai-chat-messages") as HTMLElement;
}

function renderMessages(): void {
  const container = messagesEl();
  if (!history.length) {
    container.innerHTML = `<p class="gai-chat-empty"></p>`;
    return;
  }
  container.innerHTML = history
    .map((turn) => {
      const role = turn.role === "user" ? "You" : "Alfred";
      const cls = turn.role === "user" ? "gai-chat-bubble-user" : "gai-chat-bubble-assistant";
      const reasoningBlock = turn.thinking
        ? `<details class="gai-chat-reasoning"><summary>Thought process</summary><div class="gai-chat-reasoning-body">${formatMessageHtml(turn.thinking)}</div></details>`
        : "";
      return `<div class="gai-chat-turn ${cls}"><div class="gai-chat-role">${escapeHtml(role)}</div>${reasoningBlock}<div class="gai-chat-content">${formatMessageHtml(turn.content)}</div></div>`;
    })
    .join("");
  container.scrollTop = container.scrollHeight;
}

function formatMessageHtml(text: string): string {
  return escapeHtml(text).replace(/\n/g, "<br>");
}

function setLoading(active: boolean): void {
  const dialog = ensureGaiChatDialog();
  const sendBtn = dialog.querySelector("#gai-chat-send") as HTMLButtonElement | null;
  const input = dialog.querySelector("#gai-chat-input") as HTMLTextAreaElement | null;
  if (sendBtn) sendBtn.disabled = active;
  if (input) input.disabled = active;
}

function clearLiveBubble(): void {
  messagesEl().querySelector(".gai-chat-live")?.remove();
}

function stageLabel(stage: string): string {
  if (stage === "sql") return "Llama (SQL)";
  if (stage === "answer") return "Llama (answer)";
  return "Llama";
}

function createLiveBubble(): LiveBubble {
  clearLiveBubble();
  const container = messagesEl();
  container.insertAdjacentHTML(
    "beforeend",
    `<div class="gai-chat-turn gai-chat-bubble-assistant gai-chat-live">
      <div class="gai-chat-role">Alfred</div>
      <div class="gai-chat-progress" aria-live="polite"></div>
      <div class="gai-chat-stream-wrap" hidden>
        <div class="gai-chat-stream-label"></div>
        <pre class="gai-chat-stream"></pre>
      </div>
      <details class="gai-chat-reasoning" hidden>
        <summary>Thought process</summary>
        <div class="gai-chat-reasoning-body"></div>
      </details>
      <div class="gai-chat-content gai-chat-thinking">Thinking…</div>
    </div>`,
  );
  const root = container.querySelector(".gai-chat-live") as HTMLElement;
  const bubble: LiveBubble = {
    root,
    progressEl: root.querySelector(".gai-chat-progress") as HTMLElement,
    streamEl: root.querySelector(".gai-chat-stream") as HTMLElement,
    streamLabelEl: root.querySelector(".gai-chat-stream-label") as HTMLElement,
    contentEl: root.querySelector(".gai-chat-content") as HTMLElement,
    thinkingWrapEl: root.querySelector(".gai-chat-reasoning") as HTMLElement,
    thinkingBodyEl: root.querySelector(".gai-chat-reasoning-body") as HTMLElement,
    answerText: "",
    thinkingText: "",
    streamText: "",
    streamStage: "",
  };
  appendProgressLine(bubble, "Starting…");
  container.scrollTop = container.scrollHeight;
  return bubble;
}

function appendProgressLine(bubble: LiveBubble, message: string): void {
  const line = document.createElement("div");
  line.className = "gai-chat-progress-line";
  line.textContent = message;
  bubble.progressEl.appendChild(line);
  messagesEl().scrollTop = messagesEl().scrollHeight;
}

function updateStreamOutput(bubble: LiveBubble): void {
  const wrap = bubble.streamEl.closest(".gai-chat-stream-wrap") as HTMLElement;
  if (bubble.streamText) {
    wrap.hidden = false;
    bubble.streamLabelEl.textContent = stageLabel(bubble.streamStage);
    bubble.streamEl.textContent = bubble.streamText;
  } else {
    wrap.hidden = true;
    bubble.streamEl.textContent = "";
  }
  messagesEl().scrollTop = messagesEl().scrollHeight;
}

function setBubbleAnswer(bubble: LiveBubble, answer: string): void {
  bubble.answerText = answer;
  bubble.contentEl.classList.remove("gai-chat-thinking");
  bubble.contentEl.innerHTML = formatMessageHtml(answer);
  messagesEl().scrollTop = messagesEl().scrollHeight;
}

function updateThinkingOutput(bubble: LiveBubble): void {
  if (bubble.thinkingText) {
    bubble.thinkingWrapEl.hidden = false;
    bubble.thinkingBodyEl.innerHTML = formatMessageHtml(bubble.thinkingText);
  }
  messagesEl().scrollTop = messagesEl().scrollHeight;
}

function handleStreamEvent(bubble: LiveBubble, event: StreamEvent): void {
  const type = str(event.type);
  if (type === "progress") {
    appendProgressLine(bubble, str(event.message));
    return;
  }
  if (type === "stage") {
    bubble.streamStage = str(event.stage);
    bubble.streamText = "";
    updateStreamOutput(bubble);
    return;
  }
  if (type === "token") {
    if (str(event.kind) === "thinking") {
      bubble.thinkingText += str(event.text);
      updateThinkingOutput(bubble);
      return;
    }
    const stage = str(event.stage);
    if (stage && stage !== bubble.streamStage) {
      bubble.streamStage = stage;
      bubble.streamText = "";
    }
    bubble.streamText += str(event.text);
    updateStreamOutput(bubble);
    if (stage === "answer") {
      bubble.answerText += str(event.text);
      bubble.contentEl.classList.remove("gai-chat-thinking");
      bubble.contentEl.innerHTML = formatMessageHtml(bubble.answerText);
      messagesEl().scrollTop = messagesEl().scrollHeight;
    }
    return;
  }
  if (type === "sql") {
    const sql = str(event.sql).trim();
    const reasoning = str(event.reasoning).trim();
    if (reasoning) appendProgressLine(bubble, reasoning);
    if (sql) {
      const block = document.createElement("pre");
      block.className = "gai-chat-sql";
      block.textContent = sql;
      bubble.progressEl.appendChild(block);
      messagesEl().scrollTop = messagesEl().scrollHeight;
    }
    return;
  }
  if (type === "done") {
    const thinking = str(event.thinking).trim();
    if (thinking && !bubble.thinkingText) {
      bubble.thinkingText = thinking;
      updateThinkingOutput(bubble);
    }
    const answer = str(event.answer).trim();
    if (answer) setBubbleAnswer(bubble, answer);
    return;
  }
  if (type === "error") {
    const err = str(event.error).trim() || "Request failed";
    setBubbleAnswer(bubble, `Sorry, I couldn't answer that: ${err}`);
  }
}

function parseStreamLine(line: string, onEvent: (event: StreamEvent) => void): StreamEvent | null {
  if (!line.trim()) return null;
  try {
    const event = JSON.parse(line) as StreamEvent;
    onEvent(event);
    return event;
  } catch {
    return null;
  }
}

function finalizeStreamEvent(event: StreamEvent | null, fallback: StreamEvent): StreamEvent {
  if (!event) return fallback;
  if (event.type === "done" || event.type === "error") return event;
  if (event.ok === true && str(event.answer).trim()) {
    return { type: "done", ...event };
  }
  if (event.ok === false) {
    return { type: "error", ...event };
  }
  return fallback;
}

async function requestGaiChatStream(
  message: string,
  onEvent: (event: StreamEvent) => void,
): Promise<StreamEvent> {
  const res = await fetch("/api/gai/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    body: JSON.stringify({
      message,
      history: history.slice(0, -1),
      session: sessionContext,
      stream: true,
    }),
  });
  if (!res.ok) {
    let errText = `Request failed (${res.status})`;
    try {
      const data = (await res.json()) as LooseObj;
      errText = str(data.error) || errText;
    } catch {
      // ignore parse errors
    }
    throw new Error(errText);
  }

  const contentType = res.headers.get("Content-Type") || "";
  if (contentType.includes("application/json")) {
    const data = (await res.json()) as StreamEvent;
    const event = finalizeStreamEvent(data, { type: "error", ok: false, error: "No response" });
    onEvent(event);
    return event;
  }

  if (!res.body) {
    throw new Error("Streaming response not supported");
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let finalEvent: StreamEvent = { type: "error", ok: false, error: "No response" };

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";
    for (const line of lines) {
      const event = parseStreamLine(line, onEvent);
      finalEvent = finalizeStreamEvent(event, finalEvent);
    }
  }

  if (buffer.trim()) {
    const event = parseStreamLine(buffer, onEvent);
    finalEvent = finalizeStreamEvent(event, finalEvent);
  }

  return finalEvent;
}

async function sendMessage(raw: string): Promise<void> {
  const message = raw.trim();
  if (!message || inflight) return;

  const gen = generation;
  history.push({ role: "user", content: message });
  renderMessages();
  setLoading(true);
  const bubble = createLiveBubble();

  inflight = (async () => {
    try {
      const result = await requestGaiChatStream(message, (event) => {
        if (gen !== generation) return;
        handleStreamEvent(bubble, event);
      });
      if (gen !== generation) return;
      if (result.type === "error" || result.ok === false) {
        throw new Error(str(result.error) || "Request failed");
      }
      const answer = str(result.answer).trim() || bubble.answerText.trim() || "No answer returned.";
      const thinking = str(result.thinking).trim() || bubble.thinkingText.trim();
      history.push({ role: "assistant", content: answer, ...(thinking ? { thinking } : {}) });
      const lastPerson = str(result.last_person).trim();
      if (lastPerson) sessionContext.last_person = lastPerson;
    } catch (err) {
      if (gen !== generation) return;
      const msg = err instanceof Error ? err.message : String(err);
      history.push({ role: "assistant", content: `Sorry, I couldn't answer that: ${msg}` });
    } finally {
      inflight = null;
      if (gen === generation) {
        clearLiveBubble();
        setLoading(false);
        renderMessages();
        const input = ensureGaiChatDialog().querySelector("#gai-chat-input") as HTMLTextAreaElement | null;
        input?.focus();
      }
    }
  })();

  await inflight;
}

function resetGaiChat(): void {
  generation++;
  history = [];
  sessionContext = {};
  inflight = null;
  setLoading(false);
  clearLiveBubble();
  renderMessages();
  const dialog = ensureGaiChatDialog();
  const input = dialog.querySelector("#gai-chat-input") as HTMLTextAreaElement | null;
  if (input) input.value = "";
  input?.focus();
}

export function openGaiChatModal(): void {
  const dialog = ensureGaiChatDialog();
  renderMessages();
  if (!dialog.open) dialog.showModal();
  const input = dialog.querySelector("#gai-chat-input") as HTMLTextAreaElement | null;
  input?.focus();
}

export function mountGaiChatDialog(): void {
  ensureGaiChatDialog();
}

export function bindGaiChatPanel(): void {
  const dialog = ensureGaiChatDialog();
  const form = dialog.querySelector("#gai-chat-form") as HTMLFormElement | null;
  const input = dialog.querySelector("#gai-chat-input") as HTMLTextAreaElement | null;

  form?.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!input) return;
    const value = input.value;
    input.value = "";
    void sendMessage(value);
  });

  input?.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      form?.requestSubmit();
    }
  });

  document.getElementById("gai-chat-btn")?.addEventListener("click", () => {
    openGaiChatModal();
  });
}
