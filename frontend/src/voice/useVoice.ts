/**
 * Voice mode's one conversation with the user: listen, place what they said, say a few words back, and ring when a
 * task has something for them (backend/app/api/voice.py).
 *
 * The phases, as the screen shows them:
 *   off → starting → listening ⇄ hearing → thinking → speaking → listening
 *                                   ↑                                  ↓ (a task finished or asks)
 *                                   └──────────── ringing ◄────────────┘   pick up: speaking
 * Speaking over Relay pauses it at once (OpenAI's voice detection); if the words were only the phone's own voice
 * coming back, or a cough, it goes on; otherwise it stops and the words are placed as the next request.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Chat, type Message, type Provider, type Run } from "../api";
import { COMPANION } from "../companion/flag";
import { releaseToComputer, sendPhoneLocation } from "../lib/location";
import type { Picks } from "../lib/models";
import { onUpdate } from "../lib/updates";
import { Bell, isEcho, Listener, onEveryTap, type Output, Speaker } from "./speech";
import type { PrivateEntry } from "../components/PrivateFields";
import { privateAskOf, sendPrivately } from "./privately";
import type { Asking } from "./desk";

export type Phase = "off" | "starting" | "listening" | "hearing" | "thinking" | "speaking" | "ringing" | "error";
export type Focus = { chatId: string; title: string };
/** A task with something to say: what Relay wrote, what it says aloud, and whether it waits for a private entry. */
export type Call = {
  chatId: string;
  title: string;
  text: string;
  spoken: string;
  sensitive: boolean;
  login: boolean;
  failed: boolean;
};
type Placed = { chat_id: string; run_id: string; status: string; title: string; acknowledgment: string; kind: string };
type Detail = { messages: Message[]; runs: Run[] };

const WORKING = ["queued", "running"];
const HEARD = ["completed", "waiting_user", "failed"];
const RESUME_MS = 1600; // a pause with no words after it: the interruption was a noise, the reply goes on
const SORRY = "Sorry, that didn't go through. Try again in a moment.";
const OUTPUT_KEY = "relay.voice.output";
// The phone app remembers each task's status as last seen, so one that finished while the app was closed or in the
// background (a push brought the user back, companion/push.ts) rings when the app opens, as if it had been watching.
const STATUSES_KEY = "relay.voice.statuses";

function storedStatuses(): Map<string, string | null> | null {
  if (!COMPANION) return null;
  try {
    const kept = JSON.parse(localStorage.getItem(STATUSES_KEY) || "null") as Record<string, string | null> | null;
    return kept ? new Map(Object.entries(kept)) : null;
  } catch {
    return null;
  }
}

function keepStatuses(statuses: Map<string, string | null | undefined>) {
  if (!COMPANION) return;
  try {
    localStorage.setItem(STATUSES_KEY, JSON.stringify(Object.fromEntries(statuses)));
  } catch {}
}

function storedOutput(): Output {
  try {
    return localStorage.getItem(OUTPUT_KEY) === "earpiece" ? "earpiece" : "speaker";
  } catch {
    return "speaker";
  }
}

export function useVoice(provider: Provider, models: Picks | null, deviceId?: string) {
  const [phase, setPhaseState] = useState<Phase>("off");
  const [caption, setCaption] = useState("");
  const [said, setSaid] = useState("");
  const [focus, setFocus] = useState<Focus | null>(null);
  const [call, setCall] = useState<Call | null>(null);
  const [asking, setAsking] = useState<Asking | null>(null); // a private form open for a task's question
  const [error, setError] = useState("");
  const [muted, setMuted] = useState(true);
  const [working, setWorking] = useState<Chat[]>([]);
  const [output, setOutputState] = useState<Output>(storedOutput);

  const phaseRef = useRef<Phase>("off");
  const focusRef = useRef<Focus | null>(null);
  const picks = useRef({ provider, models, deviceId });
  picks.current = { provider, models, deviceId };
  const wanted = useRef(false);
  const queue = useRef<Call[]>([]);
  const statuses = useRef<Map<string, string | null | undefined> | null>(storedStatuses());
  const resumeTimer = useRef(0);
  const ears = useRef<Listener | null>(null);
  const mouth = useRef<Speaker | null>(null);
  const bell = useRef<Bell | null>(null);
  const listener = () => {
    if (!ears.current) {
      ears.current = new Listener();
      ears.current.output = storedOutput();
    }
    return ears.current;
  };
  const speaker = () => (mouth.current ??= new Speaker());
  const ringer = () => (bell.current ??= new Bell());

  const setPhase = useCallback((next: Phase) => {
    phaseRef.current = next;
    setPhaseState(next);
  }, []);

  const speak = useCallback(
    async (text: string) => {
      if (!text.trim()) return;
      setSaid(text);
      setPhase("speaking");
      // The microphone stays open while Relay speaks, so speaking over it interrupts (onSpeech below).
      try {
        await speaker().say(text);
      } catch (e) {
        setError((e as Error).message);
      }
    },
    [setPhase],
  );

  /** Ring for the next task waiting to be heard, if nothing else is going on. */
  const maybeRing = useCallback(() => {
    if (phaseRef.current !== "listening" || !queue.current.length) return;
    const next = queue.current.shift()!;
    setCall(next);
    setPhase("ringing");
    ringer().start();
  }, [setPhase]);

  /** Back to listening after speaking, or on to the next call. Without a working microphone, back to the error. */
  const settle = useCallback(() => {
    if (!wanted.current) return;
    if (listener().state === "error") {
      setError(listener().error);
      setPhase("error");
      return;
    }
    setPhase("listening");
    maybeRing();
  }, [maybeRing, setPhase]);

  /** Place what the user said (or typed) and say the acknowledgment. */
  const utter = useCallback(
    async (text: string) => {
      setCaption(text);
      setError("");
      setPhase("thinking");
      const { provider, models, deviceId } = picks.current;
      // The task runs where the user is: their own phone's position from the phone app, else this window's.
      if (deviceId) void (COMPANION ? sendPhoneLocation(deviceId) : releaseToComputer(deviceId)).catch(() => {});
      try {
        const placed = await api<Placed>("/voice/utterance", "POST", {
          text,
          focus_chat_id: focusRef.current?.chatId ?? null,
          provider,
          models,
        });
        const next = { chatId: placed.chat_id, title: placed.title };
        focusRef.current = next;
        setFocus(next);
        await speak(placed.acknowledgment);
      } catch (e) {
        setError((e as Error).message);
        await speak(SORRY);
      }
      settle();
    },
    [settle, setPhase, speak],
  );

  /** Answer the call: say what the task has to say. */
  const pickUp = useCallback(async () => {
    const current = call;
    if (!current) return;
    ringer().stop();
    focusRef.current = { chatId: current.chatId, title: current.title };
    setFocus(focusRef.current);
    setCall(null);
    // A question that wants something private (a card, a code) opens its form as it is said.
    void privateAskOf(current.chatId)
      .then((ask) => ask && setAsking({ chatId: current.chatId, ask }))
      .catch(() => {});
    await speak(current.spoken || current.text);
    settle();
  }, [call, settle, speak]);

  const dismiss = useCallback(() => {
    ringer().stop();
    setCall(null);
    settle();
  }, [settle]);

  // --- the ears -------------------------------------------------------------------------------------------------

  useEffect(() => {
    const ears = listener();
    const onState = (e: Event) => {
      const state = (e as CustomEvent<string>).detail;
      if (!wanted.current) return;
      if (state === "error") {
        setError(ears.error);
        setPhase("error");
      } else if (state === "listening" || state === "muted") {
        if (phaseRef.current === "starting" || phaseRef.current === "error") setPhase("listening");
        setMuted(state === "muted");
      }
    };
    const onSpeech = () => {
      window.clearTimeout(resumeTimer.current);
      const now = phaseRef.current;
      if (now === "ringing") {
        void pickUpRef.current(); // speaking to a ringing phone answers it
        return;
      }
      if (speaker().speaking) speaker().pause();
      if (now === "listening" || now === "speaking") {
        setCaption("");
        setPhase("hearing");
      }
    };
    const onSilence = () => {
      // Words follow within a moment; if none come, what was heard was not the user.
      if (speaker().paused) {
        window.clearTimeout(resumeTimer.current);
        resumeTimer.current = window.setTimeout(() => {
          if (speaker().paused) {
            speaker().resume();
            setPhase("speaking");
          }
        }, RESUME_MS);
      }
    };
    const onPartial = (e: Event) => setCaption((e as CustomEvent<string>).detail);
    const onFinal = (e: Event) => {
      const text = (e as CustomEvent<string>).detail;
      window.clearTimeout(resumeTimer.current);
      if (speaker().paused || speaker().speaking) {
        if (isEcho(text, speaker().current)) {
          speaker().resume();
          setPhase("speaking");
          return;
        }
        speaker().stop();
      }
      if (phaseRef.current === "thinking") return; // the last request is still being placed; one at a time
      void utter(text);
    };
    ears.addEventListener("state", onState);
    ears.addEventListener("speech", onSpeech);
    ears.addEventListener("silence", onSilence);
    ears.addEventListener("partial", onPartial);
    ears.addEventListener("final", onFinal);
    return () => {
      ears.removeEventListener("state", onState);
      ears.removeEventListener("speech", onSpeech);
      ears.removeEventListener("silence", onSilence);
      ears.removeEventListener("partial", onPartial);
      ears.removeEventListener("final", onFinal);
    };
  }, [setPhase, utter]);
  const pickUpRef = useRef(pickUp);
  pickUpRef.current = pickUp;

  // --- the tasks: a finished one rings --------------------------------------------------------------------------

  useEffect(() => {
    const announce = async (chat: Chat) => {
      try {
        const detail = await api<Detail>(`/chats/${chat.id}`);
        const reply = [...detail.messages].reverse().find((m) => m.role === "assistant" && m.content);
        const run = detail.runs.at(-1);
        const questions = run?.questions || [];
        const failed = chat.status === "failed";
        const text = reply?.content || (failed ? run?.error || "Something went wrong." : "");
        if (!text) return;
        queue.current.push({
          chatId: chat.id,
          title: chat.title,
          text,
          spoken: reply?.spoken || "",
          sensitive: questions.some((q) => q.sensitive),
          login: questions.some((q) => q.kind === "login"),
          failed,
        });
        maybeRing();
      } catch {
        // the next change to the list brings it round again
      }
    };
    return onUpdate<Chat[]>("chats", (list) => {
      const next = new Map(list.map((c) => [c.id, c.status]));
      setWorking(list.filter((c) => WORKING.includes(c.status || "")));
      const before = statuses.current;
      statuses.current = next;
      keepStatuses(next);
      if (!before) return; // the first list says where things stand; only changes from here on are news
      for (const chat of list) {
        const was = before.get(chat.id);
        if (was && WORKING.includes(was) && HEARD.includes(chat.status || "")) void announce(chat);
      }
    });
  }, [maybeRing]);

  // --- the screen stays on while voice is on: a phone that dims and locks would put the app in the background, and
  // there the microphone closes (it would cost while nobody speaks). In front again, the microphone reopens. ------

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
      // not allowed here (low battery, a browser without it): the screen keeps its own timeout
    }
  }, []);
  const letSleep = useCallback(() => {
    void lock.current?.release().catch(() => {});
    lock.current = null;
  }, []);

  useEffect(() => {
    const visibility = () => {
      if (!wanted.current) return;
      if (document.hidden) listener().stop();
      else {
        void keepAwake(); // the lock is let go whenever the page is hidden
        void listener().start();
      }
    };
    document.addEventListener("visibilitychange", visibility);
    return () => document.removeEventListener("visibilitychange", visibility);
  }, [keepAwake]);

  useEffect(
    () => () => {
      wanted.current = false;
      ears.current?.stop();
      mouth.current?.stop();
      bell.current?.stop();
      letSleep();
    },
    [letSleep],
  );

  // --- what the screen calls -------------------------------------------------------------------------------------

  const start = useCallback(async () => {
    wanted.current = true;
    setError("");
    speaker().unlock();
    setPhase("starting");
    void keepAwake();
    await listener().start();
  }, [keepAwake, setPhase]);

  // The screen opens listening without a tap, but browsers only play sound after one: the first tap anywhere (the
  // mute key, the speaker key, the keypad) unlocks the speaker, so the first acknowledgment is heard.
  useEffect(() => {
    return onEveryTap(() => speaker().unlock());
  }, []);

  const stop = useCallback(() => {
    wanted.current = false;
    window.clearTimeout(resumeTimer.current);
    letSleep();
    listener().stop();
    speaker().stop();
    ringer().stop();
    setCall(null);
    setCaption("");
    setPhase("off");
  }, [letSleep, setPhase]);

  const toggleMute = useCallback(async () => {
    const next = !listener().muted;
    setMuted(next); // the key answers at once; unmuting opens the microphone, which takes a moment
    if (!(await listener().setMuted(next))) setMuted(true); // it would not open: still muted
  }, []);

  /** Where Relay's voice plays (see Output). The microphone is opened again with the matching processing. */
  const setOutput = useCallback(
    async (next: Output) => {
      try {
        localStorage.setItem(OUTPUT_KEY, next);
      } catch {}
      setOutputState(next);
      if (listener().output === next) return;
      listener().output = next;
      if (wanted.current && phaseRef.current !== "off") {
        listener().stop();
        setPhase("starting");
        await listener().start();
      }
    },
    [setPhase],
  );

  /** A tap while Relay speaks: it stops, and listens. */
  const interrupt = useCallback(() => {
    if (phaseRef.current !== "speaking") return;
    speaker().stop(); // whoever awaited the speech settles
  }, []);

  /** Typed instead of spoken: an ordinary request. A private value never comes here (see privately). */
  const type = useCallback(
    async (text: string) => {
      await utter(text);
    },
    [utter],
  );

  /** Values given privately from the keypad: to the focused task, never transcribed or sent to the front desk. */
  const privately = useCallback(
    async (entries: PrivateEntry[], chatId = focusRef.current?.chatId) => {
      try {
        const said = await sendPrivately(chatId, entries);
        setAsking(null);
        await speak(said);
        settle();
        return true;
      } catch (e) {
        setError((e as Error).message);
        return false;
      }
    },
    [settle, speak],
  );

  return {
    phase,
    caption,
    said,
    focus,
    call,
    error,
    muted,
    working,
    output,
    start,
    stop,
    pickUp,
    dismiss,
    toggleMute,
    setOutput,
    interrupt,
    type,
    privately,
    asking,
    dismissAsk: () => setAsking(null),
  };
}
