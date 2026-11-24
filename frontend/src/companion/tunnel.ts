/**
 * The phone's connection to its computer, through the coordinator: the handshake, then encrypted records
 * (the protocol is described in backend/app/companion/tunnel.py). `installShims` makes the rest of the app
 * use it without knowing: `/api` fetches become requests over the tunnel, EventSource becomes a subscription to a
 * run's events and the live view's stream one to the phone's screen, so the UI is the desktop app's own code.
 *
 * The link comes back by itself, with nothing for the user to do: when the socket closes, when it stops answering
 * (a phone that slept the app, a network that changed), when the app comes back to the front, and when the computer
 * reconnects (its API restarted: it no longer knows the old session, so every "online" starts a new handshake).
 * Reads that were in flight are sent again once the link is back; a change that may have reached the computer is
 * not, and the app says so.
 */

import { Handshake, HandshakeError, Session, unb64 } from "./crypto";

export type TunnelState =
  | "connecting" // to the coordinator
  | "offline" // the coordinator is reached, the computer is not there
  | "handshake"
  | "pending" // paired on the wire; the user is approving this phone on the computer
  | "linked"
  | "denied"
  | "unlinked" // the computer no longer knows this phone
  | "expired" // the QR code's pairing secret is no longer valid
  | "error";

export type Options = {
  origin: string;
  mailbox: string;
  computerKey: Uint8Array;
  /** The QR code's secret while pairing; empty for a linked phone. */
  secret: Uint8Array;
  identity: CryptoKeyPair;
  phoneName: string;
};

type Pending = {
  resolve: (r: { status: number; body: unknown; cache?: string }) => void;
  reject: (e: Error) => void;
  timer: number;
  /** A read: sent again once the link is back. */
  again?: () => void;
};
/** What a subscription follows: a run's events after `after`, the window's update stream (health, the phone, the
 *  chat list), or the phone's screen (frames as `{jpeg}`, base64). */
type Topic = { run: string; after: number } | { updates: true } | { screen: string };
type Subscription = {
  topic: Topic;
  onEvent: (event: Record<string, unknown>) => void;
  onEnd: () => void;
};

const text = new TextEncoder();
const untext = new TextDecoder();
const REQUEST_TIMEOUT = 60_000;
const QUEUE_TIMEOUT = 45_000; // a change waiting for the link; reads wait as long as it takes
const PING_MS = 25_000;
const PONG_MS = 8_000; // a socket that has not answered a ping by then is gone, whatever the browser thinks
const PROBE_MS = 4_000; // back in front: an open socket must answer this soon
// Back in front after this long away, or on a network that changed, an open socket is more likely dead than not
// (Android lets the app's sockets go while it sleeps it): start a new one at once rather than wait for the probe.
const STALE_MS = 20_000;
// A connection or handshake that has not finished by then starts over: a socket that never opens, or a hello the
// computer never answers (it reached a computer connection that was being replaced).
const SETTLE_MS = 10_000;
const RETRY_MS = [500, 1000, 2000, 4000, 8000, 15000];
/** For a change that may or may not have reached the computer when the link dropped. */
const UNSURE = "Reconnected to your session. If that didn't go through, try it again.";
const WAITING = "Still reconnecting to your session. Try again in a moment.";

export class Tunnel extends EventTarget {
  state: TunnelState = "connecting";
  /** Linked at least once: a drop after that is "reconnecting", not a first connection. */
  wasLinked = false;
  /** Shown while pending: the same code the computer shows. */
  code = "";
  computerName = "";
  phoneId = "";
  error = "";
  /** The coordinator's key for Web Push subscriptions (push.ts), once it has said. */
  pushKey = "";

  private ws: WebSocket | null = null;
  private session: Session | null = null;
  private handshake: Handshake | null = null;
  private next = 1;
  private pending = new Map<number, Pending>();
  private queue: (() => void)[] = [];
  private subscriptions = new Map<number, Subscription>();
  private attempt = 0;
  private closed = false;
  private pingTimer = 0;
  private pongTimer = 0;
  private retryTimer = 0;
  private stallTimer = 0;
  private hiddenAt = 0;
  private wake = (event: Event) => this.resume(event.type === "online");

  constructor(private options: Options) {
    super();
  }

  private set(state: TunnelState, error = "") {
    if (state === "linked") this.wasLinked = true;
    if (state !== "connecting" && state !== "handshake") window.clearTimeout(this.stallTimer);
    this.state = state;
    this.error = error;
    this.dispatchEvent(new Event("change"));
  }

  connect() {
    if (this.closed) return;
    window.clearTimeout(this.retryTimer);
    if (!this.listening) {
      this.listening = true;
      document.addEventListener("visibilitychange", this.wake);
      window.addEventListener("online", this.wake);
      window.addEventListener("pageshow", this.wake);
    }
    const url = new URL(this.options.origin);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    url.pathname = `/ws/${this.options.mailbox}`;
    url.search = "?role=phone";
    const ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    this.lost();
    if (this.state !== "pending") this.set("connecting");
    ws.onopen = () => {
      this.attempt = 0;
      this.pingTimer = window.setInterval(() => this.ping(PONG_MS), PING_MS);
    };
    ws.onmessage = (event) => this.receive(event.data as string | ArrayBuffer).catch((e) => this.fail(e));
    ws.onclose = () => this.closedSocket(ws);
    ws.onerror = () => {};
    this.settleWithin(ws);
  }

  /** Start over on `ws` unless the link is up (or waiting on the user or the computer) by SETTLE_MS. */
  private settleWithin(ws: WebSocket) {
    window.clearTimeout(this.stallTimer);
    this.stallTimer = window.setTimeout(() => {
      if (this.ws === ws && (this.state === "connecting" || this.state === "handshake")) this.abandon(ws);
    }, SETTLE_MS);
  }
  private listening = false;

  /** The socket is gone (closed, or it stopped answering): try again shortly. */
  private closedSocket(ws: WebSocket) {
    if (ws !== this.ws) return;
    ws.onopen = ws.onmessage = ws.onclose = null;
    window.clearInterval(this.pingTimer);
    window.clearTimeout(this.pongTimer);
    window.clearTimeout(this.stallTimer);
    this.ws = null;
    this.lost();
    if (this.closed || ["denied", "unlinked", "expired"].includes(this.state)) return;
    if (this.state !== "pending") this.set("connecting");
    // In the background the browser holds timers back; coming to the front reconnects at once (resume).
    this.retryTimer = window.setTimeout(() => this.connect(), RETRY_MS[Math.min(this.attempt++, RETRY_MS.length - 1)]);
  }

  /** Drop a socket that has stopped answering, without waiting for the browser to notice. */
  private abandon(ws: WebSocket) {
    try {
      ws.close();
    } catch {}
    this.closedSocket(ws);
  }

  private ping(within: number) {
    const ws = this.ws;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    try {
      ws.send("ping");
    } catch {
      return this.abandon(ws);
    }
    window.clearTimeout(this.pongTimer);
    this.pongTimer = window.setTimeout(() => this.abandon(ws), within);
  }

  /** Back in front, or the network came back: make sure the link is up now rather than when a timer allows. */
  private resume(networkChanged: boolean) {
    if (this.closed) return;
    if (document.hidden) {
      this.hiddenAt ||= Date.now();
      return;
    }
    const stale = networkChanged || (this.hiddenAt > 0 && Date.now() - this.hiddenAt > STALE_MS);
    this.hiddenAt = 0;
    if (["denied", "unlinked", "expired"].includes(this.state)) return;
    const ws = this.ws;
    if (!ws || ws.readyState === WebSocket.CLOSING || ws.readyState === WebSocket.CLOSED) {
      this.attempt = 0;
      this.connect();
    } else if (stale) {
      this.abandon(ws);
      this.attempt = 0;
      this.connect(); // now, not after the retry pause abandon set
    } else if (ws.readyState === WebSocket.OPEN) {
      this.ping(PROBE_MS);
    }
  }

  close() {
    this.closed = true;
    window.clearTimeout(this.retryTimer);
    window.clearInterval(this.pingTimer);
    window.clearTimeout(this.pongTimer);
    window.clearTimeout(this.stallTimer);
    document.removeEventListener("visibilitychange", this.wake);
    window.removeEventListener("online", this.wake);
    window.removeEventListener("pageshow", this.wake);
    this.ws?.close();
    for (const [, waiting] of this.pending) {
      window.clearTimeout(waiting.timer);
      waiting.reject(new Error("Closed"));
    }
    this.pending.clear();
  }

  private fail(error: unknown) {
    const message = error instanceof Error ? error.message : String(error);
    if (error instanceof HandshakeError) {
      if (/not linked/i.test(message)) this.set("unlinked", message);
      else if (/pairing code/i.test(message)) this.set("expired", message);
      else this.set("error", message);
    } else {
      this.set("error", message);
    }
    if (this.ws) this.abandon(this.ws);
  }

  /** The session is over (the socket closed, or the computer went away or came back): reads go out again once a
   *  new one is up; a change that was sent may have reached the computer, so it is not sent twice. */
  private lost() {
    this.session = null;
    this.handshake = null;
    for (const [id, waiting] of this.pending) {
      window.clearTimeout(waiting.timer);
      this.pending.delete(id);
      if (waiting.again) this.queue.push(waiting.again);
      else waiting.reject(new Error(UNSURE));
    }
  }

  private async receive(data: string | ArrayBuffer) {
    if (typeof data === "string") {
      if (data === "pong") {
        window.clearTimeout(this.pongTimer);
        return;
      }
      let control: { t?: string } = {};
      try {
        control = JSON.parse(data);
      } catch {
        return;
      }
      if (control.t === "push_key" && typeof (control as { key?: unknown }).key === "string") {
        this.pushKey = (control as { key: string }).key;
        this.dispatchEvent(new Event("pushkey"));
      } else if (control.t === "online") {
        // The computer is there: a new connection of ours, or the computer connected again (its API restarted).
        // Either way it holds no session for us, so start one.
        this.lost();
        await this.begin();
      } else if (control.t === "offline") {
        this.lost();
        if (this.state !== "pending") this.set("offline");
      }
      return;
    }
    const frame = new Uint8Array(data);
    if (this.session === null) {
      if (!this.handshake) return;
      const { reply, session } = await this.handshake.finish(frame);
      this.handshake = null;
      this.session = session;
      this.code = session.code;
      this.computerName = String(reply.name || "your computer");
      if (reply.status === "linked") {
        this.phoneId = String(reply.phone_id || "");
        this.set("linked");
        this.flush();
        this.resubscribe();
      } else {
        this.set("pending");
      }
      return;
    }
    const record = JSON.parse(untext.decode(await this.session.decrypt(frame)));
    switch (record.t) {
      case "res": {
        const waiting = this.pending.get(record.id);
        if (!waiting) return;
        this.pending.delete(record.id);
        window.clearTimeout(waiting.timer);
        waiting.resolve({ status: record.status, body: record.body, cache: record.cache });
        return;
      }
      case "ev": {
        const sub = this.subscriptions.get(record.id);
        if (!sub) return;
        if ("run" in sub.topic) sub.topic.after = Math.max(sub.topic.after, Number(record.eid) || 0);
        sub.onEvent(record.data);
        return;
      }
      case "end": {
        const sub = this.subscriptions.get(record.id);
        if (sub) sub.onEnd();
        return;
      }
      case "paired":
        this.phoneId = String(record.phone_id || "");
        this.computerName = String(record.name || this.computerName);
        this.options.secret = new Uint8Array();
        this.set("linked");
        this.flush();
        return;
      case "denied":
        this.set("denied");
        return;
    }
  }

  private async begin() {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    if (this.state !== "pending") this.set("handshake");
    this.settleWithin(this.ws);
    const handshake = new Handshake(this.options.identity, this.options.computerKey, this.options.secret, { name: this.options.phoneName });
    this.handshake = handshake;
    const hello = await handshake.start();
    if (this.handshake === handshake) this.ws?.send(hello); // not if the link changed meanwhile
  }

  private async send(record: Record<string, unknown>) {
    if (!this.session || !this.ws || this.ws.readyState !== WebSocket.OPEN) throw new Error("Not connected");
    const session = this.session;
    // Frames must leave in counter order: encrypt and send under one chain of promises.
    this.sending = this.sending.catch(() => {}).then(async () => {
      const frame = await session.encrypt(text.encode(JSON.stringify(record)));
      if (this.session !== session) throw new Error("Not connected"); // a new session began meanwhile
      this.ws?.send(frame);
    });
    await this.sending;
  }
  private sending: Promise<void> = Promise.resolve();

  private flush() {
    const queued = this.queue;
    this.queue = [];
    for (const go of queued) go();
  }

  /** A request to the computer's API, waiting for the link if it is not up yet. A read waits however long the link
   *  takes to come back and is sent again if the link drops before the answer; a change is sent once. */
  request(method: string, path: string, body?: unknown): Promise<{ status: number; body: unknown; cache?: string }> {
    const read = method === "GET" || method === "HEAD";
    return new Promise((resolve, reject) => {
      const go = () => {
        const id = this.next++;
        const timer = window.setTimeout(() => {
          this.pending.delete(id);
          reject(new Error("Your computer is taking a while to answer. Try again in a moment."));
        }, REQUEST_TIMEOUT);
        this.pending.set(id, { resolve, reject, timer, again: read ? () => go() : undefined });
        this.send({ t: "req", id, method, path, body }).catch(() => {
          const waiting = this.pending.get(id);
          if (!waiting) return; // the link dropped and lost() has already dealt with it
          this.pending.delete(id);
          window.clearTimeout(timer);
          if (read) this.queue.push(go);
          else reject(new Error(WAITING));
        });
      };
      if (this.state === "linked" && this.session) return go();
      if (read) {
        this.queue.push(go);
        return;
      }
      const timer = window.setTimeout(() => {
        this.queue = this.queue.filter((g) => g !== later);
        reject(new Error(WAITING));
      }, QUEUE_TIMEOUT);
      const later = () => {
        window.clearTimeout(timer);
        go();
      };
      this.queue.push(later);
    });
  }

  /** Tell the computer where to ring this phone while the app is in the background (push.ts), or that it should
   *  stop (`null`). Sent once the link is up. */
  sendPush(subscription: Record<string, unknown> | null) {
    const go = () => this.send({ t: "push", subscription }).catch(() => this.queue.push(go));
    if (this.state === "linked" && this.session) void go();
    else this.queue.push(go);
  }

  /** Follow `topic` until the returned function is called; it is followed again whenever the link comes back. */
  subscribe(topic: Topic, onEvent: Subscription["onEvent"], onEnd: () => void) {
    const id = this.next++;
    const sub = { topic, onEvent, onEnd };
    this.subscriptions.set(id, sub);
    if (this.state === "linked" && this.session) this.send(this.subRecord(id, sub)).catch(() => {});
    return () => {
      this.subscriptions.delete(id);
      if (this.session) this.send({ t: "unsub", id }).catch(() => {});
    };
  }

  private resubscribe() {
    for (const [id, sub] of this.subscriptions) this.send(this.subRecord(id, sub)).catch(() => {});
  }

  private subRecord(id: number, sub: Subscription) {
    return { t: "sub", id, ...sub.topic };
  }
}

// What the API says may be kept (a Cache-Control max-age on a GET, passed along by the computer) is kept on this phone,
// as a browser would keep it for the window: a link preview is read once a week, not on every visit.
const KEPT = "relay-api";
const KEPT_UNTIL = "x-kept-until";
let pruned = false;

async function kept(): Promise<Cache | null> {
  if (!("caches" in window)) return null;
  const cache = await caches.open(KEPT);
  if (!pruned) {
    pruned = true; // once per visit: what has gone stale is dropped, so the store does not grow for ever
    for (const key of await cache.keys()) {
      const entry = await cache.match(key);
      if (Number(entry?.headers.get(KEPT_UNTIL)) <= Date.now()) await cache.delete(key);
    }
  }
  return cache;
}

/** Route the app's `/api` traffic through the tunnel. */
export function installShims(tunnel: Tunnel) {
  const realFetch = window.fetch.bind(window);
  window.fetch = async (input, init) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    if (!url.startsWith("/api/")) return realFetch(input, init);
    const path = url.slice(4);
    const screen = /^\/devices\/([^/?]+)\/stream/.exec(path);
    if (screen) return screenStream(tunnel, decodeURIComponent(screen[1]), init?.signal);
    let body: unknown;
    if (init?.body && typeof init.body === "string") {
      try {
        body = JSON.parse(init.body);
      } catch {
        body = init.body;
      }
    }
    try {
      const method = (init?.method || "GET").toUpperCase();
      const store = method === "GET" ? await kept().catch(() => null) : null;
      const hit = await store?.match(url);
      if (hit && Number(hit.headers.get(KEPT_UNTIL)) > Date.now()) return hit;
      const result = await tunnel.request(method, path, body);
      const json = typeof result.body === "string" ? result.body : JSON.stringify(result.body);
      const headers: Record<string, string> = { "Content-Type": "application/json" };
      const age = Number(/max-age=(\d+)/.exec(result.cache || "")?.[1]);
      if (store && age > 0 && (result.status === 200 || result.status === 404)) {
        headers[KEPT_UNTIL] = String(Date.now() + age * 1000);
        store.put(url, new Response(json, { status: result.status, headers })).catch(() => {});
      }
      return new Response(json, { status: result.status, headers });
    } catch (error) {
      const detail = error instanceof Error ? error.message : WAITING;
      return new Response(JSON.stringify({ detail }), { status: 503, headers: { "Content-Type": "application/json" } });
    }
  };

  class TunnelEventSource extends EventTarget {
    static readonly CONNECTING = 0;
    static readonly OPEN = 1;
    static readonly CLOSED = 2;
    readonly url: string;
    readonly withCredentials = false;
    readyState = 0;
    onmessage: ((event: MessageEvent) => void) | null = null;
    onerror: ((event: Event) => void) | null = null;
    onopen: ((event: Event) => void) | null = null;
    private stop: () => void;

    constructor(url: string | URL) {
      super();
      this.url = String(url);
      const match = this.url.match(/^\/api\/runs\/([^/?]+)\/stream(?:\?after=(\d+))?/);
      const updates = this.url === "/api/updates";
      if (!match && !updates) throw new Error(`Unsupported stream ${this.url}`);
      this.readyState = 1;
      this.stop = tunnel.subscribe(
        updates ? { updates: true } : { run: match![1], after: Number(match![2] || 0) },
        (event) => {
          const message = new MessageEvent("message", { data: JSON.stringify(event), lastEventId: String(event.id) });
          this.onmessage?.(message);
          this.dispatchEvent(message);
        },
        () => {
          const error = new Event("error");
          this.onerror?.(error);
          this.dispatchEvent(error);
        },
      );
    }

    close() {
      this.readyState = 2;
      this.stop();
    }
  }
  (window as unknown as { EventSource: unknown }).EventSource = TunnelEventSource;
}

/** The phone's screen as the MJPEG stream the live view reads (lib/useLiveFrames.ts), its frames coming over the
 *  tunnel at the computer's lower rate. Watching only: nothing here acts on the phone. */
function screenStream(tunnel: Tunnel, device: string, signal?: AbortSignal | null) {
  let stop = () => {};
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      const finish = (error?: unknown) => {
        stop();
        try {
          if (error) controller.error(error);
          else controller.close();
        } catch {} // already finished
      };
      stop = tunnel.subscribe(
        { screen: device },
        (frame) => {
          const jpeg = unb64(String(frame.jpeg || ""));
          controller.enqueue(text.encode(`--frame\r\nContent-Type: image/jpeg\r\nContent-Length: ${jpeg.length}\r\n\r\n`));
          controller.enqueue(jpeg);
          controller.enqueue(text.encode("\r\n"));
        },
        () => finish(),
      );
      signal?.addEventListener("abort", () => finish(new DOMException("Stopped watching", "AbortError")));
    },
    cancel() {
      stop();
    },
  });
  return new Response(body, { headers: { "Content-Type": "multipart/x-mixed-replace; boundary=frame" } });
}

export function parsePairing(hash: string) {
  const params = new URLSearchParams(hash.replace(/^#/, ""));
  const mailbox = params.get("m");
  const computerKey = params.get("d");
  const secret = params.get("s");
  if (!mailbox || !computerKey || !secret) return null;
  return {
    version: Number(params.get("v") || 0),
    mailbox,
    computerKey: unb64(computerKey),
    secret: unb64(secret),
    computerName: params.get("n") || "your computer",
    expires: Number(params.get("x") || 0),
  };
}
