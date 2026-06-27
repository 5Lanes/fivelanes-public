import type { LooseObj } from "./types.js";
import { str } from "./utils.js";

let enabledFeatures = new Set<string>();
let premiumUnlocked = false;
let featuresLoaded = false;
let featuresPromise: Promise<void> | null = null;

function applyFeaturesConfig(data: LooseObj): void {
  const features = data.enabled_features;
  enabledFeatures = new Set(
    Array.isArray(features) ? features.map((f) => str(f).trim()).filter(Boolean) : [],
  );
  premiumUnlocked = Boolean(data.premium_unlocked);
  featuresLoaded = true;
}

export async function ensureFeaturesLoaded(): Promise<void> {
  if (featuresLoaded) return;
  if (!featuresPromise) {
    featuresPromise = (async () => {
      const res = await fetch("/api/config", { credentials: "same-origin" });
      if (!res.ok) throw new Error(`Config load failed (${res.status})`);
      applyFeaturesConfig((await res.json()) as LooseObj);
    })();
  }
  await featuresPromise;
}

export function setFeaturesConfigForTests(data: LooseObj): void {
  applyFeaturesConfig(data);
  featuresLoaded = true;
  featuresPromise = Promise.resolve();
}

export function isFeatureEnabled(featureId: string): boolean {
  return enabledFeatures.has(featureId);
}

export function isPremiumUnlocked(): boolean {
  return premiumUnlocked;
}

export function applyNavFeatureVisibility(): void {
  document.querySelectorAll<HTMLElement>("[data-feature]").forEach((el) => {
    const featureId = (el.dataset.feature || "").trim();
    if (!featureId) return;
    el.hidden = !isFeatureEnabled(featureId);
  });
}
