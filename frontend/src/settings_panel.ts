import type { LooseObj } from "./shared/types.js";
import { str } from "./shared/utils.js";

export type BackendName = "claude" | "llama";
export type EmailCaptureMode = "forwards" | "labels";

export type ScheduleConfig = {
  interval_sec: number;
  quiet_start_hour: number;
  quiet_end_hour: number;
  timezone: string;
  active_weekdays: boolean;
  active_weekends: boolean;
};

const INTERVAL_OPTIONS = [
  { sec: 300, label: "5 min" },
  { sec: 600, label: "10 min" },
  { sec: 900, label: "15 min" },
  { sec: 1800, label: "30 min" },
  { sec: 3600, label: "1 hour" },
];

let dialogEl: HTMLDialogElement | null = null;
let controlsLocked = false;

function backendDisplayName(backend: BackendName): string {
  return backend === "claude" ? "Claude" : "Llama";
}

function hourOptions(): string {
  return Array.from({ length: 24 }, (_, hour) => {
    const label = `${String(hour).padStart(2, "0")}:00`;
    return `<option value="${hour}">${label}</option>`;
  }).join("");
}

function intervalOptions(selectedSec: number): string {
  const known = new Set(INTERVAL_OPTIONS.map((o) => o.sec));
  const extra = known.has(selectedSec)
    ? ""
    : `<option value="${selectedSec}">${formatInterval(selectedSec)}</option>`;
  const opts = INTERVAL_OPTIONS.map(
    (o) => `<option value="${o.sec}"${o.sec === selectedSec ? " selected" : ""}>${o.label}</option>`,
  ).join("");
  return extra + opts;
}

export function formatInterval(sec: number): string {
  if (sec % 3600 === 0) return `${sec / 3600} hour${sec === 3600 ? "" : "s"}`;
  if (sec % 60 === 0) return `${sec / 60} min`;
  return `${sec}s`;
}

function parseSchedule(data: LooseObj): ScheduleConfig {
  return {
    interval_sec: Number(data.interval_sec) || 900,
    quiet_start_hour: Number(data.quiet_start_hour ?? 19),
    quiet_end_hour: Number(data.quiet_end_hour ?? 6),
    timezone: str(data.timezone),
    active_weekdays: data.active_weekdays !== false,
    active_weekends: data.active_weekends !== false,
  };
}

function parseBackend(data: LooseObj): BackendName {
  const backend = str(data.backend).toLowerCase();
  if (backend !== "claude" && backend !== "llama") {
    throw new Error(`Unexpected backend: ${backend || "(empty)"}`);
  }
  return backend;
}

function parseEmailCapture(data: LooseObj): EmailCaptureMode {
  const mode = str(data.email_capture).toLowerCase();
  if (mode !== "forwards" && mode !== "labels") return "forwards";
  return mode;
}

function backendSwitchEl(): HTMLDivElement | null {
  return dialogEl?.querySelector("#backend-switch") ?? null;
}

function backendLabelEl(): HTMLSpanElement | null {
  return dialogEl?.querySelector("#backend-label") ?? null;
}

function setDialogError(message: string): void {
  const errEl = dialogEl?.querySelector(".settings-error") as HTMLParagraphElement | null;
  if (!errEl) return;
  errEl.textContent = message;
  errEl.hidden = !message;
}

function setBackendLabel(backend: BackendName): void {
  const name = backendDisplayName(backend);
  const label = backendLabelEl();
  if (label) label.textContent = `Backend: ${name}`;
  backendSwitchEl()?.setAttribute("aria-label", `LLM backend, ${name} selected`);
}

export function updateBackendSwitch(backend: BackendName): void {
  setBackendLabel(backend);
  backendSwitchEl()?.querySelectorAll<HTMLButtonElement>("[data-backend]").forEach((btn) => {
    const active = btn.dataset.backend === backend;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-pressed", active ? "true" : "false");
    btn.setAttribute("aria-current", active ? "true" : "false");
  });
}

export function setBackendControlsDisabled(disabled: boolean): void {
  backendSwitchEl()?.querySelectorAll<HTMLButtonElement>("[data-backend]").forEach((btn) => {
    btn.disabled = disabled;
  });
}

export function syncBackendFromStatus(status: LooseObj): void {
  const raw = str(status.backend).toLowerCase();
  if (raw === "claude" || raw === "llama") {
    updateBackendSwitch(raw);
  }
}

function updateEmailCaptureSwitch(mode: EmailCaptureMode): void {
  dialogEl?.querySelectorAll<HTMLButtonElement>("[data-email-capture]").forEach((btn) => {
    const active = btn.dataset.emailCapture === mode;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-pressed", active ? "true" : "false");
  });
  const note = dialogEl?.querySelector(".settings-labels-note") as HTMLParagraphElement | null;
  if (note) note.hidden = mode !== "labels";
}

function setEmailCaptureDisabled(disabled: boolean): void {
  dialogEl?.querySelectorAll<HTMLButtonElement>("[data-email-capture]").forEach((btn) => {
    btn.disabled = disabled;
  });
}

function fillScheduleFields(config: ScheduleConfig): void {
  const intervalEl = dialogEl?.querySelector("#schedule-interval") as HTMLSelectElement | null;
  const quietStartEl = dialogEl?.querySelector("#schedule-quiet-start") as HTMLSelectElement | null;
  const quietEndEl = dialogEl?.querySelector("#schedule-quiet-end") as HTMLSelectElement | null;
  const tzEl = dialogEl?.querySelector("#schedule-timezone") as HTMLInputElement | null;
  const weekdaysEl = dialogEl?.querySelector("#schedule-weekdays") as HTMLInputElement | null;
  const weekendsEl = dialogEl?.querySelector("#schedule-weekends") as HTMLInputElement | null;

  if (intervalEl) {
    intervalEl.innerHTML = intervalOptions(config.interval_sec);
    intervalEl.value = String(config.interval_sec);
  }
  if (quietStartEl) quietStartEl.value = String(config.quiet_start_hour);
  if (quietEndEl) quietEndEl.value = String(config.quiet_end_hour);
  if (tzEl) tzEl.value = config.timezone;
  if (weekdaysEl) weekdaysEl.checked = config.active_weekdays;
  if (weekendsEl) weekendsEl.checked = config.active_weekends;
}

function readScheduleFields(): ScheduleConfig {
  const intervalEl = dialogEl?.querySelector("#schedule-interval") as HTMLSelectElement | null;
  const quietStartEl = dialogEl?.querySelector("#schedule-quiet-start") as HTMLSelectElement | null;
  const quietEndEl = dialogEl?.querySelector("#schedule-quiet-end") as HTMLSelectElement | null;
  const tzEl = dialogEl?.querySelector("#schedule-timezone") as HTMLInputElement | null;
  const weekdaysEl = dialogEl?.querySelector("#schedule-weekdays") as HTMLInputElement | null;
  const weekendsEl = dialogEl?.querySelector("#schedule-weekends") as HTMLInputElement | null;

  return {
    interval_sec: Number(intervalEl?.value ?? 900),
    quiet_start_hour: Number(quietStartEl?.value ?? 19),
    quiet_end_hour: Number(quietEndEl?.value ?? 6),
    timezone: (tzEl?.value ?? "").trim(),
    active_weekdays: weekdaysEl?.checked ?? true,
    active_weekends: weekendsEl?.checked ?? true,
  };
}

async function fetchAppConfig(): Promise<LooseObj> {
  const res = await fetch("/api/config", { credentials: "same-origin" });
  if (!res.ok) throw new Error(`Config load failed (${res.status})`);
  return (await res.json()) as LooseObj;
}

async function setBackend(backend: BackendName): Promise<void> {
  const res = await fetch("/api/config/backend", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    body: JSON.stringify({ backend }),
  });
  const data = (await res.json().catch(() => ({}))) as LooseObj;
  if (!res.ok) {
    throw new Error(str(data.error) || `Backend update failed (${res.status})`);
  }
  updateBackendSwitch(backend);
}

async function setEmailCapture(mode: EmailCaptureMode): Promise<void> {
  const res = await fetch("/api/config/email-capture", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    body: JSON.stringify({ email_capture: mode }),
  });
  const data = (await res.json().catch(() => ({}))) as LooseObj;
  if (!res.ok) {
    throw new Error(str(data.error) || `Email capture update failed (${res.status})`);
  }
  updateEmailCaptureSwitch(parseEmailCapture(data));
}

async function saveSchedule(config: ScheduleConfig): Promise<ScheduleConfig> {
  const res = await fetch("/api/config/schedule", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    body: JSON.stringify(config),
  });
  const data = (await res.json().catch(() => ({}))) as LooseObj;
  if (!res.ok) {
    throw new Error(str(data.error) || `Schedule update failed (${res.status})`);
  }
  return parseSchedule((data.schedule as LooseObj) ?? config);
}

async function loadSettingsIntoDialog(): Promise<void> {
  const data = await fetchAppConfig();
  updateBackendSwitch(parseBackend(data));
  updateEmailCaptureSwitch(parseEmailCapture(data));
  fillScheduleFields(parseSchedule((data.schedule as LooseObj) ?? {}));
}

export function mountSettingsDialog(): void {
  if (document.getElementById("settings-dialog")) return;

  const dialog = document.createElement("dialog");
  dialog.id = "settings-dialog";
  dialog.className = "settings-dialog";
  dialog.innerHTML = `
    <form method="dialog" class="settings-form">
      <h2>Settings</h2>

      <section class="settings-section" aria-labelledby="settings-backend-heading">
        <h3 id="settings-backend-heading">Backend</h3>
        <div class="backend-control">
          <span class="backend-label" id="backend-label">Backend: …</span>
          <div class="backend-switch" id="backend-switch" role="group" aria-labelledby="backend-label">
            <button type="button" class="backend-switch-btn" data-backend="llama" aria-pressed="false">Llama</button>
            <button type="button" class="backend-switch-btn" data-backend="claude" aria-pressed="false">Claude</button>
          </div>
        </div>
      </section>

      <section class="settings-section" aria-labelledby="settings-email-heading">
        <h3 id="settings-email-heading">Email source</h3>
        <p class="settings-lead">Choose how mail enters Fivelanes.</p>
        <div class="backend-switch settings-segmented" id="email-capture-switch" role="group" aria-label="Email capture mode">
          <button type="button" class="backend-switch-btn" data-email-capture="forwards" aria-pressed="false">Forwards</button>
          <button type="button" class="backend-switch-btn" data-email-capture="labels" aria-pressed="false">Labels</button>
        </div>
        <p class="settings-labels-note" hidden>Label capture is not available yet — forwards are still used.</p>
      </section>

      <section class="settings-section" aria-labelledby="settings-schedule-heading">
        <h3 id="settings-schedule-heading">Background runs</h3>
        <p class="settings-lead">Automatic runs happen on an interval during the active window.</p>
        <label class="settings-field">
          <span>Run every</span>
          <select id="schedule-interval" name="interval_sec"></select>
        </label>
        <label class="settings-field">
          <span>Active from</span>
          <select id="schedule-quiet-end" name="quiet_end_hour">${hourOptions()}</select>
        </label>
        <label class="settings-field">
          <span>Active until</span>
          <select id="schedule-quiet-start" name="quiet_start_hour">${hourOptions()}</select>
        </label>
        <label class="settings-field">
          <span>Timezone</span>
          <input id="schedule-timezone" name="timezone" type="text" placeholder="System local" autocomplete="off" />
        </label>
        <div class="settings-checkboxes">
          <label class="settings-checkbox">
            <input type="checkbox" id="schedule-weekdays" name="active_weekdays" checked />
            <span>Weekdays</span>
          </label>
          <label class="settings-checkbox">
            <input type="checkbox" id="schedule-weekends" name="active_weekends" checked />
            <span>Weekends</span>
          </label>
        </div>
        <p class="settings-hint">Leave timezone blank to use the server's local timezone.</p>
      </section>

      <p class="settings-error" hidden></p>
      <div class="settings-actions">
        <button type="button" class="settings-cancel">Cancel</button>
        <button type="submit">Save</button>
      </div>
    </form>
  `;
  document.body.appendChild(dialog);
  dialogEl = dialog;
}

export function setSettingsControlsLocked(locked: boolean): void {
  controlsLocked = locked;
  setBackendControlsDisabled(locked);
  setEmailCaptureDisabled(locked);
}

export function bindSettingsPanel(): void {
  const btn = document.getElementById("settings-btn");
  if (!dialogEl || !btn) return;

  btn.addEventListener("click", () => {
    void (async () => {
      try {
        setDialogError("");
        await loadSettingsIntoDialog();
        setSettingsControlsLocked(controlsLocked);
        dialogEl?.showModal();
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        setDialogError(msg);
        dialogEl?.showModal();
      }
    })();
  });

  backendSwitchEl()?.querySelectorAll<HTMLButtonElement>("[data-backend]").forEach((el) => {
    el.addEventListener("click", () => {
      const backend = el.dataset.backend as BackendName | undefined;
      if (!backend || el.classList.contains("active") || el.disabled) return;
      void (async () => {
        try {
          setDialogError("");
          setBackendControlsDisabled(true);
          await setBackend(backend);
        } catch (err) {
          const msg = err instanceof Error ? err.message : String(err);
          setDialogError(msg);
        } finally {
          if (!controlsLocked) setBackendControlsDisabled(false);
        }
      })();
    });
  });

  dialogEl.querySelectorAll<HTMLButtonElement>("[data-email-capture]").forEach((el) => {
    el.addEventListener("click", () => {
      const mode = el.dataset.emailCapture as EmailCaptureMode | undefined;
      if (!mode || el.classList.contains("active") || el.disabled) return;
      void (async () => {
        try {
          setDialogError("");
          setEmailCaptureDisabled(true);
          await setEmailCapture(mode);
        } catch (err) {
          const msg = err instanceof Error ? err.message : String(err);
          setDialogError(msg);
        } finally {
          if (!controlsLocked) setEmailCaptureDisabled(false);
        }
      })();
    });
  });

  dialogEl.querySelector(".settings-cancel")?.addEventListener("click", () => {
    dialogEl?.close();
  });

  dialogEl.querySelector(".settings-form")?.addEventListener("submit", (event) => {
    event.preventDefault();
    void (async () => {
      const submitBtn = dialogEl?.querySelector('button[type="submit"]') as HTMLButtonElement | null;
      try {
        setDialogError("");
        const schedule = readScheduleFields();
        if (!schedule.active_weekdays && !schedule.active_weekends) {
          setDialogError("Enable at least weekdays or weekends.");
          return;
        }
        if (submitBtn) submitBtn.disabled = true;
        await saveSchedule(schedule);
        dialogEl?.close();
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        setDialogError(msg);
      } finally {
        if (submitBtn) submitBtn.disabled = false;
      }
    })();
  });

  dialogEl.addEventListener("click", (event) => {
    if (event.target === dialogEl) dialogEl?.close();
  });

  void (async () => {
    try {
      const data = await fetchAppConfig();
      updateBackendSwitch(parseBackend(data));
    } catch {
      // Settings button still works; backend label updates when dialog opens.
    }
  })();
}
