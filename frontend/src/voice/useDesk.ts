/**
 * Voice mode through the reception desk (desk.ts, backend/app/runtime/desk.py): the same screen and phases as
 * useVoice.ts, with one Realtime model in place of transcription, the front desk and text-to-speech.
 *
 *   off → starting → listening ⇄ hearing → thinking → speaking → listening
 *
 * Talking is a call (`line`): the screen opens with a Call key and nothing else open. Pressing it rings ("calling")
 * until the desk answers, which is the line being up and the microphone open; only then is anything the user says
 * sent, and the keys are mute and end call ("on"). Not answered in ANSWER_MS (penrose is off, or cannot be reached),
 * the call ends as "unavailable" and the screen offers the chat instead. A line that drops in the middle of a call
 * reconnects by itself, with the same limit.
 *
 * The desk greets in a word or two when it picks up a call the user placed (not one that rings again by itself
 * after the chat, and not one placed by opening a card: that card's news is said first). Otherwise it is silent
 * until spoken to. What sessions have for the user (finished, a question, failed) waits as one card per
 * session (`cards`); the card the user opens is the signal for what to talk about (`answer`: it focuses that session
 * and the desk tells its news). While the line is open the desk tells the news of the session being talked about
 * itself, from the state as it is then (desk.ts); the others' stays on their cards. While the app is in the background
 * the line is closed and Web Push is the soft nudge (companion/sw.ts): tapping it lands here with that session's card
 * opened.
 */

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, type Chat, type Provider } from "../api";
import { COMPANION } from "../companion/flag";
import { releaseToComputer, sendPhoneLocation } from "../lib/location";
import type { Picks } from "../lib/models";
import { onUpdate } from "../lib/updates";
import { type Asking, Desk, type DeskEvent, type Focus, type Picture } from "./desk";
import type { PrivateEntry } from "../components/PrivateFields";
import { privateAskOf, sendPrivately } from "./privately";
import type { Shown } from "./Shown";
import { hasChosenRoute, listRoutes, rememberSink, storedSink, type Route } from "./outputs";
import { Bell, onEveryTap, type Output, Ringback } from "./speech";
import type { Call, Phase } from "./useVoice";

type Events = { events: DeskEvent[]; latest: number };
/** What a session has for the user, waiting on the screen: its unheard events, newest card first. */
export type Card = {
  chatId: string;
  title: string;
  session: string;
  kind: DeskEvent["kind"];
  private: boolean;
  events: DeskEvent[];
};
const NEEDS_YOU: DeskEvent["kind"][] = ["asks", "failed"];

const WORKING = ["queued", "running"];
/** The call: "idle" until the user presses Call; "calling" until the desk answers; "on"; "unavailable" when it did
 *  not answer in time. */
export type Line = "idle" | "calling" | "on" | "unavailable";
const ANSWER_MS = 20_000; // a call rings this long, and a dropped line is given this long to come back
// The call as the user last left it: the screen closes when they open the chat, and coming back to it in the middle
// of a call rings it again by itself, muted or not as it was. A call they ended stays ended.
let lastCall = false;
let lastMuted = false;
const OUTPUT_KEY = "relay.voice.output";

function storedOutput(): Output {
  try {
    return localStorage.getItem(OUTPUT_KEY) === "earpiece" ? "earpiece" : "speaker";
  } catch {
    return "speaker";
  }
}

export function useDesk(provider: Provider, models: Picks | null, deviceId?: string) {
  const [phase, setPhaseState] = useState<Phase>("off");
  const [caption, setCaption] = useState("");
  const [said, setSaid] = useState("");
  const [focus, setFocus] = useState<Focus | null>(null);
  const [cards, setCards] = useState<Card[]>([]);
  const [error, setError] = useState("");
  const [line_, setLineState] = useState<Line>("idle");
  const lineRef = useRef<Line>("idle");
  const setLine = (next: Line) => {
    lineRef.current = next;
    lastCall = next === "calling" || next === "on";
    setLineState(next);
  };
  const [ready, setReady] = useState(false); // the line is up
  const [muted, setMutedState] = useState(lastMuted);
  const setMuted = (next: boolean) => {
    lastMuted = next;
    setMutedState(next);
  };
  const [shown, setShown] = useState<Shown | null>(null);
  // The form a task's question wants filled in privately (a card, a password, a code), open on the screen.
  const [asking, setAsking] = useState<Asking | null>(null);
  const [silent, setSilent] = useState(false); // speaking, but the browser waits for a tap before playing sound
  const [working, setWorking] = useState<Chat[]>([]);
  const [output, setOutputState] = useState<Output>(storedOutput);
  const [sink, setSinkState] = useState(storedSink);

  const phaseRef = useRef<Phase>("off");
  const focusRef = useRef<Focus | null>(null);
  const picks = useRef({ provider, models, deviceId });
  picks.current = { provider, models, deviceId };
  const wanted = useRef(false);
  // Whether the screen is still there. What the screen awaits (a card's news, a reopened line) can come back after the
  // user has gone to the chat: it must not open the microphone and the line again behind a screen that is gone. A
  // layout effect, so it is true before the screen's own start (a passive effect) runs.
  const alive = useRef(false);
  useLayoutEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  const cardsRef = useRef<Card[]>([]);
  const line = useRef<Desk | null>(null);
  const bell = useRef<Bell | null>(null);
  const desk = () => {
    if (!line.current) {
      line.current = new Desk();
      line.current.output = storedOutput();
      line.current.setSink(storedSink());
      line.current.muted = lastMuted;
      line.current.picks = () => ({ provider: picks.current.provider, models: picks.current.models });
    }
    return line.current;
  };
  const ringer = () => (bell.current ??= new Bell());

  const setPhase = useCallback((next: Phase) => {
    phaseRef.current = next;
    setPhaseState(next);
  }, []);

  const putCards = (next: Card[]) => {
    cardsRef.current = next;
    setCards(next);
  };

  /** Add events as cards (one per session, newest first). `chime`: a soft sound once if one needs the user, for news
   *  that arrives while the line is closed; never for what was waiting when the screen opened. */
  const addCards = useCallback((events: DeskEvent[], chime = false) => {
    let next = cardsRef.current;
    let needs = false;
    for (const e of events) {
      if (e.kind === "progress") continue;
      const was = next.find((c) => c.chatId === e.chat_id);
      if (was?.events.some((x) => x.id === e.id)) continue;
      const card: Card = {
        chatId: e.chat_id,
        title: e.title,
        session: e.session,
        kind: e.kind,
        private: e.private,
        events: [...(was?.events ?? []), e],
      };
      next = [card, ...next.filter((c) => c.chatId !== e.chat_id)];
      needs = needs || NEEDS_YOU.includes(e.kind);
    }
    if (next === cardsRef.current) return;
    putCards(next);
    if (chime && needs) ringer().start(true);
  }, []);

  /** Take a card off the screen; `heard` also tells Relay the user is done with it (else it comes back next time). */
  const removeCard = useCallback((chatId: string, heard: boolean) => {
    if (!cardsRef.current.some((c) => c.chatId === chatId)) return;
    putCards(cardsRef.current.filter((c) => c.chatId !== chatId));
    if (heard) void api("/voice/desk/announced", "POST", { chat_id: chatId }).catch(() => {});
  }, []);

  // --- the line ---------------------------------------------------------------------------------------------------

  const hello = useRef(false); // the call ringing now was placed by the user: the desk greets when it picks up

  /** The desk answers the call: the line is up and, unless the user came back to a call they had muted, the
   *  microphone is open. */
  const answered = useCallback(() => {
    const d = desk();
    if (lineRef.current !== "calling" || !d.connected || !(d.muted || d.hears)) return;
    setLine("on");
    setPhase("listening");
    if (hello.current) d.greet();
    hello.current = false;
  }, [setPhase]);

  // The call ends (see `hangUp`, which is made further down: the listeners below reach it through this).
  const end = useRef<() => void>(() => {});

  useEffect(() => {
    const d = desk();
    const onState = (e: Event) => {
      const state = (e as CustomEvent<string>).detail;
      if (!wanted.current) return;
      if (state === "error") {
        if (d.micProblem) {
          // The browser would not give the microphone in the middle of a call: the call stays, muted.
          setError(d.micProblem);
          setPhase("error");
          return;
        }
        // The line could not be opened, or dropped. It is tried again by itself (desk.ts `later`): the call goes on
        // ringing, or says it is reconnecting, until ANSWER_MS is up.
        setReady(false);
      } else if (state === "ready") {
        setReady(true);
        answered();
        if (lineRef.current !== "on") return;
        if (phaseRef.current === "starting" || phaseRef.current === "error" || phaseRef.current === "thinking") {
          setPhase("listening");
        }
      } else if (state === "connecting") {
        setReady(false);
        if (phaseRef.current === "listening") setPhase("starting");
      }
    };
    const onSpeech = () => {
      setCaption("");
      if (phaseRef.current === "listening" || phaseRef.current === "speaking") setPhase("hearing");
    };
    const onHeard = (e: Event) => {
      setCaption((e as CustomEvent<string>).detail);
      setError("");
      if (phaseRef.current === "hearing") setPhase("thinking");
      // The task runs where the user is: their own phone's position from the phone app, else this window's.
      const { deviceId } = picks.current;
      if (deviceId) void (COMPANION ? sendPhoneLocation(deviceId) : releaseToComputer(deviceId)).catch(() => {});
    };
    const onSaying = (e: Event) => setSaid((e as CustomEvent<string>).detail);
    const onSpeaking = () => {
      setPhase("speaking");
      // Opened from the lock screen or a notification, the page may not play sound yet: say so, until a tap.
      setSilent(desk().silent);
    };
    const onSpoken = () => {
      if (wanted.current && phaseRef.current === "speaking") setPhase("listening");
    };
    const onFocus = (e: Event) => {
      focusRef.current = (e as CustomEvent<Focus>).detail;
      setFocus(focusRef.current);
      // The desk is talking about it (routed to it, or read its reply): Relay has marked its news heard.
      if (focusRef.current) removeCard(focusRef.current.chatId, false);
      // A task that already waits for something private (asked before this screen opened) shows its form too.
      const chatId = focusRef.current?.chatId;
      void privateAskOf(chatId)
        .then((ask) => ask && chatId && setAsking((was) => (was?.chatId === chatId ? was : { chatId, ask })))
        .catch(() => {});
    };
    const onAsk = (e: Event) => setAsking((e as CustomEvent<Asking>).detail);
    const onTool = () => {
      if (phaseRef.current !== "speaking") setPhase("thinking");
    };
    const onTooled = () => {
      if (phaseRef.current === "thinking") setPhase("listening");
    };
    // The desk had nothing to say to what it heard (a fragment, "hold on"): listening again, not waiting on a reply.
    const onQuiet = () => {
      if (phaseRef.current === "thinking" || phaseRef.current === "hearing") setPhase("listening");
    };
    // The desk told a session's news and the user heard it to the end: its card goes.
    const onTold = (e: Event) => removeCard((e as CustomEvent<string>).detail, false);
    const onError = (e: Event) => setError((e as CustomEvent<string>).detail);
    // The browser would not give the microphone. After an unmute, the key goes back to muted; for a call still
    // ringing, there is no call: it ends, and the screen says why.
    const onMuted = (e: Event) => {
      if (lineRef.current === "calling" && d.micProblem) {
        const why = d.micProblem;
        end.current();
        setError(why);
        setPhase("error");
        return;
      }
      setMuted((e as CustomEvent<boolean>).detail);
    };
    const onShow = (e: Event) => {
      const pictures = (e as CustomEvent<Picture[]>).detail;
      setShown((was) => ({ pictures, key: (was?.key || 0) + 1 }));
    };
    const onNews = (e: Event) => addCards((e as CustomEvent<DeskEvent[]>).detail);
    const pairs: [string, (e: Event) => void][] = [
      ["news", onNews],
      ["state", onState],
      ["speech", onSpeech],
      ["heard", onHeard],
      ["saying", onSaying],
      ["speaking", onSpeaking],
      ["spoken", onSpoken],
      ["focus", onFocus],
      ["ask", onAsk],
      ["tool", onTool],
      ["tooled", onTooled],
      ["quiet", onQuiet],
      ["told", onTold],
      ["error", onError],
      ["muted", onMuted],
      ["show", onShow],
    ];
    for (const [name, fn] of pairs) d.addEventListener(name, fn);
    return () => {
      for (const [name, fn] of pairs) d.removeEventListener(name, fn);
    };
  }, [addCards, answered, removeCard, setPhase]);

  // Placing the call (`dial`, made further down: `answer` reaches it through this).
  const place = useRef<() => void>(() => {});

  /** Open a session: the user's signal for what to talk about. Its card (or, from a notification, the session the
   *  push was about) becomes the one being talked about, the call is placed if there is none (unmuted if there is),
   *  and the desk tells that session's news once it has answered. Without a chat, the newest card. */
  const answer = useCallback(
    async (chatId?: string) => {
      let card = cardsRef.current.find((c) => c.chatId === chatId) ?? (chatId ? undefined : cardsRef.current[0]);
      if (!card && chatId) {
        // From a notification, before the screen has its cards.
        try {
          addCards((await api<Events>("/voice/desk/events?unannounced=1")).events);
        } catch {}
        card = cardsRef.current.find((c) => c.chatId === chatId);
      }
      if (!alive.current) return; // the user left for the chat while the news was coming
      const target = card?.chatId ?? chatId;
      if (target) {
        const title = card?.title ?? "";
        focusRef.current = { chatId: target, title };
        setFocus(focusRef.current);
        putCards(cardsRef.current.filter((c) => c.chatId !== target));
      }
      const d = desk();
      if (target) d.answer({ chatId: target, title: card?.title ?? "", session: card?.session ?? `s:${target.slice(0, 6)}` }, card?.events ?? []);
      if (lineRef.current === "on") {
        void d.setMuted(false);
        setMuted(false);
      } else {
        place.current();
        if (target) hello.current = false; // the card's news is what the desk says on picking up
      }
    },
    [addCards],
  );

  /** "Later" on a card: it goes (the user has seen it), and Relay no longer holds it as news; no chat, all of them. */
  const dismiss = useCallback(
    (chatId?: string) => {
      for (const c of chatId ? [{ chatId }] : cardsRef.current) removeCard(c.chatId, true);
    },
    [removeCard],
  );

  // --- the sessions' news: told by the desk, or rung while muted ------------------------------------------------

  useEffect(() => {
    const fetchNews = async (query: string) => {
      try {
        const got = await api<Events>(`/voice/desk/events?${query}`);
        if (!got.events.length) return;
        const d = desk();
        if (d.connected) d.inject(got.events);
        else {
          // The line is closed (the app was in the background, or the microphone could not be opened): the news waits
          // as cards, with one soft chime if something needs the user. The cursor stays, so a later fetch brings them
          // again, deduplicated. Where a session is on its way is not worth a card.
          d.cursor = Math.max(d.cursor, ...got.events.filter((e) => e.kind === "progress").map((e) => e.id));
          addCards(got.events, true);
        }
      } catch {
        // the next change brings it round again
      }
    };
    // What the user has not heard waits as cards, quietly (the open brings the same when the line comes up).
    void api<Events>("/voice/desk/events?unannounced=1").then((got) => addCards(got.events)).catch(() => {});
    const offDesk = onUpdate<number>("desk", (latest) => {
      const d = desk();
      // Before the line has opened once, the cursor is 0: ask for what is untold, not for every event ever logged.
      if (latest > d.cursor) void fetchNews(d.cursor ? `after=${d.cursor}` : "unannounced=1");
    });
    const offChats = onUpdate<Chat[]>("chats", (list) => setWorking(list.filter((c) => WORKING.includes(c.status || ""))));
    return () => {
      offDesk();
      offChats();
    };
  }, [addCards]);

  // --- the screen stays on while voice is on; hidden, the line and the microphone close -----------------------------

  const lock = useRef<WakeLockSentinel | null>(null);
  const keepAwake = useCallback(async () => {
    if (lock.current || !("wakeLock" in navigator) || document.hidden) return;
    try {
      const sentinel = await navigator.wakeLock.request("screen");
      lock.current = sentinel;
      sentinel.addEventListener("release", () => {
        if (lock.current === sentinel) lock.current = null;
      });
    } catch {
      // not allowed here: the screen keeps its own timeout
    }
  }, []);
  const letSleep = useCallback(() => {
    void lock.current?.release().catch(() => {});
    lock.current = null;
  }, []);

  useEffect(() => {
    const visibility = () => {
      if (!wanted.current || !alive.current) return;
      if (document.hidden) desk().stop();
      else {
        void keepAwake();
        wanted.current = true;
        void desk().start();
      }
    };
    document.addEventListener("visibilitychange", visibility);
    return () => document.removeEventListener("visibilitychange", visibility);
  }, [keepAwake]);

  useEffect(
    () => () => {
      wanted.current = false;
      line.current?.stop();
      bell.current?.stop();
      letSleep();
    },
    [letSleep],
  );

  // --- what the screen calls -------------------------------------------------------------------------------------

  /** Place the call: it rings until the desk answers (`answered`), or ANSWER_MS is up. On a call already, try the
   *  line and the microphone again (the screen's "try again"). */
  const dial = useCallback(
    async (mutedCall = false, again = false) => {
      if (!alive.current) return;
      const d = desk();
      wanted.current = true;
      setError("");
      d.unlock();
      void keepAwake();
      if (lineRef.current !== "on") {
        d.muted = mutedCall;
        setMuted(mutedCall);
        hello.current = !again;
        setLine("calling");
        setReady(d.connected);
        setPhase("starting");
      }
      await d.start();
      answered();
    },
    [answered, keepAwake, setPhase],
  );
  place.current = () => void dial();

  /** The screen has opened: nothing is placed, unless the user left it in the middle of a call. */
  const start = useCallback(async () => {
    if (lastCall) await dial(lastMuted, true);
  }, [dial]);

  useEffect(() => {
    return onEveryTap(() => {
      desk().unlock();
      setSilent(false);
    });
  }, []);

  const stop = useCallback(() => {
    wanted.current = false;
    letSleep();
    desk().stop();
    ringer().stop();
    setCaption("");
    setShown(null);
    setPhase("off");
  }, [letSleep, setPhase]);

  /** End the call (or stop it ringing): the line and the microphone close. */
  const hangUp = useCallback(() => {
    stop();
    setLine("idle");
    setMuted(false);
  }, [stop]);
  end.current = hangUp;

  // A call that is ringing is heard ringing, until it is answered, given up or cancelled.
  const ringback = useRef<Ringback | null>(null);
  useEffect(() => {
    if (line_ !== "calling") return;
    const ring = (ringback.current ??= new Ringback());
    ring.start(desk().audio);
    return () => ring.stop();
  }, [line_]);

  // Not answered in time, or the line of a call did not come back: penrose is unavailable.
  const waiting = line_ === "calling" || (line_ === "on" && !ready);
  useEffect(() => {
    if (!waiting) return;
    const timer = window.setTimeout(() => {
      stop();
      setLine("unavailable");
      setMuted(false);
    }, ANSWER_MS);
    return () => window.clearTimeout(timer);
  }, [waiting, stop]);

  const toggleMute = useCallback(async () => {
    const next = !desk().muted;
    setMuted(next); // the key answers at once; unmuting opens the microphone, which takes a moment
    await desk().setMuted(next); // if the browser will not give it, the desk says "muted" (onMuted) and the key goes back
  }, []);

  const setOutput = useCallback(
    async (next: Output, keep = true) => {
      try {
        if (keep) localStorage.setItem(OUTPUT_KEY, next);
      } catch {}
      setOutputState(next);
      if (desk().output === next) return;
      desk().output = next;
      if (alive.current && wanted.current && phaseRef.current !== "off") {
        desk().stop();
        wanted.current = true;
        setPhase("starting");
        await desk().start();
      }
    },
    [setPhase],
  );

  /** Pick a route from the output menu: its device (where the browser lets the page choose one) and its processing.
   *  A pick of the user's is remembered, and stops the phone choosing for them; one made by `follow` is not. */
  const setRoute = useCallback(
    async (route: Route, automatic = false) => {
      if (!automatic) rememberSink(route.sink, true);
      setSinkState(route.sink);
      desk().setSink(route.sink);
      await setOutput(route.mode, !automatic);
    },
    [setOutput],
  );

  // A car, headset or headphones that connects takes the voice (and a microphone to talk back into it), as it does for
  // a call or music, and the phone's own speaker has it again when it goes: unless the user picked a route themself
  // (the output menu), which is left alone. Android Auto and a car's Bluetooth are listed by the browser like any
  // other output once it has the microphone's permission, so this looks again when the list changes and when the
  // line is up. `followed` is the device taken this way.
  const followed = useRef("");
  const follow = useCallback(async () => {
    if (!alive.current || hasChosenRoute()) return;
    const routes = await listRoutes();
    if (!alive.current || hasChosenRoute()) return;
    const external = routes.find((r) => r.sink && (r.icon === "bluetooth" || r.icon === "headphones"));
    if (external && followed.current !== external.sink) {
      followed.current = external.sink;
      await setRoute(external, true);
    } else if (!external && followed.current) {
      followed.current = "";
      await setRoute({ key: "speaker", label: "Speaker", icon: "speaker", mode: "speaker", sink: "" }, true);
    }
  }, [setRoute]);
  useEffect(() => {
    void follow();
    navigator.mediaDevices?.addEventListener?.("devicechange", follow);
    return () => navigator.mediaDevices?.removeEventListener?.("devicechange", follow);
  }, [follow]);
  useEffect(() => {
    if (phase === "listening") void follow(); // the microphone's permission names the devices
  }, [phase === "listening"]); // eslint-disable-line react-hooks/exhaustive-deps

  const interrupt = useCallback(() => {
    if (phaseRef.current !== "speaking") return;
    desk().interrupt();
  }, []);

  /** Typed instead of spoken: words for the desk. A private value never comes here (see privately). */
  const type = useCallback(
    async (text: string) => {
      setCaption(text);
      setPhase("thinking");
      desk().type(text);
    },
    [setPhase],
  );

  /** Values given privately from the keypad: to the focused task, never to the desk. */
  const privately = useCallback(async (entries: PrivateEntry[], chatId = focusRef.current?.chatId) => {
    try {
      setSaid(await sendPrivately(chatId, entries));
      setAsking(null);
      return true;
    } catch (e) {
      setError((e as Error).message);
      return false;
    }
  }, []);

  const hide = useCallback(() => setShown(null), []);

  return {
    phase,
    shown,
    hide,
    caption,
    said,
    focus,
    call: null as Call | null, // the front desk's ring (useVoice.ts); the desk has cards instead
    cards,
    error,
    muted,
    line: line_,
    reconnecting: line_ === "on" && !ready,
    dial,
    hangUp,
    silent,
    working,
    output,
    start,
    stop,
    pickUp: answer,
    answer,
    dismiss,
    toggleMute,
    setOutput,
    sink,
    setRoute,
    interrupt,
    type,
    privately,
    asking,
    dismissAsk: () => setAsking(null),
  };
}
