import AnswerOptions from "./AnswerOptions";
import CallCard from "./CallCard";
import { MessageFiles } from "./Attachments";
import { memo, type RefObject, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Ban, BellRing, ChevronRight, Coins, Eye, KeyRound, ListEnd, LockKeyhole, MessageSquareText, Minus, MoreHorizontal, Pencil, Plus, Search, Smartphone, Sparkles, Trash2, Wrench, X } from "lucide-react";
import type { ChatUsage, Event, Message, Thought, UsageRequest } from "../api";
import { COMPANION } from "../companion/flag";
import { clock, taskStatus } from "../lib/status";
import { CALL, type ChatSearch, type TimelineItem } from "../lib/timeline";
import type { Workspace } from "../lib/useWorkspace";
import Composer, { type ComposerHandle } from "./Composer";
import ContextUse from "./ContextUse";
import { ForYouHome } from "./ForYou";
import Popover from "./Popover";
import PrivateFields from "./PrivateFields";
import Rich from "./Rich";
import RunCard from "./RunCard";
import SecretInput from "./SecretInput";
import Welcome from "./Welcome";

type Props = {
  ws: Workspace;
  phoneOpen: boolean;
  onTogglePhone: () => void;
  devMode: boolean;
  onDeveloper: () => void;
  onMemory: () => void;
  composer: RefObject<ComposerHandle | null>;
};

const OPEN = ["waiting_user", "failed", "interrupted", "stopped"];

const SUGGESTIONS = [
  "Book a table for two this Saturday around 8",
  "Find a blazer under $150 for an evening out",
  "Recharge my mobile plan",
];

/** The conversation: the one large raised surface. */
export default function Conversation({ ws, phoneOpen, onTogglePhone, devMode, onDeveloper, onMemory, composer }: Props) {
  const scroller = useRef<HTMLDivElement>(null);
  const { latest, timeline, privateAsk, loginAsk } = ws;
  const status = taskStatus(latest?.status);
  const sensitive = !!privateAsk?.[1].question?.sensitive;
  // Penrose's question asks for something to be given privately (a card, a password).
  const privateFields = latest?.status === "waiting_user" && !privateAsk ? latest.private_asked : undefined;
  // Relay's latest message is the question when the task waits on a plain answer.
  const askingId =
    latest?.status === "waiting_user" && !privateAsk && !loginAsk
      ? [...timeline].reverse().find((i) => i.type === "message" && i.message.role !== "notification")?.id
      : undefined;
  const answers = askingId && latest?.options?.length ? latest.options : undefined;
  const memoryNotes = useMemo(
    () => ws.events.filter((e) => e.kind === "memories_updated" || e.kind === "memory_failed"),
    [ws.events],
  );
  const tail = timeline.at(-1);
  // A message just sent to an idle chat, or a task that has not planned yet.
  const startingUp =
    ((ws.sending && !ws.running) || (ws.running && !Object.keys(latest?.nodes || {}).length)) &&
    tail?.type !== "thinking" && !(tail?.type === "message" && tail.message.streaming);
  // Stable across renders, so the rows below re-render only when their own content changes.
  const rewindTo = useRef(ws.rewind);
  rewindTo.current = ws.rewind;
  const onRewind = useCallback((id: string, text: string) => rewindTo.current(id, text), []);
  const openChatWith = useRef(ws.selectChat);
  openChatWith.current = ws.selectChat;
  const onOpenChat = useCallback((id: string) => openChatWith.current(id), []);

  // Follow the conversation as it grows, unless the user has scrolled up to read.
  const pinned = useRef(true);
  // The empty chat's page (ForYouHome) is a list read from its top, not a conversation followed to its end.
  const home = !timeline.length && !ws.running;
  useEffect(() => {
    pinned.current = true;
    if (home && scroller.current) scroller.current.scrollTop = 0;
  }, [ws.chatId, home]);
  const asked = !!privateAsk || !!loginAsk;
  useEffect(() => {
    const el = scroller.current;
    if (el && pinned.current && !home) el.scrollTop = el.scrollHeight;
  }, [timeline, startingUp, asked, latest?.status, memoryNotes.length, home]);

  const queued = ws.pending.filter((p) => p.mode === "next");
  const placeholder = ws.running
    ? "Steer penrose, or Ctrl+Enter to send after this task"
    : latest?.status === "stopped"
      ? "What should penrose change?"
      : latest?.status === "waiting_user"
        ? sensitive || loginAsk
          ? "Or send a message instead"
          : "Reply to penrose"
        : timeline.length
          ? "Message penrose"
          : "What should penrose do?";

  return (
    <main className="convo">
      <header className="head">
        <h1>{ws.chat?.title || "New chat"}</h1>
        {status.text && (
          <span className="status">
            <span className={`sd ${status.tone}`} />
            {status.text}
          </span>
        )}
        {!COMPANION && (
          <button
            className={`ib ${phoneOpen ? "on" : ""}`}
            onClick={onTogglePhone}
            aria-pressed={phoneOpen}
            aria-label={phoneOpen ? "Hide phone" : "Show phone"}
            title={phoneOpen ? "Hide phone" : "Show phone"}
          >
            <Smartphone size={15} />
          </button>
        )}
        <Popover
          trigger={(props) => (
            <button className="ib" aria-label="More" title="More" {...props}>
              <MoreHorizontal size={16} />
            </button>
          )}
        >
          {(close) => (
            <>
              {latest && OPEN.includes(latest.status) && (
                <button className="menu-item" onClick={() => (close(), ws.discard())}>
                  <X size={14} /> Discard task
                </button>
              )}
              <button className="menu-item" onClick={() => (close(), onMemory())}>
                <Sparkles size={14} /> What penrose remembers
              </button>
              {devMode && (
                <button className="menu-item" onClick={() => (close(), onDeveloper())}>
                  <Wrench size={14} /> Developer view
                </button>
              )}
              {ws.chat && (
                <>
                  <div className="menu-sep" />
                  <button className="menu-item danger" onClick={() => (close(), ws.deleteChat(ws.chat!))}>
                    <Trash2 size={14} /> Delete task
                  </button>
                </>
              )}
            </>
          )}
        </Popover>
      </header>

      <section
        className="stream scroll"
        ref={scroller}
        onScroll={(e) => {
          const el = e.currentTarget;
          pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
        }}
      >
        {ws.needsGoogle && !timeline.length ? (
          <Welcome ws={ws} />
        ) : !timeline.length && !ws.running ? (
          // An empty chat starts from what penrose has for the user (components/ForYou.tsx); the generic
          // suggestions are for when there is nothing yet.
          <ForYouHome
            ws={ws}
            fallback={
              <div className="welcome appear">
                <h2>What should penrose do?</h2>
                <p>
                  penrose uses apps on its own phone to get things done for you, and checks with you before anything
                  you didn’t ask for.
                </p>
                <div className="suggestions">
                  {SUGGESTIONS.map((s) => (
                    <button key={s} className="btn" onClick={() => composer.current?.fill(s)}>
                      {s}
                    </button>
                  ))}
                </div>
              </div>
            }
          />
        ) : (
          <div className="stream-inner">
            {timeline.map((item, index) => {
              const message = item.type === "message" && item.message.role === "user" ? item.message : null;
              return (
                <Entry
                  key={item.id}
                  item={item}
                  asking={item.id === askingId}
                  canEdit={!!message && ws.rewindable.includes(message.id) && !ws.running && !ws.needsGoogle}
                  dropped={message ? timeline.length - 1 - index : 0}
                  unread={!!message && ws.running && ws.pending.some((p) => p.mode === "steer" && p.message_id === message.id)}
                  busy={!!message && ws.busy}
                  onRewind={onRewind}
                  onOpenChat={onOpenChat}
                />
              );
            })}
            {startingUp && (
              <div className="pending appear">
                <Working label="Getting started" />
              </div>
            )}
            {sensitive && privateAsk && <PrivateAnswer question={privateAsk[1].question?.question || ""} ws={ws} />}
            {privateFields && (
              <PrivateFields
                key={`${latest!.id}:${privateFields.kind}:${privateFields.label || ""}`}
                ask={privateFields}
                onSend={ws.sendPrivate}
                busy={ws.busy}
              />
            )}
            {/* The answers penrose's question offers (ask_user's options): one tap sends one as the user's message. */}
            {answers && <AnswerOptions options={answers} disabled={ws.busy || ws.sending} onPick={(message) => void ws.send(message)} />}
            {loginAsk && !privateAsk && <LoginTakeover question={loginAsk[1].question?.question || ""} ws={ws} />}
            {latest?.status === "stopped" && (
              <p className="note appear">Paused. Tell penrose what to change and it carries on from here.</p>
            )}
            {latest?.status === "interrupted" && (
              <p className="note err appear">
                penrose was interrupted. Check the phone, then tell penrose to carry on.
              </p>
            )}
            {memoryNotes.length > 0 && <MemoryNote events={memoryNotes} onMemory={onMemory} />}
            {/* What the chat waits for among the phone's notifications (backend/app/watches.py). */}
            {ws.watches.map((watch) => (
              <p
                key={watch.id}
                className="notified watching appear"
                title={watch.then ? `When it arrives: ${watch.then}` : "Its notifications are passed to this chat"}
              >
                <Eye size={12} aria-hidden />
                <span>
                  Watching for {watch.what}
                  {watch.then ? " · acts when it arrives" : ""}
                </span>
                <time>until {watch.until}</time>
                <button className="ib" aria-label={`Stop watching for ${watch.what}`} title="Stop watching" onClick={() => ws.stopWatching(watch)}>
                  <X size={12} />
                </button>
              </p>
            ))}
          </div>
        )}
      </section>

      <div className="composer-wrap">
        {ws.error && (
          <div className="error-note" role="alert">
            <span>{ws.error}</span>
            <button className="ib" aria-label="Dismiss" onClick={() => ws.setError("")}>
              <X size={13} />
            </button>
          </div>
        )}
        {ws.usage && ws.requests.length > 0 && <Cost usage={ws.usage} requests={ws.requests} />}
        {queued.length > 0 && (
          <div className="queued" aria-label="Sent after this task">
            {queued.map((entry) => (
              <div key={entry.id} className="queued-item appear">
                <ListEnd size={13} />
                <span className="label">After this task</span>
                <span className="text" title={entry.content}>{entry.content}</span>
                <button
                  className="ib"
                  aria-label="Edit this queued message"
                  title="Edit"
                  onClick={async () => {
                    const text = await ws.unqueue(entry);
                    if (text) composer.current?.fill(text);
                  }}
                >
                  <Pencil size={12} />
                </button>
                <button className="ib" aria-label="Remove this queued message" title="Remove" onClick={() => ws.unqueue(entry)}>
                  <X size={13} />
                </button>
              </div>
            ))}
          </div>
        )}
        {ws.needsGoogle ? (
          <p className="composer-locked">Sign in with Google on the phone to start your first task.</p>
        ) : (
          <Composer ref={composer} ws={ws} placeholder={placeholder} extra={sensitive ? { instead: true } : undefined} />
        )}
      </div>
    </main>
  );
}

type EntryProps = {
  item: TimelineItem;
  asking: boolean;
  /** For the user's messages: whether it can be rewritten, how many entries follow it, whether the task has read
   *  it yet, and whether a send or rewind is under way. */
  canEdit: boolean;
  dropped: number;
  unread: boolean;
  busy: boolean;
  onRewind: (messageId: string, text: string) => Promise<boolean>;
  onOpenChat: (chatId: string) => void;
};

/** The same entry as far as the screen goes: the timeline is rebuilt as events arrive, and its items with it. */
function sameItem(a: TimelineItem, b: TimelineItem) {
  if (a === b) return true;
  if (a.type === "run" && b.type === "run") return a.run === b.run && a.steps.length === b.steps.length;
  if (a.type === "thinking" && b.type === "thinking") return a.thinking === b.thinking;
  if (a.type === "thought" && b.type === "thought") return sameTurns(a.turns, b.turns);
  if (a.type === "search" && b.type === "search") return a.search === b.search;
  if (a.type === "message" && b.type === "message") {
    const [x, y] = [a.message, b.message];
    return (
      a.thinking === b.thinking && sameNotes(a.notes, b.notes) && sameTurns(a.earlier, b.earlier) &&
      x.content === y.content && x.role === y.role && x.created === y.created &&
      x.streaming === y.streaming && x.sending === y.sending && (x.attachments?.length ?? 0) === (y.attachments?.length ?? 0) &&
      (x.attachments?.[0]?.phone_path ?? "") === (y.attachments?.[0]?.phone_path ?? "")
    );
  }
  return false;
}

const sameNotes = (a: string[], b: string[]) => a === b || (a.length === b.length && a.every((note, i) => note === b[i]));
const sameTurns = (a: Thought[], b: Thought[]) =>
  a === b || (a.length === b.length && a.every((t, i) => t.id === b[i].id && t.thinking === b[i].thinking && sameNotes(t.notes, b[i].notes)));

const Entry = memo(EntryView, (a, b) =>
  a.asking === b.asking && a.canEdit === b.canEdit && a.dropped === b.dropped && a.unread === b.unread &&
  a.busy === b.busy && a.onRewind === b.onRewind && a.onOpenChat === b.onOpenChat && sameItem(a.item, b.item),
);

function EntryView({ item, asking, canEdit, dropped, unread, busy, onRewind, onOpenChat }: EntryProps) {
  if (item.type === "run") return <RunCard run={item.run} steps={item.steps} />;
  if (item.type === "search") return <SearchedChats search={item.search} onOpenChat={onOpenChat} />;
  if (item.type === "thinking") return <Thinking text={item.thinking} live />;
  if (item.type === "thought") return <Reasoning turns={item.turns} />;
  const { message } = item;
  // What an app on the phone showed, passed to this chat (backend/app/notifications.py): neither the user's words
  // nor penrose's, so a quiet line of its own.
  if (message.role === "notification")
    return (
      <p className="notified appear" title="A notification on the phone that this chat was waiting for">
        <BellRing size={12} aria-label="Notification" />
        <span>{message.content}</span>
        <time>{clock(message.created)}</time>
      </p>
    );
  if (message.source === CALL) return <CallCard message={message} />;
  if (message.role === "user")
    return <YourMessage message={message} canEdit={canEdit} dropped={dropped} unread={unread} busy={busy} onRewind={onRewind} />;
  const turns = item.thinking || item.notes.length
    ? [...item.earlier, { id: message.id, created: message.created, thinking: item.thinking, notes: item.notes }]
    : item.earlier;
  const body = (
    <div className="relay">
      {turns.length > 0 && <Reasoning turns={turns} />}
      <Rich streaming={!!message.streaming}>{message.content}</Rich>
    </div>
  );
  if (!asking) return body;
  return (
    <div className="ask surface appear">
      <div className="label">
        <span className="sd wait" />
        Needs your answer
        <time>{clock(message.created)}</time>
      </div>
      {body}
    </div>
  );
}

/**
 * One of the user's messages. When it can be rewritten, a pencil opens it for editing: sending the new text takes
 * the conversation back to this point, drops what came after, and carries on from the new message.
 */
type YourMessageProps = Omit<EntryProps, "item" | "asking" | "onOpenChat"> & { message: Message };

function YourMessage({ message, canEdit, dropped, unread, busy, onRewind }: YourMessageProps) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(message.content);
  const box = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    const el = box.current;
    if (!editing || !el) return;
    el.style.height = "0px";
    el.style.height = `${Math.min(el.scrollHeight, 240)}px`;
  }, [editing, text]);
  useEffect(() => {
    if (!editing) return;
    box.current?.focus();
    box.current?.setSelectionRange(text.length, text.length);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editing]);

  if (!editing)
    return (
      <>
        <div className="you-row">
          {canEdit && (
            <button
              className="ib you-edit-btn"
              aria-label="Edit this message"
              title="Edit and rewind to here"
              onClick={() => (setText(message.content), setEditing(true))}
            >
              <Pencil size={13} />
            </button>
          )}
          <div className="you-stack">
            <MessageFiles files={message.attachments || []} />
            {message.content && <div className={`you surface ${message.sending ? "sending" : ""}`}>{message.content}</div>}
          </div>
        </div>
        {unread && <small className="you-unread appear">penrose reads this at its next step</small>}
      </>
    );

  async function submit() {
    if (await onRewind(message.id, text)) setEditing(false);
  }
  return (
    <form
      className="you-edit surface appear"
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
    >
      <textarea
        ref={box}
        rows={1}
        aria-label="Edit your message"
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Escape") setEditing(false);
          else if (e.key === "Enter" && !COMPANION && !e.shiftKey && !e.nativeEvent.isComposing) {
            e.preventDefault();
            submit();
          }
        }}
      />
      <div className="you-foot">
        <small>
          {dropped ? "Everything after this message is removed. " : ""}
          What penrose already did on the phone stays done.
        </small>
        <button type="button" className="btn small" onClick={() => setEditing(false)} disabled={busy}>
          Cancel
        </button>
        <button className="btn small primary" disabled={(!text.trim() && !message.attachments?.length) || busy}>
          {busy ? "Rewinding…" : "Send"}
        </button>
      </div>
    </form>
  );
}

/** A search of earlier conversations, shown as the tool call it is: what was searched, opening to the
 *  conversations found, each a link to it. */
function SearchedChats({ search, onOpenChat }: { search: ChatSearch; onOpenChat: (chatId: string) => void }) {
  const [open, setOpen] = useState(false);
  const found = search.found || [];
  const span = [search.after && `from ${search.after}`, search.before && `to ${search.before}`].filter(Boolean).join(" ");
  const what = search.queries?.length ? search.queries.map((q) => `“${q}”`).join(", ") : span || "earlier chats";
  return (
    <div className="searched appear">
      <button className="searched-toggle" onClick={() => setOpen((v) => !v)} aria-expanded={open} disabled={!found.length}>
        <Search size={12} />
        <span>
          Searched earlier chats for {what}
          {search.queries?.length && span ? ` ${span}` : ""} · {found.length ? `${found.length} found` : "nothing found"}
        </span>
        {found.length > 0 && <ChevronRight size={12} className={open ? "turned" : ""} />}
      </button>
      {open && (
        <ul className="searched-list">
          {found.map((chat) => (
            <li key={chat.chat_id}>
              <button className="link" onClick={() => onOpenChat(chat.chat_id)}>
                <MessageSquareText size={11} /> {chat.title || "Untitled task"}
              </button>
              <time>{chat.date}</time>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** A turn still thinking: an animated row and its latest line; open, the reasoning so far. */
function Thinking({ text }: { text: string; live: true }) {
  const [open, setOpen] = useState(false);
  const peek = !open ? lastLine(text) : "";
  return (
    <div className="thinking thinking-live appear">
      <button className="thinking-toggle" onClick={() => setOpen((v) => !v)} aria-expanded={open}>
        <Working label={text ? "Thinking" : "Working on it"} />
        <ChevronRight size={12} className={open ? "turned" : ""} />
      </button>
      {peek && <div className="thinking-peek">{peek}</div>}
      {open && <div className="thinking-body">{text || "Working out the next step…"}</div>}
    </div>
  );
}

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

/**
 * Finished turns in a row, in one quiet fold: each turn's reasoning, and the notes it kept for penrose's own later
 * turns (the orchestrator's `note`, shown while penrose is being tested). Open, a switch picks reasoning or notes:
 * the reasoning as numbered steps to read, the notes grouped by the step that wrote them.
 */
function Reasoning({ turns }: { turns: Thought[] }) {
  const reasoned = turns.some((turn) => turn.thinking.trim());
  const noteCount = turns.reduce((sum, turn) => sum + turn.notes.length, 0);
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState<"reasoning" | "notes">(reasoned ? "reasoning" : "notes");
  const many = turns.length > 1;
  const label = [
    many ? `Reasoned through ${turns.length} steps` : reasoned ? "Reasoning" : "",
    noteCount ? `${plural(noteCount, "note", "notes")} to self` : "",
  ].filter(Boolean).join(" · ");
  const both = reasoned && noteCount > 0;
  const shown = both ? tab : reasoned ? "reasoning" : "notes";
  return (
    <div className="thinking">
      <button className="thinking-toggle" onClick={() => setOpen((v) => !v)} aria-expanded={open}>
        {label}
        <ChevronRight size={12} className={open ? "turned" : ""} />
      </button>
      {open && (
        <div className="reasoning appear">
          {both && (
            <div className="reasoning-switch" role="tablist">
              <button role="tab" aria-selected={shown === "reasoning"} className={shown === "reasoning" ? "on" : ""} onClick={() => setTab("reasoning")}>
                Reasoning
              </button>
              <button role="tab" aria-selected={shown === "notes"} className={shown === "notes" ? "on" : ""} onClick={() => setTab("notes")}>
                Notes <span>{noteCount}</span>
              </button>
            </div>
          )}
          {shown === "reasoning" ? (
            <ol className="reasoning-steps">
              {turns.map((turn, i) =>
                turn.thinking.trim() ? (
                  <li key={turn.id} className={many ? "" : "single"}>
                    {many && <p className="reasoning-meta">Step {i + 1} · {clock(turn.created)}</p>}
                    {paragraphs(turn.thinking).map((para, j) => (
                      <p key={j}>{para}</p>
                    ))}
                  </li>
                ) : null,
              )}
            </ol>
          ) : (
            // Each step's notes beside its dot on the same line as the reasoning's steps, so no headings are needed.
            <div className={`reasoning-notes ${many ? "" : "single"}`} aria-label="Notes to self">
              {turns.map((turn, i) =>
                turn.notes.length ? (
                  <section key={turn.id} aria-label={many ? `Step ${i + 1}` : undefined}>
                    <ul>
                      {turn.notes.map((note, j) => (
                        <li key={j}>{note}</li>
                      ))}
                    </ul>
                  </section>
                ) : null,
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** Reasoning text as paragraphs: models start each thought on a line of its own. */
const paragraphs = (text: string) => text.trim().split(/\n+/).map((para) => para.trim()).filter(Boolean);

/** Something is in progress: three bouncing dots and a label with a light sweeping across it. */
function Working({ label }: { label: string }) {
  return (
    <span className="working" role="status">
      <span className="working-dots" aria-hidden="true">
        <i />
        <i />
        <i />
      </span>
      <span className="shimmer">{label}…</span>
    </span>
  );
}

/** The end of the reasoning so far, for a one-line preview. */
function lastLine(text: string) {
  const line = text.trim().split("\n").filter(Boolean).at(-1) || "";
  return line.length > 160 ? "…" + line.slice(-160) : line;
}

/** A code or password for the phone: typed into the phone, never shown to the model or kept in logs. */
function PrivateAnswer({ question, ws }: { question: string; ws: Workspace }) {
  const [value, setValue] = useState("");
  return (
    <form
      className="ask surface appear"
      onSubmit={async (e) => {
        e.preventDefault();
        if (await ws.send(value, { sensitive: true })) setValue("");
      }}
    >
      <div className="label">
        <LockKeyhole size={12} />
        Private answer
      </div>
      <p>{question}</p>
      <div className="row">
        <SecretInput
          autoComplete="one-time-code"
          aria-label="Private answer"
          placeholder="Typed straight into the phone"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          autoFocus
        />
        <button className="btn primary" disabled={!value || ws.busy}>Send to phone</button>
      </div>
      <small className="ask-note">Not shown to the AI model and not saved in any log.</small>
    </form>
  );
}

/** Relay hands a sign-in to the user: they sign in on the phone (grown beside the conversation) and say when
 * they are done, or give their user id and let Relay carry on with their password. */
function LoginTakeover({ question, ws }: { question: string; ws: Workspace }) {
  const [withPassword, setWithPassword] = useState(false);
  const [userId, setUserId] = useState("");
  return (
    <form
      className="ask surface appear login-ask"
      onSubmit={async (e) => {
        e.preventDefault();
        if (!userId.trim()) return;
        const sent = await ws.send(
          `Sign in with my user id and password instead. My user id is ${userId.trim()}. Choose the password sign-in, enter the user id, then focus the password field and ask me for the password with ask_for_password_or_code.`,
          { instead: true },
        );
        if (sent) setUserId("");
      }}
    >
      <div className="label">
        <Smartphone size={12} />
        Your turn on the phone
      </div>
      <p>{question}</p>
      <p className="ask-note">
        Sign in on the phone beside this conversation (click to tap, type once it’s selected), then tell penrose you
        are done. penrose is paused and does not read the screen while you do.
      </p>
      {withPassword ? (
        <div className="row">
          <input
            className="input"
            aria-label="Your user id"
            placeholder="Your user id, email or phone number"
            value={userId}
            onChange={(e) => setUserId(e.target.value)}
            autoFocus
          />
          <button className="btn primary" disabled={!userId.trim() || ws.busy}>Continue</button>
          <button type="button" className="btn" onClick={() => setWithPassword(false)}>Back</button>
        </div>
      ) : (
        <div className="row">
          <button
            type="button"
            className="btn primary"
            disabled={ws.busy}
            onClick={() => ws.send("I signed in on the phone myself. Continue from the screen.", { instead: true })}
          >
            I’m signed in
          </button>
          <button type="button" className="btn" disabled={ws.busy} onClick={() => setWithPassword(true)}>
            <KeyRound size={12} /> Let penrose type my password
          </button>
        </div>
      )}
      {withPassword && (
        <small className="ask-note">penrose enters the user id, then asks for the password privately: it is typed straight into the phone.</small>
      )}
    </form>
  );
}

/** What the chat's model requests cost (1 credit = 1 US cent): the total, opening to every request as it comes. */
function Cost({ usage, requests }: { usage: ChatUsage; requests: UsageRequest[] }) {
  const [open, setOpen] = useState(false);
  const list = useRef<HTMLOListElement>(null);
  useEffect(() => {
    if (open && list.current) list.current.scrollTop = list.current.scrollHeight;
  }, [open, requests.length]);
  const n = (value: number) => value.toLocaleString();
  return (
    <div className="cost">
      <div className="cost-row">
        <button className="cost-toggle" onClick={() => setOpen((v) => !v)} aria-expanded={open}>
          <Coins size={12} />
          <span>
            {credits(usage.credits)} credits · {usage.requests} {usage.requests === 1 ? "request" : "requests"}
          </span>
          <ChevronRight size={12} className={open ? "turned" : ""} />
        </button>
        <ContextUse requests={requests} />
      </div>
      {open && (
        <ol className="cost-list" ref={list}>
          {requests.map((r) => (
            <li key={r.id} title={JSON.stringify(r.usage, null, 1)}>
              <time>{clock(r.created)}</time>
              <span className="who">{r.stage || "Model"}</span>
              <span className="model">{r.model}</span>
              <span className="tokens">
                {n(r.tokens.input)} in ({n(r.tokens.cached)} cached) · {n(r.tokens.output)} out
              </span>
              <span className="amount">{r.credits == null ? "no price" : credits(r.credits)}</span>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

/** Credits to show: two decimals, or three significant digits for less than a hundredth. */
function credits(value: number) {
  return value >= 0.01 || value === 0 ? value.toFixed(2) : value.toPrecision(3);
}

/** What the memory updates of a chat did, in one collapsible: a one-line summary; the changes open on tap. */
function MemoryNote({ events, onMemory }: { events: Event[]; onMemory: () => void }) {
  const [open, setOpen] = useState(false);
  const added: string[] = [];
  const removed: string[] = [];
  const declined: string[] = [];
  let failed = 0;
  for (const event of events) {
    if (event.kind === "memory_failed") {
      failed++;
      continue;
    }
    for (const m of (event.payload.added as { description: string; content: string }[]) || []) added.push(m.description || m.content);
    removed.push(...((event.payload.removed as string[]) || []));
    declined.push(...((event.payload.declined as string[]) || []));
  }
  const summary = [
    added.length && `Remembered ${added.length}`,
    removed.length && `Forgot ${removed.length}`,
    declined.length && `Not kept ${declined.length}`,
    failed && "Memory update failed; penrose tries again after the next task",
  ]
    .filter(Boolean)
    .join(" · ");
  // One list, as the notes to self are drawn. The summary already names what kind of change each count is; only
  // when kinds are mixed does each row carry a small mark of its own (+ remembered, − forgot, ⊘ not kept).
  const rows = [
    ...added.map((text) => ({ text, kind: "Remembered", Icon: Plus })),
    ...removed.map((text) => ({ text, kind: "Forgot", Icon: Minus })),
    ...declined.map((text) => ({ text, kind: "Not kept", Icon: Ban })),
  ];
  const mixed = [added, removed, declined].filter((items) => items.length).length > 1;
  return (
    <div className="memory-note-wrap">
      <button className="memory-note" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
        <Sparkles size={12} />
        <span>{summary || "Memory updated"}</span>
        <ChevronRight size={12} style={{ transform: open ? "rotate(90deg)" : undefined, transition: "transform 0.2s" }} />
      </button>
      {open && (
        <div className="reasoning memory-panel appear">
          {rows.length > 0 && (
            <ul className="memory-rows">
              {rows.map(({ text, kind, Icon }, i) => (
                <li key={i}>
                  {mixed && <Icon size={13} aria-label={kind} />}
                  <span>{text}</span>
                </li>
              ))}
            </ul>
          )}
          <button className="memory-open" onClick={onMemory}>
            Open memory <ChevronRight size={12} />
          </button>
        </div>
      )}
    </div>
  );
}
