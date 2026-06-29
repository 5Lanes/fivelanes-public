import { str } from "./shared/utils.js";
const INTERVAL_OPTIONS = [
    { sec: 300, label: "5 min" },
    { sec: 600, label: "10 min" },
    { sec: 900, label: "15 min" },
    { sec: 1800, label: "30 min" },
    { sec: 3600, label: "1 hour" },
];
function hourOptions() {
    return Array.from({ length: 24 }, (_, hour) => {
        const label = `${String(hour).padStart(2, "0")}:00`;
        return `<option value="${hour}">${label}</option>`;
    }).join("");
}
function intervalOptions(selectedSec) {
    const known = new Set(INTERVAL_OPTIONS.map((o) => o.sec));
    const extra = known.has(selectedSec) ? "" : `<option value="${selectedSec}">${formatInterval(selectedSec)}</option>`;
    const opts = INTERVAL_OPTIONS.map((o) => `<option value="${o.sec}"${o.sec === selectedSec ? " selected" : ""}>${o.label}</option>`).join("");
    return extra + opts;
}
export function formatInterval(sec) {
    if (sec % 3600 === 0)
        return `${sec / 3600} hour${sec === 3600 ? "" : "s"}`;
    if (sec % 60 === 0)
        return `${sec / 60} min`;
    return `${sec}s`;
}
export function formatActiveWindow(config) {
    const start = String(config.quiet_end_hour).padStart(2, "0");
    const end = String(config.quiet_start_hour).padStart(2, "0");
    return `${start}:00–${end}:00`;
}
export function scheduleSummary(config) {
    const window = formatActiveWindow(config);
    const tz = config.timezone.trim();
    const tzBit = tz ? ` (${tz})` : "";
    return `Every ${formatInterval(config.interval_sec)}, ${window}${tzBit}`;
}
function parseScheduleConfig(data) {
    return {
        interval_sec: Number(data.interval_sec),
        quiet_start_hour: Number(data.quiet_start_hour),
        quiet_end_hour: Number(data.quiet_end_hour),
        timezone: str(data.timezone),
    };
}
async function fetchSchedule() {
    const res = await fetch("/api/config", { credentials: "same-origin" });
    if (!res.ok)
        throw new Error(`Config load failed (${res.status})`);
    const data = (await res.json());
    const schedule = (data.schedule ?? {});
    return parseScheduleConfig(schedule);
}
async function saveSchedule(config) {
    const res = await fetch("/api/config/schedule", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify(config),
    });
    const data = (await res.json().catch(() => ({})));
    if (!res.ok) {
        throw new Error(str(data.error) || `Schedule update failed (${res.status})`);
    }
    return parseScheduleConfig((data.schedule ?? config));
}
function setDialogError(dialog, message) {
    const errEl = dialog.querySelector(".schedule-settings-error");
    if (!errEl)
        return;
    errEl.textContent = message;
    errEl.hidden = !message;
}
function fillForm(dialog, config) {
    const intervalEl = dialog.querySelector("#schedule-interval");
    const quietStartEl = dialog.querySelector("#schedule-quiet-start");
    const quietEndEl = dialog.querySelector("#schedule-quiet-end");
    const tzEl = dialog.querySelector("#schedule-timezone");
    if (intervalEl) {
        intervalEl.innerHTML = intervalOptions(config.interval_sec);
        intervalEl.value = String(config.interval_sec);
    }
    if (quietStartEl)
        quietStartEl.value = String(config.quiet_start_hour);
    if (quietEndEl)
        quietEndEl.value = String(config.quiet_end_hour);
    if (tzEl)
        tzEl.value = config.timezone;
}
function readForm(dialog) {
    const intervalEl = dialog.querySelector("#schedule-interval");
    const quietStartEl = dialog.querySelector("#schedule-quiet-start");
    const quietEndEl = dialog.querySelector("#schedule-quiet-end");
    const tzEl = dialog.querySelector("#schedule-timezone");
    return {
        interval_sec: Number(intervalEl?.value ?? 900),
        quiet_start_hour: Number(quietStartEl?.value ?? 19),
        quiet_end_hour: Number(quietEndEl?.value ?? 6),
        timezone: (tzEl?.value ?? "").trim(),
    };
}
function updateScheduleButtonLabel(btn, config) {
    const summary = scheduleSummary(config);
    btn.title = summary;
    btn.setAttribute("aria-label", `Run schedule settings. ${summary}`);
}
export function bindSchedulerSettings() {
    const btn = document.getElementById("schedule-settings-btn");
    const dialog = document.getElementById("schedule-settings-dialog");
    if (!btn || !dialog)
        return;
    let current = fetchSchedule().catch(() => null);
    void current.then((config) => {
        if (config)
            updateScheduleButtonLabel(btn, config);
    });
    btn.addEventListener("click", () => {
        void (async () => {
            try {
                setDialogError(dialog, "");
                const config = await fetchSchedule();
                current = Promise.resolve(config);
                fillForm(dialog, config);
                updateScheduleButtonLabel(btn, config);
                dialog.showModal();
            }
            catch (err) {
                const msg = err instanceof Error ? err.message : String(err);
                setDialogError(dialog, msg);
                dialog.showModal();
            }
        })();
    });
    dialog.querySelector(".schedule-settings-cancel")?.addEventListener("click", () => {
        dialog.close();
    });
    dialog.querySelector(".schedule-settings-form")?.addEventListener("submit", (event) => {
        event.preventDefault();
        void (async () => {
            const submitBtn = dialog.querySelector('button[type="submit"]');
            try {
                setDialogError(dialog, "");
                if (submitBtn)
                    submitBtn.disabled = true;
                const next = await saveSchedule(readForm(dialog));
                current = Promise.resolve(next);
                updateScheduleButtonLabel(btn, next);
                dialog.close();
            }
            catch (err) {
                const msg = err instanceof Error ? err.message : String(err);
                setDialogError(dialog, msg);
            }
            finally {
                if (submitBtn)
                    submitBtn.disabled = false;
            }
        })();
    });
    dialog.addEventListener("click", (event) => {
        if (event.target === dialog)
            dialog.close();
    });
}
export function mountScheduleSettingsDialog() {
    if (document.getElementById("schedule-settings-dialog"))
        return;
    const dialog = document.createElement("dialog");
    dialog.id = "schedule-settings-dialog";
    dialog.className = "schedule-settings-dialog";
    dialog.innerHTML = `
    <form method="dialog" class="schedule-settings-form">
      <h2>Run schedule</h2>
      <p class="schedule-settings-lead">
        Automatic runs happen on an interval during the active window. Quiet hours pause scheduled runs.
      </p>
      <label class="schedule-settings-field">
        <span>Run every</span>
        <select id="schedule-interval" name="interval_sec"></select>
      </label>
      <label class="schedule-settings-field">
        <span>Active from</span>
        <select id="schedule-quiet-end" name="quiet_end_hour">${hourOptions()}</select>
      </label>
      <label class="schedule-settings-field">
        <span>Active until</span>
        <select id="schedule-quiet-start" name="quiet_start_hour">${hourOptions()}</select>
      </label>
      <label class="schedule-settings-field">
        <span>Timezone</span>
        <input id="schedule-timezone" name="timezone" type="text" placeholder="System local" autocomplete="off" />
      </label>
      <p class="schedule-settings-hint">Leave timezone blank to use the server’s local timezone.</p>
      <p class="schedule-settings-error" hidden></p>
      <div class="schedule-settings-actions">
        <button type="button" class="schedule-settings-cancel">Cancel</button>
        <button type="submit">Save</button>
      </div>
    </form>
  `;
    document.body.appendChild(dialog);
}
