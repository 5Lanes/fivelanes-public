import { normalizeConversationsBundle } from "./conversation_domain.js";
export const CONVERSATIONS_BUNDLE_URL = "/api/conversations/bundle";
let currentBundle = null;
let currentSourceLabel = "";
let currentThreads = [];
export function getConversationsBundle() {
    return currentBundle;
}
export function getConversationsSourceLabel() {
    return currentSourceLabel;
}
export function getConversationThreads() {
    return currentThreads;
}
export function setConversationsBundle(data, label) {
    const normalized = normalizeConversationsBundle(data);
    currentBundle = normalized;
    currentSourceLabel = label;
    currentThreads = normalized.threads;
}
export async function loadConversationsBundle() {
    const res = await fetch(CONVERSATIONS_BUNDLE_URL);
    if (res.status === 404) {
        const err = (await res.json().catch(() => ({})));
        if (err.error === "database_not_found") {
            throw new Error("Database not found on server.");
        }
    }
    if (!res.ok) {
        const err = (await res.json().catch(() => ({})));
        throw new Error(err.error || `HTTP ${res.status}`);
    }
    const data = normalizeConversationsBundle((await res.json()));
    const label = res.headers.get("Last-Modified") || "conversations";
    return { data, label };
}
