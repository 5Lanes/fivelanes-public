import { mergeRows, setDisplaySourceAccount } from "./thread_domain.js";
import { str } from "./utils.js";
export const SUMMARIES_BUNDLE_URL = "/api/summaries/bundle";
const SUMMARIES_CACHE_KEY = "fivelanes_summaries_bundle_v3";
const SUMMARIES_ETAG_KEY = "fivelanes_summaries_bundle_etag_v3";
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
    if (!Array.isArray(data.people))
        data.people = [];
    if (!data.person_threads || typeof data.person_threads !== "object")
        data.person_threads = {};
    if (!data.person_summaries || typeof data.person_summaries !== "object")
        data.person_summaries = {};
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
        lanes.sort((a, b) => str(a.name).localeCompare(str(b.name)));
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
export function getPeople(data) {
    if (!data || !Array.isArray(data.people))
        return [];
    return data.people
        .map((row) => ({
        id: Number(row.id) || 0,
        name: str(row.name),
        created_at: str(row.created_at),
        updated_at: str(row.updated_at),
    }))
        .filter((person) => person.id > 0 && person.name);
}
export function getPersonThreadIds(data, personId) {
    if (!data || !data.person_threads || typeof data.person_threads !== "object")
        return [];
    const bucket = data.person_threads[String(personId)];
    if (!Array.isArray(bucket))
        return [];
    return bucket.map((id) => str(id)).filter(Boolean);
}
export function getPersonSummary(data, personId) {
    if (!data || !data.person_summaries || typeof data.person_summaries !== "object")
        return null;
    const raw = data.person_summaries[String(personId)];
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
export function applyPersonSummary(personId, payload) {
    if (!currentData)
        return;
    const bucket = (currentData.person_summaries || (currentData.person_summaries = {}));
    bucket[String(personId)] = {
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
export function applyPersonCreated(person) {
    if (!currentData)
        return;
    const people = Array.isArray(currentData.people) ? currentData.people : [];
    if (!people.some((row) => Number(row.id) === person.id)) {
        people.push({ ...person });
        people.sort((a, b) => str(a.name).localeCompare(str(b.name)));
        currentData.people = people;
    }
    const memberships = (currentData.person_threads || (currentData.person_threads = {}));
    if (!Array.isArray(memberships[String(person.id)]))
        memberships[String(person.id)] = [];
}
export function applyPersonThreadMembership(personId, threadId, assigned) {
    if (!currentData)
        return;
    const key = String(personId);
    const memberships = (currentData.person_threads || (currentData.person_threads = {}));
    const existing = Array.isArray(memberships[key])
        ? memberships[key].map((id) => str(id)).filter(Boolean)
        : [];
    if (assigned) {
        if (!existing.includes(threadId))
            existing.push(threadId);
    }
    else {
        memberships[key] = existing.filter((id) => id !== threadId);
        return;
    }
    memberships[key] = existing;
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
    }))
        .filter((plan) => plan.id > 0 && plan.inbox_thread_id && plan.action);
}
function setThreadHasPlanInBundle(threadId, hasPlan) {
    if (!currentData || !Array.isArray(currentData.summary))
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
export function clearSummariesBundleCache() {
    try {
        sessionStorage.removeItem(SUMMARIES_CACHE_KEY);
        sessionStorage.removeItem(SUMMARIES_ETAG_KEY);
    }
    catch {
        /* private mode */
    }
}
export async function loadLatestBundle() {
    if (location.protocol === "file:") {
        throw new Error("Summaries load is unavailable from file:// URLs.");
    }
    const headers = {};
    try {
        const etag = sessionStorage.getItem(SUMMARIES_ETAG_KEY);
        if (etag)
            headers["If-None-Match"] = etag;
    }
    catch {
        /* private mode */
    }
    const res = await fetch(SUMMARIES_BUNDLE_URL, { credentials: "same-origin", headers });
    if (res.status === 304) {
        let raw = "";
        try {
            raw = sessionStorage.getItem(SUMMARIES_CACHE_KEY) || "";
        }
        catch {
            /* private mode */
        }
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
    try {
        sessionStorage.setItem(SUMMARIES_CACHE_KEY, JSON.stringify(data));
        const etag = res.headers.get("ETag");
        if (etag)
            sessionStorage.setItem(SUMMARIES_ETAG_KEY, etag);
    }
    catch {
        /* quota / private mode */
    }
    return { data, label: `summaries · ${str(data.run_stamp) || "latest"}` };
}
