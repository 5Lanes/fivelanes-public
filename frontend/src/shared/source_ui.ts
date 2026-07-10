import {
  threadIsCalendarEvent,
  threadIsEmail,
  threadIsLinkedin,
  threadIsMeetRecording,
  threadIsSlack,
  threadIsText,
} from "./thread_domain.js";
import type { ThreadView } from "./types.js";
import { escapeHtml } from "./utils.js";

export type SourceChannel = "email" | "text" | "slack" | "linkedin" | "meet" | "calendar";

const SOURCE_LABELS: Record<SourceChannel, string> = {
  email: "Email",
  text: "Text",
  slack: "Slack",
  linkedin: "LinkedIn",
  meet: "Meet notes",
  calendar: "Calendar",
};

export function threadChannelForThread(thread: ThreadView): SourceChannel {
  if (threadIsText(thread)) return "text";
  if (threadIsSlack(thread)) return "slack";
  if (threadIsLinkedin(thread)) return "linkedin";
  if (threadIsMeetRecording(thread)) return "meet";
  if (threadIsCalendarEvent(thread)) return "calendar";
  return "email";
}

export function sourcePillHtml(channel: SourceChannel, label?: string): string {
  const text = label ?? SOURCE_LABELS[channel];
  const mod =
    channel === "email"
      ? "email"
      : channel === "text"
        ? "text"
        : channel === "slack"
          ? "slack"
          : channel === "linkedin"
            ? "linkedin"
            : channel === "meet"
              ? "meet"
              : "calendar";
  return `<span class="source-pill source-pill--${mod}">${escapeHtml(text)}</span>`;
}

export function laneAreaColorVar(colorIndex: number): string {
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

export function sourceHighlightItemHtml(channel: SourceChannel, text: string): string {
  return `<li class="source-highlight" data-source="${channel}">${sourcePillHtml(channel)}${escapeHtml(text)}</li>`;
}

export function sourceHighlightListHtml(
  items: Array<{ channel: SourceChannel; text: string }>,
): string {
  if (!items.length) return "";
  const inner = items.map((item) => sourceHighlightItemHtml(item.channel, item.text)).join("");
  return `<ul class="source-highlight-list">${inner}</ul>`;
}
