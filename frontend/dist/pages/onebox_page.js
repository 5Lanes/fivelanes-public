import { refreshDashboardScheduleRail } from "../dashboard_schedule_rail.js";
import { formatDraftReplyMarkdown, messageDirectionClass, partitionThreadsBySnooze, threadEmailSubject, threadLabel, threadMessagesForDisplay, threadMessagesForReply, } from "../shared/thread_domain.js";
import { applyLaneThreadMembership, applySavedThreadDraft, clearSummariesBundleCache, getBundleMutationGeneration, getCurrentData, getCurrentThreads, getLaneAreas, getLaneThreadIds, getLanes, loadLatestBundle, normalizeBundle, setBundleFromNetwork, } from "../shared/summaries_store.js";
import { laneAreaColorVar, sourcePillHtml, threadChannelForThread } from "../shared/source_ui.js";
import { isLikelyOwnEmail } from "../shared/owner_config.js";
import { escapeHtml, formatDate, formatRecipients, formatRelativeShort, str } from "../shared/utils.js";
import { renderDashboardThreadsInline } from "./threads_page.js";
import { setUnreadBadgeCount } from "../shared/native_bridge.js";
function extractEmailAddress(raw) {
    const angle = /<([^<>]+@[^<>]+)>/.exec(raw);
    if (angle)
        return angle[1].trim();
    const bare = raw.trim();
    return /^[^\s<>,]+@[^\s<>,]+$/.test(bare) ? bare : "";
}
function gmailComposeUrl(to, subject, body) {
    const params = new URLSearchParams({ view: "cm", fs: "1", to, su: subject, body });
    return `https://mail.google.com/mail/?${params.toString()}`;
}
const PAGE_HTML = `
<div class="view-onebox">
  <div class="onebox-grid">
    <div class="onebox-main">
      <header class="onebox-header">
        <h2>Onebox</h2>
        <div class="onebox-view-toggle thread-segmented" role="group" aria-label="Onebox view">
          <button type="button" class="nav-mode-btn active" id="onebox-view-mode-onebox" data-onebox-view-mode="onebox">Onebox</button>
          <button type="button" class="nav-mode-btn" id="onebox-view-mode-threads" data-onebox-view-mode="threads">All threads</button>
        </div>
        <button type="button" class="btn btn--default" id="onebox-pull-btn">Pull onebox</button>
      </header>
      <div id="onebox-track-filter" class="onebox-track-filter"></div>
      <div id="onebox-area-tabs" class="onebox-area-tabs" role="tablist" aria-label="Lanes"></div>
      <div id="onebox-tabs" class="onebox-tabs" role="tablist" aria-label="Tracks"></div>
      <div id="onebox-track-toolbar" class="onebox-track-toolbar"></div>
      <div id="onebox-list" class="onebox-list" role="tabpanel"></div>
      <div id="dashboard-threads-root" class="dashboard-threads-embed" hidden></div>
    </div>
    <aside class="schedule-panel meetings-panel" id="dashboard-schedule-rail" aria-label="Schedule"></aside>
  </div>
</div>`;
/** Read state is stored server-side (read_state table) so it syncs across devices; `readKeys`
 * is a local mirror of `data.read_state`, resynced whenever a fresh bundle is loaded. */
function syncReadKeysFromData(data) {
    const stored = (data?.read_state || {});
    readKeys.clear();
    for (const key of Object.keys(stored))
        readKeys.add(key);
}
async function persistReadStateChange(keys, read) {
    if (!keys.length)
        return;
    try {
        const res = await fetch(read ? "/api/read-state/mark" : "/api/read-state/unmark", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ keys }),
        });
        if (!res.ok)
            throw new Error(`Read state update failed (${res.status})`);
    }
    catch (err) {
        console.error(err);
    }
}
function setKeysRead(keys, read) {
    const data = getCurrentData();
    const bucket = (data ? (data.read_state || (data.read_state = {})) : {});
    for (const key of keys) {
        if (read) {
            readKeys.add(key);
            bucket[key] = new Date().toISOString();
        }
        else {
            readKeys.delete(key);
            delete bucket[key];
        }
    }
    void persistReadStateChange(keys, read);
}
let activeAreaId = null;
let activeTrackId = null;
let oneboxViewMode = "onebox";
let showArchivedTracks = false;
let interactionsBound = false;
const readKeys = new Set();
const expandedKeys = new Set();
const draftPanelOpenKeys = new Set();
function messageDatetime(row) {
    return str(row.cleaned?.datetime || row.summary?.datetime);
}
function messageSnippet(row) {
    const c = (row.cleaned || {});
    const text = str(c.cleaned_content) || str(c.raw_text) || "";
    const collapsed = text.replace(/\s+/g, " ").trim();
    return collapsed.length > 400 ? `${collapsed.slice(0, 397)}…` : collapsed;
}
function messageBody(row) {
    const c = (row.cleaned || {});
    return str(c.cleaned_content) || str(c.raw_text) || "(No content)";
}
function messageSender(row) {
    const c = (row.cleaned || {});
    return str(c.sender || c.forwarded_from);
}
function messageRecipients(row) {
    const c = (row.cleaned || {});
    return formatRecipients(c.recipients);
}
function messageSubject(item) {
    const c = (item.row.cleaned || {});
    const s = (item.row.summary || {});
    return str(c.subject || s.subject).trim() || threadEmailSubject(item.thread);
}
function messageKey(item) {
    const c = (item.row.cleaned || {});
    const s = (item.row.summary || {});
    const sourceId = str(c.source_id || s.source_id);
    return `${item.thread.id}::${sourceId || item.datetime}`;
}
function parseCalendarFields(bodyText) {
    const fields = {};
    for (const rawLine of bodyText.split("\n")) {
        const line = rawLine.trim();
        const whenMatch = /^When:\s*(.+?)\s*(?:→|->)\s*(.+)$/.exec(line);
        if (whenMatch) {
            fields.start = whenMatch[1].trim();
            const end = whenMatch[2].trim();
            if (end && end !== "(no end)")
                fields.end = end;
            continue;
        }
        const locMatch = /^Location:\s*(.+)$/.exec(line);
        if (locMatch) {
            fields.location = locMatch[1].trim();
            continue;
        }
        const attMatch = /^Attendees:\s*(.+)$/.exec(line);
        if (attMatch) {
            fields.attendees = attMatch[1].trim();
            continue;
        }
        const linkMatch = /^Link:\s*(.+)$/.exec(line);
        if (linkMatch)
            fields.link = linkMatch[1].trim();
    }
    return fields;
}
function calendarDetailsHtml(fields) {
    const rows = [];
    if (fields.start) {
        const startLabel = formatDate(fields.start);
        const endLabel = fields.end ? formatDate(fields.end) : "";
        rows.push(`<div class="calendar-field"><span class="calendar-field-label">Time</span><span class="calendar-field-value">${escapeHtml(startLabel)}${endLabel ? ` – ${escapeHtml(endLabel)}` : ""}</span></div>`);
    }
    if (fields.location) {
        rows.push(`<div class="calendar-field"><span class="calendar-field-label">Location</span><span class="calendar-field-value">${escapeHtml(fields.location)}</span></div>`);
    }
    if (fields.attendees) {
        rows.push(`<div class="calendar-field"><span class="calendar-field-label">Attendees</span><span class="calendar-field-value">${escapeHtml(fields.attendees)}</span></div>`);
    }
    if (fields.link) {
        rows.push(`<div class="calendar-field"><span class="calendar-field-label">Link</span><a class="calendar-field-value calendar-field-link" href="${escapeHtml(fields.link)}" target="_blank" rel="noopener noreferrer">Open event</a></div>`);
    }
    return rows.length ? `<div class="calendar-details">${rows.join("")}</div>` : "";
}
function laneMessagesSorted(data, laneId, threads) {
    const threadIds = new Set(getLaneThreadIds(data, laneId));
    const sourceAccount = str(data.source_account);
    const items = [];
    for (const thread of threads) {
        if (!threadIds.has(thread.id))
            continue;
        for (const row of threadMessagesForDisplay(thread, sourceAccount)) {
            const datetime = messageDatetime(row);
            if (!datetime)
                continue;
            items.push({ thread, row, datetime });
        }
    }
    return items.sort((a, b) => b.datetime.localeCompare(a.datetime));
}
function trackTabs(data) {
    const threads = getCurrentThreads();
    const tabs = [];
    for (const lane of getLanes(data)) {
        if (Boolean(lane.archived) !== showArchivedTracks)
            continue;
        const items = laneMessagesSorted(data, lane.id, threads);
        if (!items.length)
            continue;
        tabs.push({ lane, items });
    }
    return tabs.sort((a, b) => b.items[0].datetime.localeCompare(a.items[0].datetime));
}
/** Archived (not removed) tracks with a message newer than when they were archived. */
function laneIdsToAutoUnarchive(data) {
    const threads = getCurrentThreads();
    const out = [];
    for (const lane of getLanes(data)) {
        if (!lane.archived || lane.removed)
            continue;
        if (!lane.archived_at)
            continue;
        const items = laneMessagesSorted(data, lane.id, threads);
        if (items.length && items[0].datetime.localeCompare(lane.archived_at) > 0) {
            out.push(lane.id);
        }
    }
    return out;
}
function unreadCount(tab) {
    return tab.items.filter((item) => !readKeys.has(messageKey(item))).length;
}
function usesAreaGrouping(data) {
    return getLaneAreas(data).length > 0 || getLanes(data).some((lane) => lane.area_id != null);
}
function areaGroups(data, tabs) {
    const areas = getLaneAreas(data);
    const groups = areas.map((area) => ({
        id: area.id,
        name: area.name,
        colorIndex: area.color_index,
        tabs: [],
    }));
    const byId = new Map(groups.map((g) => [g.id, g]));
    const unassigned = [];
    for (const tab of tabs) {
        const areaId = tab.lane.area_id;
        const group = areaId != null ? byId.get(areaId) : undefined;
        if (group)
            group.tabs.push(tab);
        else
            unassigned.push(tab);
    }
    const nonEmpty = groups.filter((g) => g.tabs.length);
    if (unassigned.length)
        nonEmpty.push({ id: 0, name: "Unassigned", colorIndex: 0, tabs: unassigned });
    return nonEmpty.sort((a, b) => b.tabs[0].items[0].datetime.localeCompare(a.tabs[0].items[0].datetime));
}
function draftPanelHtml(item) {
    const thread = item.thread;
    const data = getCurrentData();
    const drafts = (data?.thread_drafts || {});
    const saved = drafts[thread.id];
    const savedIntent = saved ? str(saved.response_intent) : "";
    const savedMd = saved ? str(saved.markdown) : "";
    const showSavedOut = Boolean(savedMd);
    const isOpen = draftPanelOpenKeys.has(thread.id);
    const replyTo = threadChannelForThread(thread) === "email" ? extractEmailAddress(messageSender(item.row)) : "";
    const subject = messageSubject(item);
    const replySubject = /^re:/i.test(subject) ? subject : `Re: ${subject}`;
    const gmailLinkHtml = replyTo
        ? `<div class="draft-reply-gmail"><button type="button" class="calendar-field-link onebox-gmail-reply-btn" data-gmail-to="${escapeHtml(replyTo)}" data-gmail-subject="${escapeHtml(replySubject)}">Reply in Gmail</button></div>`
        : "";
    return `<div class="draft-reply-panel"${isOpen ? "" : " hidden"}>
    <p class="draft-reply-hint">What should this reply communicate? (Required — keeps the draft aligned with what you want.)</p>
    <textarea class="draft-intent-input" rows="2" autocomplete="off" placeholder="e.g. I want to meet next week · interested, need more information · don't want to meet">${escapeHtml(savedIntent)}</textarea>
    <div class="draft-reply-actions">
      <button type="button" class="draft-generate-btn" data-draft-thread-id="${escapeHtml(thread.id)}">Generate</button>
    </div>
    <p class="draft-reply-error" hidden></p>
    <label class="draft-output-label">Markdown — copy below</label>
    <textarea class="draft-markdown-output" readonly ${showSavedOut ? "" : "hidden"} rows="12" spellcheck="false">${escapeHtml(savedMd)}</textarea>
    ${gmailLinkHtml}
  </div>`;
}
function planActionSuggestion(item) {
    const sender = messageSender(item.row);
    const senderEmail = extractEmailAddress(sender);
    const senderName = sender.replace(/<[^<>]*>/, "").trim() || senderEmail || "them";
    if (senderEmail && isLikelyOwnEmail(senderEmail)) {
        const recipients = messageRecipients(item.row);
        const toMatch = /To:\s*([^·]+)/.exec(recipients);
        const recipientName = toMatch ? toMatch[1].trim().split(",")[0].trim() : "";
        return `Follow up with ${recipientName || "them"}`;
    }
    return `Respond to ${senderName}`;
}
function messageRowHtml(item) {
    const key = messageKey(item);
    const read = readKeys.has(key);
    const expanded = expandedKeys.has(key);
    const channel = threadChannelForThread(item.thread);
    const isCalendar = channel === "calendar";
    const subject = messageSubject(item);
    const sender = messageSender(item.row);
    const recipients = messageRecipients(item.row);
    const relative = isUpcomingCalendarItem(item)
        ? formatUpcomingRelative(item.datetime)
        : formatRelativeShort(item.datetime);
    const participants = [sender ? `From ${sender}` : "", recipients].filter(Boolean).join(" · ");
    const detailHtml = isCalendar
        ? calendarDetailsHtml(parseCalendarFields(messageBody(item.row)))
        : "";
    const snippetHtml = !isCalendar
        ? (() => {
            const snippet = messageSnippet(item.row);
            return snippet ? `<div class="onebox-row-snippet">${escapeHtml(snippet)}</div>` : "";
        })()
        : "";
    const bodyHtml = !isCalendar && expanded ? `<pre class="onebox-row-body">${escapeHtml(messageBody(item.row))}</pre>` : "";
    const dirClass = messageDirectionClass(sender);
    const removeFromTrackBtn = activeTrackId != null
        ? `<button type="button" class="onebox-remove-from-track-btn" data-track-id="${activeTrackId}" data-thread-id="${escapeHtml(item.thread.id)}" title="Remove thread from this track">Remove</button>`
        : "";
    return `<div class="onebox-row${expanded ? " is-expanded" : ""}${read ? "" : " is-unread"}${dirClass ? ` ${dirClass}` : ""}">
    <button type="button" class="onebox-row-toggle" data-message-key="${escapeHtml(key)}" aria-expanded="${expanded ? "true" : "false"}">
      <div class="onebox-row-top">
        ${read ? "" : `<span class="onebox-row-unread-dot" aria-hidden="true"></span>`}
        ${sourcePillHtml(channel)}
        <span class="onebox-row-subject">${escapeHtml(subject)}</span>
        ${relative ? `<span class="onebox-row-time">${escapeHtml(relative)}</span>` : ""}
      </div>
      ${!isCalendar && participants ? `<div class="onebox-row-participants">${escapeHtml(participants)}</div>` : ""}
      ${detailHtml}
      ${snippetHtml}
    </button>
    ${bodyHtml}
    <div class="onebox-row-actions">
      <button type="button" class="create-plan-btn" data-add-plan-thread-id="${escapeHtml(item.thread.id)}" data-plan-suggestion="${escapeHtml(planActionSuggestion(item))}">Create a plan</button>
      <button type="button" class="draft-reply-toggle" data-draft-thread-id="${escapeHtml(item.thread.id)}">Draft reply</button>
      <button type="button" class="onebox-mark-toggle-btn" data-message-key="${escapeHtml(key)}">${read ? "Mark as unread" : "Mark as read"}</button>
      ${removeFromTrackBtn}
    </div>
    ${draftPanelHtml(item)}
  </div>`;
}
function renderOneboxTrackFilter() {
    const el = document.getElementById("onebox-track-filter");
    if (!el)
        return;
    el.innerHTML = `<div class="thread-control-group">
    <span class="thread-control-label" id="onebox-track-filter-label">Show</span>
    <div class="thread-segmented" role="group" aria-labelledby="onebox-track-filter-label">
      <button type="button" class="onebox-track-filter-btn${showArchivedTracks ? "" : " active"}" data-onebox-track-filter="active" aria-pressed="${showArchivedTracks ? "false" : "true"}">Active</button>
      <button type="button" class="onebox-track-filter-btn${showArchivedTracks ? " active" : ""}" data-onebox-track-filter="archived" aria-pressed="${showArchivedTracks ? "true" : "false"}">Archived</button>
    </div>
  </div>`;
}
function renderOneboxAreaTabs(groups) {
    const tabsEl = document.getElementById("onebox-area-tabs");
    if (!tabsEl)
        return;
    if (groups.length <= 1) {
        tabsEl.innerHTML = "";
        return;
    }
    tabsEl.innerHTML = groups
        .map((group) => {
        const active = group.id === activeAreaId;
        const unread = group.tabs.reduce((sum, tab) => sum + unreadCount(tab), 0);
        const color = laneAreaColorVar(group.colorIndex);
        return `<button type="button" class="lane-tab onebox-area-tab${active ? " is-active" : ""}" role="tab" aria-selected="${active ? "true" : "false"}" id="onebox-area-tab-${group.id}" data-area-id="${group.id}">
        <span class="lane-tab-color" style="background: ${color};"></span>
        <span class="lane-tab-label">${escapeHtml(group.name)}</span>
        ${unread ? `<span class="lane-tab-count">${unread}</span>` : ""}
      </button>`;
    })
        .join("");
}
function renderOneboxTrackTabs(tabs) {
    const tabsEl = document.getElementById("onebox-tabs");
    if (!tabsEl)
        return;
    tabsEl.innerHTML = tabs
        .map((tab) => {
        const active = tab.lane.id === activeTrackId;
        const unread = unreadCount(tab);
        return `<button type="button" class="lane-tab onebox-track-tab${active ? " is-active" : ""}" role="tab" aria-selected="${active ? "true" : "false"}" id="onebox-tab-${tab.lane.id}" aria-controls="onebox-list" data-track-id="${tab.lane.id}">
        <span class="lane-tab-label">${escapeHtml(tab.lane.name)}</span>
        ${unread ? `<span class="lane-tab-count">${unread}</span>` : ""}
      </button>`;
    })
        .join("");
}
function renderOneboxToolbar(tabs) {
    const el = document.getElementById("onebox-track-toolbar");
    if (!el)
        return;
    const active = tabs.find((tab) => tab.lane.id === activeTrackId);
    if (!active) {
        el.innerHTML = "";
        return;
    }
    const unread = unreadCount(active);
    el.innerHTML = `<button type="button" class="btn btn--default onebox-mark-read-btn" data-track-id="${active.lane.id}"${unread ? "" : " disabled"}>Mark all as read</button>
    <button type="button" class="btn btn--ghost onebox-archive-btn" data-track-id="${active.lane.id}" data-archived="${showArchivedTracks ? "true" : "false"}">${showArchivedTracks ? "Unarchive track" : "Archive track"}</button>
    <button type="button" class="btn btn--danger onebox-remove-btn" data-track-id="${active.lane.id}">Remove track</button>`;
}
function isUpcomingCalendarItem(item) {
    if (threadChannelForThread(item.thread) !== "calendar")
        return false;
    const t = new Date(item.datetime).getTime();
    return !Number.isNaN(t) && t > Date.now();
}
/** formatRelativeShort() treats any future date as "Today" (it's built for past-only
 * usage elsewhere); upcoming calendar events need their own forward-looking label. */
function formatUpcomingRelative(iso) {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime()))
        return "";
    const startOfDay = (dt) => new Date(dt.getFullYear(), dt.getMonth(), dt.getDate()).getTime();
    const diffDays = Math.round((startOfDay(d) - startOfDay(new Date())) / 86400000);
    if (diffDays <= 0)
        return "Today";
    if (diffDays === 1)
        return "Tomorrow";
    if (diffDays < 7)
        return `In ${diffDays}d`;
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
function dateGroupKey(iso) {
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? "" : d.toDateString();
}
function dateGroupLabel(iso) {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime()))
        return "";
    const startOfDay = (dt) => new Date(dt.getFullYear(), dt.getMonth(), dt.getDate()).getTime();
    const diffDays = Math.round((startOfDay(new Date()) - startOfDay(d)) / 86400000);
    if (diffDays === 0)
        return "Today";
    if (diffDays === 1)
        return "Yesterday";
    return d.toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric" });
}
function timelineHtmlByDate(items) {
    let lastKey = null;
    const parts = [];
    for (const item of items) {
        const key = dateGroupKey(item.datetime);
        if (key !== lastKey) {
            lastKey = key;
            const label = dateGroupLabel(item.datetime);
            if (label)
                parts.push(`<h3 class="onebox-section-heading onebox-date-heading">${escapeHtml(label)}</h3>`);
        }
        parts.push(messageRowHtml(item));
    }
    return parts.join("");
}
function renderOneboxList(tabs) {
    const listEl = document.getElementById("onebox-list");
    if (!listEl)
        return;
    const active = tabs.find((tab) => tab.lane.id === activeTrackId);
    if (!active) {
        listEl.innerHTML = `<p class="onebox-empty">No tracks with messages yet.</p>`;
        return;
    }
    listEl.setAttribute("aria-labelledby", `onebox-tab-${active.lane.id}`);
    const upcoming = active.items
        .filter(isUpcomingCalendarItem)
        .sort((a, b) => a.datetime.localeCompare(b.datetime));
    const timeline = active.items.filter((item) => !isUpcomingCalendarItem(item));
    const upcomingHtml = upcoming.length
        ? `<div class="onebox-upcoming"><h3 class="onebox-section-heading">Upcoming</h3>${upcoming.map(messageRowHtml).join("")}</div>`
        : "";
    const timelineHtml = timeline.length
        ? timelineHtmlByDate(timeline)
        : `<p class="onebox-empty">No past messages yet.</p>`;
    listEl.innerHTML = upcomingHtml + timelineHtml;
}
function renderOneboxViewToggle() {
    document.getElementById("onebox-view-mode-onebox")?.classList.toggle("active", oneboxViewMode === "onebox");
    document.getElementById("onebox-view-mode-threads")?.classList.toggle("active", oneboxViewMode === "threads");
}
async function renderOneboxOrThreadsView() {
    renderOneboxViewToggle();
    const oneboxSectionIds = ["onebox-track-filter", "onebox-area-tabs", "onebox-tabs", "onebox-track-toolbar", "onebox-list"];
    const threadsRoot = document.getElementById("dashboard-threads-root");
    if (oneboxViewMode === "threads") {
        for (const id of oneboxSectionIds)
            document.getElementById(id)?.setAttribute("hidden", "");
        threadsRoot?.removeAttribute("hidden");
        await renderDashboardThreadsInline();
        return;
    }
    threadsRoot?.setAttribute("hidden", "");
    for (const id of oneboxSectionIds)
        document.getElementById(id)?.removeAttribute("hidden");
    renderOnebox();
}
function renderOnebox() {
    const data = getCurrentData();
    const areaTabsEl = document.getElementById("onebox-area-tabs");
    const trackTabsEl = document.getElementById("onebox-tabs");
    const toolbarEl = document.getElementById("onebox-track-toolbar");
    const listEl = document.getElementById("onebox-list");
    if (!data || !areaTabsEl || !trackTabsEl || !toolbarEl || !listEl)
        return;
    syncReadKeysFromData(data);
    renderOneboxTrackFilter();
    const allTabs = trackTabs(data);
    setUnreadBadgeCount(allTabs.reduce((sum, tab) => sum + unreadCount(tab), 0));
    if (!allTabs.length) {
        areaTabsEl.innerHTML = "";
        trackTabsEl.innerHTML = "";
        toolbarEl.innerHTML = "";
        listEl.innerHTML = `<p class="onebox-empty">${showArchivedTracks ? "No archived tracks." : "No tracks with messages yet."}</p>`;
        activeAreaId = null;
        activeTrackId = null;
        return;
    }
    let tracksInScope = allTabs;
    if (usesAreaGrouping(data)) {
        const groups = areaGroups(data, allTabs);
        const groupIds = new Set(groups.map((g) => g.id));
        if (activeAreaId == null || !groupIds.has(activeAreaId)) {
            activeAreaId = groups[0].id;
        }
        renderOneboxAreaTabs(groups);
        tracksInScope = groups.find((g) => g.id === activeAreaId)?.tabs ?? [];
    }
    else {
        areaTabsEl.innerHTML = "";
        activeAreaId = null;
    }
    const trackIds = new Set(tracksInScope.map((tab) => tab.lane.id));
    if (activeTrackId == null || !trackIds.has(activeTrackId)) {
        activeTrackId = tracksInScope[0]?.lane.id ?? null;
    }
    renderOneboxTrackTabs(tracksInScope);
    renderOneboxToolbar(tracksInScope);
    renderOneboxList(tracksInScope);
}
async function persistLaneArchive(laneId, archived) {
    const res = await fetch("/api/lanes/archive", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ lane_id: laneId, archived }),
    });
    if (!res.ok)
        throw new Error(`Archive failed (${res.status})`);
}
async function persistLaneThreadRemove(laneId, threadId) {
    const res = await fetch("/api/lanes/remove-thread", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ lane_id: laneId, thread_id: threadId }),
    });
    if (!res.ok)
        throw new Error(`Remove thread failed (${res.status})`);
}
async function persistLaneRemove(laneId) {
    const res = await fetch("/api/lanes/remove", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ lane_id: laneId }),
    });
    if (!res.ok)
        throw new Error(`Remove failed (${res.status})`);
}
async function reloadOneboxFromServer() {
    clearSummariesBundleCache();
    const mutationGenAtFetch = getBundleMutationGeneration();
    try {
        const { data, label } = await loadLatestBundle();
        setBundleFromNetwork(normalizeBundle(data), label, mutationGenAtFetch);
    }
    catch {
        /* keep current state; user can retry the action */
    }
    await renderOneboxOrThreadsView();
}
/** Auto-restore archived (not removed) tracks that received a message since archiving. */
async function autoUnarchiveNewActivity(data) {
    const laneIds = laneIdsToAutoUnarchive(data);
    if (!laneIds.length)
        return false;
    await Promise.all(laneIds.map((laneId) => persistLaneArchive(laneId, false).catch((err) => console.error(err))));
    return true;
}
async function refreshOneboxScheduleRail() {
    const data = getCurrentData();
    if (!data)
        return;
    const { active, snoozed } = partitionThreadsBySnooze(getCurrentThreads());
    const trackingThreads = [...active, ...snoozed];
    const meetingPreps = (data.meeting_preps || {});
    await refreshDashboardScheduleRail(trackingThreads, {
        threadLabel,
        meetingPreps,
        onMeetingPrepSaved: (cacheKey, prep) => {
            const current = getCurrentData();
            if (!current)
                return;
            const bucket = (current.meeting_preps || (current.meeting_preps = {}));
            bucket[cacheKey] = prep;
        },
    });
}
export function mountOneboxPage(root) {
    root.innerHTML = PAGE_HTML;
}
export async function renderOneboxPage() {
    const data = getCurrentData();
    if (data && (await autoUnarchiveNewActivity(data))) {
        await reloadOneboxFromServer();
    }
    else {
        await renderOneboxOrThreadsView();
    }
    try {
        await refreshOneboxScheduleRail();
    }
    catch (err) {
        console.error(err);
    }
}
async function requestEmailReplyDraft(threadId, responseIntent, threadSubject) {
    const thread = getCurrentThreads().find((t) => t.id === threadId);
    if (!thread)
        throw new Error("Thread not found.");
    const res = await fetch("/api/claude/email-reply", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
            thread_id: threadId,
            response_intent: responseIntent,
            thread_subject: threadSubject,
            messages: threadMessagesForReply(thread),
        }),
    });
    const data = (await res.json());
    if (!res.ok || data.ok === false) {
        const msg = str(data.error) || `Request failed (${res.status})`;
        throw new Error(msg);
    }
    return data;
}
function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
}
async function fetchOneboxPullStatus() {
    const res = await fetch("/api/pipeline/inbox-pull-status", { credentials: "same-origin" });
    return (await res.json().catch(() => ({})));
}
async function runOneboxPull() {
    const btn = document.getElementById("onebox-pull-btn");
    if (btn) {
        btn.disabled = true;
        btn.textContent = "Pulling…";
    }
    try {
        const res = await fetch("/api/pipeline/run-inbox-pull", { method: "POST" });
        const body = (await res.json().catch(() => ({})));
        if (!res.ok && res.status !== 409) {
            throw new Error(str(body.error) || `Pull failed (${res.status})`);
        }
        for (let i = 0; i < 120; i++) {
            await sleep(2000);
            const status = await fetchOneboxPullStatus();
            if (!status.running) {
                if (status.error)
                    throw new Error(str(status.error));
                break;
            }
        }
        await reloadOneboxFromServer();
    }
    catch (err) {
        console.error(err);
    }
    finally {
        if (btn) {
            btn.disabled = false;
            btn.textContent = "Pull onebox";
        }
    }
}
export function bindOneboxInteractions() {
    if (interactionsBound)
        return;
    interactionsBound = true;
    document.addEventListener("click", (ev) => {
        const pullBtn = ev.target?.closest("#onebox-pull-btn");
        if (pullBtn && !pullBtn.disabled) {
            void runOneboxPull();
            return;
        }
        const target = ev.target;
        if (!target || !target.closest(".view-onebox"))
            return;
        const viewModeBtn = target.closest("[data-onebox-view-mode]");
        if (viewModeBtn) {
            const mode = viewModeBtn.dataset.oneboxViewMode;
            if (!mode || mode === oneboxViewMode)
                return;
            oneboxViewMode = mode;
            void renderOneboxOrThreadsView();
            return;
        }
        const areaTab = target.closest(".onebox-area-tab");
        if (areaTab) {
            const areaId = Number(areaTab.dataset.areaId);
            if (Number.isNaN(areaId) || areaId === activeAreaId)
                return;
            activeAreaId = areaId;
            activeTrackId = null;
            renderOnebox();
            return;
        }
        const trackTab = target.closest(".onebox-track-tab");
        if (trackTab) {
            const trackId = Number(trackTab.dataset.trackId) || 0;
            if (!trackId || trackId === activeTrackId)
                return;
            activeTrackId = trackId;
            renderOnebox();
            return;
        }
        const addPlanBtn = target.closest("button.create-plan-btn");
        if (addPlanBtn) {
            const threadId = str(addPlanBtn.dataset.addPlanThreadId);
            const suggestion = str(addPlanBtn.dataset.planSuggestion);
            if (!threadId)
                return;
            void (async () => {
                const { openDashboardAddPlanForThread } = await import("./dashboard_page.js");
                await openDashboardAddPlanForThread(threadId);
                const actionInput = document.getElementById("schedule-plan-action-input");
                if (actionInput && !actionInput.value.trim() && suggestion)
                    actionInput.value = suggestion;
            })();
            return;
        }
        const gmailReplyBtn = target.closest(".onebox-gmail-reply-btn");
        if (gmailReplyBtn) {
            const to = str(gmailReplyBtn.dataset.gmailTo);
            const subject = str(gmailReplyBtn.dataset.gmailSubject);
            if (!to)
                return;
            const row = gmailReplyBtn.closest(".onebox-row");
            const draftOut = row?.querySelector(".draft-markdown-output");
            const body = draftOut && !draftOut.hidden ? draftOut.value : "";
            window.open(gmailComposeUrl(to, subject, body), "_blank", "noopener");
            return;
        }
        const draftToggle = target.closest("button.draft-reply-toggle");
        if (draftToggle) {
            const threadId = str(draftToggle.dataset.draftThreadId);
            if (!threadId)
                return;
            const opening = !draftPanelOpenKeys.has(threadId);
            if (opening)
                draftPanelOpenKeys.add(threadId);
            else
                draftPanelOpenKeys.delete(threadId);
            renderOnebox();
            if (opening) {
                document
                    .querySelector(".draft-reply-panel:not([hidden]) .draft-intent-input")
                    ?.focus();
            }
            return;
        }
        const draftGen = target.closest("button.draft-generate-btn");
        if (draftGen) {
            void (async () => {
                const threadId = str(draftGen.dataset.draftThreadId);
                const row = draftGen.closest(".onebox-row");
                const intentEl = row?.querySelector(".draft-intent-input");
                const intent = intentEl?.value.trim() ?? "";
                const outEl = row?.querySelector(".draft-markdown-output");
                const errEl = row?.querySelector(".draft-reply-error");
                if (!threadId || !outEl)
                    return;
                if (!intent) {
                    if (errEl) {
                        errEl.textContent = "Add what the reply should say (required).";
                        errEl.hidden = false;
                    }
                    return;
                }
                if (errEl)
                    errEl.hidden = true;
                draftGen.disabled = true;
                const thread = getCurrentThreads().find((t) => t.id === threadId);
                const subj = thread ? threadEmailSubject(thread) : "";
                try {
                    const payload = await requestEmailReplyDraft(threadId, intent, subj);
                    const markdown = str(payload.markdown) || formatDraftReplyMarkdown(payload);
                    outEl.value = markdown;
                    outEl.hidden = false;
                    applySavedThreadDraft(threadId, payload, intent);
                }
                catch (e) {
                    const msg = e instanceof Error ? e.message : String(e);
                    outEl.value = ["## Draft reply", "", `**Error:** ${msg}`, ""].join("\n");
                    outEl.hidden = false;
                }
                finally {
                    draftGen.disabled = false;
                }
            })();
            return;
        }
        const rowToggle = target.closest(".onebox-row-toggle");
        if (rowToggle) {
            const key = str(rowToggle.dataset.messageKey);
            if (!key)
                return;
            setKeysRead([key], true);
            if (expandedKeys.has(key))
                expandedKeys.delete(key);
            else
                expandedKeys.add(key);
            renderOnebox();
            return;
        }
        const markToggleBtn = target.closest(".onebox-mark-toggle-btn");
        if (markToggleBtn) {
            const key = str(markToggleBtn.dataset.messageKey);
            if (!key)
                return;
            if (readKeys.has(key)) {
                expandedKeys.delete(key);
                setKeysRead([key], false);
            }
            else {
                setKeysRead([key], true);
            }
            renderOnebox();
            return;
        }
        const markReadBtn = target.closest(".onebox-mark-read-btn");
        if (markReadBtn) {
            const laneId = Number(markReadBtn.dataset.trackId) || 0;
            const data = getCurrentData();
            if (!laneId || !data)
                return;
            const tab = trackTabs(data).find((t) => t.lane.id === laneId);
            if (!tab)
                return;
            setKeysRead(tab.items.map((item) => messageKey(item)), true);
            renderOnebox();
            return;
        }
        const archiveBtn = target.closest(".onebox-archive-btn");
        if (archiveBtn) {
            const laneId = Number(archiveBtn.dataset.trackId) || 0;
            const archived = archiveBtn.dataset.archived === "true";
            if (!laneId)
                return;
            archiveBtn.disabled = true;
            activeTrackId = null;
            void (async () => {
                try {
                    await persistLaneArchive(laneId, !archived);
                    await reloadOneboxFromServer();
                }
                catch (err) {
                    console.error(err);
                    archiveBtn.disabled = false;
                }
            })();
            return;
        }
        const trackFilterBtn = target.closest(".onebox-track-filter-btn");
        if (trackFilterBtn) {
            const archived = trackFilterBtn.dataset.oneboxTrackFilter === "archived";
            if (archived === showArchivedTracks)
                return;
            showArchivedTracks = archived;
            activeAreaId = null;
            activeTrackId = null;
            renderOnebox();
            return;
        }
        const removeFromTrackBtn = target.closest(".onebox-remove-from-track-btn");
        if (removeFromTrackBtn) {
            const laneId = Number(removeFromTrackBtn.dataset.trackId) || 0;
            const threadId = str(removeFromTrackBtn.dataset.threadId);
            if (!laneId || !threadId)
                return;
            removeFromTrackBtn.disabled = true;
            void (async () => {
                applyLaneThreadMembership(laneId, threadId, false);
                renderOnebox();
                try {
                    await persistLaneThreadRemove(laneId, threadId);
                    await reloadOneboxFromServer();
                }
                catch (err) {
                    applyLaneThreadMembership(laneId, threadId, true);
                    renderOnebox();
                    console.error(err);
                    window.alert(err instanceof Error ? err.message : String(err));
                }
            })();
            return;
        }
        const removeBtn = target.closest(".onebox-remove-btn");
        if (removeBtn) {
            const laneId = Number(removeBtn.dataset.trackId) || 0;
            if (!laneId)
                return;
            if (!window.confirm("Remove this track from the onebox? It will stop being checked for new messages and can only be restored from Sources.")) {
                return;
            }
            removeBtn.disabled = true;
            activeTrackId = null;
            void (async () => {
                try {
                    await persistLaneRemove(laneId);
                    await reloadOneboxFromServer();
                }
                catch (err) {
                    console.error(err);
                    removeBtn.disabled = false;
                }
            })();
            return;
        }
    });
}
