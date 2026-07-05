export function isoToYmdInZone(iso: string, timeZone: string): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date(iso));
}

export function formatTimeRangeInTz(startIso: string, endIso: string, timeZone: string): string {
  const opts: Intl.DateTimeFormatOptions = {
    timeZone,
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  };
  const fmt = new Intl.DateTimeFormat("en-GB", opts);
  const start = new Date(startIso);
  const end = endIso ? new Date(endIso) : start;
  return `${fmt.format(start)}–${fmt.format(end)}`;
}

export function dayHeadingLabelLong(dateKey: string): string {
  const d = new Date(`${dateKey}T12:00:00`);
  return d.toLocaleDateString(undefined, { weekday: "long" });
}

export function dayHeadingLabelShort(dateKey: string): string {
  const d = new Date(`${dateKey}T12:00:00Z`);
  return d.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" });
}

export function todayYmdLocal(): string {
  const now = new Date();
  const yy = now.getFullYear();
  const mm = String(now.getMonth() + 1).padStart(2, "0");
  const dd = String(now.getDate()).padStart(2, "0");
  return `${yy}-${mm}-${dd}`;
}

export function todayYmdInTz(timeZone: string): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date());
}

export function formatTime12InTz(iso: string, timeZone: string): string {
  return new Intl.DateTimeFormat("en-US", {
    timeZone,
    hour: "numeric",
    minute: "2-digit",
    hour12: true,
  }).format(new Date(iso));
}

export function formatTimeRange12InTz(startIso: string, endIso: string, timeZone: string): string {
  const start = formatTime12InTz(startIso, timeZone);
  if (!endIso) return start;
  const end = formatTime12InTz(endIso, timeZone);
  return `${start} – ${end}`;
}

export function addDaysToYmd(ymd: string, deltaDays: number): string | null {
  const [Y, M, D] = ymd.split("-").map(Number);
  if (!Y || !M || !D) return null;
  const u = Date.UTC(Y, M - 1, D + deltaDays, 12, 0, 0);
  return new Date(u).toISOString().slice(0, 10);
}

export function nextNDaysFromYmd(startYmd: string, n: number): string[] {
  const out: string[] = [];
  for (let i = 0; i < n; i += 1) {
    const date = addDaysToYmd(startYmd, i);
    if (date) out.push(date);
  }
  return out;
}

/** UTC instant of local midnight at the start of ``dateKey`` in ``timeZone``. */
export function startOfDayInZone(dateKey: string, timeZone: string): Date {
  const [y, m, d] = dateKey.split("-").map(Number);
  if (!y || !m || !d) return new Date(`${dateKey}T00:00:00`);
  let lo = Date.UTC(y, m - 1, d - 1, 0, 0, 0);
  let hi = Date.UTC(y, m - 1, d + 1, 0, 0, 0);
  while (lo < hi) {
    const mid = Math.floor((lo + hi) / 2);
    const key = isoToYmdInZone(new Date(mid).toISOString(), timeZone);
    if (key < dateKey) lo = mid + 1;
    else hi = mid;
  }
  return new Date(lo);
}

/** UTC instant of the last millisecond of ``dateKey`` in ``timeZone``. */
export function endOfDayInZone(dateKey: string, timeZone: string): Date {
  const next = addDaysToYmd(dateKey, 1);
  if (!next) return new Date(`${dateKey}T23:59:59.999`);
  return new Date(startOfDayInZone(next, timeZone).getTime() - 1);
}
