import { str } from "./utils.js";
let ownerName = "Owner";
let ownerEmailHints = [];
let configLoaded = false;
let configPromise = null;
function applyOwnerConfig(data) {
    ownerName = str(data.owner_name).trim() || "Owner";
    const hints = data.owner_email_hints;
    ownerEmailHints = Array.isArray(hints)
        ? hints.map((h) => str(h).trim().toLowerCase()).filter(Boolean)
        : [];
    configLoaded = true;
}
export async function ensureOwnerConfigLoaded() {
    if (configLoaded)
        return;
    if (!configPromise) {
        configPromise = (async () => {
            const res = await fetch("/api/config", { credentials: "same-origin" });
            if (!res.ok)
                throw new Error(`Config load failed (${res.status})`);
            applyOwnerConfig((await res.json()));
        })();
    }
    await configPromise;
}
export function setOwnerConfigForTests(data) {
    applyOwnerConfig(data);
    configLoaded = true;
    configPromise = Promise.resolve();
}
export function getOwnerName() {
    return ownerName;
}
export function getOwnerEmailHints() {
    return ownerEmailHints;
}
export function ownerNameVariants() {
    const variants = [];
    const seen = new Set();
    for (const token of [ownerName, ...ownerName.split(/\s+/)]) {
        const key = token.trim().toLowerCase();
        if (key && !seen.has(key)) {
            seen.add(key);
            variants.push(key);
        }
    }
    return variants.length ? variants : ["owner"];
}
export function otherPartyOwesRe() {
    const alt = ownerNameVariants()
        .map((v) => v.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"))
        .join("|");
    return new RegExp(`^(?!(?:${alt})\\b)(?:[A-Z][a-z]+(?:\\s+[A-Z][a-z]+)*)\\s+(?:owes?|hasn't|has not|needs to|must)\\b`, "i");
}
export function isLikelyOwnEmail(email) {
    const e = email.trim().toLowerCase();
    if (!e.includes("@"))
        return false;
    const local = e.split("@")[0].split("+")[0];
    for (const hint of ownerEmailHints) {
        if (!hint)
            continue;
        if (hint.includes("@")) {
            if (e === hint)
                return true;
            continue;
        }
        if (e.includes(hint))
            return true;
        if (local === hint || local.startsWith(`${hint}.`))
            return true;
    }
    return false;
}
