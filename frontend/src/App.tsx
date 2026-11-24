import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { Mic, Moon, PanelLeft, Smartphone, Sun } from "lucide-react";
import { api } from "./api";
import type { ComposerHandle } from "./components/Composer";
import type { Purpose } from "./lib/uploads";
import Conversation from "./components/Conversation";
import MemorySheet from "./components/Memory";
import ModelsSheet from "./components/Models";
import BriefingSheet from "./components/BriefingSheet";
import ClaudeSheet from "./components/Claude";
import PhonePanel from "./components/PhonePanel";
import PhonesSheet from "./components/Phones";
import Settings from "./components/Settings";
import Setup from "./components/Setup";
import Sidebar from "./components/Sidebar";
import { useTheme } from "./lib/theme";
import { useWorkspace } from "./lib/useWorkspace";
import { usePresence } from "./lib/usePresence";
import { COMPANION } from "./companion/flag";
import { openedAnswer, openedBriefing, openedChat, useNotificationOpen } from "./companion/push";
import { narrowQuery, wideQuery } from "./lib/layout";
import { useBackStack } from "./lib/backStack";

const MODE_KEY = "relay.mode";
/** The phone app opens in the mode it was last left in, voice or chat (kept in this browser). A tapped notification
 *  that opens a chat opens that instead. First launch: voice. */
function startsInVoice() {
  if (!COMPANION || openedChat(location.search)) return false;
  try {
    return localStorage.getItem(MODE_KEY) !== "chat";
  } catch {
    return true;
  }
}

// Developer mode only, so it loads (with its graph library and styles) on first use.
const DeveloperSheet = lazy(() => import("./components/Developer"));
// Voice mode (src/voice): the phone app opens in it; the window reaches it from the title bar.
const loadVoice = () => import("./voice/Voice");
const Voice = lazy(loadVoice);
// The phone app opens talking, so its screen's code and the desk's first session are asked for as the window loads,
// beside the rest of the start, rather than once the window has drawn.
if (startsInVoice()) {
  void loadVoice();
  void import("./voice/desk").then((desk) => desk.openEarly());
}

(navigator.platform);
const MAC = /Mac/.test(navigator.platform);
const SHORTCUT = MAC ? "⌘" : "Ctrl";

const SIDE_MIN = 200;
const SIDE_MAX = 420;
const SIDE_DEFAULT = 248;

/** A number kept in localStorage, if it is there to keep it in. */
function remembered(key: string, fallback: number) {
  try {
    const value = Number(localStorage.getItem(key));
    return Number.isFinite(value) && value ? value : fallback;
  } catch {
    return fallback;
  }
}
function remember(key: string, value: number) {
  try {
    localStorage.setItem(key, String(value));
  } catch {}
}

type Needs = { keys: boolean; phone: boolean };

// Asked as the page loads, beside the setup check, rather than after it. Not by the phone app: its link is its
// sign-in, the computer vouching for every request that comes over it (backend/app/companion/tunnel.py).
let sessionCheck: Promise<boolean> | null = COMPANION
  ? null
  : api<{ authenticated: boolean }>("/auth/session").then(
      (s) => s.authenticated,
      () => false,
    );
function checkSession() {
  const check = sessionCheck ?? api<{ authenticated: boolean }>("/auth/session").then((s) => s.authenticated, () => false);
  sessionCheck = null; // the next caller (after a setup step) asks again
  return check;
}

/** First run asks for whatever is still missing (an API key, the phone); then the app. */
export default function App() {
  const [needs, setNeeds] = useState<Needs | null>(null);
  useEffect(() => {
    // Development: ?setup=keys,phone shows those setup steps whatever is saved.
    const preview = import.meta.env.DEV && new URLSearchParams(location.search).get("setup");
    if (preview) return setNeeds({ keys: !preview.includes("keys"), phone: !preview.includes("phone") });
    if (!window.relay?.setupState) return setNeeds({ keys: true, phone: true });
    window.relay.setupState().then(setNeeds, () => setNeeds({ keys: true, phone: true }));
  }, []);
  if (!needs) return <div className="boot"><span className="mark" aria-hidden="true" /></div>;
  if (!needs.keys || !needs.phone) {
    return <Setup needs={{ keys: !needs.keys, phone: !needs.phone }} onDone={() => setNeeds({ keys: true, phone: true })} />;
  }
  return <Connected />;
}

/** The desktop app signs its window in itself; without a session there is nothing to show but a way back. */
function Connected() {
  const [session, setSession] = useState<"checking" | "ok" | "none">(COMPANION ? "ok" : "checking");
  useEffect(() => {
    const expired = () => setSession("none");
    window.addEventListener("relay-session-expired", expired);
    if (!COMPANION) checkSession().then((ok) => setSession(ok ? "ok" : "none"));
    return () => window.removeEventListener("relay-session-expired", expired);
  }, []);
  if (session === "ok") return <Window />;
  return (
    <div className="boot">
      <span className="mark" aria-hidden="true" />
      {session === "none" && (
        <p>
          penrose lost its connection to this window.
          <br />
          Quit penrose and open it again.
        </p>
      )}
    </div>
  );
}

function Window() {
  const ws = useWorkspace();
  usePresence(); // the user is here: the phone wakes before they ask
  const [theme, setTheme, appearance] = useTheme();
  const [devMode, setDevModeState] = useState(() => {
    try {
      return localStorage.getItem("relay.developer") === "1";
    } catch {
      return false;
    }
  });
  const [sheet, setSheet] = useState<"" | "settings" | "memory" | "models" | "claude" | "phones" | "developer">("");
  // The phone app is voice first: it opens talking, and the full interface is one tap away. The window opens as before.
  // A tapped notification opens its chat instead.
  const [voice, setVoice] = useState(startsInVoice);
  useEffect(() => {
    if (!COMPANION) return;
    try {
      localStorage.setItem(MODE_KEY, voice ? "voice" : "chat");
    } catch {}
  }, [voice]);
  // Wide windows show the phone unless hidden; narrower ones slide it in on demand. The companion has no phone panel.
  const [hidePhone, setHidePhone] = useState(COMPANION);
  const [showPhone, setShowPhone] = useState(false);
  // The companion watches the phone over the conversation, from the title bar: open only while looked at.
  const [watching, setWatching] = useState(false);
  const [isWide, setIsWide] = useState(() => wideQuery().matches);
  // The task list can be dragged wider or narrower and hidden; both are kept.
  const [sideWidth, setSideWidth] = useState(() => Math.min(SIDE_MAX, Math.max(SIDE_MIN, remembered("relay.sidebar", SIDE_DEFAULT))));
  // The companion starts with the list closed: on a phone it is a drawer over the conversation.
  const [sideHidden, setSideHidden] = useState(() => (COMPANION ? !remembered("relay.sidebar.open", 0) : remembered("relay.sidebar.hidden", 0) === 1));
  const [resizing, setResizing] = useState(false);
  useEffect(() => remember(COMPANION ? "relay.sidebar.open" : "relay.sidebar.hidden", (COMPANION ? !sideHidden : sideHidden) ? 1 : 0), [sideHidden]);
  // The drawer closes once a task is picked or started.
  const narrow = COMPANION && narrowQuery().matches;
  useEffect(() => {
    if (narrow) setSideHidden(true);
  }, [ws.chatId, narrow]);
  // The voice screen's top left is this list too. On the phone it is the drawer, which comes up over the voice screen
  // (companion.css); in the window the list sits beside the conversation, so the voice screen gives way to it.
  function openSidebar() {
    setSideHidden(false);
    if (!narrow) setVoice(false);
  }
  // Something the list opens that is not a chat (settings, memory) is a sheet over the voice screen: the drawer goes.
  const fromList = (open: () => void) => () => {
    if (voice && narrow) setSideHidden(true);
    open();
  };
  // The phone's Back button steps back through what was opened (chat, sheet, drawer, voice) instead of leaving the app.
  useBackStack(
    { voice, chat: ws.chatId, sheet, drawer: narrow && !sideHidden, watching },
    (view) => {
      setVoice(view.voice);
      ws.selectChat(view.chat);
      setSheet(view.sheet as typeof sheet);
      if (narrow) setSideHidden(!view.drawer);
      setWatching(view.watching);
    },
    COMPANION,
  );
  const search = useRef<HTMLInputElement>(null);
  const composer = useRef<ComposerHandle>(null);
  // A tapped notification opens the voice screen; its Reply opens the chat, and a reply typed into it is sent from
  // there, as if typed in the composer.
  const [outgoing, setOutgoing] = useState<{ chat: string; text: string } | null>(null);
  // The session a tapped notification was about: the voice screen opens on it, as if its card had been tapped. Tapping
  // the app's own icon has none, and opens silent.
  const [answering, setAnswering] = useState<{ chat: string; n: number } | null>(() => {
    const chat = openedAnswer(location.search);
    return chat ? { chat, n: 0 } : null;
  });
  useEffect(() => {
    if (openedAnswer(location.search)) history.replaceState(null, "", location.pathname + location.hash);
  }, []);
  // A briefing's notification was tapped: its details open as a tap on its card does, once the computer answers.
  const [briefingTapped, setBriefingTapped] = useState<number | null>(() => openedBriefing(location.search));
  useNotificationOpen(
    (chat, reply, text) => {
      setVoice(false);
      setSheet("");
      setWatching(false);
      ws.selectChat(chat);
      if (text) setOutgoing({ chat, text });
      else if (reply) requestAnimationFrame(() => composer.current?.focus());
    },
    (chat) => {
      setSheet("");
      setWatching(false);
      setVoice(true);
      setAnswering(chat ? (was) => ({ chat, n: (was?.n ?? 0) + 1 }) : null);
    },
    (id) => setBriefingTapped(id),
  );
  useEffect(() => {
    if (openedBriefing(location.search)) history.replaceState(null, "", location.pathname + location.hash);
  }, []);
  useEffect(() => {
    if (briefingTapped === null || !ws.health || ws.busy) return;
    setBriefingTapped(null);
    setVoice(false);
    setSheet("");
    setWatching(false);
    void ws.showBriefing({ id: briefingTapped });
  }, [briefingTapped, ws]);
  useEffect(() => {
    if (!outgoing || ws.chatId !== outgoing.chat) return;
    setOutgoing(null);
    void ws.send(outgoing.text).then((sent) => {
      if (!sent) composer.current?.fill(outgoing.text); // not lost: left in the composer to send again
    });
  }, [outgoing, ws]);

  useEffect(() => {
    const media = wideQuery();
    const change = () => setIsWide(media.matches);
    media.addEventListener("change", change);
    return () => media.removeEventListener("change", change);
  }, []);

  // Signing in happens on the phone, so a narrow window brings it into view. The phone app signs in from the
  // conversation's Welcome card instead, so it leaves voice mode for it.
  useEffect(() => {
    if (!ws.needsGoogle) return;
    setShowPhone(true);
    if (COMPANION) setVoice(false);
  }, [ws.needsGoogle]);

  // The app menu's "AI models…"
  const modelsBack = useRef<"" | "settings">("");
  const phonesBack = useRef<"" | "settings">("");
  useEffect(
    () =>
      window.relay?.onOpen?.((what) => {
        if (what !== "models") return;
        modelsBack.current = "";
        setSheet("models");
      }),
    [],
  );

  useEffect(() => {
    const keys = (event: KeyboardEvent) => {
      if (!(MAC ? event.metaKey : event.ctrlKey) || event.altKey || event.shiftKey) return;
      const key = event.key.toLowerCase();
      if (key === "b") {
        event.preventDefault();
        if (voice) openSidebar(); // the voice screen covers the list: bring it up, as the title bar's button does
        else setSideHidden((hidden) => !hidden);
      } else if (key === "n") {
        event.preventDefault();
        setSheet("");
        ws.newTask();
        requestAnimationFrame(() => composer.current?.focus());
      } else if (key === "k") {
        event.preventDefault();
        setSideHidden(false);
        requestAnimationFrame(() => {
          search.current?.focus();
          search.current?.select();
        });
      }
    };
    window.addEventListener("keydown", keys);
    return () => window.removeEventListener("keydown", keys);
  }, [ws, voice, narrow]);

  function setDevMode(on: boolean) {
    setDevModeState(on);
    try {
      localStorage.setItem("relay.developer", on ? "1" : "0");
    } catch {}
  }

  // Drag the edge of the task list; a double click puts it back.
  function dragSide(event: React.PointerEvent<HTMLDivElement>) {
    event.preventDefault();
    const start = event.clientX;
    const from = sideWidth;
    let width = from;
    setResizing(true);
    const move = (e: PointerEvent) => {
      width = Math.min(SIDE_MAX, Math.max(SIDE_MIN, from + e.clientX - start));
      setSideWidth(width);
    };
    const stop = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", stop);
      window.removeEventListener("pointercancel", stop);
      setResizing(false);
      remember("relay.sidebar", width);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", stop);
    window.addEventListener("pointercancel", stop);
  }

  // Files picked on the voice screen: the chat view (the one being talked about, else the one open or a new one) opens
  // with them uploading, to go with the message the user writes.
  function attachFromVoice(files: File[], purpose: Purpose, chatId?: string) {
    setVoice(false);
    if (chatId && chatId !== ws.chatId) ws.selectChat(chatId);
    requestAnimationFrame(() => requestAnimationFrame(() => composer.current?.attach(files, purpose)));
  }

  const phoneOpen = isWide ? !hidePhone : showPhone;
  const resolvedDark = appearance === "dark";

  return (
    <div
      className={`app ${hidePhone ? "no-phone" : ""} ${showPhone ? "show-phone" : ""} ${sideHidden ? "no-side" : ""} ${voice ? "voice-open" : ""} ${resizing ? "resizing" : ""} ${MAC ? "mac" : ""} ${ws.loginAsk ? "takeover" : ""}`}
      style={{ "--side": `${sideWidth}px` } as React.CSSProperties}
    >
      <div className="titlebar">
        <button
          className="ib toggle-side"
          // The voice screen covers the list, so here the button brings the list up (and the voice screen gives way).
          onClick={() => (voice ? openSidebar() : setSideHidden(!sideHidden))}
          aria-label={sideHidden || voice ? "Show tasks" : "Hide tasks"}
          aria-pressed={!sideHidden && !voice}
          title={`${sideHidden || voice ? "Show" : "Hide"} tasks (${SHORTCUT} B)`}
        >
          <PanelLeft size={15} />
        </button>
        <span className="wordmark">penrose</span>
        <span className="grow" />
        {COMPANION && (
          <button
            className={`ib ${watching ? "on" : ""}`}
            onClick={() => setWatching((v) => !v)}
            aria-pressed={watching}
            aria-label={watching ? "Hide the phone" : "Watch the phone"}
            title={watching ? "Hide the phone" : "Watch the phone"}
          >
            <Smartphone size={14} />
          </button>
        )}
        {!COMPANION && (
          <button
            className="ib"
            onClick={() => {
              phonesBack.current = "";
              setSheet("phones");
            }}
            aria-label="penrose on your phone"
            title="penrose on your phone"
          >
            <Smartphone size={14} />
          </button>
        )}
        <button className="ib" onClick={() => setVoice(true)} aria-label="Talk to penrose" title="Talk to penrose">
          <Mic size={14} />
        </button>
        <button
          className="ib"
          onClick={() => setTheme(resolvedDark ? "light" : "dark")}
          aria-label={resolvedDark ? "Light appearance" : "Dark appearance"}
          title={resolvedDark ? "Light appearance" : "Dark appearance"}
        >
          {resolvedDark ? <Sun size={14} /> : <Moon size={14} />}
        </button>
      </div>
      <div className="body">
        <Sidebar ref={search} ws={ws} shortcut={SHORTCUT} onSettings={fromList(() => setSheet("settings"))} onMemory={fromList(() => setSheet("memory"))}
          onVoice={() => {
            setVoice(true);
            if (narrow) setSideHidden(true); // the drawer gives way to the voice screen it was opened from
          }}
          voice={voice}
          onChat={() => {
            setVoice(false); // the chat that was open, as it was
            if (narrow) setSideHidden(true);
          }}
          onPicked={() => {
            setVoice(false);
            if (narrow) setSideHidden(true); // the same chat again changes no id, so the drawer's own close does not fire
          }}
          onNewTask={() => {
            setVoice(false); // a new chat is the chat view, whichever screen the list was opened from
            ws.newTask();
            // On a narrow companion the list is a drawer, so starting a task closes it, whether or not the task
            // changed (the drawer's close on a new chat id does not fire when a new task is already open).
            // The desktop list is a persistent column and stays open.
            if (narrow) setSideHidden(true);
            requestAnimationFrame(() => composer.current?.focus());
          }}
        />
        {COMPANION && <div className="side-backdrop" onClick={() => setSideHidden(true)} aria-hidden="true" />}
        <div
          className="side-resize"
          role="separator"
          aria-orientation="vertical"
          aria-label="Resize the task list"
          onPointerDown={dragSide}
          onDoubleClick={() => {
            setSideWidth(SIDE_DEFAULT);
            remember("relay.sidebar", SIDE_DEFAULT);
          }}
        />
        <Conversation
          ws={ws}
          composer={composer}
          phoneOpen={phoneOpen}
          onTogglePhone={() => (isWide ? setHidePhone((v) => !v) : setShowPhone((v) => !v))}
          devMode={devMode}
          onDeveloper={() => setSheet("developer")}
          onMemory={() => setSheet("memory")}
        />
        {!COMPANION && <PhonePanel ws={ws} />}
        {COMPANION && watching && <div className="watch-backdrop" onClick={() => setWatching(false)} aria-hidden="true" />}
        {COMPANION && watching && <PhonePanel ws={ws} watchOnly onClose={() => setWatching(false)} />}
      </div>
      {/* Before the sheets: both sit at the same level, so the sheets (settings, memory) open over the voice screen. */}
      {voice && (
        <Suspense fallback={<div className="voice" style={{ position: "fixed", inset: 0, zIndex: "var(--z-sheet)" }} aria-hidden="true" />}>
          <Voice ws={ws} onClose={() => setVoice(false)} onSidebar={openSidebar} onAttach={attachFromVoice} answering={answering} />
        </Suspense>
      )}
      {sheet === "settings" && (
        <Settings
          ws={ws}
          theme={theme}
          onTheme={setTheme}
          devMode={devMode}
          onDevMode={setDevMode}
          onMemory={() => setSheet("memory")}
          onModels={() => {
            modelsBack.current = "settings";
            setSheet("models");
          }}
          onClaude={() => setSheet("claude")}
          onPhones={() => {
            phonesBack.current = "settings";
            setSheet("phones");
          }}
          onClose={() => setSheet("")}
        />
      )}
      <BriefingSheet ws={ws} />
      {sheet === "phones" && <PhonesSheet onClose={() => setSheet(phonesBack.current)} />}
      {sheet === "models" && <ModelsSheet onClose={() => setSheet(modelsBack.current)} />}
      {sheet === "claude" && <ClaudeSheet onClose={() => setSheet("settings")} />}
      {sheet === "memory" && (
        <MemorySheet
          provider={ws.provider}
          agent={ws.models?.agent ?? null}
          onOpenChat={(id) => {
            ws.selectChat(id);
            setSheet("");
            setVoice(false);
          }}
          onClose={() => setSheet("")}
        />
      )}
      {sheet === "developer" && devMode && (
        <Suspense fallback={null}>
          <DeveloperSheet ws={ws} onClose={() => setSheet("")} />
        </Suspense>
      )}
    </div>
  );
}
