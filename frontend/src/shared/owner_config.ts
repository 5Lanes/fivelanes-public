import type { LooseObj } from "./types.js";
import { extractEmailsLower, str } from "./utils.js";

let ownerName = "Owner";
let ownerEmailHints: string[] = [];
let configLoaded = false;
let configPromise: Promise<void> | null = null;

function applyOwnerConfig(data: LooseObj): void {
  ownerName = str(data.owner_name).trim() || "Owner";
  const hints = data.owner_email_hints;
  ownerEmailHints = Array.isArray(hints)
    ? hints.map((h) => str(h).trim().toLowerCase()).filter(Boolean)
    : [];
  configLoaded = true;
}

export async function ensureOwnerConfigLoaded(): Promise<void> {
  if (configLoaded) return;
  if (!configPromise) {
    configPromise = (async () => {
      const res = await fetch("/api/config", { credentials: "same-origin" });
      if (!res.ok) throw new Error(`Config load failed (${res.status})`);
      applyOwnerConfig((await res.json()) as LooseObj);
    })();
  }
  await configPromise;
}

export function setOwnerConfigForTests(data: LooseObj): void {
  applyOwnerConfig(data);
  configLoaded = true;
  configPromise = Promise.resolve();
}

export function getOwnerName(): string {
  return ownerName;
}

export function getOwnerEmailHints(): readonly string[] {
  return ownerEmailHints;
}

export function ownerNameVariants(): string[] {
  const variants: string[] = [];
  const seen = new Set<string>();
  for (const token of [ownerName, ...ownerName.split(/\s+/)]) {
    const key = token.trim().toLowerCase();
    if (key && !seen.has(key)) {
      seen.add(key);
      variants.push(key);
    }
  }
  return variants.length ? variants : ["owner"];
}

export function otherPartyOwesRe(): RegExp {
  const alt = ownerNameVariants()
    .map((v) => v.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"))
    .join("|");
  return new RegExp(
    `^(?!(?:${alt})\\b)(?:[A-Z][a-z]+(?:\\s+[A-Z][a-z]+)*)\\s+(?:owes?|hasn't|has not|needs to|must)\\b`,
    "i",
  );
}

export function isLikelyOwnEmail(email: string): boolean {
  const e = email.trim().toLowerCase();
  if (!e.includes("@")) return false;
  const local = e.split("@")[0].split("+")[0];
  for (const hint of ownerEmailHints) {
    if (!hint) continue;
    if (hint.includes("@")) {
      if (e === hint) return true;
      continue;
    }
    if (e.includes(hint)) return true;
    if (local === hint || local.startsWith(`${hint}.`)) return true;
  }
  return false;
}

/** True when a "From"/organizer header string resolves to one of the owner's own addresses. */
export function isOwnSender(sender: string): boolean {
  for (const email of extractEmailsLower(sender)) {
    if (isLikelyOwnEmail(email)) return true;
  }
  return false;
}
