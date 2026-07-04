import { renderDashboardThreadsInline } from "./pages/threads_page.js";
export function renderDashboardThreadsSection() {
    void renderDashboardThreadsInline();
}
export function bindDashboardThreadsInteractions() {
    // Handled by bindThreadsInteractions in app.ts (global document listeners).
}
