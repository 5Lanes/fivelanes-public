import { bindPipelineControls } from "./pipeline_controls.js";
import { MEETINGS_LOOKAHEAD_DAYS, prefetchMeetings } from "./meetings_panel.js";
import {
  bindDashboardInteractions,
  mountDashboardPage,
  renderDashboardPage,
} from "./pages/dashboard_page.js";
import { bindLanesInteractions, mountLanesPage, renderLanesPage } from "./pages/lanes_page.js";
import { mountMeetingsPage, renderMeetingsPage } from "./pages/meetings_page.js";
import { bindPlansInteractions, mountPlansPage, renderPlansPage } from "./pages/plans_page.js";
import { bindThreadsInteractions, mountThreadsPage, renderThreadsPage } from "./pages/threads_page.js";
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
import { refreshPipelineRunMeta } from "./pipeline_run_meta.js";
import { clearSummariesBundleCache, loadLatestBundle, setBundle } from "./shared/summaries_store.js";
import { applyNavFeatureVisibility, isFeatureEnabled, setFeaturesConfigForTests } from "./shared/features.js";
import { setOwnerConfigForTests } from "./shared/owner_config.js";
import { setDisplaySourceAccount } from "./shared/thread_domain.js";
import type { AppRoute } from "./shared/types.js";
import { escapeHtml } from "./shared/utils.js";

const runMetaEl = document.getElementById("run-meta") as HTMLParagraphElement;
const pageRoot = document.getElementById("page-root") as HTMLDivElement;

async function rerenderCurrentPage(): Promise<void> {
  await renderPage(routeFromPathname(location.pathname));
}

export function routeFromPathname(pathname: string): AppRoute {
  const path = pathname.replace(/\/+$/, "") || "/";
  if (path === "/dashboard") return "dashboard";
  if (path === "/meetings") return "meetings";
  if (path === "/lanes") return "lanes";
  if (path === "/plans") return "plans";
  if (path === "/texts-setup") return "texts-setup";
  if (path === "/slack-setup") return "slack-setup";
  if (path === "/threads" || path === "/" || path === "/summaries.html") return "threads";
  return "threads";
}

function setActiveNav(route: AppRoute): void {
  document.querySelectorAll<HTMLAnchorElement>(".app-nav-link[data-route]").forEach((link) => {
    link.classList.toggle("active", link.dataset.route === route);
  });
}

function showBootstrapError(message: string): void {
  pageRoot.innerHTML = `<div class="view-empty"><p class="empty-state">${escapeHtml(message)}</p></div>`;
}

const ROUTE_FEATURES: Partial<Record<AppRoute, string>> = {
  "texts-setup": "texts",
  "slack-setup": "slack",
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

  if (route === "dashboard") {
    mountDashboardPage(pageRoot);
    bindDashboardInteractions();
    bindLanesInteractions();
    await renderDashboardPage();
    return;
  }

  if (route === "meetings") {
    mountMeetingsPage(pageRoot);
    await renderMeetingsPage(runMetaEl);
    return;
  }

  if (route === "lanes") {
    mountLanesPage(pageRoot);
    bindLanesInteractions();
    await renderLanesPage();
    return;
  }

  if (route === "plans") {
    mountPlansPage(pageRoot);
    bindPlansInteractions();
    await renderPlansPage();
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

  mountThreadsPage(pageRoot);
  bindThreadsInteractions();
  await renderThreadsPage();
}

async function bootstrap(): Promise<void> {
  if (location.protocol === "file:") {
    showBootstrapError(
      "This view loads data from the server; open it via the dashboard (not as a file:// URL).",
    );
    return;
  }

  prefetchMeetings(MEETINGS_LOOKAHEAD_DAYS);
  void refreshPipelineRunMeta(runMetaEl);
  bindPipelineControls(() => {
    void refreshPipelineRunMeta(runMetaEl);
    void rerenderCurrentPage();
  });
  const route = routeFromPathname(location.pathname);
  try {
    const configRes = await fetch("/api/config", { credentials: "same-origin" });
    if (!configRes.ok) throw new Error(`Config load failed (${configRes.status})`);
    const config = (await configRes.json()) as Record<string, unknown>;
    setOwnerConfigForTests(config);
    setFeaturesConfigForTests(config);
    applyNavFeatureVisibility();
    const source = typeof config.source_account === "string" ? config.source_account : "";
    if (source) setDisplaySourceAccount(source);
    if (route !== "texts-setup" && route !== "slack-setup") {
      const { data, label } = await loadLatestBundle();
      setBundle(data, label);
    }
    await renderPage(route);
    void refreshPipelineRunMeta(runMetaEl);
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error("Data load failed:", err);
    showBootstrapError(`Data load failed: ${msg}`);
  }
}

void bootstrap();
