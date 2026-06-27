import { str } from "./utils.js";
let enabledFeatures = new Set();
let premiumUnlocked = false;
let featuresLoaded = false;
let featuresPromise = null;
function applyFeaturesConfig(data) {
    const features = data.enabled_features;
    enabledFeatures = new Set(Array.isArray(features) ? features.map((f) => str(f).trim()).filter(Boolean) : []);
    premiumUnlocked = Boolean(data.premium_unlocked);
    featuresLoaded = true;
}
export async function ensureFeaturesLoaded() {
    if (featuresLoaded)
        return;
    if (!featuresPromise) {
        featuresPromise = (async () => {
            const res = await fetch("/api/config", { credentials: "same-origin" });
            if (!res.ok)
                throw new Error(`Config load failed (${res.status})`);
            applyFeaturesConfig((await res.json()));
        })();
    }
    await featuresPromise;
}
export function setFeaturesConfigForTests(data) {
    applyFeaturesConfig(data);
    featuresLoaded = true;
    featuresPromise = Promise.resolve();
}
export function isFeatureEnabled(featureId) {
    return enabledFeatures.has(featureId);
}
export function isPremiumUnlocked() {
    return premiumUnlocked;
}
export function applyNavFeatureVisibility() {
    document.querySelectorAll("[data-feature]").forEach((el) => {
        const featureId = (el.dataset.feature || "").trim();
        if (!featureId)
            return;
        el.hidden = !isFeatureEnabled(featureId);
    });
}
