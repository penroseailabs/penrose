/**
 * The companion app's service worker (built by vite-plugin-pwa, vite.config.ts): the app's files are installed and
 * served from here, the math and diagram code is cached the first time a reply needs it, and a push from the
 * coordinator (push.ts) becomes a notification while the app is not in front. The push carries the chat, its title
 * and Relay's latest reply, encrypted on the computer for this phone (backend/app/companion/webpush.py); the
 * notification shows them with a Reply button. Tapping the notification opens the voice screen on that session; the
 * notification is the soft nudge, and leaving it alone leaves the screen silent. Where the browser lets a notification take typed text (Chrome keeps `type: "text"` actions), the reply is
 * typed in the notification and handed to the app to send, since only the app holds the link to the computer;
 * elsewhere Reply opens the chat with the keyboard up. A question asking for a password or code offers no typed reply. An older computer's push says
 * only that there is something. A briefing newly ready in For you rings too; tapping it opens the briefing.
 */

/// <reference lib="webworker" />
import { clientsClaim } from "workbox-core";
import { ExpirationPlugin } from "workbox-expiration";
import { cleanupOutdatedCaches, createHandlerBoundToURL, precacheAndRoute } from "workbox-precaching";
import { NavigationRoute, registerRoute } from "workbox-routing";
import { CacheFirst } from "workbox-strategies";

declare let self: ServiceWorkerGlobalScope;

// A new worker waits until the person taps Update in the app (updateBanner.ts), which sends SKIP_WAITING; the very
// first install has nothing to wait for. Claiming at once keeps the reloaded page on the new worker.
self.addEventListener("message", (event) => {
  if ((event.data as { type?: unknown } | null)?.type === "SKIP_WAITING") void self.skipWaiting();
});
clientsClaim();
precacheAndRoute(self.__WB_MANIFEST);
cleanupOutdatedCaches();
registerRoute(new NavigationRoute(createHandlerBoundToURL("index.html"), { denylist: [/^\/ws\//, /^\/health$/] }));
// Installed files are answered by the precache first, so this only ever meets the ones left out of the install.
registerRoute(
  /\/assets\/[^/]+\.js$/,
  new CacheFirst({ cacheName: "relay-on-demand", plugins: [new ExpirationPlugin({ maxEntries: 200 })] }),
);

const TAG = "relay-ring";

/** What the computer puts in a push (webpush.py `message`, or `briefing` for a briefing just ready in For you, with
 *  `brief` its id); an older computer sends only {t: "ring"}. */
type Ring = {
  t?: string;
  chat?: unknown;
  brief?: unknown;
  quiet?: unknown;
  title?: unknown;
  body?: unknown;
  status?: unknown;
  private?: unknown;
};
/** What the app is told when a notification is tapped (push.ts useNotificationOpen). */
type Open = { t: "open"; chat: string; reply: boolean; text: string };
const str = (value: unknown) => (typeof value === "string" ? value : "");
const REPLY = { action: "reply", title: "Reply" };

async function windows() {
  return self.clients.matchAll({ type: "window", includeUncontrolled: true });
}

async function ring(push: Ring) {
  // In front, the app rings by itself from the chat list (voice/useVoice.ts): a notification on top would be noise.
  // Visible alone is not enough: a page frozen by Android when the screen locked can still report itself visible,
  // and the user never heard that ring (2026-10-31). Focus goes with the lock.
  if ((await windows()).some((client) => client.visibilityState === "visible" && client.focused)) return;
  if (push.t === "brief" && typeof push.brief === "number") return briefed(push.brief, push);
  const chat = str(push.chat);
  await self.registration.showNotification(str(push.title) || "penrose", {
    body: str(push.body) || "penrose has something for you",
    // One notification per chat: news from the same chat replaces the last, other chats' stay.
    tag: chat ? `chat:${chat}` : TAG,
    icon: "/companion/icon-192.png",
    badge: "/companion/badge-96.png",
    data: { chat },
    // Vibration, re-alerting and buttons are part of the ring; TypeScript's DOM types left them out.
    ...({
      vibrate: [200, 100, 200],
      renotify: true,
      actions: chat ? [push.private === true ? REPLY : { ...REPLY, type: "text", placeholder: "Reply to penrose" }] : [],
    } as NotificationOptions),
  });
}

/** A briefing in For you: where it is from, and what penrose made of it. Tapping it opens the briefing. Once penrose
 *  has prepared it, what it found replaces the first notification (`quiet`) without ringing again. */
async function briefed(id: number, push: Ring) {
  const quiet = push.quiet === true;
  await self.registration.showNotification(str(push.title) || "penrose", {
    body: str(push.body) || "Something is ready for you",
    tag: `brief:${id}`,
    icon: "/companion/icon-192.png",
    badge: "/companion/badge-96.png",
    data: { brief: id },
    silent: quiet,
    ...({ vibrate: quiet ? [] : [200, 100, 200], renotify: false } as NotificationOptions),
  });
}

/** Bring Relay to the front on a briefing, opened as when its card is tapped (App.tsx). */
async function openBriefing(id: number) {
  const [client] = await windows();
  if (client) {
    await client.focus().catch(() => undefined);
    client.postMessage({ t: "brief", id });
    return;
  }
  await self.clients.openWindow(`/?brief=${id}`);
}

// A notification's chat for an app that is still starting: it asks once it listens ({t: "ready"}).
let waiting: { open: Open; claimed: () => void } | null = null;

self.addEventListener("message", (event) => {
  if ((event.data as { t?: unknown } | null)?.t !== "ready" || !waiting) return;
  (event.source as Client | null)?.postMessage(waiting.open);
  waiting.claimed();
  waiting = null;
});

/** Bring Relay to the front on the voice screen, opened on the session the notification was about (the same as
 *  tapping its card there: voice/useDesk.ts `answer`). */
async function openVoice(chat: string) {
  const [client] = await windows();
  if (client) {
    await client.focus().catch(() => undefined);
    client.postMessage({ t: "voice", chat });
    return;
  }
  // The app opens talking, on that session (App.tsx reads `answer`).
  await self.clients.openWindow(chat ? `/?answer=${encodeURIComponent(chat)}` : "/");
}

/** Bring Relay to the front on the chat; the app opens it, sends a typed reply or raises the keyboard (push.ts). */
async function open(message: Open) {
  const [client] = await windows();
  if (client) {
    await client.focus().catch(() => undefined);
    client.postMessage(message);
    return;
  }
  // Kept here until the new window asks for it, at most a minute (the worker stays awake while it waits).
  const claimed = new Promise<void>((resolve) => {
    waiting = { open: message, claimed: resolve };
    setTimeout(resolve, 60_000);
  });
  await self.clients.openWindow(`/?chat=${encodeURIComponent(message.chat)}`);
  await claimed;
}

self.addEventListener("push", (event) => {
  let push: Ring = {};
  try {
    push = (event.data?.json() as Ring) ?? {};
  } catch {
    // not JSON: an empty ring
  }
  event.waitUntil(ring(push));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const brief = (event.notification.data as { brief?: unknown } | null)?.brief;
  if (typeof brief === "number") return event.waitUntil(openBriefing(brief));
  const chat = str((event.notification.data as { chat?: unknown } | null)?.chat);
  // What was typed into the notification, where the browser offers that (not in TypeScript's DOM types yet).
  const text = str((event as NotificationEvent & { reply?: unknown }).reply).trim();
  // Tapping the notification itself goes to the voice screen; only Reply (typed or not) goes to the chat.
  if (chat && (event.action === "reply" || text)) {
    event.waitUntil(open({ t: "open", chat, reply: true, text }));
  } else {
    event.waitUntil(openVoice(chat));
  }
});
