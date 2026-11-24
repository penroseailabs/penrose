import { forwardRef, useMemo, useState } from "react";
import { AudioLines, Brain, ChevronRight, MessageSquare, Plus, Search, Settings2 } from "lucide-react";
import type { Chat } from "../api";
import { dayLabel, phoneStatus, taskStatus } from "../lib/status";
import type { Workspace } from "../lib/useWorkspace";
import { ForYouSide } from "./ForYou";

type Props = {
  ws: Workspace;
  onSettings: () => void;
  onMemory: () => void;
  /** Start a new chat from the list (the window also closes the list: App.tsx). */
  onNewTask: () => void;
  /** Switch to the voice screen (the title bar's microphone does the same). */
  onVoice: () => void;
  /** The voice screen is the one showing: the row then switches to the chat view instead. */
  voice?: boolean;
  onChat?: () => void;
  /** A chat was picked from the list (the voice screen, which this list can open over, then gives way to it). */
  onPicked?: () => void;
  shortcut: string;
};

type Section = "tasks";

function storedFolded(): Section[] {
  try {
    return JSON.parse(localStorage.getItem("relay.sidebar.folded") || "[]");
  } catch {
    return [];
  }
}

/** The source list: new task, memory, search, and every task with its state at a glance, by day, under a header
 * that folds. */
const Sidebar = forwardRef<HTMLInputElement, Props>(function Sidebar({ ws, onSettings, onMemory, onNewTask, onVoice, voice, onChat, onPicked, shortcut }, searchRef) {
  const [query, setQuery] = useState("");
  const [folded, setFolded] = useState<Section[]>(storedFolded);
  const mine = useMemo(() => {
    const q = query.trim().toLowerCase();
    const map = new Map<string, Chat[]>();
    // Newest activity first: the chat's last run update (every message starts or steers a run), else its creation.
    const recent = (chat: Chat) => chat.updated || chat.created;
    for (const chat of [...ws.chats].sort((a, b) => recent(b) - recent(a))) {
      if (q && !chat.title.toLowerCase().includes(q)) continue;
      const label = dayLabel(chat.updated || chat.created);
      map.set(label, [...(map.get(label) || []), chat]);
    }
    return [...map.entries()];
  }, [ws.chats, query]);
  const phone = phoneStatus(ws.health, !!ws.device?.connected);
  const account = ws.googleAccount;
  const searching = !!query.trim();

  const fold = (section: Section) =>
    setFolded((current) => {
      const next = current.includes(section) ? current.filter((s) => s !== section) : [...current, section];
      try {
        localStorage.setItem("relay.sidebar.folded", JSON.stringify(next));
      } catch {}
      return next;
    });
  // A search shows every match, folded or not.
  const open = (section: Section) => searching || !folded.includes(section);

  const row = (chat: Chat) => {
    const status = taskStatus(chat.status);
    const flagged = ["work", "wait", "err"].includes(status.tone);
    return (
      <button
        key={chat.id}
        className={`task ${chat.id === ws.chatId ? "active" : ""}`}
        onClick={() => {
          ws.selectChat(chat.id);
          onPicked?.();
        }}
        onPointerEnter={() => ws.prefetchChat(chat.id)}
        onContextMenu={async (event) => {
          if (!window.relay?.menu) return;
          event.preventDefault();
          if ((await window.relay.menu([{ id: "delete", label: "Delete…" }])) === "delete") ws.deleteChat(chat);
        }}
        title={chat.title}
        aria-current={chat.id === ws.chatId ? "page" : undefined}
      >
        <span className="t">{chat.title}</span>
        {flagged && <span className={`sd ${status.tone}`} role="img" aria-label={status.text} title={status.text} />}
      </button>
    );
  };

  const header = (section: Section, title: string, count: number, extra?: React.ReactNode) => (
    <div className="side-section-head">
      <button className="side-section-toggle" onClick={() => fold(section)} aria-expanded={open(section)} disabled={searching}>
        <span>{title}</span>
        {!open(section) && !!count && <span className="count">{count}</span>}
        <ChevronRight size={12} className={open(section) ? "turned" : ""} />
      </button>
      {extra}
    </div>
  );

  const matches = mine.length;
  return (
    <aside className="side">
      <label className="field">
        <Search size={14} />
        <input
          ref={searchRef}
          placeholder="Search"
          aria-label="Search tasks"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === "Escape" && (setQuery(""), e.currentTarget.blur())}
        />
        <span className="kbd">{shortcut} K</span>
      </label>
      <div className="nav">
        <button className="nav-row" onClick={onNewTask}>
          <span className="nav-ic round"><Plus size={14} strokeWidth={2} /></span>
          New chat
          <span className="kbd">{shortcut} N</span>
        </button>
        {voice && onChat ? (
          <button className="nav-row" onClick={onChat}>
            <span className="nav-ic"><MessageSquare size={16} strokeWidth={1.75} /></span>
            Chat mode
          </button>
        ) : (
          <button className="nav-row" onClick={onVoice}>
            <span className="nav-ic"><AudioLines size={16} strokeWidth={1.75} /></span>
            Voice mode
          </button>
        )}
        <button className="nav-row" onClick={onMemory}>
          <span className="nav-ic"><Brain size={16} strokeWidth={1.75} /></span>
          Memory
        </button>
      </div>
      <nav className="tasks scroll" aria-label="Tasks">
        {!searching && <ForYouSide ws={ws} onPicked={onPicked} />}
        {!ws.chats.length && <p className="side-empty">Your tasks will appear here.</p>}
        {!!ws.chats.length && !matches && <p className="side-empty">No tasks match “{query}”.</p>}
        {(!searching || !!mine.length) && !!ws.chats.length && (
          <section className="side-section">
            {header(
              "tasks",
              "Tasks",
              mine.reduce((n, [, list]) => n + list.length, 0),
              <button className="ib side-section-add" aria-label="New chat" title="New chat" onClick={onNewTask}>
                <Plus size={13} />
              </button>,
            )}
            {open("tasks") &&
              (mine.length ? (
                mine.map(([label, list]) => (
                  <section key={label} className="side-day">
                    <div className="group">{label}</div>
                    <div className="list">{list.map(row)}</div>
                  </section>
                ))
              ) : (
                <p className="side-empty">Tasks you start appear here.</p>
              ))}
          </section>
        )}
      </nav>
      <div className="me">
        <span className="avatar" aria-hidden="true">{(account || "p").slice(0, 1).toUpperCase()}</span>
        <span className="who">
          <b title={account}>{account || <span className="wordmark">penrose</span>}</b>
          <small>
            <span className={`sd ${phone.tone}`} />
            {phone.text}
          </small>
        </span>
        <button className="ib" aria-label="Settings" title="Settings" onClick={onSettings}>
          <Settings2 size={15} />
        </button>
      </div>
    </aside>
  );
});

export default Sidebar;
