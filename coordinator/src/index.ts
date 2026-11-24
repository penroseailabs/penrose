/**
 * The companion coordinator: a Cloudflare Worker that serves the companion app (frontend/dist-companion) and
 * passes frames between a computer and its phones. It reads none of them: every frame after the first is
 * ciphertext between the two devices (backend/app/companion/crypto.py), and the first is useless without the
 * secret from the QR code.
 *
 * Each phone connects to /ws/<mailbox>?role=phone; the computer to /ws/<owner>?role=desktop, where the mailbox
 * is the SHA-256 of the owner secret. Phones and this worker know the mailbox; only the computer knows the
 * secret, so nothing else can attach as the computer's end. One Durable Object per mailbox:
 *
 *   phone → computer   binary frames, forwarded with the phone's 8-byte peer id in front
 *   computer → phone   binary frames with that peer id in front, delivered to that phone without it
 *   computer           text {"t":"open","peer"} / {"t":"close","peer"} as phones come and go,
 *                      and may send {"t":"close","peer"} to drop one
 *   phone              text {"t":"online"} / {"t":"offline"} as the computer comes and goes;
 *                      "ping" is answered "pong" without waking the object
 *
 * Ringing a phone whose app is in the background (push.ts): each mailbox has a VAPID key pair, made here and kept
 * in the object's storage; a phone is told the public key on connect, text {"t":"push_key","key"}, subscribes with
 * it and sends the subscription to the computer inside the ciphertext. When a task has something for the user the
 * computer sends text {"t":"push","phone_id","subscription","body","topic"}: `body` is the notification, encrypted
 * by the computer for that subscription, which this object posts as it is (it cannot read it); without one it sends
 * a body that says only {"t":"ring"}. A subscription the service reports gone (404, 410) is reported back as
 * {"t":"push_gone","phone_id"}, and the computer forgets it.
 */

import { DurableObject } from "cloudflare:workers";
import { applicationServerKey, asSubscription, deliver, makeVapid, MAX_BODY, send, unb64url, type Vapid } from "./push";

const TOPIC = /^[A-Za-z0-9_-]{1,32}$/;

export interface Env {
  MAILBOX: DurableObjectNamespace<Mailbox>;
  ASSETS: Fetcher;
}

const MAILBOX = /^[A-Za-z0-9_-]{32,64}$/;
const PEER_BYTES = 8;
const MAX_PHONES = 16;
const MAX_FRAME = 4 * 1024 * 1024;

// `since`: when a computer attached. A replaced computer connection is closed but can stay listed until Cloudflare
// sees it go (a minute or two for one whose process was killed), so frames go to the newest open one.
type Attachment = { role: "desktop" | "phone"; peer: string; since?: number };
const OPEN = 1; // WebSocket.READY_STATE_OPEN

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const match = url.pathname.match(/^\/ws\/([^/]+)$/);
    if (match) {
      if (request.headers.get("Upgrade") !== "websocket") return new Response("Expected a WebSocket", { status: 426 });
      const name = match[1];
      const role = url.searchParams.get("role");
      if (!MAILBOX.test(name) || (role !== "desktop" && role !== "phone")) {
        return new Response("Bad request", { status: 400 });
      }
      const mailbox = role === "desktop" ? await mailboxOf(name) : name;
      return env.MAILBOX.get(env.MAILBOX.idFromName(mailbox)).fetch(request);
    }
    if (url.pathname === "/health") return Response.json({ ok: true });
    const response = await env.ASSETS.fetch(request);
    // A built file that is not here (an earlier deploy's, asked for by a page the phone kept) is not found: the
    // single-page fallback would answer with the app's page, which a browser drops as a stylesheet without a word,
    // and which a service worker installing would keep as that file.
    if (url.pathname.startsWith("/assets/") && (response.headers.get("Content-Type") || "").startsWith("text/html"))
      return new Response("Not found", { status: 404 });
    return response;
  },
};

/** The mailbox an owner secret opens: its SHA-256, base64url without padding (as backend/app/companion/identity.py). */
async function mailboxOf(owner: string) {
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(owner)));
  return btoa(String.fromCharCode(...digest)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function hex(bytes: Uint8Array) {
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

function unhex(text: string) {
  const out = new Uint8Array(text.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(text.slice(i * 2, i * 2 + 2), 16);
  return out;
}

function tell(ws: WebSocket, record: unknown) {
  try {
    ws.send(JSON.stringify(record));
  } catch {}
}

export class Mailbox extends DurableObject<Env> {
  private vapidKeys: Vapid | null = null;
  /** This worker's own address, the contact a push service is given (VAPID's `sub`). */
  private origin = "";

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    ctx.setWebSocketAutoResponse(new WebSocketRequestResponsePair("ping", "pong"));
  }

  /** The mailbox's VAPID keys: made the first time a phone connects, kept for the mailbox's life. */
  async vapid(): Promise<Vapid> {
    if (!this.vapidKeys) {
      this.vapidKeys = (await this.ctx.storage.get<Vapid>("vapid")) ?? null;
      if (!this.vapidKeys) {
        this.vapidKeys = await makeVapid();
        await this.ctx.storage.put("vapid", this.vapidKeys);
      }
    }
    return this.vapidKeys;
  }

  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    const role = url.searchParams.get("role") as Attachment["role"];
    await this.learnOrigin(url.origin);
    const pair = new WebSocketPair();
    const [client, server] = [pair[0], pair[1]];
    if (role === "desktop") {
      for (const old of this.ctx.getWebSockets("desktop")) old.close(1000, "replaced");
      this.ctx.acceptWebSocket(server, ["desktop"]);
      server.serializeAttachment({ role, peer: "", since: Date.now() } satisfies Attachment);
      for (const phone of this.ctx.getWebSockets("phone")) {
        const { peer } = phone.deserializeAttachment() as Attachment;
        tell(server, { t: "open", peer });
        tell(phone, { t: "online" });
      }
    } else {
      if (this.ctx.getWebSockets("phone").length >= MAX_PHONES) return new Response("Too many phones", { status: 429 });
      const peer = hex(crypto.getRandomValues(new Uint8Array(PEER_BYTES)));
      this.ctx.acceptWebSocket(server, ["phone", `phone:${peer}`]);
      server.serializeAttachment({ role, peer } satisfies Attachment);
      tell(server, { t: "push_key", key: applicationServerKey(await this.vapid()) });
      const desktop = this.desktop();
      if (desktop) {
        tell(desktop, { t: "open", peer });
        tell(server, { t: "online" });
      } else {
        tell(server, { t: "offline" });
      }
    }
    return new Response(null, { status: 101, webSocket: client });
  }

  /**
   * This worker's address for VAPID's `sub`. It is learned from a connection, and the object forgets it whenever it
   * hibernates between messages, so it is kept in storage too: a push signed with an empty `sub` can be refused.
   */
  async subject(): Promise<string> {
    if (!this.origin) this.origin = (await this.ctx.storage.get<string>("origin")) ?? "";
    return this.origin;
  }

  async learnOrigin(origin: string) {
    if (origin === (await this.subject())) return;
    this.origin = origin;
    await this.ctx.storage.put("origin", origin);
  }

  /** Ring one phone through its push service, on the computer's word; tell the computer if the subscription is gone. */
  async push(desktop: WebSocket, control: { phone_id?: unknown; subscription?: unknown; body?: unknown; topic?: unknown }) {
    const subscription = asSubscription(control.subscription);
    const phoneId = typeof control.phone_id === "string" ? control.phone_id : "";
    if (!subscription || !phoneId) return;
    const topic = typeof control.topic === "string" && TOPIC.test(control.topic) ? control.topic : "ring";
    let body: Uint8Array | null = null;
    if (typeof control.body === "string") {
      try {
        body = unb64url(control.body);
      } catch {}
      if (!body || body.length > MAX_BODY) return;
    }
    try {
      const vapid = await this.vapid();
      const subject = await this.subject();
      const { status, gone } = body
        ? await deliver(vapid, subscription, body, subject, { topic })
        : await send(vapid, subscription, { t: "ring" }, subject, { topic });
      console.log(`push to ${phoneId}: ${status}`);
      if (gone) tell(desktop, { t: "push_gone", peer: "", phone_id: phoneId });
    } catch (error) {
      console.log(`push to ${phoneId} failed: ${error}`);
    }
  }

  /** The computer's live connection: the newest open one, never one being replaced or gone. */
  desktop(): WebSocket | undefined {
    let best: WebSocket | undefined;
    let newest = -1;
    for (const ws of this.ctx.getWebSockets("desktop")) {
      if (ws.readyState !== OPEN) continue;
      const since = (ws.deserializeAttachment() as Attachment).since ?? 0;
      if (since > newest) [best, newest] = [ws, since];
    }
    return best;
  }

  phone(peer: string): WebSocket | undefined {
    return this.ctx.getWebSockets(`phone:${peer}`)[0];
  }

  async webSocketMessage(ws: WebSocket, message: string | ArrayBuffer) {
    const { role, peer } = ws.deserializeAttachment() as Attachment;
    if (role === "desktop") {
      if (typeof message === "string") {
        try {
          const control = JSON.parse(message);
          if (control?.t === "close" && typeof control.peer === "string") {
            this.phone(control.peer)?.close(1000, "closed by the computer");
          } else if (control?.t === "push") {
            await this.push(ws, control);
          }
        } catch {}
        return;
      }
      if (message.byteLength <= PEER_BYTES || message.byteLength > MAX_FRAME) return;
      const target = this.phone(hex(new Uint8Array(message, 0, PEER_BYTES)));
      try {
        target?.send(message.slice(PEER_BYTES));
      } catch {}
      return;
    }
    if (typeof message === "string" || message.byteLength > MAX_FRAME) return; // "ping" is answered for us
    const desktop = this.desktop();
    if (!desktop) {
      tell(ws, { t: "offline" });
      return;
    }
    const frame = new Uint8Array(PEER_BYTES + message.byteLength);
    frame.set(unhex(peer), 0);
    frame.set(new Uint8Array(message), PEER_BYTES);
    try {
      desktop.send(frame);
    } catch {}
  }

  async webSocketClose(ws: WebSocket) {
    this.gone(ws);
  }

  async webSocketError(ws: WebSocket) {
    this.gone(ws);
  }

  gone(ws: WebSocket) {
    const { role, peer } = ws.deserializeAttachment() as Attachment;
    if (role === "phone") {
      const desktop = this.desktop();
      if (desktop) tell(desktop, { t: "close", peer });
    } else if (!this.desktop() || this.desktop() === ws) {
      for (const phone of this.ctx.getWebSockets("phone")) tell(phone, { t: "offline" });
    }
  }
}
