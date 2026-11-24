/**
 * Ringing while the app is in the background. Android freezes a web app that is not in front, so the ring in
 * voice/useVoice.ts only sounds while Relay is open; for the rest, the phone subscribes to Web Push with the
 * coordinator's key (tunnel.ts `pushKey`) and sends the subscription to the computer over the tunnel. When a task
 * has something for the user the computer asks the coordinator to push, and the service worker (sw.ts) shows a
 * notification with the chat's title and Relay's reply, encrypted on the computer for this phone alone (neither the
 * coordinator nor the push service can read it). Tapping it opens the voice screen; its Reply opens or answers the chat (useNotificationOpen).
 *
 * The user is asked once, from the voice screen, after voice has started; the answer the browser keeps
 * (Notification.permission) is the answer, and "not now" is kept here so the question is not asked again.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { COMPANION } from "./flag";
import type { Tunnel } from "./tunnel";

const LATER_KEY = "relay.push.later";
let tunnel: Tunnel | null = null;

export const pushSupported = () =>
  COMPANION && "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;

/** Keep the tunnel to send subscriptions over, and send the one this phone holds whenever the link is (re)made. */
export function attachPush(t: Tunnel) {
  tunnel = t;
  if (!pushSupported()) return;
  let linked = false;
  t.addEventListener("change", () => {
    const now = t.state === "linked";
    if (now && !linked) void syncPush();
    linked = now;
  });
  t.addEventListener("pushkey", () => {
    if (t.state === "linked") void syncPush();
  });
}

function keyBytes(key: string): Uint8Array {
  const binary = atob(key.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (key.length % 4)) % 4));
  return Uint8Array.from(binary, (c) => c.charCodeAt(0));
}

function sameKey(a: ArrayBuffer | null | undefined, b: Uint8Array) {
  if (!a || a.byteLength !== b.byteLength) return false;
  const bytes = new Uint8Array(a);
  return bytes.every((v, i) => v === b[i]);
}

/** Subscribe (or keep the subscription) with the coordinator's key and tell the computer. */
async function subscribe(): Promise<boolean> {
  if (!tunnel || !tunnel.pushKey || Notification.permission !== "granted") return false;
  const registration = await navigator.serviceWorker.ready;
  const key = keyBytes(tunnel.pushKey);
  let subscription = await registration.pushManager.getSubscription();
  if (subscription && !sameKey(subscription.options.applicationServerKey, key)) {
    // The coordinator's key changed (it was set up again): the old subscription would be signed with the wrong key.
    await subscription.unsubscribe().catch(() => {});
    subscription = null;
  }
  subscription ??= await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
  tunnel.sendPush(subscription.toJSON() as Record<string, unknown>);
  return true;
}

/** Granted earlier: make sure the computer has a subscription that still works. */
export async function syncPush() {
  if (!pushSupported()) return;
  try {
    await subscribe();
  } catch {
    // the push service could not be reached, or the browser refused: next time
  }
}

/** The user said yes on the voice screen: ask the browser, then subscribe. */
export async function enablePush(): Promise<boolean> {
  if (!pushSupported()) return false;
  try {
    if ((await Notification.requestPermission()) !== "granted") return false;
    return await subscribe();
  } catch {
    return false;
  }
}

/** Stop being rung in the background. */
export async function disablePush() {
  if (!pushSupported()) return;
  try {
    const registration = await navigator.serviceWorker.ready;
    await (await registration.pushManager.getSubscription())?.unsubscribe();
  } catch {}
  tunnel?.sendPush(null);
}

/** The voice screen's one question, shown while voice is on and the browser has not been asked. */
export function usePushOffer(active: boolean) {
  const [offer, setOffer] = useState(false);
  useEffect(() => {
    if (!active || !pushSupported() || Notification.permission !== "default") return setOffer(false);
    let later = false;
    try {
      later = localStorage.getItem(LATER_KEY) === "1";
    } catch {}
    setOffer(!later);
  }, [active]);
  const allow = useCallback(async () => {
    setOffer(false);
    await enablePush();
  }, []);
  const later = useCallback(() => {
    setOffer(false);
    try {
      localStorage.setItem(LATER_KEY, "1");
    } catch {}
  }, []);
  return { offer, allow, later };
}

/** A notification's chat to open, from the address (the app was not running) or the service worker (it was). */
export function openedChat(search: string): { chat: string; reply: boolean } | null {
  const params = new URLSearchParams(search);
  const chat = params.get("chat");
  return chat ? { chat, reply: params.get("reply") === "1" } : null;
}

/** The session a tapped notification was about, from the address of an app that was not running ("/?answer=…"). */
export function openedAnswer(search: string): string | null {
  return new URLSearchParams(search).get("answer") || null;
}

/** The briefing a tapped notification was about, from the address of an app that was not running ("/?brief=…"). */
export function openedBriefing(search: string): number | null {
  const id = Number(new URLSearchParams(search).get("brief"));
  return Number.isInteger(id) && id > 0 ? id : null;
}

/**
 * A tapped notification (sw.ts). Tapping the notification itself calls `voice` with the session it was about (if it
 * said): the voice screen opens on it; its Reply button calls `open` with the chat: `reply` says the button was
 * pressed, `text` is what was typed into the notification, to be sent. A briefing's notification calls `brief` with
 * its id, while the app runs (one that was not running reads it with openedBriefing).
 */
export function useNotificationOpen(
  open: (chat: string, reply: boolean, text: string) => void,
  voice: (chat: string) => void,
  brief: (id: number) => void,
) {
  const latest = useRef(open);
  latest.current = open;
  const toVoice = useRef(voice);
  toVoice.current = voice;
  const toBriefing = useRef(brief);
  toBriefing.current = brief;
  useEffect(() => {
    if (!COMPANION) return;
    const first = openedChat(location.search);
    if (first) {
      history.replaceState(null, "", location.pathname + location.hash);
      latest.current(first.chat, first.reply, "");
    }
    if (!("serviceWorker" in navigator)) return;
    const onMessage = (event: MessageEvent) => {
      const data = event.data as { t?: unknown; chat?: unknown; reply?: unknown; text?: unknown; id?: unknown } | null;
      if (data?.t === "voice") return toVoice.current(typeof data.chat === "string" ? data.chat : "");
      if (data?.t === "brief" && typeof data.id === "number") return toBriefing.current(data.id);
      if (data?.t !== "open" || typeof data.chat !== "string" || !data.chat) return;
      latest.current(data.chat, data.reply === true, typeof data.text === "string" ? data.text : "");
    };
    navigator.serviceWorker.addEventListener("message", onMessage);
    navigator.serviceWorker.startMessages();
    // Opened by a notification: the worker holds what it was about until the app asks.
    navigator.serviceWorker.controller?.postMessage({ t: "ready" });
    return () => navigator.serviceWorker.removeEventListener("message", onMessage);
  }, []);
}
