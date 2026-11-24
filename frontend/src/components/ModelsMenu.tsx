import { Check, ChevronDown } from "lucide-react";
import { type Health, providerReady } from "../api";
import {
  type Catalog,
  EFFORT_LABEL,
  picksLabel,
  type Picks,
  PROVIDER_ORDER,
  providerName,
  SIDE_TITLE,
  SIDES,
  withEffort,
  withModel,
} from "../lib/models";
import type { Workspace } from "../lib/useWorkspace";
import Popover from "./Popover";

/**
 * The models the next task runs on, as a small button that names them and opens the picker above itself. The
 * composer shows it under the text; the voice screen shows it at the bottom. The same picks, kept in this browser.
 */
export default function ModelsMenu({ ws, disabled = false }: { ws: Workspace; disabled?: boolean }) {
  return (
    <Popover
      place="above-start"
      className="models-menu"
      trigger={(props) => (
        <button
          type="button"
          className="model"
          disabled={disabled || !ws.catalog}
          title="Models for this task: the agent and the subagent"
          {...props}
        >
          {picksLabel(ws.catalog, ws.models)}
          <ChevronDown size={12} />
        </button>
      )}
    >
      {() =>
        ws.catalog &&
        ws.models && <ModelPicker catalog={ws.catalog} picks={ws.models} health={ws.health} onChange={ws.setModels} />
      }
    </Popover>
  );
}

type PickerProps = {
  catalog: Catalog;
  picks: Picks;
  /** Which providers have a key: the others are listed, but cannot be chosen. */
  health: Health | null;
  /** A choice applies at once: the picks with it. */
  onChange: (picks: Picks) => void;
};

/**
 * The models for a task, in two columns: the agent on the left (plans the task, talks to the user) and the
 * subagent on the right (works the phone). Each has its model, from any provider with a key, and its effort. The
 * composer's picks, and in the settings those for what notifications start.
 */
export function ModelPicker({ catalog, picks, health, onChange }: PickerProps) {
  return (
    <div className="models">
      {SIDES.map((side) => {
        const pick = picks[side];
        const chosen = catalog.models.find((m) => m.id === pick.model && m.provider === pick.provider);
        return (
          <section key={side} className="models-col" aria-label={SIDE_TITLE[side].name}>
            <header>
              <b>{SIDE_TITLE[side].name}</b>
              <small>{SIDE_TITLE[side].role}</small>
            </header>
            {PROVIDER_ORDER.map((provider) => {
              const entries = catalog.models.filter((m) => m.provider === provider && m.sides.includes(side));
              if (!entries.length) return null;
              const ready = providerReady(health, provider);
              return (
                <div key={provider} role="group" aria-label={providerName(provider)}>
                  <div className="menu-label">{providerName(provider)}</div>
                  {entries.map((m) => {
                    const on = m.id === pick.model && m.provider === pick.provider;
                    return (
                      <button
                        key={m.id}
                        type="button"
                        role="menuitemradio"
                        aria-checked={on}
                        className="menu-item"
                        disabled={!ready}
                        onClick={() => onChange(withModel(catalog, picks, side, m))}
                      >
                        <span className="name">{m.label}</span>
                        {on ? (
                          <Check size={13} className="check" />
                        ) : (
                          !ready && <span className="hint">No API key</span>
                        )}
                      </button>
                    );
                  })}
                </div>
              );
            })}
            <div className="effort">
              <span className="menu-label">Effort</span>
              <div className="seg" role="group" aria-label={`${SIDE_TITLE[side].name} effort`}>
                {(chosen?.efforts ?? [pick.effort]).map((effort) => (
                  <button
                    key={effort}
                    type="button"
                    aria-pressed={effort === pick.effort}
                    onClick={() => onChange(withEffort(picks, side, effort))}
                  >
                    {EFFORT_LABEL[effort] ?? effort}
                  </button>
                ))}
              </div>
            </div>
          </section>
        );
      })}
    </div>
  );
}
