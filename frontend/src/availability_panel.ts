/**
 * Threads page right-rail availability: loads out/availability_calendar_latest.json
 * and shows likely-open / virtual-only windows for the next 7 calendar days (document TZ).
 */

import {
  dayHeadingLabelShort,
  endOfDayInZone,
  formatTimeRangeInTz,
  isoToYmdInZone,
  nextNDaysFromYmd,
  startOfDayInZone,
  todayYmdInTz,
} from "./shared/time_ui.js";
import { isFeatureEnabled } from "./shared/features.js";
import { escapeHtml } from "./shared/utils.js";

type LooseObj = Record<string, unknown>;

type Layer = "parenting" | "child_home" | "busy" | "open" | "commit";

interface AgendaItem {
  layer: Layer;
  start: Date;
  end: Date;
  timeLine: string;
  title: string;
  detail?: string;
}

function str(v: unknown): string {
  return typeof v === "string" ? v : "";
}


function subtractDateIntervals(
  base: Array<{ start: Date; end: Date }>,
  toRemove: Array<{ start: Date; end: Date }>,
): Array<{ start: Date; end: Date }> {
  if (!base.length) return [];
  if (!toRemove.length) return base.slice();
  const removes = [...toRemove]
    .filter((r) => r.end.getTime() > r.start.getTime())
    .sort((a, b) => a.start.getTime() - b.start.getTime());
  const out: Array<{ start: Date; end: Date }> = [];
  for (const b of base) {
    let cur = b.start;
    for (const r of removes) {
      if (r.end.getTime() <= cur.getTime()) continue;
      if (r.start.getTime() >= b.end.getTime()) break;
      if (r.start.getTime() > cur.getTime()) {
        out.push({ start: cur, end: r.start });
      }
      if (r.end.getTime() > cur.getTime()) {
        cur = r.end;
      }
      if (cur.getTime() >= b.end.getTime()) break;
    }
    if (cur.getTime() < b.end.getTime()) out.push({ start: cur, end: b.end });
  }
  return out.filter((x) => x.end.getTime() > x.start.getTime());
}

function segmentIsoIntervalForDay(
  isoStart: string,
  isoEnd: string,
  dayKey: string,
  tz: string,
): { start: Date; end: Date } | null {
  const t0 = new Date(isoStart).getTime();
  const t1 = new Date(isoEnd).getTime();
  if (!(t1 > t0)) return null;
  const startDay = isoToYmdInZone(isoStart, tz);
  const endDay = isoToYmdInZone(isoEnd, tz);
  if (dayKey !== startDay && dayKey !== endDay) return null;
  if (startDay === dayKey && endDay === dayKey) {
    return { start: new Date(isoStart), end: new Date(isoEnd) };
  }
  if (startDay === dayKey) {
    return { start: new Date(isoStart), end: endOfDayInZone(dayKey, tz) };
  }
  return { start: startOfDayInZone(dayKey, tz), end: new Date(isoEnd) };
}

function formatDatePairInTz(a: Date, b: Date, timeZone: string): string {
  return formatTimeRangeInTz(a.toISOString(), b.toISOString(), timeZone);
}

function parseHm(hm: string | undefined): { h: number; m: number } | null {
  if (!hm || !/^\d{1,2}:\d{2}$/.test(hm)) return null;
  const [h, m] = hm.split(":").map((x) => parseInt(x, 10));
  return { h, m };
}

function formatHm24(p: { h: number; m: number }): string {
  return `${String(p.h).padStart(2, "0")}:${String(p.m).padStart(2, "0")}`;
}

function formatLocalHmRange(start?: string, end?: string): string {
  const sm = parseHm(start);
  const em = parseHm(end);
  if (sm && em) return `${formatHm24(sm)}–${formatHm24(em)}`;
  return `${start ?? ""}–${end ?? ""}`;
}

interface CommitmentRow {
  date?: string;
  title?: string;
  kind?: string;
  location_hint?: string;
  start_local?: string;
  assumed_end_local?: string;
  _note?: string;
}

function commitmentRangeOnDay(
  c: CommitmentRow,
  dateKey: string,
  tz: string,
): { start: Date; end: Date } | null {
  if (c.date !== dateKey) return null;
  const sm = parseHm(c.start_local);
  const em = parseHm(c.assumed_end_local);
  if (!sm || !em) return null;
  const dayStart = startOfDayInZone(dateKey, tz);
  const start = new Date(dayStart.getTime() + (sm.h * 60 + sm.m) * 60_000);
  const end = new Date(dayStart.getTime() + (em.h * 60 + em.m) * 60_000);
  if (!(end.getTime() > start.getTime())) return null;
  return { start, end };
}

function getTimeZone(data: LooseObj): string {
  const meta = (data.meta || {}) as LooseObj;
  return str(meta.timezone).trim() || "America/New_York";
}

function buildDayAgenda(dateKey: string, data: LooseObj): AgendaItem[] {
  const tz = getTimeZone(data);
  const items: AgendaItem[] = [];

  const parentingByDate = new Map<string, LooseObj>();
  for (const row of (data.parenting_unavailable_local as LooseObj[]) ?? []) {
    parentingByDate.set(str(row.date), row);
  }
  const childHomeByDate = new Map<string, LooseObj>();
  for (const row of (data.child_home_virtual_only_local as LooseObj[]) ?? []) {
    childHomeByDate.set(str(row.date), row);
  }
  const openByDate = new Map<string, LooseObj>();
  for (const row of (data.availability_for_new_meetings_iso as LooseObj[]) ?? []) {
    openByDate.set(str(row.date), row);
  }
  const commitments = (data.calendar_commitments_from_screenshot as CommitmentRow[]) ?? [];
  const busy = (data.busy_with_buffers_iso as { start: string; end: string; source?: string }[]) ?? [];

  const pday = parentingByDate.get(dateKey);
  for (const iv of (pday?.intervals_iso as LooseObj[]) ?? []) {
    const seg = segmentIsoIntervalForDay(str(iv.start), str(iv.end), dateKey, tz);
    if (!seg) continue;
    items.push({
      layer: "parenting",
      start: seg.start,
      end: seg.end,
      timeLine: formatDatePairInTz(seg.start, seg.end, tz),
      title: "Parenting",
      detail: str(iv.id),
    });
  }

  const chDay = childHomeByDate.get(dateKey);
  const childHomeSegs: Array<{ start: Date; end: Date; id: string }> = [];
  for (const iv of (chDay?.intervals_iso as LooseObj[]) ?? []) {
    const seg = segmentIsoIntervalForDay(str(iv.start), str(iv.end), dateKey, tz);
    if (!seg) continue;
    childHomeSegs.push({ start: seg.start, end: seg.end, id: str(iv.id) });
  }

  const busySegs: Array<{ start: Date; end: Date }> = [];
  for (const b of busy) {
    const seg = segmentIsoIntervalForDay(b.start, b.end, dateKey, tz);
    if (!seg) continue;
    busySegs.push(seg);
    items.push({
      layer: "busy",
      start: seg.start,
      end: seg.end,
      timeLine: formatDatePairInTz(seg.start, seg.end, tz),
      title: "Busy (+buffers)",
      detail: b.source,
    });
  }

  const now = new Date();
  const virtualOnlyAvail = subtractDateIntervals(
    childHomeSegs.map(({ start, end }) => ({ start, end })),
    busySegs,
  )
    .map((seg) => {
      if (seg.end.getTime() <= now.getTime()) return null;
      if (seg.start.getTime() < now.getTime()) return { start: now, end: seg.end };
      return seg;
    })
    .filter((x): x is { start: Date; end: Date } => x !== null);
  for (const seg of virtualOnlyAvail) {
    items.push({
      layer: "child_home",
      start: seg.start,
      end: seg.end,
      timeLine: formatDatePairInTz(seg.start, seg.end, tz),
      title: "Virtual only",
      detail: "",
    });
  }

  const openDay = openByDate.get(dateKey);
  for (const w of (openDay?.likely_open_windows as LooseObj[]) ?? []) {
    const seg = segmentIsoIntervalForDay(str(w.start), str(w.end), dateKey, tz);
    if (!seg) continue;
    if (seg.end.getTime() <= now.getTime()) continue;
    const start = seg.start.getTime() < now.getTime() ? now : seg.start;
    items.push({
      layer: "open",
      start,
      end: seg.end,
      timeLine: formatDatePairInTz(start, seg.end, tz),
      title: "Open",
      detail: str(w._note),
    });
  }

  for (const c of commitments) {
    const seg = commitmentRangeOnDay(c, dateKey, tz);
    if (!seg) continue;
    const hint = c.location_hint ? `${c.kind ?? ""} · ${c.location_hint}`.trim() : (c.kind ?? "");
    items.push({
      layer: "commit",
      start: seg.start,
      end: seg.end,
      timeLine: formatLocalHmRange(c.start_local, c.assumed_end_local),
      title: c.title ?? "(event)",
      detail: hint || str(c._note),
    });
  }

  const layerOrder: Record<Layer, number> = { parenting: 0, child_home: 1, busy: 2, open: 3, commit: 4 };
  items.sort((a, b) => {
    const d0 = a.start.getTime() - b.start.getTime();
    if (d0 !== 0) return d0;
    return layerOrder[a.layer] - layerOrder[b.layer];
  });
  return items;
}

function renderAgendaDayHtml(dateKey: string, data: LooseObj, opts: { openOnly: boolean }): string {
  let items = buildDayAgenda(dateKey, data);
  if (opts.openOnly) items = items.filter((it) => it.layer === "open" || it.layer === "child_home");
  const openMeta = ((data.availability_for_new_meetings_iso as LooseObj[]) ?? []).find((d) => str(d.date) === dateKey);
  const parentingMeta = ((data.parenting_unavailable_local as LooseObj[]) ?? []).find((d) => str(d.date) === dateKey);
  const childHomeMeta = ((data.child_home_virtual_only_local as LooseObj[]) ?? []).find((d) => str(d.date) === dateKey);
  const foot = opts.openOnly
    ? [str(openMeta?._note)].filter(Boolean).join(" ")
    : [str(openMeta?._note), str(parentingMeta?._note), str(childHomeMeta?._note)].filter(Boolean).join(" ");

  const emptyMsg = opts.openOnly ? "No open or virtual-only slots this day." : "No blocks this day.";
  const rows =
    items.length === 0
      ? `<li class="dash-avail-empty">${escapeHtml(emptyMsg)}</li>`
      : items
          .map((it) => {
            const tip = [it.timeLine, it.title, it.detail].filter(Boolean).join("\n");
            const sub = it.detail ? `<div class="dash-avail-detail">${escapeHtml(it.detail)}</div>` : "";
            return `<li class="dash-avail-row dash-avail-row--${it.layer}" title="${escapeHtml(tip)}">
            <div class="dash-avail-time">${escapeHtml(it.timeLine)}</div>
            <div class="dash-avail-body">
              <div class="dash-avail-title">${escapeHtml(it.title)}</div>
              ${sub}
            </div>
          </li>`;
          })
          .join("");

  return `<section class="dash-avail-day" data-date="${escapeHtml(dateKey)}">
    <header class="dash-avail-day-head">
      <div class="dash-avail-day-name">${escapeHtml(dayHeadingLabelShort(dateKey))}</div>
      <div class="dash-avail-day-ymd">${escapeHtml(dateKey)}</div>
    </header>
    <ul class="dash-avail-list">${rows}</ul>
    ${foot ? `<p class="dash-avail-foot">${escapeHtml(foot)}</p>` : ""}
  </section>`;
}

const LEFT_NAV_AVAILABILITY_DAYS = 7;

function renderAgendaHtml(data: LooseObj): string {
  const tz = getTimeZone(data);
  const today = todayYmdInTz(tz);
  const days = nextNDaysFromYmd(today, LEFT_NAV_AVAILABILITY_DAYS);
  if (!days.length) return '<p class="dash-avail-error">No date range in availability file.</p>';
  const openOnly = true;
  return `<div class="dash-avail-agenda">${days.map((d) => renderAgendaDayHtml(d, data, { openOnly })).join("")}</div>`;
}

/** Loads /out/availability_calendar_latest.json and fills #availability-section. */
export async function refreshAvailabilityPanel(): Promise<void> {
  const section = document.getElementById("availability-section");
  const metaEl = document.getElementById("availability-meta");
  const agendaEl = document.getElementById("availability-agenda");
  if (!section || !metaEl || !agendaEl) return;
  if (!isFeatureEnabled("availability")) return;

  const url = `/out/availability_calendar_latest.json?cb=${Date.now()}`;
  try {
    const res = await fetch(url, { credentials: "same-origin", cache: "no-store" });
    if (!res.ok) {
      section.hidden = false;
      metaEl.innerHTML = `No calendar availability file yet (<code>out/availability_calendar_latest.json</code>, HTTP ${res.status}). It is written when <code>dashboard_server.py</code> runs its scheduled export.`;
      agendaEl.innerHTML = "";
      return;
    }
    const data = (await res.json()) as LooseObj;
    const tz = getTimeZone(data);
    section.hidden = false;
    metaEl.textContent = `Next ${LEFT_NAV_AVAILABILITY_DAYS} days · ${tz}`;
    agendaEl.innerHTML = renderAgendaHtml(data);
  } catch (e) {
    section.hidden = false;
    metaEl.textContent = `Availability load failed: ${e instanceof Error ? e.message : String(e)}`;
    agendaEl.innerHTML = "";
  }
}
