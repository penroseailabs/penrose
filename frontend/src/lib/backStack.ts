import { useEffect, useRef } from "react";

/** What is on screen, as far as the phone's Back button is concerned. */
export type View = { voice: boolean; chat: string; sheet: string; drawer: boolean; watching: boolean };

type Entry = { i: number; view: View };

const key = (v: View) => `${+v.voice}|${v.chat}|${v.sheet}|${+v.drawer}|${+v.watching}`;

/**
 * Makes the Back button (Android's, the browser's) walk back through what the user opened instead of leaving the app:
 * a chat opened from the voice screen, a sheet over a chat, the drawer. Each screen the user moves to is a history
 * entry carrying its view; Back restores the view of the entry it lands on. Nothing is in the address, so it works the
 * same behind the tunnel, and a reload starts from the app's home as before.
 *
 * Moves that should not leave a step behind: closing what the previous entry was (Back is used instead, so the stack
 * does not grow), the drawer giving way to the chat or sheet picked in it (that entry is rewritten), and a new chat
 * getting its id when its first message is sent.
 */
export function useBackStack(view: View, restore: (view: View) => void, on = true) {
  const stack = useRef<Entry[]>([]);
  const index = useRef(0);
  const latest = useRef(restore);
  latest.current = restore;

  // Back and Forward: show the view of the entry landed on.
  useEffect(() => {
    if (!on) return;
    const pop = (event: PopStateEvent) => {
      const entry = event.state as Entry | null;
      if (!entry || typeof entry.i !== "number" || !entry.view) return;
      index.current = entry.i;
      stack.current[entry.i] = entry;
      latest.current(entry.view);
    };
    window.addEventListener("popstate", pop);
    return () => window.removeEventListener("popstate", pop);
  }, [on]);

  // The user moved: record it.
  useEffect(() => {
    if (!on) return;
    const here = stack.current[index.current];
    if (!here) {
      const first: Entry = { i: 0, view };
      stack.current = [first];
      history.replaceState(first, "");
      return;
    }
    if (key(here.view) === key(view)) return;
    const before = stack.current[index.current - 1];
    if (before && key(before.view) === key(view)) {
      history.back(); // closing what was opened: the same as Back
      return;
    }
    const replace =
      here.view.drawer ||
      (here.view.chat === "" && view.chat !== "" && key({ ...here.view, chat: view.chat }) === key(view));
    const entry: Entry = { i: replace ? index.current : index.current + 1, view };
    stack.current = stack.current.slice(0, entry.i);
    stack.current[entry.i] = entry;
    index.current = entry.i;
    if (replace) history.replaceState(entry, "");
    else history.pushState(entry, "");
  }, [on, view.voice, view.chat, view.sheet, view.drawer, view.watching]);
}
