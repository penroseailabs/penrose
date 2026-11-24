/**
 * Talking into the chat instead of typing (the composer's mic): the voice screen's ears (speech.ts Listener, the same
 * transcription) with none of its mouth or desk. Each thing said is handed over as text once it is complete, and the
 * line stays open for the next until the user goes back to typing. The browser's own noise and echo processing is
 * on (Output "earpiece"): nothing plays here, and it is the better setting for one voice close to the microphone.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Listener } from "./speech";

export type Dictation = {
  /** The microphone is open (or opening) and what is said goes to the chat. */
  on: boolean;
  /** Listening is paused: the microphone is closed, but the line stays up and the keyboard stays away. */
  muted: boolean;
  /** Why it could not open (no permission, no key): shown where the waves would be. */
  error: string;
  start: () => void;
  stop: () => void;
  toggleMute: () => void;
};

export function useDictation(onText: (text: string) => void): Dictation {
  const [on, setOn] = useState(false);
  const [error, setError] = useState("");
  const [muted, setMuted] = useState(false);
  const ears = useRef<Listener | null>(null);
  const hand = useRef(onText);
  hand.current = onText;

  const stop = useCallback(() => {
    ears.current?.stop();
    ears.current = null;
    setOn(false);
    setMuted(false);
  }, []);

  const start = useCallback(() => {
    if (ears.current) return;
    const listener = new Listener();
    listener.output = "earpiece";
    listener.muted = false;
    ears.current = listener;
    setError("");
    setMuted(false);
    setOn(true);
    listener.addEventListener("final", (e) => hand.current((e as CustomEvent<string>).detail));
    listener.addEventListener("state", () => {
      if (ears.current !== listener || listener.state !== "error") return;
      setError(listener.error);
      listener.stop(); // no retry loop behind a message the user is reading
      ears.current = null;
      setOn(false);
    });
    void listener.start();
  }, []);

  /** Pause or resume listening. The key answers at once; unmuting opens the microphone, which takes a moment. */
  const toggleMute = useCallback(async () => {
    const listener = ears.current;
    if (!listener) return;
    const next = !listener.muted;
    setMuted(next);
    if (!(await listener.setMuted(next))) setMuted(true); // it would not open: still muted (the error shows)
  }, []);

  // A page in the background must not keep the microphone: back in front, the user taps the mic again.
  useEffect(() => {
    const hidden = () => document.hidden && stop();
    document.addEventListener("visibilitychange", hidden);
    return () => {
      document.removeEventListener("visibilitychange", hidden);
      ears.current?.stop();
      ears.current = null;
    };
  }, [stop]);

  return { on, muted, error, start, stop, toggleMute };
}
