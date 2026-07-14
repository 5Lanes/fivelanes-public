import { bindGaiChatPanel, mountGaiChatDialog } from "./gai_chat_ui.js";
import { bindPipelineControls } from "./pipeline_controls.js";
import { bindSettingsPanel, mountSettingsDialog } from "./settings_panel.js";
import { MEETINGS_LOOKAHEAD_DAYS, prefetchMeetings } from "./meetings_panel.js";
import {
  applyOneboxLocationHash,
  bindOneboxInteractions,
  mountOneboxPage,
  renderOneboxPage,
} from "./pages/onebox_page.js";
import { bindThreadsInteractions, mountThreadsPage, renderThreadsPage } from "./pages/threads_page.js";
import {
  bindSourcesInteractions,
  mountSourcesPage,
  renderSourcesPage,
} from "./pages/sources_page.js";
import {
  bindLinkedinSetupInteractions,
  mountLinkedinSetupPage,
  renderLinkedinSetupPage,
} from "./pages/linkedin_setup_page.js";
import {
  bindSlackSetupInteractions,
  mountSlackSetupPage,
  renderSlackSetupPage,
} from "./pages/slack_setup_page.js";
import {
  bindTextsSetupInteractions,
  mountTextsSetupPage,
  renderTextsSetupPage,
} from "./pages/texts_setup_page.js";
import {
  bindMeetRecordingsSetupInteractions,
  mountMeetRecordingsSetupPage,
  renderMeetRecordingsSetupPage,
} from "./pages/meet_recordings_setup_page.js";
import { refreshPipelineRunMeta } from "./pipeline_run_meta.js";
import { refreshPlanNotifications } from "./shared/plan_notifications.js";
import { getBundleMutationGeneration, loadLatestBundle, readCachedBundle, setBundle, setBundleFromNetwork } from "./shared/summaries_store.js";
import { applyNavFeatureVisibility, isFeatureEnabled, setFeaturesConfigForTests } from "./shared/features.js";
import { setOwnerConfigForTests } from "./shared/owner_config.js";
import { setDisplaySourceAccount } from "./shared/thread_domain.js";
import type { AppRoute } from "./shared/types.js";
import { escapeHtml } from "./shared/utils.js";

const runMetaEl = document.getElementById("run-meta") as HTMLParagraphElement;
const pageRoot = document.getElementById("page-root") as HTMLDivElement;

const LEGACY_SETUP_REDIRECTS: Record<string, string> = {
  "/texts-setup": "/sources#texts",
  "/slack-setup": "/sources#slack",
  "/linkedin-setup": "/sources#linkedin",
  "/meet-recordings-setup": "/sources#meet",
  "/calendar-setup": "/sources#calendar",
};

/** Returns true if the browser is navigating away (legacy redirect). */
export function applyLegacyRouteRedirect(pathname: string, search = location.search): boolean {
  const path = pathname.replace(/\/+$/, "") || "/";
  const params = new URLSearchParams(search);

  if (path === "/" || path === "/summaries.html") {
    location.replace("/onebox");
    return true;
  }
  if (path === "/threads") {
    const thread = params.get("thread")?.trim();
    location.replace(thread ? `/onebox?thread=${encodeURIComponent(thread)}` : "/onebox");
    return true;
  }
  if (path === "/meetings") {
    location.replace("/onebox#schedule");
    return true;
  }
  if (path === "/lanes") {
    location.replace("/onebox#lanes");
    return true;
  }
  if (path === "/plans") {
    const thread = params.get("thread")?.trim();
    location.replace(
      thread ? `/onebox?thread=${encodeURIComponent(thread)}#schedule-plans` : "/onebox#schedule-plans",
    );
    return true;
  }
  if (path === "/dashboard") {
    const thread = params.get("thread")?.trim();
    location.replace(thread ? `/onebox?thread=${encodeURIComponent(thread)}` : "/onebox");
    return true;
  }
  const setupTarget = LEGACY_SETUP_REDIRECTS[path];
  if (setupTarget) {
    location.replace(setupTarget);
    return true;
  }
  return false;
}

async function rerenderCurrentPage(): Promise<void> {
  await renderPage(routeFromPathname(location.pathname));
  refreshPlanNotifications();
}

export function routeFromPathname(pathname: string): AppRoute {
  const path = pathname.replace(/\/+$/, "") || "/";
  if (path === "/sources") return "sources";
  if (path === "/onebox") return "onebox";
  if (path === "/texts-setup") return "texts-setup";
  if (path === "/slack-setup") return "slack-setup";
  if (path === "/linkedin-setup") return "linkedin-setup";
  if (path === "/meet-recordings-setup") return "meet-recordings-setup";
  return "onebox";
}

function setActiveNav(route: AppRoute): void {
  document.querySelectorAll<HTMLElement>(".app-nav-link[data-route]").forEach((link) => {
    link.classList.toggle("active", link.dataset.route === route);
  });
  document.getElementById("sources-nav-btn")?.toggleAttribute("hidden", route === "sources");
  document.getElementById("onebox-return-btn")?.toggleAttribute("hidden", route !== "sources");
}

function showBootstrapError(message: string): void {
  pageRoot.innerHTML = `<div class="view-empty"><p class="empty-state">${escapeHtml(message)}</p></div>`;
}

const ROUTE_FEATURES: Partial<Record<AppRoute, string>> = {
  "texts-setup": "texts",
  "slack-setup": "slack",
  "linkedin-setup": "linkedin",
  "meet-recordings-setup": "meet_recordings",
};

async function renderPage(route: AppRoute): Promise<void> {
  setActiveNav(route);
  pageRoot.innerHTML = "";

  const requiredFeature = ROUTE_FEATURES[route];
  if (requiredFeature && !isFeatureEnabled(requiredFeature)) {
    pageRoot.innerHTML =
      '<div class="view-empty"><p class="empty-state">This feature requires Fivelanes Premium.</p></div>';
    return;
  }

  if (route === "sources") {
    mountSourcesPage(pageRoot);
    bindSourcesInteractions();
    await renderSourcesPage();
    return;
  }

  if (route === "onebox") {
    mountOneboxPage(pageRoot);
    bindOneboxInteractions();
    bindThreadsInteractions();
    await renderOneboxPage();
    return;
  }

  if (route === "texts-setup") {
    mountTextsSetupPage(pageRoot);
    bindTextsSetupInteractions();
    await renderTextsSetupPage();
    return;
  }

  if (route === "slack-setup") {
    mountSlackSetupPage(pageRoot);
    bindSlackSetupInteractions();
    await renderSlackSetupPage();
    return;
  }

  if (route === "linkedin-setup") {
    mountLinkedinSetupPage(pageRoot);
    bindLinkedinSetupInteractions();
    await renderLinkedinSetupPage();
    return;
  }

  if (route === "meet-recordings-setup") {
    mountMeetRecordingsSetupPage(pageRoot);
    bindMeetRecordingsSetupInteractions();
    await renderMeetRecordingsSetupPage();
    return;
  }

  mountThreadsPage(pageRoot);
  bindThreadsInteractions();
  await renderThreadsPage();
}

function applyConfig(config: Record<string, unknown>): void {
  setOwnerConfigForTests(config);
  setFeaturesConfigForTests(config);
  applyNavFeatureVisibility();
  const source = typeof config.source_account === "string" ? config.source_account : "";
  if (source) setDisplaySourceAccount(source);
}

async function bootstrap(): Promise<void> {
  if (location.protocol === "file:") {
    showBootstrapError(
      "This view loads data from the server; open it via the app server (not as a file:// URL).",
    );
    return;
  }

  if (applyLegacyRouteRedirect(location.pathname, location.search)) return;

  prefetchMeetings(MEETINGS_LOOKAHEAD_DAYS);
  void refreshPipelineRunMeta(runMetaEl);
  mountSettingsDialog();
  bindSettingsPanel();
  mountGaiChatDialog();
  bindGaiChatPanel();
  bindPipelineControls(() => {
    void refreshPipelineRunMeta(runMetaEl);
    void rerenderCurrentPage();
  });
  const route = routeFromPathname(location.pathname);
  const needsBundle =
    route !== "texts-setup" &&
    route !== "slack-setup" &&
    route !== "linkedin-setup" &&
    route !== "meet-recordings-setup" &&
    route !== "sources";
  const cached = needsBundle ? readCachedBundle() : null;
  try {
    if (cached) {
      setBundle(cached.data, cached.label);
      await renderPage(route);
    }

    const mutationGenAtFetch = getBundleMutationGeneration();
    const configPromise = fetch("/api/config", { credentials: "same-origin" }).then(async (res) => {
      if (!res.ok) throw new Error(`Config load failed (${res.status})`);
      return (await res.json()) as Record<string, unknown>;
    });
    const bundlePromise = needsBundle ? loadLatestBundle() : Promise.resolve(null);
    const [config, fresh] = await Promise.all([configPromise, bundlePromise]);
    applyConfig(config);

    if (needsBundle && fresh) {
      if (setBundleFromNetwork(fresh.data, fresh.label, mutationGenAtFetch)) {
        await renderPage(route);
      }
    } else if (!cached) {
      await renderPage(route);
    }

    refreshPlanNotifications();
    void refreshPipelineRunMeta(runMetaEl);

    window.addEventListener("hashchange", () => {
      if (routeFromPathname(location.pathname) === "onebox") {
        void applyOneboxLocationHash();
      }
    });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error("Data load failed:", err);
    if (!cached) showBootstrapError(`Data load failed: ${msg}`);
  }
}

void bootstrap();
