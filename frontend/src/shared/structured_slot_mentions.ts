import type { CounterpartySlot, SlotMention } from "./types.js";

function parseHmToMinutes(hm: string): number | null {
  const trimmed = hm.trim();
  const m24 = trimmed.match(/^(\d{1,2}):(\d{2})$/);
  if (m24) {
    const hour = Number(m24[1]);
    const minute = Number(m24[2]);
    if (hour < 0 || hour > 23 || minute < 0 || minute > 59) return null;
    return hour * 60 + minute;
  }
  const m12 = trimmed.match(/^(\d{1,2})(?::(\d{2}))?\s*(am|pm)$/i);
  if (!m12) return null;
  let hour = Number(m12[1]);
  const minute = Number(m12[2] || 0);
  const mer = m12[3].toLowerCase();
  if (hour < 1 || hour > 12 || minute < 0 || minute > 59) return null;
  if (hour === 12) hour = 0;
  if (mer === "pm") hour += 12;
  return hour * 60 + minute;
}

function minuteTo12hVariants(minute: number): string[] {
  const h24 = Math.floor(minute / 60);
  const m = minute % 60;
  const h12 = h24 % 12 || 12;
  const mer = h24 >= 12 ? "PM" : "AM";
  const merLower = mer.toLowerCase();
  const variants = new Set<string>();
  variants.add(`${h12} ${mer}`);
  variants.add(`${h12} ${merLower}`);
  if (m === 0) {
    variants.add(`${h12}${merLower}`);
  } else {
    variants.add(`${h12}:${String(m).padStart(2, "0")} ${mer}`);
    variants.add(`${h12}:${String(m).padStart(2, "0")} ${merLower}`);
  }
  return [...variants];
}

function rangeSearchVariants(startMinute: number, endMinute: number): string[] {
  const starts = minuteTo12hVariants(startMinute);
  const ends = minuteTo12hVariants(endMinute);
  const out = new Set<string>();
  for (const s of starts) {
    for (const e of ends) {
      out.add(`${s} – ${e}`);
      out.add(`${s} - ${e}`);
      out.add(`${s}–${e}`);
      out.add(`${s}-${e}`);
      out.add(`${s} to ${e}`);
    }
  }
  const startH24 = Math.floor(startMinute / 60);
  const endH24 = Math.floor(endMinute / 60);
  const startMer = startH24 >= 12 ? "PM" : "AM";
  const endMer = endH24 >= 12 ? "PM" : "AM";
  if (startMer === endMer) {
    const startH12 = startH24 % 12 || 12;
    const endH12 = endH24 % 12 || 12;
    const endMin = endMinute % 60;
    const endTail =
      endMin === 0
        ? `${endH12} ${endMer}`
        : `${endH12}:${String(endMin).padStart(2, "0")} ${endMer}`;
    for (const sep of [" – ", " - ", "–", "-"]) {
      out.add(`${startH12}${sep}${endTail}`);
      out.add(`${startH12}${sep}${endTail.replace("PM", "pm").replace("AM", "am")}`);
    }
  }
  return [...out];
}

function mergeMentions(mentions: SlotMention[]): SlotMention[] {
  const sorted = [...mentions].sort((a, b) => a.start - b.start || a.end - b.end);
  const out: SlotMention[] = [];
  for (const mention of sorted) {
    const prev = out[out.length - 1];
    if (prev && mention.start < prev.end) continue;
    out.push(mention);
  }
  return out;
}

export function findStructuredMentionsInText(text: string, slots: CounterpartySlot[]): SlotMention[] {
  const out: SlotMention[] = [];
  const lower = text.toLowerCase();
  for (const slot of slots) {
    const startMinute = parseHmToMinutes(slot.start);
    const endMinute = parseHmToMinutes(slot.end);
    if (startMinute === null || endMinute === null || endMinute <= startMinute) continue;
    for (const variant of rangeSearchVariants(startMinute, endMinute)) {
      let from = 0;
      const needle = variant.toLowerCase();
      while (from < lower.length) {
        const idx = lower.indexOf(needle, from);
        if (idx < 0) break;
        const before = idx > 0 ? lower[idx - 1] : " ";
        const after = idx + needle.length < lower.length ? lower[idx + needle.length] : " ";
        if (/[a-z0-9]/i.test(before) || /[a-z0-9]/i.test(after)) {
          from = idx + 1;
          continue;
        }
        out.push({
          raw: text.slice(idx, idx + variant.length),
          start: idx,
          end: idx + variant.length,
          date_key: slot.date,
          start_minute: startMinute,
          end_minute: endMinute,
          label: `${slot.date} ${slot.start}-${slot.end}`,
        });
        from = idx + variant.length;
      }
    }
  }
  return mergeMentions(out);
}

export function formatCounterpartySlotLabel(slot: CounterpartySlot): string {
  const startMinute = parseHmToMinutes(slot.start);
  const endMinute = parseHmToMinutes(slot.end);
  if (startMinute === null || endMinute === null) {
    return `${slot.date} ${slot.start}–${slot.end}`;
  }
  const [s] = minuteTo12hVariants(startMinute);
  const [e] = minuteTo12hVariants(endMinute);
  const date = new Date(`${slot.date}T12:00:00`);
  const dateLabel = Number.isNaN(date.getTime())
    ? slot.date
    : new Intl.DateTimeFormat("en-US", { weekday: "short", month: "short", day: "numeric" }).format(date);
  return `${dateLabel} · ${s} – ${e}`;
}
