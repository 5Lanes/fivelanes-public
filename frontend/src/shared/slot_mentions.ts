import type { SlotMention } from "./types.js";

const MONTHS: Record<string, number> = {
  jan: 1,
  january: 1,
  feb: 2,
  february: 2,
  mar: 3,
  march: 3,
  apr: 4,
  april: 4,
  may: 5,
  jun: 6,
  june: 6,
  jul: 7,
  july: 7,
  aug: 8,
  august: 8,
  sep: 9,
  sept: 9,
  september: 9,
  oct: 10,
  october: 10,
  nov: 11,
  november: 11,
  dec: 12,
  december: 12,
};

type ParsedTime = { minute: number; meridiem: "am" | "pm" | "" };

function parseTimeToken(token: string): ParsedTime | null {
  const m = token.trim().match(/^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$/i);
  if (!m) return null;
  let hour = Number(m[1] || 0);
  const minute = Number(m[2] || 0);
  const meridiem = ((m[3] || "").toLowerCase() as "am" | "pm" | "") || "";
  if (hour < 0 || hour > 12 || minute < 0 || minute > 59) return null;
  if (hour === 12) hour = 0;
  if (meridiem === "pm") hour += 12;
  return { minute: hour * 60 + minute, meridiem };
}

function normalizeRange(start: ParsedTime, end: ParsedTime): { startMinute: number; endMinute: number } | null {
  let startMinute = start.minute;
  let endMinute = end.minute;
  if (!start.meridiem && end.meridiem) {
    if (end.meridiem === "pm" && startMinute < 12 * 60) startMinute += 12 * 60;
  }
  if (startMinute === endMinute) return null;
  if (endMinute < startMinute) {
    // Common shorthand like "12-3 PM" or "11-1 PM".
    if (endMinute + 12 * 60 > startMinute) endMinute += 12 * 60;
  }
  if (endMinute <= startMinute) return null;
  if (endMinute > 24 * 60) endMinute = 24 * 60;
  return { startMinute, endMinute };
}

function chooseYear(month: number, day: number, yearRaw: string): number {
  if (yearRaw) return Number(yearRaw);
  const now = new Date();
  const currentYear = now.getFullYear();
  const candidate = new Date(currentYear, month - 1, day);
  const windowMs = 180 * 24 * 60 * 60 * 1000;
  if (candidate.getTime() + windowMs < now.getTime()) return currentYear + 1;
  return currentYear;
}

function toYmd(year: number, month: number, day: number): string {
  return `${String(year).padStart(4, "0")}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
}

const WEEKDAYS: Record<string, number> = {
  sunday: 0,
  monday: 1,
  tuesday: 2,
  wednesday: 3,
  thursday: 4,
  friday: 5,
  saturday: 6,
};

const TIME_OF_DAY: Record<string, { start: number; end: number }> = {
  morning: { start: 9 * 60, end: 12 * 60 },
  afternoon: { start: 12 * 60, end: 17 * 60 },
  evening: { start: 17 * 60, end: 21 * 60 },
};

const MONTH_WORD =
  "jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?";

const ORDINAL = "(?:st|nd|rd|th)?";

const TIME_TOKEN = String.raw`\d{1,2}(?::\d{2})?\s*(?:am|pm)?`;

const SLOT_RE = new RegExp(
  String.raw`\b(${MONTH_WORD})\s+(\d{1,2})${ORDINAL}(?:,\s*(\d{4}))?(?:,\s*|\s+|:\s*)(${TIME_TOKEN})\s*(?:-|–|to)\s*(${TIME_TOKEN})`,
  "gi",
);

const DATE_ANCHOR_RE = new RegExp(
  String.raw`\b(${MONTH_WORD})\s+(\d{1,2})${ORDINAL}(?:,\s*(\d{4}))?\b`,
  "gi",
);

const TIME_RANGE_RE = new RegExp(
  String.raw`\b(${TIME_TOKEN})\s*(?:-|–|to)\s*(${TIME_TOKEN})`,
  "gi",
);

const WEEKDAY_SLOT_RE =
  /\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s+((?:morning|afternoon|evening)(?:\s*(?:\/|\bor\b|\band\b)\s*(?:morning|afternoon|evening))*)/gi;

function sentenceEnd(text: string, from: number): number {
  const slice = text.slice(from);
  const m = slice.search(/[.!?](?:\s|$)/);
  return m >= 0 ? from + m : text.length;
}

function dateKeyFromMatch(match: RegExpMatchArray): string | null {
  const monthWord = String(match[1] || "").toLowerCase();
  const day = Number(match[2] || 0);
  const month = MONTHS[monthWord] || 0;
  if (!month || day <= 0 || day > 31) return null;
  const year = chooseYear(month, day, String(match[3] || ""));
  return toYmd(year, month, day);
}

function mentionFromTimeRange(
  text: string,
  startRaw: string,
  endRaw: string,
  absStart: number,
  absEnd: number,
  dateKey: string,
): SlotMention | null {
  const s = parseTimeToken(startRaw);
  const e = parseTimeToken(endRaw);
  if (!s || !e) return null;
  const range = normalizeRange(s, e);
  if (!range) return null;
  const raw = text.slice(absStart, absEnd);
  return {
    raw,
    start: absStart,
    end: absEnd,
    date_key: dateKey,
    start_minute: range.startMinute,
    end_minute: range.endMinute,
    label: `${dateKey} ${startRaw}-${endRaw}`,
  };
}

function extractColonSeparatedSlots(text: string, asOf: Date): SlotMention[] {
  void asOf;
  const out: SlotMention[] = [];
  for (const dateMatch of text.matchAll(DATE_ANCHOR_RE)) {
    const dateStart = dateMatch.index ?? 0;
    const dateEnd = dateStart + String(dateMatch[0] || "").length;
    const dateKey = dateKeyFromMatch(dateMatch);
    if (!dateKey) continue;
    const sentEnd = sentenceEnd(text, dateStart);
    const afterDate = text.slice(dateEnd, sentEnd);
    const colonIdx = afterDate.indexOf(":");
    if (colonIdx < 0) continue;
    const scanFrom = dateEnd + colonIdx + 1;
    const scanText = text.slice(scanFrom, sentEnd);
    for (const timeMatch of scanText.matchAll(TIME_RANGE_RE)) {
      const relStart = timeMatch.index ?? 0;
      const relEnd = relStart + String(timeMatch[0] || "").length;
      const mention = mentionFromTimeRange(
        text,
        String(timeMatch[1] || ""),
        String(timeMatch[2] || ""),
        scanFrom + relStart,
        scanFrom + relEnd,
        dateKey,
      );
      if (mention) out.push(mention);
    }
  }
  return out;
}

function parseTimeOfDayPhrase(phrase: string): { startMinute: number; endMinute: number } | null {
  const parts = phrase
    .toLowerCase()
    .split(/\s*(?:\/|\bor\b|\band\b)\s*/i)
    .map((s) => s.trim())
    .filter(Boolean);
  let startMinute = 24 * 60;
  let endMinute = 0;
  for (const part of parts) {
    const window = TIME_OF_DAY[part];
    if (!window) continue;
    startMinute = Math.min(startMinute, window.start);
    endMinute = Math.max(endMinute, window.end);
  }
  if (endMinute <= startMinute) return null;
  return { startMinute, endMinute };
}

function nextWeekdayYmd(weekday: number, from = new Date()): string {
  const anchor = new Date(from);
  anchor.setHours(12, 0, 0, 0);
  const delta = (weekday - anchor.getDay() + 7) % 7;
  anchor.setDate(anchor.getDate() + delta);
  return toYmd(anchor.getFullYear(), anchor.getMonth() + 1, anchor.getDate());
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

export function extractSlotMentions(text: string, asOf: Date = new Date()): SlotMention[] {
  const out: SlotMention[] = [];
  for (const match of text.matchAll(SLOT_RE)) {
    const monthWord = String(match[1] || "").toLowerCase();
    const day = Number(match[2] || 0);
    const year = chooseYear(MONTHS[monthWord] || 0, day, String(match[3] || ""));
    const month = MONTHS[monthWord] || 0;
    const startRaw = String(match[4] || "");
    const endRaw = String(match[5] || "");
    if (!month || day <= 0 || day > 31) continue;
    const s = parseTimeToken(startRaw);
    const e = parseTimeToken(endRaw);
    if (!s || !e) continue;
    const range = normalizeRange(s, e);
    if (!range) continue;
    const raw = String(match[0] || "");
    out.push({
      raw,
      start: match.index ?? 0,
      end: (match.index ?? 0) + raw.length,
      date_key: toYmd(year, month, day),
      start_minute: range.startMinute,
      end_minute: range.endMinute,
      label: `${toYmd(year, month, day)} ${startRaw}-${endRaw}`,
    });
  }

  for (const match of text.matchAll(WEEKDAY_SLOT_RE)) {
    const weekdayWord = String(match[1] || "").toLowerCase();
    const timePhrase = String(match[2] || "");
    const weekday = WEEKDAYS[weekdayWord];
    if (weekday === undefined) continue;
    const range = parseTimeOfDayPhrase(timePhrase);
    if (!range) continue;
    const raw = String(match[0] || "");
    const dateKey = nextWeekdayYmd(weekday, asOf);
    out.push({
      raw,
      start: match.index ?? 0,
      end: (match.index ?? 0) + raw.length,
      date_key: dateKey,
      start_minute: range.startMinute,
      end_minute: range.endMinute,
      label: `${dateKey} ${timePhrase}`,
    });
  }

  return mergeMentions(out);
}
