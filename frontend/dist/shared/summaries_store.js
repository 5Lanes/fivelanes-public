import { mergeRows, setDisplaySourceAccount } from "./thread_domain.js";
import { isTodoPlanThreadId } from "./plan_helpers.js";
import { str } from "./utils.js";
export const SUMMARIES_BUNDLE_URL = "/api/summaries/bundle";
const SUMMARIES_CACHE_KEY = "fivelanes_summaries_bundle_v4";
const SUMMARIES_ETAG_KEY = "fivelanes_summaries_bundle_etag_v4";
const SUMMARIES_LOCAL_CACHE_KEY = "fivelanes_summaries_bundle_ls_v1";
const SUMMARIES_LOCAL_ETAG_KEY = "fivelanes_summaries_bundle_etag_ls_v1";
function readStorageItem(storage, key) {
    try {
        return storage.getItem(key) || "";
    }
    catch {
        return "";
    }
}
function trySetStorageItem(storage, key, value) {
    try {
        storage.setItem(key, value);
        return true;
    }
    catch {
        return false;
    }
}
function removeStorageItem(storage, key) {
    try {
        storage.removeItem(key);
    }
    catch {
        /* private mode */
    }
}
function readCachedBundleRaw() {
    return (readStorageItem(sessionStorage, SUMMARIES_CACHE_KEY) ||
        readStorageItem(localStorage, SUMMARIES_LOCAL_CACHE_KEY));
}
function readCachedBundleEtag() {
    if (!readCachedBundleRaw()) {
        removeStorageItem(sessionStorage, SUMMARIES_ETAG_KEY);
        removeStorageItem(localStorage, SUMMARIES_LOCAL_ETAG_KEY);
        return "";
    }
    return (readStorageItem(sessionStorage, SUMMARIES_ETAG_KEY) ||
        readStorageItem(localStorage, SUMMARIES_LOCAL_ETAG_KEY));
}
function writeCachedBundle(data, etag) {
    const raw = JSON.stringify(data);
    if (trySetStorageItem(sessionStorage, SUMMARIES_CACHE_KEY, raw)) {
        if (etag)
            trySetStorageItem(sessionStorage, SUMMARIES_ETAG_KEY, etag);
        else
            removeStorageItem(sessionStorage, SUMMARIES_ETAG_KEY);
    }
    else {
        removeStorageItem(sessionStorage, SUMMARIES_CACHE_KEY);
        removeStorageItem(sessionStorage, SUMMARIES_ETAG_KEY);
    }
    if (trySetStorageItem(localStorage, SUMMARIES_LOCAL_CACHE_KEY, raw)) {
        if (etag)
            trySetStorageItem(localStorage, SUMMARIES_LOCAL_ETAG_KEY, etag);
        else
            removeStorageItem(localStorage, SUMMARIES_LOCAL_ETAG_KEY);
    }
    else {
        removeStorageItem(localStorage, SUMMARIES_LOCAL_CACHE_KEY);
        removeStorageItem(localStorage, SUMMARIES_LOCAL_ETAG_KEY);
    }
}
let currentData = null;
let currentSourceLabel = "";
let currentThreads = [];
export function getCurrentData() {
    return currentData;
}
export function getCurrentSourceLabel() {
    return currentSourceLabel;
}
export function getCurrentThreads() {
    return currentThreads;
}
export function setCurrentThreads(threads) {
    currentThreads = threads;
}
export function normalizeBundle(data) {
    if (!data || typeof data !== "object")
        throw new Error("Invalid JSON: expected an object.");
    if (!data.thread_drafts || typeof data.thread_drafts !== "object")
        data.thread_drafts = {};
    if (!data.meeting_preps || typeof data.meeting_preps !== "object")
        data.meeting_preps = {};
    if (!Array.isArray(data.lanes))
        data.lanes = [];
    if (!data.lane_threads || typeof data.lane_threads !== "object")
        data.lane_threads = {};
    if (!data.lane_summaries || typeof data.lane_summaries !== "object")
        data.lane_summaries = {};
    if (!Array.isArray(data.thread_plans))
        data.thread_plans = [];
    if (!data.pending_message_counts || typeof data.pending_message_counts !== "object") {
        data.pending_message_counts = {};
    }
    if (typeof data.source_account !== "string")
        data.source_account = "";
    return data;
}
export function getLanes(data) {
    if (!data || !Array.isArray(data.lanes))
        return [];
    return data.lanes
        .map((row) => ({
        id: Number(row.id) || 0,
        name: str(row.name),
        created_at: str(row.created_at),
        updated_at: str(row.updated_at),
    }))
        .filter((lane) => lane.id > 0 && lane.name);
}
export function getLaneThreadIds(data, laneId) {
    if (!data || !data.lane_threads || typeof data.lane_threads !== "object")
        return [];
    const bucket = data.lane_threads[String(laneId)];
    if (!Array.isArray(bucket))
        return [];
    return bucket.map((id) => str(id)).filter(Boolean);
}
export function applyLaneCreated(lane) {
    if (!currentData)
        return;
    const lanes = Array.isArray(currentData.lanes) ? currentData.lanes : [];
    if (!lanes.some((row) => Number(row.id) === lane.id)) {
        lanes.push({ ...lane });
        currentData.lanes = lanes;
    }
    const memberships = (currentData.lane_threads || (currentData.lane_threads = {}));
    if (!Array.isArray(memberships[String(lane.id)]))
        memberships[String(lane.id)] = [];
}
export function applyLaneThreadMembership(laneId, threadId, inLane) {
    if (!currentData)
        return;
    const key = String(laneId);
    const memberships = (currentData.lane_threads || (currentData.lane_threads = {}));
    const existing = Array.isArray(memberships[key])
        ? memberships[key].map((id) => str(id)).filter(Boolean)
        : [];
    if (inLane) {
        if (!existing.includes(threadId))
            existing.push(threadId);
    }
    else {
        memberships[key] = existing.filter((id) => id !== threadId);
        return;
    }
    memberships[key] = existing;
}
export function getLaneSummary(data, laneId) {
    if (!data || !data.lane_summaries || typeof data.lane_summaries !== "object")
        return null;
    const raw = data.lane_summaries[String(laneId)];
    if (!raw || typeof raw !== "object")
        return null;
    const row = raw;
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
export function applyLaneSummary(laneId, payload) {
    if (!currentData)
        return;
    const bucket = (currentData.lane_summaries || (currentData.lane_summaries = {}));
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
export function applyLaneRemoved(laneId) {
    if (!currentData)
        return;
    const key = String(laneId);
    if (Array.isArray(currentData.lanes)) {
        currentData.lanes = currentData.lanes.filter((row) => Number(row.id) !== laneId);
    }
    const memberships = currentData.lane_threads;
    if (memberships && key in memberships)
        delete memberships[key];
    const summaries = currentData.lane_summaries;
    if (summaries && key in summaries)
        delete summaries[key];
}
export function getThreadPlans(data) {
    if (!data || !Array.isArray(data.thread_plans))
        return [];
    return data.thread_plans
        .map((row) => ({
        id: Number(row.id) || 0,
        inbox_thread_id: str(row.inbox_thread_id),
        action: str(row.action),
        step_type: str(row.step_type) || "follow up needed",
        by_when: str(row.by_when),
        created_at: str(row.created_at),
        updated_at: str(row.updated_at),
        activity_checkpoint: str(row.activity_checkpoint),
        needs_completion_check: Boolean(row.needs_completion_check),
    }))
        .filter((plan) => plan.id > 0 && plan.inbox_thread_id && plan.action);
}
function setThreadHasPlanInBundle(threadId, hasPlan) {
    if (isTodoPlanThreadId(threadId) || !currentData || !Array.isArray(currentData.summary))
        return;
    for (const row of currentData.summary) {
        if (str(row.thread_id) === threadId) {
            row.has_plan = hasPlan ? 1 : 0;
        }
    }
}
export function threadHasPlan(threadId) {
    if (!currentData || !Array.isArray(currentData.summary))
        return false;
    return currentData.summary.some((row) => str(row.thread_id) === threadId && Number(row.has_plan || 0) === 1);
}
export function applyPlanCreated(plan) {
    if (!currentData)
        return;
    const plans = Array.isArray(currentData.thread_plans)
        ? currentData.thread_plans
        : [];
    if (!plans.some((row) => Number(row.id) === plan.id)) {
        plans.unshift({ ...plan });
        currentData.thread_plans = plans;
    }
    setThreadHasPlanInBundle(plan.inbox_thread_id, true);
}
export function applyPlanUpdated(plan) {
    if (!currentData)
        return;
    const plans = Array.isArray(currentData.thread_plans)
        ? currentData.thread_plans
        : [];
    const idx = plans.findIndex((row) => Number(row.id) === plan.id);
    const oldThread = idx >= 0 ? str(plans[idx].inbox_thread_id) : "";
    const row = { ...plan };
    if (idx >= 0)
        plans[idx] = row;
    else
        plans.unshift(row);
    currentData.thread_plans = plans;
    setThreadHasPlanInBundle(plan.inbox_thread_id, true);
    if (oldThread && oldThread !== plan.inbox_thread_id) {
        const stillHas = getThreadPlans(currentData).some((p) => p.inbox_thread_id === oldThread);
        setThreadHasPlanInBundle(oldThread, stillHas);
    }
}
export function applyPlanDeleted(planId) {
    if (!currentData || !Array.isArray(currentData.thread_plans))
        return;
    const plans = currentData.thread_plans;
    const removed = plans.find((row) => Number(row.id) === planId);
    const threadId = removed ? str(removed.inbox_thread_id) : "";
    currentData.thread_plans = plans.filter((row) => Number(row.id) !== planId);
    if (threadId) {
        const stillHas = getThreadPlans(currentData).some((p) => p.inbox_thread_id === threadId);
        setThreadHasPlanInBundle(threadId, stillHas);
    }
}
export function applyPlanCompletionAcknowledged(plan) {
    applyPlanUpdated(plan);
}
export function applyPlanCompletionDismissed(planId) {
    if (!currentData || !Array.isArray(currentData.thread_plans))
        return;
    for (const row of currentData.thread_plans) {
        if (Number(row.id) === planId) {
            row.needs_completion_check = false;
        }
    }
}
export function setBundle(data, sourceLabel) {
    currentData = normalizeBundle(data);
    currentSourceLabel = sourceLabel;
    setDisplaySourceAccount(str(currentData.source_account));
    currentThreads = mergeRows(currentData);
}
export function applySavedThreadDraft(threadId, data, responseIntent) {
    if (!currentData)
        return;
    const bucket = (currentData.thread_drafts || (currentData.thread_drafts = {}));
    bucket[threadId] = {
        response_intent: str(data.response_intent) || responseIntent,
        markdown: str(data.markdown),
        reply_body: str(data.reply_body),
        rationale: str(data.rationale),
        open_questions: Array.isArray(data.open_questions) ? data.open_questions : [],
        saved_at: str(data.draft_updated_at) || str(data.saved_at),
    };
}
export function applyThreadSummary(threadId, summary) {
    if (!currentData || !Array.isArray(currentData.summary))
        return;
    const tid = threadId.trim();
    if (!tid)
        return;
    for (const row of currentData.summary) {
        if (str(row.thread_id) !== tid)
            continue;
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
export function clearSummariesBundleCache() {
    removeStorageItem(sessionStorage, SUMMARIES_CACHE_KEY);
    removeStorageItem(sessionStorage, SUMMARIES_ETAG_KEY);
    removeStorageItem(localStorage, SUMMARIES_LOCAL_CACHE_KEY);
    removeStorageItem(localStorage, SUMMARIES_LOCAL_ETAG_KEY);
}
export function readCachedBundle() {
    const raw = readCachedBundleRaw();
    if (!raw)
        return null;
    try {
        const data = JSON.parse(raw);
        if (!data || typeof data !== "object" || Array.isArray(data))
            return null;
        return { data, label: `summaries · ${str(data.run_stamp) || "latest"}` };
    }
    catch {
        return null;
    }
}
export function bundleChanged(prev, next) {
    return (str(prev.run_stamp) !== str(next.data.run_stamp) ||
        str(prev.generated_at) !== str(next.data.generated_at));
}
export async function loadLatestBundle() {
    if (location.protocol === "file:") {
        throw new Error("Summaries load is unavailable from file:// URLs.");
    }
    const headers = {};
    const etag = readCachedBundleEtag();
    if (etag)
        headers["If-None-Match"] = etag;
    const res = await fetch(SUMMARIES_BUNDLE_URL, { credentials: "same-origin", headers });
    if (res.status === 304) {
        const raw = readCachedBundleRaw();
        if (!raw)
            throw new Error("Summaries bundle not in cache (304 without stored copy)");
        const data = JSON.parse(raw);
        return { data, label: `summaries · ${str(data.run_stamp) || "latest"}` };
    }
    if (!res.ok) {
        const errBody = (await res.json().catch(() => ({})));
        throw new Error(str(errBody.error) || `Could not load summaries (${res.status})`);
    }
    const data = (await res.json());
    const cleaned = Array.isArray(data.cleaned) ? data.cleaned : [];
    const summary = Array.isArray(data.summary) ? data.summary : [];
    if (!cleaned.length && !summary.length) {
        throw new Error("No threads found. Track text threads under Texts setup or run the email pipeline.");
    }
    writeCachedBundle(data, res.headers.get("ETag"));
    return { data, label: `summaries · ${str(data.run_stamp) || "latest"}` };
}
