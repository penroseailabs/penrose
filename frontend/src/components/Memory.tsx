import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { Check, MessageSquareText, Pencil, Search, Sparkles, Trash2, X } from "lucide-react";
import { api, type Memory, type Provider } from "../api";
import type { ModelPick } from "../lib/models";
import Sheet from "./Sheet";

type Props = {
  provider: Provider;
  /** The agent picked in the composer: the memory agent sorts a note you add on it. */
  agent: ModelPick | null;
  /** Open the conversation a memory was learned from. */
  onOpenChat: (chatId: string) => void;
  onClose: () => void;
};

/** How long a forgotten memory takes to fold out of the list: the transition in app.css (.memory-list li). */
const LEAVE_MS = 200;

const GROUPS: { type: Memory["type"]; title: string; empty: string }[] = [
  { type: "user", title: "About you", empty: "Nothing yet. Tell penrose your name, where you live, who is in your household." },
  { type: "person", title: "People", empty: "Who the people in your life are to you, and how you reach them." },
  { type: "thing", title: "Things, places and routines", empty: "What you own, where you go, what you order or do regularly." },
  { type: "feedback", title: "How you like things done", empty: "penrose keeps your corrections and confirmed choices here." },
  { type: "open", title: "Open matters", empty: "What is pending right now: an order, a reply you owe, a plan in the making. Each lapses on its last day." },
  { type: "reference", title: "Where things are", empty: "Which app, account, group or document holds what." },
  { type: "app", title: "What penrose learned about apps", empty: "Logins, where things are, what gets in the way: learned as penrose works." },
];

/** What Relay remembers about you: every memory by kind, add, edit, and forget. */
export default function MemorySheet({ provider, agent, onOpenChat, onClose }: Props) {
  const [memories, setMemories] = useState<Memory[]>([]);
  const [auto, setAuto] = useState(true);
  const [value, setValue] = useState("");
  const [adding, setAdding] = useState(false);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");
  const [query, setQuery] = useState("");
  const refresh = useCallback(
    () =>
      api<{ memories: Memory[] }>("/memories")
        .then((r) => setMemories(r.memories))
        .catch((e) => setError(e.message)),
    [],
  );
  // An edit: the row the server kept takes its place in the list. False, with the error shown, when it was not saved.
  const edited = useCallback(async (change: Promise<Memory>) => {
    setError("");
    try {
      const row = await change;
      setMemories((list) => list.map((m) => (m.id === row.id ? row : m)));
      return true;
    } catch (e) {
      setError((e as Error).message);
      return false;
    }
  }, []);
  // Forgetting is the tap's own doing, not the server's reply: the row folds away at once (`leaving`, for as long
  // as the fold takes) and then stays out of the list (`gone`: ids are never reused, so a list fetched before the
  // delete landed cannot bring it back). The delete goes out meanwhile, and nothing waits for it, so tapping down
  // a list keeps pace with the finger. Only if it fails does the row return, beside the error.
  const [leaving, setLeaving] = useState<ReadonlySet<string>>(new Set());
  const [gone, setGone] = useState<ReadonlySet<string>>(new Set());
  const without = (set: ReadonlySet<string>, id: string) => new Set([...set].filter((x) => x !== id));
  const forget = (m: Memory) => {
    setError("");
    setNote("");
    setLeaving((s) => new Set(s).add(m.id));
    const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const fold = window.setTimeout(() => {
      setGone((s) => new Set(s).add(m.id));
      setLeaving((s) => without(s, m.id));
    }, still ? 0 : LEAVE_MS);
    api(`/memories/${m.id}`, "DELETE").catch((e) => {
      window.clearTimeout(fold);
      setLeaving((s) => without(s, m.id));
      setGone((s) => without(s, m.id));
      const what = m.description.length > 60 ? `${m.description.slice(0, 60).trimEnd()}…` : m.description;
      setError(`Could not forget “${what}”: ${(e as Error).message}`);
    });
  };
  useEffect(() => {
    refresh();
    api<{ auto: boolean }>("/memory-settings").then((s) => setAuto(s.auto)).catch(() => {});
  }, [refresh]);

  const add = async () => {
    setAdding(true);
    setError("");
    try {
      const r = await api<{ saved: Memory[]; removed: string[]; placed: boolean; declined: string[] }>("/memories", "POST", { content: value, provider, agent });
      setValue("");
      setNote(
        r.declined?.length
          ? `Not kept: ${r.declined.join(" · ")}`
          : r.placed
            ? r.removed.length
              ? `Kept, and ${r.removed.length} older ${r.removed.length === 1 ? "memory" : "memories"} merged into it.`
              : "Kept."
            : "Kept as written (penrose could not sort it right now).",
      );
      refresh();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setAdding(false);
    }
  };

  // Every word typed must appear somewhere in the memory, in any order.
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  const searching = words.length > 0;
  const matches = (m: Memory) => {
    const text = `${m.description} ${m.content} ${m.app || ""}`.toLowerCase();
    return words.every((w) => text.includes(w));
  };
  const shown = memories.filter((m) => !gone.has(m.id));
  const found = shown.some(matches);

  return (
    <Sheet title="What penrose remembers" onClose={onClose} className="memory">
      <div className="memory-body scroll">
      <p className="sheet-lede">
        Lasting details you share and lessons penrose learns as it works, so it can get straight to work. Never
        passwords, codes, card numbers or ID numbers; health, religion, politics and similar topics only when you
        ask penrose to remember them.
      </p>
      <div className="set-row">
        <div>
          <b>Learn from my tasks</b>
          <small>Pick up details from conversations. When off, penrose still remembers what you ask it to.</small>
        </div>
        <button
          className="switch"
          role="switch"
          aria-checked={auto}
          aria-label="Learn from my tasks"
          onClick={async () => {
            const next = !auto;
            setAuto(next);
            try {
              await api("/memory-settings", "PUT", { auto: next });
            } catch (e) {
              setAuto(!next);
              setError((e as Error).message);
            }
          }}
        />
      </div>
      <form
        className="memory-add"
        onSubmit={(e) => {
          e.preventDefault();
          add();
        }}
      >
        <input
          className="input"
          aria-label="Something to remember"
          placeholder="For example: I’m vegetarian and live in South Austin"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          disabled={adding}
        />
        <button className="btn" disabled={!value.trim() || adding}>
          {adding ? "Sorting…" : "Add"}
        </button>
      </form>
      {note && <p className="note">{note}</p>}
      {error && <p className="note err">{error}</p>}
      {/* Finds a memory by any of its words: what it says, its summary, the app it is about. */}
      <label className="field memory-search">
        <Search size={14} />
        <input
          type="search"
          aria-label="Search memories"
          placeholder="Search memories"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === "Escape" && query && (e.stopPropagation(), setQuery(""))}
        />
        {query && (
          <button type="button" className="ib" aria-label="Clear search" title="Clear" onClick={() => setQuery("")}>
            <X size={13} />
          </button>
        )}
      </label>
      {searching && !found && <p className="note">No memories match “{query.trim()}”.</p>}
      <div className="memory-groups">
        {GROUPS.map((group) => {
          const items = shown.filter((m) => m.type === group.type && matches(m));
          if (searching && !items.length) return null;
          return (
            <section key={group.type} className="memory-group">
              <h3>
                {group.title} <span>{items.filter((m) => !leaving.has(m.id)).length || ""}</span>
              </h3>
              {!items.length && <p className="empty">{group.empty}</p>}
              <ul className="memory-list">
                {items.map((m) => (
                  <Item
                    key={m.id}
                    memory={m}
                    leaving={leaving.has(m.id)}
                    onChange={edited}
                    onForget={() => forget(m)}
                    onOpenChat={onOpenChat}
                  />
                ))}
              </ul>
            </section>
          );
        })}
      </div>
      </div>
    </Sheet>
  );
}

/** One memory: its summary line, its words (the Why and How to apply lines set apart), where it came from. */
function Item({
  memory: m,
  leaving,
  onChange,
  onForget,
  onOpenChat,
}: {
  memory: Memory;
  /** Forgotten a moment ago: folding out of the list. */
  leaving: boolean;
  /** An edit on its way to the server: true once it is saved. */
  onChange: (change: Promise<Memory>) => Promise<boolean>;
  onForget: () => void;
  onOpenChat: (chatId: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [description, setDescription] = useState(m.description);
  const [content, setContent] = useState(m.content);
  const [saving, setSaving] = useState(false);
  const lines = m.content.split("\n").filter(Boolean);
  const fact = lines.filter((l) => !/^(Why|How to apply):/i.test(l)).join(" ");
  const notes = lines.filter((l) => /^(Why|How to apply):/i.test(l));
  const updated = new Date(m.updated * 1000).toLocaleDateString();
  // Forgotten: the row folds from its own height to nothing, so the rows below slide up instead of jumping. A row
  // that comes back (the delete failed) takes its natural height again.
  const row = useRef<HTMLLIElement>(null);
  useLayoutEffect(() => {
    const el = row.current;
    if (!el) return;
    if (!leaving) {
      el.style.height = "";
      return;
    }
    el.style.height = `${el.offsetHeight}px`;
    void el.offsetHeight; // the starting height is laid out before the fold begins
    el.style.height = "0px";
  }, [leaving]);

  // The editor stays, with the words as typed, until the server has them: nothing flashes back to the old text,
  // and an edit that could not be saved is still there to try again.
  const save = async () => {
    setSaving(true);
    const kept = await onChange(
      api<Memory>(`/memories/${m.id}`, "PUT", { content: content.trim(), description: description.trim() }),
    );
    setSaving(false);
    if (kept) setEditing(false);
  };

  if (editing) {
    return (
      <li className="editing">
        <div>
          <input
            className="input"
            aria-label="Summary"
            value={description}
            maxLength={150}
            onChange={(e) => setDescription(e.target.value)}
          />
          <textarea
            className="input"
            aria-label="Memory"
            value={content}
            maxLength={600}
            rows={3}
            onChange={(e) => setContent(e.target.value)}
            autoFocus
          />
        </div>
        <button className="ib" aria-label="Save" title="Save" disabled={saving || !description.trim()} onClick={save}>
          <Check size={14} />
        </button>
        <button className="ib" aria-label="Cancel" title="Cancel" onClick={() => (setEditing(false), setContent(m.content), setDescription(m.description))}>
          <X size={14} />
        </button>
      </li>
    );
  }
  return (
    <li ref={row} className={leaving ? "leaving" : undefined} aria-hidden={leaving || undefined}>
      <div>
        <b>{m.description}</b>
        {fact && fact !== m.description && <p>{fact}</p>}
        {notes.map((n) => (
          <p key={n} className="aside">
            {n}
          </p>
        ))}
        <small>
          {m.type === "app" && m.app && <span className="pkg">{m.app}</span>}
          {m.source === "chat" ? (
            m.chat_id ? (
              <button className="link" onClick={() => onOpenChat(m.chat_id!)}>
                <MessageSquareText size={10} /> From a conversation
              </button>
            ) : (
              <>
                <Sparkles size={10} /> Learned from a task
              </>
            )
          ) : m.source === "reflection" ? (
            <>
              <Sparkles size={10} /> Noticed across your tasks
            </>
          ) : (
            "Added by you"
          )}
          {" · "}
          {updated}
          {m.expires && ` · until ${m.expires}`}
        </small>
      </div>
      <button className="ib" aria-label="Edit" title="Edit" onClick={() => setEditing(true)}>
        <Pencil size={13} />
      </button>
      <button
        className="ib"
        aria-label="Forget this"
        title="Forget this"
        disabled={leaving}
        onClick={onForget}
      >
        <Trash2 size={14} />
      </button>
    </li>
  );
}
