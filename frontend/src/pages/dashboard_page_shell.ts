const PAGE_HTML = `
<div class="view-dashboard">
  <div class="dashboard-status-banner-wrap" id="dashboard-status-banner-wrap" hidden></div>
  <div class="dashboard-grid">
    <section class="dashboard-lanes-section" id="dashboard-lanes" aria-labelledby="dashboard-lanes-heading">
      <h2 id="dashboard-lanes-heading" class="section-title">Lanes</h2>
      <p class="lanes-hint">
        <span class="lanes-actions">
          <button type="button" id="collapse-all-tracks">Collapse tracks</button>
          <button type="button" id="expand-all-tracks">Expand tracks</button>
        </span>
      </p>
      <div id="lanes-list" class="lanes-list dashboard-lanes-list"></div>
      <form class="create-lane-form" id="create-lane-form" hidden>
        <input type="text" name="lane-name" id="lane-name-input" placeholder="Track name" required />
        <button type="submit">Create</button>
        <button type="button" class="create-lane-cancel" id="create-lane-cancel">Cancel</button>
        <p class="lane-create-error" id="lane-create-error" hidden></p>
      </form>
    </section>
    <aside class="schedule-panel meetings-panel" id="dashboard-schedule-rail" aria-label="Schedule"></aside>
    <section class="threads-section" id="dashboard-threads" aria-labelledby="dashboard-threads-heading">
      <h2 id="dashboard-threads-heading" class="section-title">Threads</h2>
      <div id="dashboard-threads-root"></div>
    </section>
  </div>
</div>`;

export function mountDashboardPage(root: HTMLElement): void {
  root.innerHTML = PAGE_HTML;
}

export { PAGE_HTML };
