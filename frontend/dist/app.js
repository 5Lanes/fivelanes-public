import { bindPipelineControls } from "./pipeline_controls.js";
import { bindSettingsPanel, mountSettingsDialog } from "./settings_panel.js";
import { MEETINGS_LOOKAHEAD_DAYS, prefetchMeetings } from "./meetings_panel.js";
import { bindDashboardInteractions, mountDashboardPage, renderDashboardPage, } from "./pages/dashboard_page.js";
import { bindLanesInteractions, mountLanesPage, renderLanesPage } from "./pages/lanes_page.js";
import { mountMeetingsPage, renderMeetingsPage } from "./pages/meetings_page.js";
import { bindPlansInteractions, mountPlansPage, renderPlansPage } from "./pages/plans_page.js";
import { bindThreadsInteractions, mountThreadsPage, renderThreadsPage } from "./pages/threads_page.js";
import { bindSourcesInteractions, mountSourcesPage, renderSourcesPage, } from "./pages/sources_page.js";
import { bindLinkedinSetupInteractions, mountLinkedinSetupPage, renderLinkedinSetupPage, } from "./pages/linkedin_setup_page.js";
import { bindSlackSetupInteractions, mountSlackSetupPage, renderSlackSetupPage, } from "./pages/slack_setup_page.js";
import { bindTextsSetupInteractions, mountTextsSetupPage, renderTextsSetupPage, } from "./pages/texts_setup_page.js";
import { bindMeetRecordingsSetupInteractions, mountMeetRecordingsSetupPage, renderMeetRecordingsSetupPage, } from "./pages/meet_recordings_setup_page.js";
import { refreshPipelineRunMeta } from "./pipeline_run_meta.js";
import { refreshPlanNotifications } from "./shared/plan_notifications.js";
import { bundleChanged, getBundleMutationGeneration, loadLatestBundle, readCachedBundle, setBundle, setBundleFromNetwork } from "./shared/summaries_store.js";
import { applyNavFeatureVisibility, isFeatureEnabled, setFeaturesConfigForTests } from "./shared/features.js";
import { setOwnerConfigForTests } from "./shared/owner_config.js";
import { setDisplaySourceAccount } from "./shared/thread_domain.js";
import { escapeHtml } from "./shared/utils.js";
const runMetaEl = document.getElementById("run-meta");
const pageRoot = document.getElementById("page-root");
const LEGACY_SETUP_REDIRECTS = {
    "/texts-setup": "/sources#texts",
    "/slack-setup": "/sources#slack",
    "/linkedin-setup": "/sources#linkedin",
    "/meet-recordings-setup": "/sources#meet",
};
/** Returns true if the browser is navigating away (legacy redirect). */
export function applyLegacyRouteRedirect(pathname, search = location.search) {
    const path = pathname.replace(/\/+$/, "") || "/";
    const params = new URLSearchParams(search);
    if (path === "/" || path === "/summaries.html") {
        location.replace("/dashboard");
        return true;
    }
    if (path === "/threads") {
        const thread = params.get("thread")?.trim();
        location.replace(thread ? `/dashboard?thread=${encodeURIComponent(thread)}` : "/dashboard");
        return true;
    }
    if (path === "/meetings") {
        location.replace("/dashboard#schedule");
        return true;
    }
    if (path === "/lanes") {
        location.replace("/dashboard#lanes");
        return true;
    }
    if (path === "/plans") {
        const thread = params.get("thread")?.trim();
        location.replace(thread ? `/dashboard?thread=${encodeURIComponent(thread)}#schedule-plans` : "/dashboard#schedule-plans");
        return true;
    }
    const setupTarget = LEGACY_SETUP_REDIRECTS[path];
    if (setupTarget) {
        location.replace(setupTarget);
        return true;
    }
    return false;
}
async function rerenderCurrentPage() {
    await renderPage(routeFromPathname(location.pathname));
    refreshPlanNotifications();
}
export function routeFromPathname(pathname) {
    const path = pathname.replace(/\/+$/, "") || "/";
    if (path === "/dashboard")
        return "dashboard";
    if (path === "/sources")
        return "sources";
    if (path === "/meetings")
        return "meetings";
    if (path === "/lanes")
        return "lanes";
    if (path === "/plans")
        return "plans";
    if (path === "/texts-setup")
        return "texts-setup";
    if (path === "/slack-setup")
        return "slack-setup";
    if (path === "/linkedin-setup")
        return "linkedin-setup";
    if (path === "/meet-recordings-setup")
        return "meet-recordings-setup";
    if (path === "/threads" || path === "/" || path === "/summaries.html")
        return "threads";
    return "dashboard";
}
function setActiveNav(route) {
    document.querySelectorAll(".app-nav-link[data-route]").forEach((link) => {
        link.classList.toggle("active", link.dataset.route === route);
    });
}
function showBootstrapError(message) {
    pageRoot.innerHTML = `<div class="view-empty"><p class="empty-state">${escapeHtml(message)}</p></div>`;
}
const ROUTE_FEATURES = {
    "texts-setup": "texts",
    "slack-setup": "slack",
    "linkedin-setup": "linkedin",
    "meet-recordings-setup": "meet_recordings",
};
async function renderPage(route) {
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
        bindThreadsInteractions();
        await renderDashboardPage();
        return;
    }
    if (route === "sources") {
        mountSourcesPage(pageRoot);
        bindSourcesInteractions();
        await renderSourcesPage();
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
function applyConfig(config) {
    setOwnerConfigForTests(config);
    setFeaturesConfigForTests(config);
    applyNavFeatureVisibility();
    const source = typeof config.source_account === "string" ? config.source_account : "";
    if (source)
        setDisplaySourceAccount(source);
}
async function bootstrap() {
    if (location.protocol === "file:") {
        showBootstrapError("This view loads data from the server; open it via the dashboard (not as a file:// URL).");
        return;
    }
    if (applyLegacyRouteRedirect(location.pathname, location.search))
        return;
    prefetchMeetings(MEETINGS_LOOKAHEAD_DAYS);
    void refreshPipelineRunMeta(runMetaEl);
    mountSettingsDialog();
    bindSettingsPanel();
    bindPipelineControls(() => {
        void refreshPipelineRunMeta(runMetaEl);
        void rerenderCurrentPage();
    });
    const route = routeFromPathname(location.pathname);
    const needsBundle = route !== "texts-setup" &&
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
            if (!res.ok)
                throw new Error(`Config load failed (${res.status})`);
            return (await res.json());
        });
        const bundlePromise = needsBundle ? loadLatestBundle() : Promise.resolve(null);
        const [config, fresh] = await Promise.all([configPromise, bundlePromise]);
        applyConfig(config);
        if (needsBundle && fresh) {
            if (!cached || bundleChanged(cached.data, fresh)) {
                if (setBundleFromNetwork(fresh.data, fresh.label, mutationGenAtFetch)) {
                    await renderPage(route);
                }
            }
        }
        else if (!cached) {
            await renderPage(route);
        }
        refreshPlanNotifications();
        void refreshPipelineRunMeta(runMetaEl);
    }
    catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        console.error("Data load failed:", err);
        if (!cached)
            showBootstrapError(`Data load failed: ${msg}`);
    }
}
void bootstrap();
