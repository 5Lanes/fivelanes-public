import { escapeHtml, str } from "./shared/utils.js";
import { sourcePillHtml } from "./shared/source_ui.js";
function parseEmailCapture(data) {
    const mode = str(data.email_capture).toLowerCase();
    if (mode !== "forwards" && mode !== "labels")
        return "forwards";
    return mode;
}
async function fetchAppConfig() {
    const res = await fetch("/api/config", { credentials: "same-origin" });
    if (!res.ok)
        throw new Error(`Config load failed (${res.status})`);
    return (await res.json());
}
async function setEmailCapture(mode) {
    const res = await fetch("/api/config/email-capture", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ email_capture: mode }),
    });
    const data = (await res.json().catch(() => ({})));
    if (!res.ok) {
        throw new Error(str(data.error) || `Email capture update failed (${res.status})`);
    }
    return parseEmailCapture(data);
}
function updateEmailCaptureUi(root, mode) {
    root.querySelectorAll("[data-email-capture]").forEach((btn) => {
        const active = btn.dataset.emailCapture === mode;
        btn.classList.toggle("active", active);
        btn.setAttribute("aria-pressed", active ? "true" : "false");
    });
    const note = root.querySelector(".email-capture-labels-note");
    if (note)
        note.hidden = mode !== "labels";
}
function panelShell(forwardAddress) {
    const addr = forwardAddress
        ? `<code class="email-forward-address">${escapeHtml(forwardAddress)}</code>`
        : `<span class="email-forward-missing">Set <code>SOURCE_ACCOUNT</code> in your data <code>.env</code>.</span>`;
    return `<div class="setup-grid">
    <header class="setup-header">
      <h2>${sourcePillHtml("email", "Email")} Inbox capture</h2>
      <p class="setup-lead">Choose how mail enters Fivelanes. Tracked threads appear on the dashboard.</p>
      <section class="setup-card">
        <h3>Capture mode</h3>
        <p class="setup-hint">Forwards send copies to Fivelanes; labels apply rules in Gmail.</p>
        <span class="control-label">Mode</span>
        <div class="segmented email-capture-switch" role="group" aria-label="Email capture mode">
          <button type="button" data-email-capture="forwards" aria-pressed="false">Forwards</button>
          <button type="button" data-email-capture="labels" aria-pressed="false">Labels</button>
        </div>
        <p class="setup-hint email-capture-labels-note" hidden>Label capture is not available yet — forwards are still used.</p>
        <p class="email-capture-error setup-hint" hidden></p>
      </section>
    </header>
    <section class="setup-card">
      <h3>Forwarding address</h3>
      <p class="setup-hint">Add this address as a Gmail forwarding recipient, or create a filter that applies your Fivelanes label.</p>
      <p class="setup-hint">${addr}</p>
      <div class="setup-actions">
        <button type="button" class="btn btn--primary email-copy-address-btn"${forwardAddress ? "" : " disabled"}>Copy address</button>
      </div>
    </section>
  </div>`;
}
let panelBound = false;
export async function renderEmailCapturePanel(root) {
    const data = await fetchAppConfig();
    const sourceAccount = str(data.source_account).trim().toLowerCase();
    const forwardAddress = sourceAccount ? sourceAccount : "";
    if (!root.dataset.mounted) {
        root.innerHTML = panelShell(forwardAddress);
        root.dataset.mounted = "1";
        bindEmailCapturePanel(root);
    }
    else {
        const addrEl = root.querySelector(".email-forward-address");
        if (addrEl)
            addrEl.textContent = forwardAddress;
        else {
            const hint = root.querySelector(".setup-card:last-child .setup-hint:nth-of-type(2)");
            if (hint) {
                hint.innerHTML = forwardAddress
                    ? `<code class="email-forward-address">${escapeHtml(forwardAddress)}</code>`
                    : `<span class="email-forward-missing">Set <code>SOURCE_ACCOUNT</code> in your data <code>.env</code>.</span>`;
            }
        }
        const copyBtn = root.querySelector(".email-copy-address-btn");
        if (copyBtn)
            copyBtn.disabled = !forwardAddress;
    }
    updateEmailCaptureUi(root, parseEmailCapture(data));
}
export function bindEmailCapturePanel(root) {
    if (panelBound)
        return;
    panelBound = true;
    root.addEventListener("click", (ev) => {
        const target = ev.target;
        const captureBtn = target.closest("[data-email-capture]");
        if (captureBtn && root.contains(captureBtn)) {
            const mode = captureBtn.dataset.emailCapture;
            if (!mode || captureBtn.classList.contains("active") || captureBtn.disabled)
                return;
            const errEl = root.querySelector(".email-capture-error");
            captureBtn.disabled = true;
            void (async () => {
                try {
                    if (errEl)
                        errEl.hidden = true;
                    const applied = await setEmailCapture(mode);
                    updateEmailCaptureUi(root, applied);
                }
                catch (err) {
                    if (errEl) {
                        errEl.textContent = err instanceof Error ? err.message : String(err);
                        errEl.hidden = false;
                    }
                }
                finally {
                    captureBtn.disabled = false;
                }
            })();
            return;
        }
        if (target.closest(".email-copy-address-btn")) {
            const addr = root.querySelector(".email-forward-address")?.textContent?.trim();
            if (!addr)
                return;
            void navigator.clipboard.writeText(addr);
        }
    });
}
