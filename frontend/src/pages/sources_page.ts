import { bindEmailCapturePanel, renderEmailCapturePanel } from "../email_capture_panel.js";
import { isFeatureEnabled } from "../shared/features.js";
import {
  bindCalendarSetupInteractions,
  mountCalendarSetupPage,
  renderCalendarSetupPage,
} from "./calendar_setup_page.js";
import {
  bindLinkedinSetupInteractions,
  mountLinkedinSetupPage,
  renderLinkedinSetupPage,
} from "./linkedin_setup_page.js";
import {
  bindMeetRecordingsSetupInteractions,
  mountMeetRecordingsSetupPage,
  renderMeetRecordingsSetupPage,
} from "./meet_recordings_setup_page.js";
import {
  bindSlackSetupInteractions,
  mountSlackSetupPage,
  renderSlackSetupPage,
} from "./slack_setup_page.js";
import {
  bindTextsSetupInteractions,
  mountTextsSetupPage,
  renderTextsSetupPage,
} from "./texts_setup_page.js";
import {
  bindRemovedTracksInteractions,
  mountRemovedTracksPanel,
  renderRemovedTracksPanel,
} from "./removed_tracks_panel.js";

const SOURCES_TAB_KEY = "fivelanes_sources_tab";
const VALID_SOURCES = ["email", "texts", "slack", "linkedin", "meet", "calendar", "removed"] as const;
type SourceTab = (typeof VALID_SOURCES)[number];

const PAGE_HTML = `
<div class="view-sources">
  <div class="source-tab-bar" role="tablist" aria-label="Sources">
    <button type="button" class="source-tab is-active" role="tab" data-source="email" aria-controls="source-panel-email">Email</button>
    <button type="button" class="source-tab" role="tab" data-source="texts" data-feature="texts" aria-controls="source-panel-texts">Texts</button>
    <button type="button" class="source-tab" role="tab" data-source="slack" data-feature="slack" aria-controls="source-panel-slack">Slack</button>
    <button type="button" class="source-tab" role="tab" data-source="linkedin" data-feature="linkedin" aria-controls="source-panel-linkedin">LinkedIn</button>
    <button type="button" class="source-tab" role="tab" data-source="meet" data-feature="meet_recordings" aria-controls="source-panel-meet">Meet notes</button>
    <button type="button" class="source-tab" role="tab" data-source="calendar" data-feature="calendar_events" aria-controls="source-panel-calendar">Calendar</button>
    <button type="button" class="source-tab" role="tab" data-source="removed" aria-controls="source-panel-removed">Removed tracks</button>
  </div>
  <div class="source-tab-panel is-active" role="tabpanel" id="source-panel-email" data-source="email"></div>
  <div class="source-tab-panel" role="tabpanel" id="source-panel-texts" data-source="texts" hidden></div>
  <div class="source-tab-panel" role="tabpanel" id="source-panel-slack" data-source="slack" hidden></div>
  <div class="source-tab-panel" role="tabpanel" id="source-panel-linkedin" data-source="linkedin" hidden></div>
  <div class="source-tab-panel" role="tabpanel" id="source-panel-meet" data-source="meet" hidden></div>
  <div class="source-tab-panel" role="tabpanel" id="source-panel-calendar" data-source="calendar" hidden></div>
  <div class="source-tab-panel" role="tabpanel" id="source-panel-removed" data-source="removed" hidden></div>
</div>`;

let panelsInitialized = false;
let tabBarBound = false;

function sourceFromLocation(): SourceTab {
  const hash = location.hash.replace(/^#/, "").trim();
  if (VALID_SOURCES.includes(hash as SourceTab)) return hash as SourceTab;
  try {
    const stored = localStorage.getItem(SOURCES_TAB_KEY);
    if (stored && VALID_SOURCES.includes(stored as SourceTab)) return stored as SourceTab;
  } catch {
    /* ignore */
  }
  return "email";
}

function sourceTabEnabled(source: SourceTab): boolean {
  if (source === "email") return true;
  if (source === "texts") return isFeatureEnabled("texts");
  if (source === "slack") return isFeatureEnabled("slack");
  if (source === "linkedin") return isFeatureEnabled("linkedin");
  if (source === "meet") return isFeatureEnabled("meet_recordings");
  if (source === "removed") return true;
  return isFeatureEnabled("calendar_events");
}

function setActiveSourceTab(source: SourceTab): void {
  document.querySelectorAll<HTMLElement>(".view-sources .source-tab").forEach((tab) => {
    const id = tab.dataset.source as SourceTab;
    const enabled = sourceTabEnabled(id);
    tab.hidden = !enabled;
    const active = id === source && enabled;
    tab.classList.toggle("is-active", active);
    tab.setAttribute("aria-selected", active ? "true" : "false");
  });
  document.querySelectorAll<HTMLElement>(".view-sources .source-tab-panel").forEach((panel) => {
    const active = (panel.dataset.source as SourceTab) === source;
    panel.classList.toggle("is-active", active);
    panel.toggleAttribute("hidden", !active);
  });
  try {
    localStorage.setItem(SOURCES_TAB_KEY, source);
  } catch {
    /* ignore */
  }
  if (location.hash.replace(/^#/, "") !== source) {
    history.replaceState(null, "", `/sources#${source}`);
  }
}

async function ensureSourcePanels(): Promise<void> {
  if (panelsInitialized) return;
  panelsInitialized = true;

  const emailPanel = document.getElementById("source-panel-email");
  if (emailPanel) {
    await renderEmailCapturePanel(emailPanel);
    bindEmailCapturePanel(emailPanel);
  }

  const textsPanel = document.getElementById("source-panel-texts");
  if (textsPanel && isFeatureEnabled("texts")) {
    mountTextsSetupPage(textsPanel);
    bindTextsSetupInteractions();
  }

  const slackPanel = document.getElementById("source-panel-slack");
  if (slackPanel && isFeatureEnabled("slack")) {
    mountSlackSetupPage(slackPanel);
    bindSlackSetupInteractions();
  }

  const linkedinPanel = document.getElementById("source-panel-linkedin");
  if (linkedinPanel && isFeatureEnabled("linkedin")) {
    mountLinkedinSetupPage(linkedinPanel);
    bindLinkedinSetupInteractions();
  }

  const meetPanel = document.getElementById("source-panel-meet");
  if (meetPanel && isFeatureEnabled("meet_recordings")) {
    mountMeetRecordingsSetupPage(meetPanel);
    bindMeetRecordingsSetupInteractions();
  }

  const calendarPanel = document.getElementById("source-panel-calendar");
  if (calendarPanel && isFeatureEnabled("calendar_events")) {
    mountCalendarSetupPage(calendarPanel);
    bindCalendarSetupInteractions();
  }

  const removedPanel = document.getElementById("source-panel-removed");
  if (removedPanel) {
    mountRemovedTracksPanel(removedPanel);
    bindRemovedTracksInteractions();
  }
}

async function refreshSourcePanel(source: SourceTab): Promise<void> {
  if (source === "email") {
    const panel = document.getElementById("source-panel-email");
    if (panel) await renderEmailCapturePanel(panel);
    return;
  }
  if (source === "texts" && isFeatureEnabled("texts")) {
    await renderTextsSetupPage();
    return;
  }
  if (source === "slack" && isFeatureEnabled("slack")) {
    await renderSlackSetupPage();
    return;
  }
  if (source === "linkedin" && isFeatureEnabled("linkedin")) {
    await renderLinkedinSetupPage();
    return;
  }
  if (source === "meet" && isFeatureEnabled("meet_recordings")) {
    await renderMeetRecordingsSetupPage();
    return;
  }
  if (source === "calendar" && isFeatureEnabled("calendar_events")) {
    await renderCalendarSetupPage();
    return;
  }
  if (source === "removed") {
    await renderRemovedTracksPanel();
  }
}

function bindSourcesTabBar(): void {
  if (tabBarBound) return;
  tabBarBound = true;
  document.querySelector(".view-sources .source-tab-bar")?.addEventListener("click", (ev) => {
    const tab = (ev.target as HTMLElement).closest(".source-tab") as HTMLButtonElement | null;
    if (!tab) return;
    const source = tab.dataset.source as SourceTab;
    if (!source || !sourceTabEnabled(source)) return;
    void showSourceTab(source);
  });
  window.addEventListener("hashchange", () => {
    void showSourceTab(sourceFromLocation());
  });
}

export async function showSourceTab(source: SourceTab): Promise<void> {
  if (!sourceTabEnabled(source)) source = "email";
  await ensureSourcePanels();
  setActiveSourceTab(source);
  await refreshSourcePanel(source);
}

export function mountSourcesPage(root: HTMLElement): void {
  root.innerHTML = PAGE_HTML;
  bindSourcesTabBar();
}

export async function renderSourcesPage(): Promise<void> {
  await showSourceTab(sourceFromLocation());
}

export function bindSourcesInteractions(): void {
  // Tab bar and setup pages bind their own handlers.
}

export { VALID_SOURCES, sourceFromLocation };
