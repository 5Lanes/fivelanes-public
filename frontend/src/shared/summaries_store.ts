import { mergeRows, setDisplaySourceAccount } from "./thread_domain.js";
import { isTodoPlanThreadId } from "./plan_helpers.js";
import type { LaneAreaView, LaneSummaryView, LaneView, LooseObj, ThreadView, PlanView } from "./types.js";
import { str } from "./utils.js";

export const SUMMARIES_BUNDLE_URL = "/api/summaries/bundle";
const SUMMARIES_CACHE_KEY = "fivelanes_summaries_bundle_v6";
const SUMMARIES_ETAG_KEY = "fivelanes_summaries_bundle_etag_v6";
const SUMMARIES_LOCAL_CACHE_KEY = "fivelanes_summaries_bundle_ls_v3";
const SUMMARIES_LOCAL_ETAG_KEY = "fivelanes_summaries_bundle_etag_ls_v3";

function readStorageItem(storage: Storage, key: string): string {
  try {
    return storage.getItem(key) || "";
  } catch {
    return "";
  }
}

function trySetStorageItem(storage: Storage, key: string, value: string): boolean {
  try {
    storage.setItem(key, value);
    return true;
  } catch {
    return false;
  }
}

function removeStorageItem(storage: Storage, key: string): void {
  try {
    storage.removeItem(key);
  } catch {
    /* private mode */
  }
}

function readCachedBundleRaw(): string {
  return (
    readStorageItem(sessionStorage, SUMMARIES_CACHE_KEY) ||
    readStorageItem(localStorage, SUMMARIES_LOCAL_CACHE_KEY)
  );
}

function readCachedBundleEtag(): string {
  const sessionRaw = readStorageItem(sessionStorage, SUMMARIES_CACHE_KEY);
  if (sessionRaw) {
    return readStorageItem(sessionStorage, SUMMARIES_ETAG_KEY);
  }
  const localRaw = readStorageItem(localStorage, SUMMARIES_LOCAL_CACHE_KEY);
  if (localRaw) {
    return readStorageItem(localStorage, SUMMARIES_LOCAL_ETAG_KEY);
  }
  return "";
}

function writeCachedBundle(data: LooseObj, etag: string | null): void {
  const raw = JSON.stringify(data);
  if (trySetStorageItem(sessionStorage, SUMMARIES_CACHE_KEY, raw)) {
    if (etag) trySetStorageItem(sessionStorage, SUMMARIES_ETAG_KEY, etag);
    else removeStorageItem(sessionStorage, SUMMARIES_ETAG_KEY);
  } else {
    removeStorageItem(sessionStorage, SUMMARIES_CACHE_KEY);
    removeStorageItem(sessionStorage, SUMMARIES_ETAG_KEY);
  }
  if (trySetStorageItem(localStorage, SUMMARIES_LOCAL_CACHE_KEY, raw)) {
    if (etag) trySetStorageItem(localStorage, SUMMARIES_LOCAL_ETAG_KEY, etag);
    else removeStorageItem(localStorage, SUMMARIES_LOCAL_ETAG_KEY);
  } else {
    removeStorageItem(localStorage, SUMMARIES_LOCAL_CACHE_KEY);
    removeStorageItem(localStorage, SUMMARIES_LOCAL_ETAG_KEY);
  }
}

let currentData: LooseObj | null = null;
let currentSourceLabel = "";
let currentThreads: ThreadView[] = [];
let bundleMutationGeneration = 0;

export function getBundleMutationGeneration(): number {
  return bundleMutationGeneration;
}

function bumpBundleMutation(): void {
  bundleMutationGeneration++;
}

export function getCurrentData(): LooseObj | null {
  return currentData;
}

export function getCurrentSourceLabel(): string {
  return currentSourceLabel;
}

export function getCurrentThreads(): ThreadView[] {
  return currentThreads;
}

export function setCurrentThreads(threads: ThreadView[]): void {
  currentThreads = threads;
}

export function normalizeBundle(data: LooseObj): LooseObj {
  if (!data || typeof data !== "object") throw new Error("Invalid JSON: expected an object.");
  if (!data.thread_drafts || typeof data.thread_drafts !== "object") data.thread_drafts = {};
  if (!data.meeting_preps || typeof data.meeting_preps !== "object") data.meeting_preps = {};
  if (!Array.isArray(data.lanes)) data.lanes = [];
  if (!Array.isArray(data.lane_areas)) data.lane_areas = [];
  if (!data.lane_threads || typeof data.lane_threads !== "object") data.lane_threads = {};
  if (!data.lane_summaries || typeof data.lane_summaries !== "object") data.lane_summaries = {};
  if (!Array.isArray(data.thread_plans)) data.thread_plans = [];
  if (!data.pending_message_counts || typeof data.pending_message_counts !== "object") {
    data.pending_message_counts = {};
  }
  if (!data.new_since_refresh_counts || typeof data.new_since_refresh_counts !== "object") {
    data.new_since_refresh_counts = {};
  }
  if (typeof data.source_account !== "string") data.source_account = "";
  return data;
}

export function getLaneAreas(data: LooseObj | null): LaneAreaView[] {
  if (!data || !Array.isArray(data.lane_areas)) return [];
  return (data.lane_areas as LooseObj[])
    .map((row) => ({
      id: Number(row.id) || 0,
      name: str(row.name),
      color_index: Number(row.color_index) || 0,
      sort_order: Number(row.sort_order) || 0,
      created_at: str(row.created_at),
      updated_at: str(row.updated_at),
    }))
    .filter((area) => area.id > 0 && area.name)
    .sort((a, b) => a.sort_order - b.sort_order || a.name.localeCompare(b.name));
}

export function getLanes(data: LooseObj | null): LaneView[] {
  if (!data || !Array.isArray(data.lanes)) return [];
  return (data.lanes as LooseObj[])
    .map((row) => ({
      id: Number(row.id) || 0,
      name: str(row.name),
      created_at: str(row.created_at),
      updated_at: str(row.updated_at),
      archived: Boolean(row.archived),
      area_id: row.area_id == null || row.area_id === "" ? null : Number(row.area_id) || null,
    }))
    .filter((lane) => lane.id > 0 && lane.name);
}

export function getTracksForArea(data: LooseObj | null, areaId: number): LaneView[] {
  return getLanes(data).filter((lane) => lane.area_id === areaId);
}

export function threadTrackPath(data: LooseObj | null, threadId: string): string | null {
  if (!data || !data.lane_threads || typeof data.lane_threads !== "object") return null;
  const memberships = data.lane_threads as LooseObj;
  for (const [laneKey, ids] of Object.entries(memberships)) {
    if (!Array.isArray(ids) || !ids.map((id) => str(id)).includes(threadId)) continue;
    const laneId = Number(laneKey) || 0;
    const lane = getLanes(data).find((l) => l.id === laneId);
    if (!lane) continue;
    const area =
      lane.area_id != null ? getLaneAreas(data).find((a) => a.id === lane.area_id) : null;
    if (area) return `${area.name} → ${lane.name}`;
    return lane.name;
  }
  return null;
}

export function threadLaneIds(data: LooseObj | null, threadId: string): number[] {
  if (!data || !data.lane_threads || typeof data.lane_threads !== "object") return [];
  const tid = threadId.trim();
  if (!tid) return [];
  const memberships = data.lane_threads as LooseObj;
  const out: number[] = [];
  for (const [laneKey, ids] of Object.entries(memberships)) {
    if (!Array.isArray(ids) || !ids.map((id) => str(id)).includes(tid)) continue;
    const laneId = Number(laneKey) || 0;
    if (laneId > 0 && !out.includes(laneId)) out.push(laneId);
  }
  return out.sort((a, b) => a - b);
}

export function getLaneThreadIds(data: LooseObj | null, laneId: number): string[] {
  if (!data || !data.lane_threads || typeof data.lane_threads !== "object") return [];
  const bucket = (data.lane_threads as LooseObj)[String(laneId)];
  if (!Array.isArray(bucket)) return [];
  return bucket.map((id) => str(id)).filter(Boolean);
}

export function applyLaneCreated(lane: LaneView): void {
  if (!currentData) return;
  const lanes = Array.isArray(currentData.lanes) ? (currentData.lanes as LooseObj[]) : [];
  if (!lanes.some((row) => Number(row.id) === lane.id)) {
    lanes.push({ ...lane });
    currentData.lanes = lanes;
  }
  const memberships = (currentData.lane_threads ||= {}) as LooseObj;
  if (!Array.isArray(memberships[String(lane.id)])) memberships[String(lane.id)] = [];
}

export function applyLaneThreadMembership(laneId: number, threadId: string, inLane: boolean): void {
  if (!currentData) return;
  const key = String(laneId);
  const memberships = (currentData.lane_threads ||= {}) as LooseObj;
  const existing = Array.isArray(memberships[key])
    ? (memberships[key] as unknown[]).map((id) => str(id)).filter(Boolean)
    : [];
  if (inLane) {
    if (!existing.includes(threadId)) existing.push(threadId);
  } else {
    memberships[key] = existing.filter((id) => id !== threadId);
    bumpBundleMutation();
    syncSummariesBundleCache();
    return;
  }
  memberships[key] = existing;
  bumpBundleMutation();
  syncSummariesBundleCache();
}

export function getLaneSummary(data: LooseObj | null, laneId: number): LaneSummaryView | null {
  if (!data || !data.lane_summaries || typeof data.lane_summaries !== "object") return null;
  const raw = (data.lane_summaries as LooseObj)[String(laneId)];
  if (!raw || typeof raw !== "object") return null;
  const row = raw as LooseObj;
  const summary = str(row.summary);
  const highlights = Array.isArray(row.highlights)
    ? row.highlights.map((x) => str(x)).filter(Boolean)
    : [];
  const current_priorities = Array.isArray(row.current_priorities)
    ? row.current_priorities.map((x) => str(x)).filter(Boolean)
    : [];
  const waiting_on_others = Array.isArray(row.waiting_on_others)
    ? row.waiting_on_others.map((x) => str(x)).filter(Boolean)
    : [];
  const tone_overview = str(row.tone_overview);
  const updated_at = str(row.updated_at);
  if (!summary && !highlights.length && !current_priorities.length && !waiting_on_others.length) {
    return null;
  }
  return {
    summary,
    highlights,
    current_priorities,
    waiting_on_others,
    tone_overview,
    updated_at,
  };
}

export function applyLaneSummary(laneId: number, payload: LooseObj): void {
  if (!currentData) return;
  const bucket = (currentData.lane_summaries ||= {}) as LooseObj;
  bucket[String(laneId)] = {
    summary: str(payload.summary),
    highlights: Array.isArray(payload.highlights) ? payload.highlights : [],
    current_priorities: Array.isArray(payload.current_priorities)
      ? payload.current_priorities
      : [],
    waiting_on_others: Array.isArray(payload.waiting_on_others) ? payload.waiting_on_others : [],
    tone_overview: str(payload.tone_overview),
    updated_at: str(payload.summary_updated_at) || str(payload.updated_at),
  };
}

export function applyLaneArchived(laneId: number, archived: boolean): void {
  if (!currentData || !Array.isArray(currentData.lanes)) return;
  for (const row of currentData.lanes as LooseObj[]) {
    if (Number(row.id) === laneId) {
      row.archived = archived;
      return;
    }
  }
}

export function applyLaneAreaAssigned(laneId: number, areaId: number | null): void {
  if (!currentData || !Array.isArray(currentData.lanes)) return;
  for (const row of currentData.lanes as LooseObj[]) {
    if (Number(row.id) === laneId) {
      row.area_id = areaId;
      return;
    }
  }
}

export function applyLaneAreaCreated(area: LaneAreaView): void {
  if (!currentData) return;
  const areas = Array.isArray(currentData.lane_areas) ? (currentData.lane_areas as LooseObj[]) : [];
  if (!areas.some((row) => Number(row.id) === area.id)) {
    areas.push({ ...area });
    currentData.lane_areas = areas;
  }
}

export function applyLaneRemoved(laneId: number): void {
  if (!currentData) return;
  const key = String(laneId);
  if (Array.isArray(currentData.lanes)) {
    currentData.lanes = (currentData.lanes as LooseObj[]).filter(
      (row) => Number(row.id) !== laneId,
    );
  }
  const memberships = currentData.lane_threads as LooseObj | undefined;
  if (memberships && key in memberships) delete memberships[key];
  const summaries = currentData.lane_summaries as LooseObj | undefined;
  if (summaries && key in summaries) delete summaries[key];
}

export function getThreadPlans(data: LooseObj | null): PlanView[] {
  if (!data || !Array.isArray(data.thread_plans)) return [];
  return (data.thread_plans as LooseObj[])
    .map((row) => ({
      id: Number(row.id) || 0,
      inbox_thread_id: str(row.inbox_thread_id),
      action: str(row.action),
      step_type: str(row.step_type) || "follow up needed",
      by_when: str(row.by_when),
      created_at: str(row.created_at),
      updated_at: str(row.updated_at),
    }))
    .filter((plan) => plan.id > 0 && plan.inbox_thread_id && plan.action);
}

function setThreadHasPlanInBundle(threadId: string, hasPlan: boolean): void {
  if (isTodoPlanThreadId(threadId) || !currentData || !Array.isArray(currentData.summary)) return;
  for (const row of currentData.summary as LooseObj[]) {
    if (str(row.thread_id) === threadId) {
      row.has_plan = hasPlan ? 1 : 0;
    }
  }
}

export function threadHasPlan(threadId: string): boolean {
  if (!currentData || !Array.isArray(currentData.summary)) return false;
  return (currentData.summary as LooseObj[]).some(
    (row) => str(row.thread_id) === threadId && Number(row.has_plan || 0) === 1,
  );
}

export function applyPlanCreated(plan: PlanView): void {
  if (!currentData) return;
  bumpBundleMutation();
  const plans = Array.isArray(currentData.thread_plans)
    ? (currentData.thread_plans as LooseObj[])
    : [];
  if (!plans.some((row) => Number(row.id) === plan.id)) {
    plans.unshift({ ...plan });
    currentData.thread_plans = plans;
  }
  setThreadHasPlanInBundle(plan.inbox_thread_id, true);
  syncSummariesBundleCache();
}

export function applyPlanUpdated(plan: PlanView): void {
  if (!currentData) return;
  bumpBundleMutation();
  const plans = Array.isArray(currentData.thread_plans)
    ? (currentData.thread_plans as LooseObj[])
    : [];
  const idx = plans.findIndex((row) => Number(row.id) === plan.id);
  const oldThread = idx >= 0 ? str(plans[idx].inbox_thread_id) : "";
  const row = { ...plan };
  if (idx >= 0) plans[idx] = row;
  else plans.unshift(row);
  currentData.thread_plans = plans;
  setThreadHasPlanInBundle(plan.inbox_thread_id, true);
  if (oldThread && oldThread !== plan.inbox_thread_id) {
    const stillHas = getThreadPlans(currentData).some((p) => p.inbox_thread_id === oldThread);
    setThreadHasPlanInBundle(oldThread, stillHas);
  }
  syncSummariesBundleCache();
}

export function applyPlanDeleted(planId: number): void {
  if (!currentData || !Array.isArray(currentData.thread_plans)) return;
  bumpBundleMutation();
  const plans = currentData.thread_plans as LooseObj[];
  const removed = plans.find((row) => Number(row.id) === planId);
  const threadId = removed ? str(removed.inbox_thread_id) : "";
  currentData.thread_plans = plans.filter((row) => Number(row.id) !== planId);
  if (threadId) {
    const stillHas = getThreadPlans(currentData).some((p) => p.inbox_thread_id === threadId);
    setThreadHasPlanInBundle(threadId, stillHas);
  }
  syncSummariesBundleCache();
}

export function setBundle(data: LooseObj, sourceLabel: string): void {
  currentData = normalizeBundle(data);
  currentSourceLabel = sourceLabel;
  setDisplaySourceAccount(str(currentData.source_account));
  currentThreads = mergeRows(currentData);
}

/** Apply a network-fetched bundle only if no local plan mutations happened during the fetch. */
export function setBundleFromNetwork(
  data: LooseObj,
  sourceLabel: string,
  mutationGenerationAtFetchStart: number,
): boolean {
  if (mutationGenerationAtFetchStart !== bundleMutationGeneration) return false;
  setBundle(data, sourceLabel);
  return true;
}

export function applySavedThreadDraft(threadId: string, data: LooseObj, responseIntent: string): void {
  if (!currentData) return;
  const bucket = (currentData.thread_drafts ||= {}) as LooseObj;
  bucket[threadId] = {
    response_intent: str(data.response_intent) || responseIntent,
    markdown: str(data.markdown),
    reply_body: str(data.reply_body),
    rationale: str(data.rationale),
    open_questions: Array.isArray(data.open_questions) ? data.open_questions : [],
    saved_at: str(data.draft_updated_at) || str(data.saved_at),
  };
}

export function applyThreadSummary(threadId: string, summary: LooseObj): void {
  if (!currentData || !Array.isArray(currentData.summary)) return;
  const tid = threadId.trim();
  if (!tid) return;
  for (const row of currentData.summary as LooseObj[]) {
    if (str(row.thread_id) !== tid) continue;
    const preserved = {
      thread_id: row.thread_id,
      source_id: row.source_id,
      datetime: row.datetime,
      sender: row.sender,
      subject: row.subject,
      cleaned_content: row.cleaned_content,
      quoted_reply: row.quoted_reply,
      signature: row.signature,
      snoozed: row.snoozed,
      has_plan: row.has_plan,
    };
    Object.assign(row, summary, preserved);
    row.summary_api_error = str(summary.api_error);
  }
}

export function syncSummariesBundleCache(): void {
  if (!currentData) return;
  writeCachedBundle(currentData, null);
}

export function clearSummariesBundleCache(): void {
  removeStorageItem(sessionStorage, SUMMARIES_CACHE_KEY);
  removeStorageItem(sessionStorage, SUMMARIES_ETAG_KEY);
  removeStorageItem(localStorage, SUMMARIES_LOCAL_CACHE_KEY);
  removeStorageItem(localStorage, SUMMARIES_LOCAL_ETAG_KEY);
}

export function readCachedBundle(): { data: LooseObj; label: string } | null {
  const raw = readCachedBundleRaw();
  if (!raw) return null;
  try {
    const data = JSON.parse(raw) as LooseObj;
    if (!data || typeof data !== "object" || Array.isArray(data)) return null;
    return { data, label: `summaries · ${str(data.run_stamp) || "latest"}` };
  } catch {
    return null;
  }
}

export async function loadLatestBundle(): Promise<{ data: LooseObj; label: string }> {
  if (location.protocol === "file:") {
    throw new Error("Summaries load is unavailable from file:// URLs.");
  }
  const headers: Record<string, string> = {};
  const etag = readCachedBundleEtag();
  if (etag) headers["If-None-Match"] = etag;

  const res = await fetch(SUMMARIES_BUNDLE_URL, { credentials: "same-origin", headers });
  if (res.status === 304) {
    const raw = readCachedBundleRaw();
    if (!raw) throw new Error("Summaries bundle not in cache (304 without stored copy)");
    const data = JSON.parse(raw) as LooseObj;
    return { data, label: `summaries · ${str(data.run_stamp) || "latest"}` };
  }
  if (!res.ok) {
    const errBody = (await res.json().catch(() => ({}))) as LooseObj;
    throw new Error(str(errBody.error) || `Could not load summaries (${res.status})`);
  }
  const data = (await res.json()) as LooseObj;
  const cleaned = Array.isArray(data.cleaned) ? data.cleaned : [];
  const summary = Array.isArray(data.summary) ? data.summary : [];
  if (!cleaned.length && !summary.length) {
    throw new Error("No threads found. Track text threads under Texts setup or run the email pipeline.");
  }
  writeCachedBundle(data, res.headers.get("ETag"));
  return { data, label: `summaries · ${str(data.run_stamp) || "latest"}` };
}
