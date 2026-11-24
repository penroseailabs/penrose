import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { confirmDialog } from "./confirm";
import {
  api,
  type Attachment,
  type Briefing,
  type BriefingDetail,
  type Chat,
  type ChatUsage,
  type Device,
  type Event,
  type ForYou,
  type GooglePhone,
  type Health,
  type Message,
  type Pending,
  type Watch,
  PROVIDERS,
  type Provider,
  type Run,
  type Thought,
  type UsageRequest,
} from "../api";
import { COMPANION } from "../companion/flag";
import { type Catalog, effectivePicks, type Picks, storedPicks, storePicks } from "./models";
import { releaseToComputer, sendPhoneLocation, usePhoneLocation } from "./location";
import { onUpdate } from "./updates";
import { ACTIVE } from "./status";
import { collectEvents, conversation, lastEventId } from "./timeline";

type Detail = {
  messages: Message[];
  thoughts?: Thought[];
  runs: Run[];
  rewindable: string[];
  pending?: Pending[];
  watches?: Watch[];
  usage?: ChatUsage | null;
  requests?: UsageRequest[];
};
const EMPTY: Detail = { messages: [], runs: [], rewindable: [] };
// Until the list has been heard: `apps_on` -1, so nothing takes the user for one who has chosen no app.
const NOTHING_FOR_YOU: ForYou = { ready: [], waiting: [], came_in: [], more: 0, unseen: 0, apps_on: -1 };
/** mode, while a task works: "steer" (Enter) reaches it at its next step, "next" (Ctrl+Enter) waits for it to end. */
export type SendExtra = {
  sensitive?: boolean;
  instead?: boolean;
  mode?: "steer" | "next";
  /** Files that go with the message (finished uploads, lib/uploads.ts). */
  attachments?: Attachment[];
};

/** Events that only stream a model's words; every other kind means the chat itself changed. */
const STREAMING = new Set(["stage_delta", "stage_thinking", "stage_image", "usage"]);
/** The fewest milliseconds between two reloads of the open chat while events pour in. */
const RELOAD_GAP = 1000;
/** While a task works, a slow reload in case an event was missed (events drive the rest). */
const SAFETY_RELOAD = 15000;

function stored<T extends string>(key: string, fallback: T, allowed?: readonly string[]): T {
  try {
    const value = localStorage.getItem(key);
    if (value && (!allowed || allowed.includes(value))) return value as T;
  } catch {}
  return fallback;
}
/** Map.set that keeps the map to its `limit` newest entries: a window open for days opens many chats. */
const CACHED = 30;
function remember<K, V>(map: Map<K, V>, key: K, value: V, limit = CACHED) {
  map.delete(key); // re-inserted as the newest
  map.set(key, value);
  while (map.size > limit) map.delete(map.keys().next().value as K);
}
/** The previous value when the new one says the same, so React skips the re-render. */
function keep<T>(previous: T, next: T): T {
  return JSON.stringify(previous) === JSON.stringify(next) ? previous : next;
}

/** Everything the window shows, and what the user can do with it. */
export function useWorkspace() {
  const [chats, setChats] = useState<Chat[]>([]);
  const [chatId, setChatId] = useState("");
  const [messages, setMessages] = useState<Message[]>([]);
  // penrose's notes to itself and the reasoning of its silent turns (shown while it is being tested).
  const [thoughts, setThoughts] = useState<Thought[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  // The user's messages that can be rewritten, and a counter that reopens the event stream after a rewind.
  const [rewindable, setRewindable] = useState<string[]>([]);
  // Messages sent while the task works that it has not read (steer) or that wait for it to end (next).
  const [pending, setPending] = useState<Pending[]>([]);
  // What the chat waits for among the phone's notifications.
  const [watches, setWatches] = useState<Watch[]>([]);
  const [streamKey, setStreamKey] = useState(0);
  // What the chat's model requests cost, each and in all.
  const [usage, setUsage] = useState<ChatUsage | null>(null);
  const [requests, setRequests] = useState<UsageRequest[]>([]);
  const [events, setEvents] = useState<Event[]>([]);
  // The user's messages shown at once, before the server has them.
  const [outbox, setOutbox] = useState<Message[]>([]);
  const [health, setHealth] = useState<Health | null>(null);
  // What penrose has for the user: briefings, the chats that wait for an answer, what else came in.
  const [forYou, setForYou] = useState<ForYou>(NOTHING_FOR_YOU);
  // The briefing being opened (openBriefing): its card says so until its chat is on screen.
  const [opening, setOpening] = useState<number | null>(null);
  // The briefing whose details are on screen (showBriefing): what the feed has of it at once, `detail` once /look answers.
  const [shownBriefing, setShown] = useState<{ item: Briefing; detail: BriefingDetail | null } | null>(null);
  const [devices, setDevices] = useState<Device[]>([]);
  const [deviceId, setDeviceId] = useState("");
  // The Google account on the phone: undefined until read, null when the phone cannot say.
  const [googleAccounts, setGoogleAccounts] = useState<string[] | null | undefined>(undefined);
  // The models a task runs on (lib/models.ts): the backend's catalog, and what the user picked (kept in this browser).
  // Before the user picks, the provider they used to choose stands in, Claude at first.
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [chosen, setChosen] = useState<Picks | null>(storedPicks);
  const [fallbackProvider] = useState<Provider>(() =>
    stored("relay.provider", "anthropic" as Provider, PROVIDERS.map((p) => p.id)),
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  // The open chat, readable from callbacks that outlive a render.
  const chatRef = useRef(chatId);
  // Every chat seen this session, so switching back shows it at once; and each run's events, to resume streams.
  const details = useRef(new Map<string, Detail>());
  const runEvents = useRef(new Map<string, Event[]>());
  // What is on screen, so a reload that changed nothing re-renders nothing; and the newest reload's number.
  const shown = useRef("");
  const reloads = useRef(0);

  const latest = runs.at(-1);
  const running = !!latest && ACTIVE.includes(latest.status);
  const sending = outbox.length > 0;
  const timeline = useMemo(() => {
    // A sent message shows until the server's copy arrives.
    const waiting = outbox.filter(
      (local) => !messages.some((m) => m.role === "user" && m.content === local.content && m.created >= local.created - 5),
    );
    return conversation(waiting.length ? [...messages, ...waiting] : messages, runs, events, running, thoughts);
  }, [messages, outbox, runs, events, running, thoughts]);
  const device = devices.find((d) => d.id === deviceId);
  const chat = chats.find((c) => c.id === chatId);
  // What a task would run on now, and the agent's provider (the memory panel uses it).
  const models = useMemo(
    () => (catalog ? effectivePicks(catalog, chosen, fallbackProvider, health) : null),
    [catalog, chosen, fallbackProvider, health],
  );
  const provider: Provider = models?.agent.provider ?? fallbackProvider;

  // Codes only the user can give, privately; every other question arrives as one of Relay's messages
  // and is answered in the composer.
  const waiting = Object.entries(latest?.nodes || {}).filter(([, node]) => node.status === "waiting_user");
  const privateAsk = latest?.status === "waiting_user" ? waiting.find(([, node]) => node.question?.sensitive) : undefined;
  // A sign-in the user completes on the phone themselves: the phone grows, and they say when they are done.
  const loginAsk = latest?.status === "waiting_user" ? waiting.find(([, node]) => node.question?.kind === "login") : undefined;
  // Tasks wait until the user has signed in to Google on the phone (unknown or unreadable: no gate).
  const googleAccount = googleAccounts?.[0] || "";
  const needsGoogle = !!device?.connected && Array.isArray(googleAccounts) && googleAccounts.length === 0;

  // Chats deleted (or being deleted) in this window: kept out of any list that loads meanwhile.
  const deleting = useRef(new Set<string>());
  const refreshChats = useCallback(async () => {
    const list = (await api<Chat[]>("/chats")).filter((c) => !deleting.current.has(c.id));
    setChats((previous) => keep(previous, list));
  }, []);

  /** Put a chat's detail on screen (when it is the open one), skipping the render when nothing changed. */
  const apply = useCallback((detail: Detail) => {
    const key = JSON.stringify(detail);
    if (key === shown.current) return;
    shown.current = key;
    setMessages(detail.messages);
    setThoughts(detail.thoughts || []);
    setRuns(detail.runs);
    setRewindable(detail.rewindable || []);
    setPending(detail.pending || []);
    setWatches(detail.watches || []);
    setUsage(detail.usage || null);
    setRequests(detail.requests || []);
  }, []);

  /** Load a chat from the server. Only the newest reload is shown, so a slow, older answer never wins. */
  const reloadChat = useCallback(
    async (id = chatRef.current) => {
      if (!id) return;
      const number = ++reloads.current;
      try {
        const detail = await api<Detail>(`/chats/${id}`);
        remember(details.current, id, detail);
        if (id === chatRef.current && number === reloads.current) apply(detail);
      } catch (e) {
        if (id === chatRef.current) setError((e as Error).message);
      }
    },
    [apply],
  );

  useEffect(() => {
    refreshChats().catch((e) => setError(e.message));
  }, [refreshChats]);

  // The models on offer, once; asked again when the API first answers (health) if it was not up yet.
  const up = !!health;
  useEffect(() => {
    if (!catalog) api<Catalog>("/models").then(setCatalog).catch(() => {});
  }, [catalog, up]);

  const devicesRef = useRef<Device[]>([]);
  devicesRef.current = devices;
  // Health, the phone (with its Google accounts) and the task list, pushed as they change (lib/updates.ts). The
  // task list too: a task can start elsewhere (the phone app, voice) while the window only watches.
  useEffect(() => {
    const stops = [
      onUpdate<Health>("health", (h) => {
        setHealth((previous) => keep(previous, h));
      }),
      onUpdate<Device[]>("devices", (d) => {
        setDevices((previous) => keep(previous, d));
        setDeviceId((id) => id || d[0]?.id || "");
        // The phone's accounts, once the gateway has read them (null: not yet, or it cannot say).
        const accounts = d.find((x) => x.connected)?.accounts;
        if (accounts !== undefined && accounts !== null) setGoogleAccounts((previous) => keep(previous, accounts));
      }),
      onUpdate<Chat[]>("chats", (list) =>
        setChats((previous) => keep(previous, list.filter((c) => !deleting.current.has(c.id)))),
      ),
      onUpdate<ForYou>("for_you", (list) => setForYou((previous) => keep(previous, list))),
      onUpdate("down", () => setHealth(null)),
    ];
    // Back from elsewhere: the user may have signed in or out of Google on the phone meanwhile. Reading it makes
    // the gateway push the accounts again if they changed.
    const back = () => {
      const phone = devicesRef.current.find((x) => x.connected)?.id;
      if (!document.hidden && phone) api<GooglePhone>(`/devices/${phone}/google`).catch(() => {});
    };
    document.addEventListener("visibilitychange", back);
    return () => {
      stops.forEach((stop) => stop());
      document.removeEventListener("visibilitychange", back);
    };
  }, []);


  // The open conversation: loaded when opened and when the window comes back; while a task works, its events
  // trigger reloads (below), with a slow reload as a safety net.
  useEffect(() => {
    if (!chatId) return;
    reloadChat(chatId);
    const wake = () => !document.hidden && reloadChat(chatId);
    document.addEventListener("visibilitychange", wake);
    return () => document.removeEventListener("visibilitychange", wake);
  }, [chatId, reloadChat]);
  useEffect(() => {
    if (!chatId || !running) return;
    const timer = window.setInterval(() => !document.hidden && reloadChat(chatId), SAFETY_RELOAD);
    return () => window.clearInterval(timer);
  }, [chatId, running, reloadChat]);

  // The open chat changed elsewhere (a message spoken on the voice screen or sent from another window started or
  // continued a task): the pushed list shows it first, so reload. While a task works its own events do this.
  const openStamp = chat ? `${chat.id} ${chat.updated} ${chat.status}` : "";
  const lastStamp = useRef("");
  useEffect(() => {
    const before = lastStamp.current;
    lastStamp.current = openStamp;
    if (!openStamp || !before.startsWith(`${chatId} `) || before === openStamp || running) return;
    reloadChat(chatId);
  }, [openStamp]); // eslint-disable-line react-hooks/exhaustive-deps

  // Keep the task list's status dots current while a task runs.
  useEffect(() => {
    refreshChats().catch(() => {});
  }, [latest?.status, refreshChats]);

  // The latest task's events, streamed: added once per frame, and resumed after the last one this window saw.
  useEffect(() => {
    const runId = latest?.id;
    if (!runId) return setEvents([]);
    const known = runEvents.current.get(runId) || [];
    setEvents(known);
    const after = lastEventId(known);
    const source = new EventSource(`/api/runs/${runId}/stream${after ? `?after=${after}` : ""}`);
    const opened = Date.now();
    let batch: Event[] = [];
    let frame = 0;
    let reload = 0;
    let lastReload = 0;
    const flush = () => {
      frame = 0;
      const items = batch;
      batch = [];
      setEvents((previous) => {
        const next = collectEvents(previous, items);
        remember(runEvents.current, runId, next);
        return next;
      });
    };
    source.onmessage = (message) => {
      const event = JSON.parse(message.data) as Event;
      batch.push(event);
      if (!frame) frame = requestAnimationFrame(flush);
      // The chat changed (a step, a status, a reply): reload it soon, at most once a second. Events replayed
      // from before the stream opened changed nothing new.
      if (STREAMING.has(event.kind) || event.created * 1000 < opened - 2000 || reload) return;
      const wait = Math.max(150, lastReload + RELOAD_GAP - Date.now());
      reload = window.setTimeout(() => {
        reload = 0;
        lastReload = Date.now();
        reloadChat();
      }, wait);
    };
    return () => {
      source.close();
      cancelAnimationFrame(frame);
      window.clearTimeout(reload);
      if (batch.length) remember(runEvents.current, runId, collectEvents(runEvents.current.get(runId) || [], batch));
    };
  }, [latest?.id, streamKey, reloadChat]);

  const where = usePhoneLocation(device?.id, !!device?.connected);

  /** Pick the models for the tasks to come; kept in this browser until the user changes them. */
  function setModels(next: Picks) {
    setChosen(next);
    storePicks(next);
  }

  /** Open a chat: what this window last saw of it at once, then the server's current copy. */
  function open(id: string) {
    chatRef.current = id;
    setChatId(id);
    shown.current = "";
    apply((id && details.current.get(id)) || EMPTY);
    setOutbox([]);
    setError("");
  }
  function selectChat(id: string) {
    if (id !== chatId) open(id);
  }
  function newTask() {
    open("");
  }
  /** Load a chat before it is opened (the pointer is on it in the list), so opening it is instant. */
  const prefetching = useRef(new Set<string>());
  function prefetchChat(id: string) {
    if (id === chatId || details.current.has(id) || prefetching.current.has(id)) return;
    prefetching.current.add(id);
    api<Detail>(`/chats/${id}`)
      .then((detail) => remember(details.current, id, detail))
      .catch(() => {})
      .finally(() => prefetching.current.delete(id));
  }

  // The chats of the briefings For you shows opened, loaded as the list comes: tapping one shows it at once.
  const openedBriefings = forYou.ready.flatMap((item) => (item.chat_id ? [item.chat_id] : [])).slice(0, 6).join(" ");
  useEffect(() => {
    openedBriefings.split(" ").filter(Boolean).forEach(prefetchChat); // it skips what it has
  }, [openedBriefings]);

  // Right after Stop, the worker may still be unwinding the task for a moment.
  async function retrying(post: () => Promise<unknown>) {
    for (let attempt = 0; ; attempt++) {
      try {
        return await post();
      } catch (e) {
        if (attempt >= 10 || !/finishing this run/.test((e as Error).message)) throw e;
        await new Promise((resolve) => setTimeout(resolve, 600));
      }
    }
  }

  /** The open chat's id; a new conversation is created now when there is none yet (a file can be picked before the
   *  first message). `onCreated` hears a new chat's id before the window moves to it. */
  async function ensureChat(onCreated?: (id: string) => void) {
    if (chatId) return chatId;
    const created = await api<Chat>("/chats", "POST");
    onCreated?.(created.id);
    chatRef.current = created.id;
    setChatId(created.id);
    setChats((list) => [created, ...list.filter((c) => c.id !== created.id)]);
    return created.id;
  }

  /** A task runs where the user is: their own phone's position when they start it from there, else this window's. */
  async function placePhone() {
    if (device?.id) await (COMPANION ? sendPhoneLocation(device.id) : releaseToComputer(device.id));
  }

  async function send(text: string, extra: SendExtra = {}) {
    // While the task works a message still goes: it steers the task, or waits for it to end (extra.mode).
    const files = extra.attachments?.length ? extra.attachments : undefined;
    if ((!text.trim() && !files) || busy || needsGoogle) return false;
    setBusy(true);
    setError("");
    const structured = !!(extra.sensitive || extra.instead);
    // A plain message shows in the conversation at once (a queued one shows in the queue once it is saved).
    const local: Message | null =
      structured || extra.mode === "next"
        ? null
        : { id: `local-${Date.now()}`, role: "user", content: text, created: Date.now() / 1000, sending: true, attachments: files };
    if (local) setOutbox((list) => [...list, local]);
    try {
      const id = await ensureChat();
      await placePhone();
      await retrying(() =>
        structured && latest
          ? api<Run>(`/runs/${latest.id}/reply`, "POST", {
              content: text,
              node_id: (privateAsk || loginAsk)?.[0] || null,
              sensitive: !!extra.sensitive,
              instead: extra.instead || undefined,
            })
          : api<Run>(`/chats/${id}/messages`, "POST", {
              content: text,
              provider,
              models,
              mode: extra.mode || "steer",
              attachments: files?.map((a) => a.id),
            }),
      );
      // The chat moves to the top of the list at once; the server's own list (just below) then confirms it.
      const now = Date.now() / 1000;
      setChats((list) => list.map((c) => (c.id === id ? { ...c, updated: Math.max(c.updated || 0, now) } : c)));
      await Promise.all([reloadChat(id), refreshChats().catch(() => {})]);
      return true;
    } catch (e) {
      setError((e as Error).message);
      return false;
    } finally {
      if (local) setOutbox((list) => list.filter((m) => m !== local));
      setBusy(false);
    }
  }

  /** Values the user gives privately (a card, a password), asked for or not: the task learns that they came and
   * their labels, never what they are; the phone types them where the task asks. */
  async function sendPrivate(entries: { label: string; value: string }[]) {
    if (!entries.length || busy || needsGoogle) return false;
    setBusy(true);
    setError("");
    try {
      let id = chatId;
      if (!id) {
        const created = await api<Chat>("/chats", "POST");
        id = created.id;
        chatRef.current = id;
        setChatId(id);
        setChats((list) => [created, ...list.filter((c) => c.id !== id)]);
      }
      await retrying(() => api<Run>(`/chats/${id}/private`, "POST", { entries, provider, models }));
      await Promise.all([reloadChat(id), refreshChats().catch(() => {})]);
      return true;
    } catch (e) {
      setError((e as Error).message);
      return false;
    } finally {
      setBusy(false);
    }
  }

  /** Go back to one of the user's messages and write it again: it and everything after it are removed. */
  async function rewind(messageId: string, text: string) {
    const kept = messages.find((m) => m.id === messageId)?.attachments?.map((a) => a.id);
    if ((!text.trim() && !kept?.length) || !chatId || busy || running) return false;
    setBusy(true);
    setError("");
    try {
      await retrying(() => api<Run>(`/chats/${chatId}/messages/${messageId}/rewind`, "POST", {
          content: text,
          provider,
          models,
          attachments: kept,
        }));
      // What streamed in for the dropped part is gone from the server: start the stream over.
      if (latest) runEvents.current.delete(latest.id);
      setEvents([]);
      setStreamKey((n) => n + 1);
      await Promise.all([reloadChat(chatId), refreshChats().catch(() => {})]);
      return true;
    } catch (e) {
      setError((e as Error).message);
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function reload() {
    await reloadChat();
  }
  /** Pause the task. Returns the queued messages that never went out, for the composer to take back. */
  async function stop(): Promise<string[]> {
    if (!latest) return [];
    try {
      const stopped = await api<Run & { unsent?: string[] }>(`/runs/${latest.id}/stop`, "POST");
      await reload();
      return stopped.unsent || [];
    } catch (e) {
      setError((e as Error).message);
      return [];
    }
  }
  /** Take back a queued message before it goes out. Returns its text, or "" when it already went. */
  async function unqueue(entry: Pending): Promise<string> {
    if (!chatId) return "";
    try {
      const taken = await api<{ content: string }>(`/chats/${chatId}/queued/${entry.id}`, "DELETE");
      setPending((list) => list.filter((p) => p.id !== entry.id));
      return taken.content;
    } catch (e) {
      setError((e as Error).message);
      await reload();
      return "";
    }
  }
  /** End one of the chat's watches: its notifications are no longer passed here, and it wakes nothing. */
  async function stopWatching(watch: Watch) {
    if (!chatId) return;
    try {
      const left = await api<{ watches: Watch[] }>(`/chats/${chatId}/watches/${watch.id}`, "DELETE");
      setWatches(left.watches);
    } catch (e) {
      setError((e as Error).message);
      await reload();
    }
  }
  /** Open a briefing: its chat, begun (the first time) with what arrived and a task on what can be done about it.
   *  Returns the chat's id, or "" when it could not be opened. */
  /**
   * Open a briefing (a card, or its notification, which knows only the id): its chat, begun with a task on it. One
   * request, which answers with the chat itself: on the phone app each request crosses the tunnel. The phone's
   * position goes beside it, not before: the task reads before it needs the phone placed.
   */
  async function openBriefing(item: Pick<Briefing, "id" | "chat_id">) {
    if (item.chat_id) {
      // Begun already, perhaps by penrose on its own before the user looked: shown at once, and marked seen.
      selectChat(item.chat_id);
      api(`/for-you/${item.id}/open`, "POST", { provider, models }).catch(() => {});
      return item.chat_id;
    }
    if (busy || needsGoogle) return "";
    setBusy(true);
    setOpening(item.id);
    setError("");
    try {
      void placePhone();
      const opened = await api<{ chat_id: string; chat: Detail }>(`/for-you/${item.id}/open`, "POST", { provider, models });
      remember(details.current, opened.chat_id, opened.chat);
      open(opened.chat_id);
      refreshChats().catch(() => {});
      return opened.chat_id;
    } catch (e) {
      setError((e as Error).message);
      return "";
    } finally {
      setBusy(false);
      setOpening(null);
    }
  }
  /** Show a briefing's details, starting nothing: the sheet opens with what the feed has and fills in when the
   *  server answers (which also marks it seen). A briefing that is gone closes the sheet. */
  async function showBriefing(item: Pick<Briefing, "id">) {
    const known = [...forYou.ready, ...forYou.came_in].find((b) => b.id === item.id);
    setShown((now) => (now?.item.id === item.id ? now : { item: known ?? ({ id: item.id } as Briefing), detail: null }));
    try {
      const detail = await api<BriefingDetail>(`/for-you/${item.id}/look`, "POST");
      setShown((now) => (now?.item.id === item.id ? { item: detail, detail } : now));
    } catch (e) {
      setShown((now) => (now?.item.id === item.id ? null : now));
      setError((e as Error).message);
    }
  }
  function closeBriefing() {
    setShown(null);
  }
  /** Answer an opened briefing with one of the things it offers: the offer's words go as the user's message. */
  async function answerBriefing(item: Briefing, message: string) {
    if (!item.chat_id || busy || needsGoogle) return false;
    setBusy(true);
    setError("");
    try {
      await placePhone();
      await retrying(() => api<Run>(`/chats/${item.chat_id}/messages`, "POST", { content: message, provider, models, mode: "steer" }));
      details.current.delete(item.chat_id);
      open(item.chat_id);
      await refreshChats().catch(() => {});
      return true;
    } catch (e) {
      setError((e as Error).message);
      return false;
    } finally {
      setBusy(false);
    }
  }
  /** Take a briefing off the list; with `mute`, its conversation is not briefed on again. */
  async function dismissBriefing(item: Briefing, mute = false) {
    setForYou((list) => ({
      ...list,
      ready: list.ready.filter((b) => b.id !== item.id),
      came_in: list.came_in.filter((b) => b.id !== item.id),
    }));
    try {
      setForYou(await api<ForYou>(`/for-you/${item.id}/dismiss`, "POST", { mute }));
    } catch (e) {
      setError((e as Error).message);
      // It was not dismissed: the list as the server has it, with the card back in it.
      api<ForYou>("/for-you").then(setForYou).catch(() => {});
    }
  }
  /** Take a waiting chat off the list; the task itself is untouched. */
  async function dismissWaiting(chat: string) {
    setForYou((list) => ({ ...list, waiting: list.waiting.filter((w) => w.chat_id !== chat) }));
    try {
      setForYou(await api<ForYou>(`/for-you/waiting/${chat}/dismiss`, "POST"));
    } catch (e) {
      setError((e as Error).message);
      api<ForYou>("/for-you").then(setForYou).catch(() => {});
    }
  }
  async function discard() {
    if (!latest) return;
    const ask = { message: "Discard this task?", detail: "penrose forgets its plan and progress.", action: "Discard" };
    const ok = window.relay?.confirm ? await window.relay.confirm(ask) : await confirmDialog(ask);
    if (!ok) return;
    try {
      await api(`/runs/${latest.id}/cancel`, "POST");
      await reload();
    } catch (e) {
      setError((e as Error).message);
    }
  }
  async function deleteChat(target: Chat) {
    const message = `Delete “${target.title}”?`;
    const detail = "Its conversation and history are removed. What penrose learned stays in memory.";
    const ok = window.relay?.confirm
      ? await window.relay.confirm({ message, detail, action: "Delete" })
      : await confirmDialog({ message, detail, action: "Delete" });
    if (!ok) return;
    // Gone from the list at once; back in it if the server could not delete it.
    deleting.current.add(target.id);
    setChats((list) => list.filter((c) => c.id !== target.id));
    details.current.delete(target.id);
    if (target.id === chatId) newTask();
    try {
      // Kept in `deleting`: a list that started loading before the delete may still arrive with it.
      await api(`/chats/${target.id}`, "DELETE");
    } catch (e) {
      deleting.current.delete(target.id);
      setError((e as Error).message);
      refreshChats().catch(() => {});
    }
  }
  async function phoneAction(body: object) {
    if (!deviceId) return;
    try {
      await api(`/devices/${deviceId}/actions`, "POST", body);
    } catch (e) {
      setError((e as Error).message);
    }
  }

  return {
    chats, chat, chatId, messages, runs, latest, events, timeline, running, sending, privateAsk, loginAsk, rewindable,
    pending, watches, usage, requests, health, forYou, opening, shownBriefing, device, googleAccount, needsGoogle, provider, catalog, models, busy, error, where,
    setError, setModels, selectChat, prefetchChat, newTask, ensureChat, send, sendPrivate, rewind, stop, unqueue, stopWatching, openBriefing, showBriefing, closeBriefing, answerBriefing, dismissBriefing, dismissWaiting, discard, deleteChat, phoneAction,
  };
}

export type Workspace = ReturnType<typeof useWorkspace>;
