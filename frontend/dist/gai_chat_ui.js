/**
 * GAI chat modal: ask questions about inbox data via POST /api/gai/chat.
 */
import { escapeHtml, str } from "./shared/utils.js";
let dialogEl = null;
let history = [];
let sessionContext = {};
let inflight = null;
let generation = 0;
function ensureGaiChatDialog() {
    if (dialogEl)
        return dialogEl;
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
        if (event.target === dialog)
            dialog.close();
    });
    dialog.querySelector(".gai-chat-close")?.addEventListener("click", () => dialog.close());
    dialog.querySelector(".gai-chat-reset")?.addEventListener("click", () => resetGaiChat());
    dialog.addEventListener("close", () => {
        const input = dialog.querySelector("#gai-chat-input");
        if (input)
            input.value = "";
    });
    dialogEl = dialog;
    return dialog;
}
function messagesEl() {
    const dialog = ensureGaiChatDialog();
    return dialog.querySelector("#gai-chat-messages");
}
function renderMessages() {
    const container = messagesEl();
    if (!history.length) {
        container.innerHTML = `<p class="gai-chat-empty"></p>`;
        return;
    }
    container.innerHTML = history
        .map((turn) => {
        const role = turn.role === "user" ? "You" : "Alfred";
        const cls = turn.role === "user" ? "gai-chat-bubble-user" : "gai-chat-bubble-assistant";
        return `<div class="gai-chat-turn ${cls}"><div class="gai-chat-role">${escapeHtml(role)}</div><div class="gai-chat-content">${formatMessageHtml(turn.content)}</div></div>`;
    })
        .join("");
    container.scrollTop = container.scrollHeight;
}
function formatMessageHtml(text) {
    return escapeHtml(text).replace(/\n/g, "<br>");
}
function setLoading(active) {
    const dialog = ensureGaiChatDialog();
    const sendBtn = dialog.querySelector("#gai-chat-send");
    const input = dialog.querySelector("#gai-chat-input");
    if (sendBtn)
        sendBtn.disabled = active;
    if (input)
        input.disabled = active;
}
function clearLiveBubble() {
    messagesEl().querySelector(".gai-chat-live")?.remove();
}
function stageLabel(stage) {
    if (stage === "sql")
        return "Llama (SQL)";
    if (stage === "answer")
        return "Llama (answer)";
    return "Llama";
}
function createLiveBubble() {
    clearLiveBubble();
    const container = messagesEl();
    container.insertAdjacentHTML("beforeend", `<div class="gai-chat-turn gai-chat-bubble-assistant gai-chat-live">
      <div class="gai-chat-role">Alfred</div>
      <div class="gai-chat-progress" aria-live="polite"></div>
      <div class="gai-chat-stream-wrap" hidden>
        <div class="gai-chat-stream-label"></div>
        <pre class="gai-chat-stream"></pre>
      </div>
      <div class="gai-chat-content gai-chat-thinking">Thinking…</div>
    </div>`);
    const root = container.querySelector(".gai-chat-live");
    const bubble = {
        root,
        progressEl: root.querySelector(".gai-chat-progress"),
        streamEl: root.querySelector(".gai-chat-stream"),
        streamLabelEl: root.querySelector(".gai-chat-stream-label"),
        contentEl: root.querySelector(".gai-chat-content"),
        answerText: "",
        streamText: "",
        streamStage: "",
    };
    appendProgressLine(bubble, "Starting…");
    container.scrollTop = container.scrollHeight;
    return bubble;
}
function appendProgressLine(bubble, message) {
    const line = document.createElement("div");
    line.className = "gai-chat-progress-line";
    line.textContent = message;
    bubble.progressEl.appendChild(line);
    messagesEl().scrollTop = messagesEl().scrollHeight;
}
function updateStreamOutput(bubble) {
    const wrap = bubble.streamEl.closest(".gai-chat-stream-wrap");
    if (bubble.streamText) {
        wrap.hidden = false;
        bubble.streamLabelEl.textContent = stageLabel(bubble.streamStage);
        bubble.streamEl.textContent = bubble.streamText;
    }
    else {
        wrap.hidden = true;
        bubble.streamEl.textContent = "";
    }
    messagesEl().scrollTop = messagesEl().scrollHeight;
}
function setBubbleAnswer(bubble, answer) {
    bubble.answerText = answer;
    bubble.contentEl.classList.remove("gai-chat-thinking");
    bubble.contentEl.innerHTML = formatMessageHtml(answer);
    messagesEl().scrollTop = messagesEl().scrollHeight;
}
function handleStreamEvent(bubble, event) {
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
        if (reasoning)
            appendProgressLine(bubble, reasoning);
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
        const answer = str(event.answer).trim();
        if (answer)
            setBubbleAnswer(bubble, answer);
        return;
    }
    if (type === "error") {
        const err = str(event.error).trim() || "Request failed";
        setBubbleAnswer(bubble, `Sorry, I couldn't answer that: ${err}`);
    }
}
function parseStreamLine(line, onEvent) {
    if (!line.trim())
        return null;
    try {
        const event = JSON.parse(line);
        onEvent(event);
        return event;
    }
    catch {
        return null;
    }
}
function finalizeStreamEvent(event, fallback) {
    if (!event)
        return fallback;
    if (event.type === "done" || event.type === "error")
        return event;
    if (event.ok === true && str(event.answer).trim()) {
        return { type: "done", ...event };
    }
    if (event.ok === false) {
        return { type: "error", ...event };
    }
    return fallback;
}
async function requestGaiChatStream(message, onEvent) {
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
            const data = (await res.json());
            errText = str(data.error) || errText;
        }
        catch {
            // ignore parse errors
        }
        throw new Error(errText);
    }
    const contentType = res.headers.get("Content-Type") || "";
    if (contentType.includes("application/json")) {
        const data = (await res.json());
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
    let finalEvent = { type: "error", ok: false, error: "No response" };
    while (true) {
        const { done, value } = await reader.read();
        if (done)
            break;
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
async function sendMessage(raw) {
    const message = raw.trim();
    if (!message || inflight)
        return;
    const gen = generation;
    history.push({ role: "user", content: message });
    renderMessages();
    setLoading(true);
    const bubble = createLiveBubble();
    inflight = (async () => {
        try {
            const result = await requestGaiChatStream(message, (event) => {
                if (gen !== generation)
                    return;
                handleStreamEvent(bubble, event);
            });
            if (gen !== generation)
                return;
            if (result.type === "error" || result.ok === false) {
                throw new Error(str(result.error) || "Request failed");
            }
            const answer = str(result.answer).trim() || bubble.answerText.trim() || "No answer returned.";
            history.push({ role: "assistant", content: answer });
            const lastPerson = str(result.last_person).trim();
            if (lastPerson)
                sessionContext.last_person = lastPerson;
        }
        catch (err) {
            if (gen !== generation)
                return;
            const msg = err instanceof Error ? err.message : String(err);
            history.push({ role: "assistant", content: `Sorry, I couldn't answer that: ${msg}` });
        }
        finally {
            inflight = null;
            if (gen === generation) {
                clearLiveBubble();
                setLoading(false);
                renderMessages();
                const input = ensureGaiChatDialog().querySelector("#gai-chat-input");
                input?.focus();
            }
        }
    })();
    await inflight;
}
function resetGaiChat() {
    generation++;
    history = [];
    sessionContext = {};
    inflight = null;
    setLoading(false);
    clearLiveBubble();
    renderMessages();
    const dialog = ensureGaiChatDialog();
    const input = dialog.querySelector("#gai-chat-input");
    if (input)
        input.value = "";
    input?.focus();
}
export function openGaiChatModal() {
    const dialog = ensureGaiChatDialog();
    renderMessages();
    if (!dialog.open)
        dialog.showModal();
    const input = dialog.querySelector("#gai-chat-input");
    input?.focus();
}
export function mountGaiChatDialog() {
    ensureGaiChatDialog();
}
export function bindGaiChatPanel() {
    const dialog = ensureGaiChatDialog();
    const form = dialog.querySelector("#gai-chat-form");
    const input = dialog.querySelector("#gai-chat-input");
    form?.addEventListener("submit", (event) => {
        event.preventDefault();
        if (!input)
            return;
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
