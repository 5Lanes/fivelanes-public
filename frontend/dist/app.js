import { bindPipelineControls } from "./pipeline_controls.js";
import { MEETINGS_LOOKAHEAD_DAYS, prefetchMeetings } from "./meetings_panel.js";
import { bindDashboardInteractions, mountDashboardPage, renderDashboardPage, } from "./pages/dashboard_page.js";
import { bindLanesInteractions, mountLanesPage, renderLanesPage } from "./pages/lanes_page.js";
import { bindPeopleInteractions, mountPeoplePage, renderPeoplePage } from "./pages/people_page.js";
import { mountMeetingsPage, renderMeetingsPage } from "./pages/meetings_page.js";
import { bindPlansInteractions, mountPlansPage, renderPlansPage } from "./pages/plans_page.js";
import { bindThreadsInteractions, mountThreadsPage, renderThreadsPage } from "./pages/threads_page.js";
import { bindTextsSetupInteractions, mountTextsSetupPage, renderTextsSetupPage, } from "./pages/texts_setup_page.js";
import { refreshPipelineRunMeta } from "./pipeline_run_meta.js";
import { loadLatestBundle, setBundle } from "./shared/summaries_store.js";
import { ensureOwnerConfigLoaded } from "./shared/owner_config.js";
import { setDisplaySourceAccount } from "./shared/thread_domain.js";
import { escapeHtml } from "./shared/utils.js";
const runMetaEl = document.getElementById("run-meta");
const pageRoot = document.getElementById("page-root");
async function rerenderCurrentPage() {
    await renderPage(routeFromPathname(location.pathname));
}
export function routeFromPathname(pathname) {
    const path = pathname.replace(/\/+$/, "") || "/";
    if (path === "/dashboard")
        return "dashboard";
    if (path === "/meetings")
        return "meetings";
    if (path === "/lanes")
        return "lanes";
    if (path === "/people")
        return "people";
    if (path === "/plans")
        return "plans";
    if (path === "/texts-setup")
        return "texts-setup";
    if (path === "/threads" || path === "/" || path === "/summaries.html")
        return "threads";
    return "threads";
}
function setActiveNav(route) {
    document.querySelectorAll(".app-nav-link[data-route]").forEach((link) => {
        link.classList.toggle("active", link.dataset.route === route);
    });
}
function showBootstrapError(message) {
    pageRoot.innerHTML = `<div class="view-empty"><p class="empty-state">${escapeHtml(message)}</p></div>`;
}
async function renderPage(route) {
    setActiveNav(route);
    pageRoot.innerHTML = "";
    if (route === "dashboard") {
        mountDashboardPage(pageRoot);
        bindDashboardInteractions();
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
    if (route === "people") {
        mountPeoplePage(pageRoot);
        bindPeopleInteractions();
        await renderPeoplePage();
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
    mountThreadsPage(pageRoot);
    bindThreadsInteractions();
    await renderThreadsPage();
}
async function bootstrap() {
    if (location.protocol === "file:") {
        showBootstrapError("This view loads data from the server; open it via the dashboard (not as a file:// URL).");
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
        await ensureOwnerConfigLoaded();
        const configRes = await fetch("/api/config", { credentials: "same-origin" });
        if (configRes.ok) {
            const config = (await configRes.json());
            const source = typeof config.source_account === "string" ? config.source_account : "";
            if (source)
                setDisplaySourceAccount(source);
        }
        if (route !== "texts-setup") {
            const { data, label } = await loadLatestBundle();
            setBundle(data, label);
        }
        await renderPage(route);
        void refreshPipelineRunMeta(runMetaEl);
    }
    catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        console.error("Data load failed:", err);
        showBootstrapError(`Data load failed: ${msg}`);
    }
}
void bootstrap();
