/**
 * What the window shows besides the open chat, pushed by the API as it changes (backend/app/api/updates.py): the
 * services' health, the phone, the chat list, the companion's status and the "For you" list. One stream for the whole window; each
 * listener gets the latest value at once and every change after.
 */

export type UpdateKind = "health" | "devices" | "chats" | "companion" | "desk" | "for_you";
type Listener = (value: unknown) => void;

const listeners = new Map<string, Set<Listener>>();
const latest = new Map<string, unknown>();
let source: EventSource | null = null;

function emit(kind: string, value: unknown) {
  latest.set(kind, value);
  listeners.get(kind)?.forEach((listener) => listener(value));
}

function open() {
  if (source) return;
  source = new EventSource("/api/updates");
  source.onmessage = (message) => {
    const update = JSON.parse(message.data) as Record<string, unknown>;
    for (const [kind, value] of Object.entries(update)) emit(kind, value);
  };
  // The browser reconnects by itself, and the API then sends everything again; meanwhile nothing is known. One it
  // gave up on (the API answered with an error) is opened again shortly.
  source.onerror = () => {
    latest.clear();
    emit("down", true);
    if (source?.readyState === EventSource.CLOSED) {
      source = null;
      window.setTimeout(open, 2000);
    }
  };
}

/** Follow one kind of update. Returns the function that stops following. */
export function onUpdate<T>(kind: UpdateKind | "down", listener: (value: T) => void): () => void {
  open();
  const set = listeners.get(kind) || new Set<Listener>();
  listeners.set(kind, set);
  const wrapped = listener as Listener;
  set.add(wrapped);
  if (latest.has(kind)) wrapped(latest.get(kind));
  return () => set.delete(wrapped);
}
