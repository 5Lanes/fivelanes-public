import { renderDashboardThreadsInline } from "./pages/threads_page.js";

export function renderDashboardThreadsSection(): void {
  void renderDashboardThreadsInline();
}

export function bindDashboardThreadsInteractions(): void {
  // Handled by bindThreadsInteractions in app.ts (global document listeners).
}
