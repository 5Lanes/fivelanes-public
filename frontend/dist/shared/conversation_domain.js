import { str } from "./utils.js";
export function normalizeConversationsBundle(data) {
    if (!data || typeof data !== "object") {
        throw new Error("Invalid JSON: expected an object.");
    }
    const threads = Array.isArray(data.threads) ? data.threads : [];
    data.threads = threads.map(normalizeConversationThread);
    return data;
}
export function normalizeConversationThread(raw) {
    const row = (raw && typeof raw === "object" ? raw : {});
    const messages = Array.isArray(row.messages) ? row.messages.map(normalizeConversationMessage) : [];
    const chatIdRaw = row.chat_id;
    const chat_id = chatIdRaw != null && String(chatIdRaw).trim() !== ""
        ? Number(chatIdRaw)
        : undefined;
    return {
        id: str(row.id),
        contact_label: str(row.contact_label || row.display_name),
        chat_identifier: str(row.chat_identifier),
        service_name: str(row.service_name),
        snoozed: Number(row.snoozed || 0),
        messages,
        chat_id: Number.isFinite(chat_id) ? chat_id : undefined,
        last_message_at: str(row.last_message_at),
        message_count: row.message_count != null ? Number(row.message_count) : undefined,
    };
}
export function normalizeConversationMessage(raw) {
    const row = (raw && typeof raw === "object" ? raw : {});
    return {
        source_id: str(row.source_id),
        datetime: str(row.datetime),
        sender: str(row.sender),
        body: str(row.body),
        is_from_me: Boolean(row.is_from_me),
    };
}
export function conversationLabel(thread) {
    const name = thread.contact_label || thread.chat_identifier || thread.id;
    const service = thread.service_name ? ` · ${thread.service_name}` : "";
    return `${name}${service}`;
}
export function candidateLabel(thread) {
    const base = conversationLabel(thread);
    const count = thread.message_count === 1
        ? "1 message"
        : thread.message_count != null
            ? `${thread.message_count} messages`
            : "";
    const when = thread.last_message_at ? ` · ${thread.last_message_at}` : "";
    return count ? `${base} (${count}${when})` : base;
}
export function latestMessageDatetime(thread) {
    const msgs = thread.messages;
    if (!msgs.length)
        return thread.last_message_at || "";
    return msgs[msgs.length - 1]?.datetime || "";
}
