import { useEffect, useRef, useState } from "react";
import { ArrowUpRight, LockKeyhole } from "lucide-react";
import { confirmDialog } from "../lib/confirm";
import { loadKeys, saveKey, type SavedKeys } from "../lib/keys";
import SecretInput from "./SecretInput";
import Sheet from "./Sheet";

const MODELS: { id: ProviderId; name: string; by: string; url: string; placeholder: string }[] = [
  { id: "anthropic", name: "Claude", by: "Anthropic", url: "https://platform.claude.com/settings/keys", placeholder: "sk-ant-…" },
  { id: "openai", name: "GPT", by: "OpenAI", url: "https://platform.openai.com/api-keys", placeholder: "sk-…" },
  { id: "deepseek", name: "DeepSeek", by: "Lowest cost", url: "https://platform.deepseek.com/api_keys", placeholder: "sk-…" },
];

type Saved = SavedKeys;

/**
 * The API keys Relay's agents run on, one row per provider. A key is checked with its provider before it is
 * kept; the desktop app stores it encrypted and restarts the services that use it. The same rows work from a
 * linked phone, which sends the key to the computer over the tunnel (lib/keys.ts).
 */
export function ModelKeys({ onSaved, open: initialOpen }: { onSaved?: (saved: Saved) => void; open?: ProviderId }) {
  const [saved, setSaved] = useState<Saved | null>(null);
  const [open, setOpen] = useState<ProviderId | "">("");

  function show(keys: Saved) {
    setSaved(keys);
    onSaved?.(keys);
    return keys;
  }

  async function load() {
    const keys = await loadKeys();
    return keys ? show(keys) : undefined;
  }

  useEffect(() => {
    load().then((keys) => {
      // First run: the first model is ready to take a key.
      if (keys && initialOpen && !Object.values(keys).some(Boolean)) setOpen(initialOpen);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="keys">
      {MODELS.map((model) => (
        <KeyRow
          key={model.id}
          model={model}
          saved={saved?.[model.id] || ""}
          open={open === model.id}
          onOpen={(on) => setOpen(on ? model.id : "")}
          onSaved={async (keys) => {
            setOpen("");
            // A phone's answer carries the keys: the API restarts a moment later, so asking it again could miss.
            if (keys) show(keys);
            else await load();
          }}
        />
      ))}
    </div>
  );
}

function KeyRow({
  model,
  saved,
  open,
  onOpen,
  onSaved,
}: {
  model: (typeof MODELS)[number];
  saved: string;
  open: boolean;
  onOpen: (on: boolean) => void;
  onSaved: (keys?: Saved) => Promise<void>;
}) {
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (open) input.current?.focus();
    else {
      setValue("");
      setError("");
    }
  }, [open]);

  async function save(key: string) {
    setBusy(true);
    setError("");
    const result = await saveKey(model.id, key);
    setBusy(false);
    if (result.ok) await onSaved(result.keys);
    else setError(result.error || "The key could not be saved.");
  }

  async function remove() {
    const ok = window.relay?.confirm
      ? await window.relay.confirm({
          message: `Remove the ${model.name} key?`,
          detail: "penrose stops using this model until a key is added again.",
          action: "Remove",
        })
      : await confirmDialog({
          message: `Remove the ${model.name} key?`,
          detail: "penrose stops using this model until a key is added again.",
          action: "Remove",
        });
    if (ok) await save("");
  }

  return (
    <div className="key">
      <div className="key-head">
        <div className="who">
          <b>{model.name}</b>
          {saved ? (
            <small>
              <span className="sd done" />
              Connected, key ending {saved.replace("…", "")}
            </small>
          ) : (
            <small>{model.by}</small>
          )}
        </div>
        {saved && !open ? (
          <>
            <button className="btn quiet" onClick={remove} disabled={busy}>Remove</button>
            <button className="btn small" onClick={() => onOpen(true)}>Replace</button>
          </>
        ) : (
          !open && <button className="btn small" onClick={() => onOpen(true)}>Add key</button>
        )}
      </div>
      {open && (
        <form
          className="key-entry appear"
          onSubmit={(event) => {
            event.preventDefault();
            if (value.trim()) save(value.trim());
          }}
        >
          <SecretInput
            ref={input}
            autoComplete="off"
            spellCheck={false}
            placeholder={model.placeholder}
            aria-label={`${model.name} API key`}
            value={value}
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={(e) => e.key === "Escape" && (e.stopPropagation(), onOpen(false))}
            disabled={busy}
          />
          <button type="button" className="btn quiet" onClick={() => onOpen(false)} disabled={busy}>
            Cancel
          </button>
          <button type="submit" className="btn small primary" disabled={busy || !value.trim()}>
            {busy ? "Checking…" : "Save"}
          </button>
        </form>
      )}
      {open && (
        <p className="key-note">
          {error ? (
            <span className="err">{error}</span>
          ) : (
            <>
              Create one at{" "}
              <a href={model.url} target="_blank" rel="noreferrer">
                {new URL(model.url).hostname}
                <ArrowUpRight size={11} />
              </a>
            </>
          )}
        </p>
      )}
    </div>
  );
}

export function KeysFoot() {
  return (
    <div className="card-foot">
      <LockKeyhole size={13} />
      Keys are encrypted on this computer and only ever sent to their own provider.
    </div>
  );
}

export const SUBSCRIPTION_NOTE =
  "ChatGPT and Claude subscriptions can’t be used here; an API key is separate and billed per use.";

export default function ModelsSheet({ onClose }: { onClose: () => void }) {
  return (
    <Sheet title="AI models" onClose={onClose}>
      <p className="sheet-lede">penrose’s agents run on your own API keys. Add any of these, and pick between them as you write a task.</p>
      <ModelKeys />
      <KeysFoot />
      <p className="key-note">{SUBSCRIPTION_NOTE}</p>
    </Sheet>
  );
}
