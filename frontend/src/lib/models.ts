import { PROVIDERS, type Health, type Provider, providerReady } from "../api";

/**
 * The models a task runs on. Two picks, each a model and an effort: the agent (plans, talks to the user, and
 * keeps the memory) and the subagent (works the phone). They may come from different providers. The catalog is
 * the backend's (backend/app/agents/catalog.py); the picks persist in this browser, and each message carries them.
 */
export type Side = "agent" | "subagent";
export const SIDES: Side[] = ["agent", "subagent"];
export type ModelPick = { provider: Provider; model: string; effort: string };
export type Picks = Record<Side, ModelPick>;
export type CatalogModel = {
  id: string;
  provider: Provider;
  label: string;
  short: string;
  sides: Side[];
  efforts: string[];
};
export type Catalog = { models: CatalogModel[]; defaults: Record<Provider, Picks> };

export const SIDE_TITLE: Record<Side, { name: string; role: string }> = {
  agent: { name: "Agent", role: "Plans the task and talks to you" },
  subagent: { name: "Subagent", role: "Works the phone, step by step" },
};
export const EFFORT_LABEL: Record<string, string> = {
  off: "Off",
  low: "Low",
  medium: "Medium",
  high: "High",
  max: "Max",
};
/** The order providers are listed in, in each column. */
export const PROVIDER_ORDER: Provider[] = ["anthropic", "deepseek", "openai"];
export const providerName = (id: Provider) => PROVIDERS.find((p) => p.id === id)?.label ?? id;

const KEY = "relay.models";

export function storedPicks(): Picks | null {
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) || "null");
    if (
      SIDES.every(
        (s) =>
          typeof raw?.[s]?.provider === "string" &&
          typeof raw[s].model === "string" &&
          typeof raw[s].effort === "string",
      )
    ) {
      return raw as Picks;
    }
  } catch {}
  return null;
}
export function storePicks(picks: Picks) {
  try {
    localStorage.setItem(KEY, JSON.stringify(picks));
  } catch {}
}

/** The catalog's entry for a pick on a side, or undefined (a model the catalog no longer offers). */
export function entryFor(catalog: Catalog, side: Side, pick: ModelPick) {
  return catalog.models.find((m) => m.id === pick.model && m.provider === pick.provider && m.sides.includes(side));
}

function usable(catalog: Catalog, side: Side, pick: ModelPick) {
  const isDefault = catalog.defaults[pick.provider]?.[side]?.model === pick.model;
  const entry = entryFor(catalog, side, pick);
  const efforts = entry?.efforts ?? catalog.models.find((m) => m.provider === pick.provider)?.efforts ?? [];
  return (!!entry || isDefault) && efforts.includes(pick.effort);
}

/**
 * The picks a task would run on now: the user's, else what each provider ran before there were picks (the
 * provider the user last chose, Claude at first). A side whose provider has no API key moves to the first
 * provider that has one, as the single model choice did.
 */
export function effectivePicks(
  catalog: Catalog,
  chosen: Picks | null,
  fallback: Provider,
  health: Health | null,
): Picks {
  const ready = PROVIDERS.find((p) => providerReady(health, p.id))?.id ?? fallback;
  const next = {} as Picks;
  for (const side of SIDES) {
    let pick = chosen && usable(catalog, side, chosen[side]) ? chosen[side] : catalog.defaults[fallback][side];
    if (!providerReady(health, pick.provider)) pick = catalog.defaults[ready][side];
    next[side] = pick;
  }
  return next;
}

/** `picks` with `side` on `model`: the effort stays when the model has it, else it is the provider's default. */
export function withModel(catalog: Catalog, picks: Picks, side: Side, model: CatalogModel): Picks {
  const current = picks[side].effort;
  const fallback = catalog.defaults[model.provider][side].effort;
  const effort = model.efforts.includes(current)
    ? current
    : model.efforts.includes(fallback)
      ? fallback
      : model.efforts[0];
  return { ...picks, [side]: { provider: model.provider, model: model.id, effort } };
}

export function withEffort(picks: Picks, side: Side, effort: string): Picks {
  return { ...picks, [side]: { ...picks[side], effort } };
}

/** The composer button's text: "Opus 5.5 · Haiku 5.5". */
export function picksLabel(catalog: Catalog | null, picks: Picks | null) {
  if (!catalog || !picks) return "Model";
  const short = (side: Side) => entryFor(catalog, side, picks[side])?.short ?? picks[side].model;
  return `${short("agent")} · ${short("subagent")}`;
}
