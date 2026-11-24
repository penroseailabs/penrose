import { useEffect } from "react";
import { api } from "../api";

// Signs that the user is here and may ask for something: the window (or the phone app) came up or got focus, they
// are typing, clicking or scrolling, or they are simply still at it (in front, with something done in the last ten
// minutes: reading an answer, thinking about what to ask). Each sign tells penrose to wake the phone now and keep
// it awake a minute (api/devices.py presence), so the task that follows finds it awake, out of Doze and its apps
// back on the network, instead of waking it then. Without the last kind the phone slept a minute after the last
// key press, under a user who was still reading, and their next task started on a phone only just woken
// (2026-11-20).
const EVERY_MS = 15_000; // the API keeps it awake 60 s per sign: one every 15 s while active is plenty
const STILL_HERE_MS = 10 * 60_000; // in front and untouched for longer than this: gone, the phone may sleep

export function usePresence() {
  useEffect(() => {
    let last = 0;
    let touched = Date.now();
    const sign = () => {
      if (document.visibilityState !== "visible") return;
      const now = Date.now();
      if (now - last < EVERY_MS) return;
      last = now;
      api("/devices/presence", "POST").catch(() => {}); // only a head start: never worth an error
    };
    const act = () => {
      touched = Date.now();
      sign();
    };
    const still = () => {
      if (document.hasFocus() && Date.now() - touched < STILL_HERE_MS) sign();
    };
    act(); // opened
    document.addEventListener("visibilitychange", act);
    window.addEventListener("focus", act);
    window.addEventListener("keydown", act, { passive: true });
    window.addEventListener("pointerdown", act, { passive: true });
    window.addEventListener("wheel", act, { passive: true });
    const timer = window.setInterval(still, EVERY_MS);
    return () => {
      document.removeEventListener("visibilitychange", act);
      window.removeEventListener("focus", act);
      window.removeEventListener("keydown", act);
      window.removeEventListener("pointerdown", act);
      window.removeEventListener("wheel", act);
      window.clearInterval(timer);
    };
  }, []);
}
