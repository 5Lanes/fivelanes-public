import { threadIsLinkedin, threadIsMeetRecording, threadIsSlack, threadIsText, } from "./thread_domain.js";
import { escapeHtml } from "./utils.js";
const SOURCE_LABELS = {
    email: "Email",
    text: "Text",
    slack: "Slack",
    linkedin: "LinkedIn",
    meet: "Meet notes",
};
export function threadChannelForThread(thread) {
    if (threadIsText(thread))
        return "text";
    if (threadIsSlack(thread))
        return "slack";
    if (threadIsLinkedin(thread))
        return "linkedin";
    if (threadIsMeetRecording(thread))
        return "meet";
    return "email";
}
export function sourcePillHtml(channel, label) {
    const text = label ?? SOURCE_LABELS[channel];
    const mod = channel === "email"
        ? "email"
        : channel === "text"
            ? "text"
            : channel === "slack"
                ? "slack"
                : channel === "linkedin"
                    ? "linkedin"
                    : "meet";
    return `<span class="source-pill source-pill--${mod}">${escapeHtml(text)}</span>`;
}
export function laneAreaColorVar(colorIndex) {
    const colors = [
        "var(--lane-coral)",
        "var(--lane-orange)",
        "var(--lane-yellow)",
        "var(--lane-teal)",
        "var(--lane-blue)",
        "var(--lane-purple)",
    ];
    return colors[((colorIndex % colors.length) + colors.length) % colors.length];
}
export function sourceHighlightItemHtml(channel, text) {
    return `<li class="source-highlight" data-source="${channel}">${sourcePillHtml(channel)}${escapeHtml(text)}</li>`;
}
export function sourceHighlightListHtml(items) {
    if (!items.length)
        return "";
    const inner = items.map((item) => sourceHighlightItemHtml(item.channel, item.text)).join("");
    return `<ul class="source-highlight-list">${inner}</ul>`;
}
