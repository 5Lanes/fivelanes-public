import { conversationLabel, latestMessageDatetime, } from "../shared/conversation_domain.js";
import { getConversationThreads, getConversationsBundle, } from "../shared/conversations_store.js";
import { escapeHtml, formatDate, str } from "../shared/utils.js";
const PAGE_HTML = `
<div class="dashboard-layout">
  <aside class="conversation-nav" id="conversation-nav">
    <h2>Conversations</h2>
    <ul id="conversation-nav-list"></ul>
    <p class="conversation-nav-hint">
      <a href="/texts-setup">Import more</a>
    </p>
  </aside>
  <div class="main-panel">
    <div id="conversation-cards" class="cards"></div>
  </div>
</div>`;
let navObserver = null;
let interactionsBound = false;
function cardsEl() {
    return document.getElementById("conversation-cards");
}
function navListEl() {
    return document.getElementById("conversation-nav-list");
}
function messageBlockHtml(msg) {
    const dt = str(msg.datetime);
    const sender = msg.is_from_me ? "me" : str(msg.sender) || "unknown";
    const body = str(msg.body) || "(empty)";
    return `<div class="message-block conversation-message">
    <div class="card-top"><time datetime="${escapeHtml(dt)}">${formatDate(dt)}</time>
    <span class="conversation-sender">${escapeHtml(sender)}</span></div>
    <p class="conversation-body">${escapeHtml(body)}</p>
  </div>`;
}
function renderCards(threads) {
    const el = cardsEl();
    el.innerHTML = "";
    if (!threads.length) {
        el.innerHTML = `<p class="empty-state">No imported conversations yet.
      <a href="/texts-setup">Texts setup</a> to import threads, then return here to read them.</p>`;
        return;
    }
    for (const thread of threads) {
        const dt = latestMessageDatetime(thread);
        const nMsg = thread.messages.length;
        const label = conversationLabel(thread);
        const messagesHtml = nMsg > 1
            ? `<div class="thread-messages">${thread.messages.map(messageBlockHtml).join("")}</div>`
            : nMsg === 1
                ? messageBlockHtml(thread.messages[0])
                : `<p class="conversation-empty-thread">No messages stored for this thread.</p>`;
        const art = document.createElement("article");
        art.className = "card conversation-card";
        art.id = `conversation-${thread.id}`;
        art.innerHTML =
            `<div class="card-top"><time datetime="${escapeHtml(dt)}">${formatDate(dt)}</time>` +
                `<span class="count-pill">${nMsg} msg${nMsg === 1 ? "" : "s"}</span></div>` +
                `<h3>${escapeHtml(label)}</h3>` +
                messagesHtml;
        el.appendChild(art);
    }
}
function renderNav(threads) {
    const list = navListEl();
    list.innerHTML = "";
    for (const thread of threads) {
        const li = document.createElement("li");
        const btn = document.createElement("button");
        btn.type = "button";
        btn.textContent = conversationLabel(thread);
        btn.dataset.conversationId = thread.id;
        btn.addEventListener("click", () => {
            document
                .querySelectorAll("#conversation-nav button[data-conversation-id]")
                .forEach((el) => el.classList.remove("active"));
            btn.classList.add("active");
            document
                .getElementById(`conversation-${thread.id}`)
                ?.scrollIntoView({ behavior: "smooth", block: "start" });
        });
        li.appendChild(btn);
        list.appendChild(li);
    }
}
function bindScrollNavHighlight() {
    if (navObserver) {
        navObserver.disconnect();
        navObserver = null;
    }
    const cards = Array.from(document.querySelectorAll("article.conversation-card"));
    if (!cards.length)
        return;
    navObserver = new IntersectionObserver((entries) => {
        let topVisible = null;
        let bestY = Number.POSITIVE_INFINITY;
        for (const entry of entries) {
            if (!entry.isIntersecting)
                continue;
            const el = entry.target;
            const y = Math.abs(el.getBoundingClientRect().top);
            if (y < bestY) {
                bestY = y;
                topVisible = el.id.replace(/^conversation-/, "");
            }
        }
        if (!topVisible)
            return;
        document
            .querySelectorAll("#conversation-nav button[data-conversation-id]")
            .forEach((btn) => {
            btn.classList.toggle("active", btn.dataset.conversationId === topVisible);
        });
    }, { root: null, rootMargin: "0px 0px -70% 0px", threshold: [0.1, 0.5, 1] });
    cards.forEach((c) => navObserver?.observe(c));
}
export function mountConversationsPage(root) {
    root.innerHTML = PAGE_HTML;
}
export async function renderConversationsPage() {
    if (!getConversationsBundle())
        return;
    const threads = getConversationThreads();
    renderCards(threads);
    renderNav(threads);
    bindScrollNavHighlight();
}
export function bindConversationsInteractions() {
    if (interactionsBound)
        return;
    interactionsBound = true;
}
