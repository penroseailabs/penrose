/**
 * Web Push from the coordinator, on WebCrypto only (the `web-push` package does not run on Workers).
 *
 * A push service takes a request signed with the mailbox's VAPID key (RFC 8292: an ES256 JWT in the Authorization
 * header, the public key beside it, the same key the phone subscribed with) and a body encrypted to the
 * subscription's own key (RFC 8291 and 8188, `aes128gcm`): the service cannot read it either. What Relay puts in
 * that body says only that there is something, never what (index.ts).
 */

export type Subscription = { endpoint: string; keys: { p256dh: string; auth: string } };
/** The mailbox's VAPID key pair, kept as JWKs in the Durable Object's storage. */
export type Vapid = { publicKey: JsonWebKey; privateKey: JsonWebKey };

const JWT_SECONDS = 12 * 3600; // at most 24 h by the RFC
const RECORD_SIZE = 4096;
const text = (s: string) => new TextEncoder().encode(s);
// The Workers types spell ECDH's `public` parameter `$public` and type a generated pair as a union; the runtime is the
// standard one.
type DeriveParams = Parameters<SubtleCrypto["deriveBits"]>[0];
const ecdh = (publicKey: CryptoKey) => ({ name: "ECDH", public: publicKey }) as unknown as DeriveParams;

export function b64url(bytes: Uint8Array): string {
  let binary = "";
  for (const b of bytes) binary += String.fromCharCode(b);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function unb64url(s: string): Uint8Array {
  const binary = atob(s.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (s.length % 4)) % 4));
  return Uint8Array.from(binary, (c) => c.charCodeAt(0));
}

function concat(parts: Uint8Array[]): Uint8Array {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let at = 0;
  for (const part of parts) {
    out.set(part, at);
    at += part.length;
  }
  return out;
}

/** A subscription as a phone sends it (PushSubscription.toJSON()), or null if it is not one. */
export function asSubscription(value: unknown): Subscription | null {
  const sub = value as Partial<Subscription> | null;
  const keys = sub?.keys as Partial<Subscription["keys"]> | undefined;
  if (typeof sub?.endpoint !== "string" || typeof keys?.p256dh !== "string" || typeof keys?.auth !== "string") return null;
  let endpoint: URL;
  try {
    endpoint = new URL(sub.endpoint);
  } catch {
    return null;
  }
  if (endpoint.protocol !== "https:" || sub.endpoint.length > 2048) return null;
  try {
    if (unb64url(keys.p256dh).length !== 65 || unb64url(keys.auth).length < 16) return null;
  } catch {
    return null;
  }
  return { endpoint: sub.endpoint, keys: { p256dh: keys.p256dh, auth: keys.auth } };
}

export async function makeVapid(): Promise<Vapid> {
  const pair = (await crypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, true, ["sign", "verify"])) as CryptoKeyPair;
  return {
    publicKey: (await crypto.subtle.exportKey("jwk", pair.publicKey)) as JsonWebKey,
    privateKey: (await crypto.subtle.exportKey("jwk", pair.privateKey)) as JsonWebKey,
  };
}

/** The key a phone subscribes with (`applicationServerKey`): the public point, uncompressed, base64url. */
export function applicationServerKey(vapid: Vapid): string {
  return b64url(concat([new Uint8Array([4]), unb64url(vapid.publicKey.x!), unb64url(vapid.publicKey.y!)]));
}

/** The Authorization header for one push service (`audience` is its origin); `subject` says who to contact. */
export async function vapidAuthorization(vapid: Vapid, audience: string, subject: string): Promise<string> {
  const header = b64url(text(JSON.stringify({ typ: "JWT", alg: "ES256" })));
  const exp = Math.floor(Date.now() / 1000) + JWT_SECONDS;
  const claims = b64url(text(JSON.stringify({ aud: audience, exp, sub: subject })));
  const key = await crypto.subtle.importKey("jwk", vapid.privateKey, { name: "ECDSA", namedCurve: "P-256" }, false, ["sign"]);
  const signature = await crypto.subtle.sign({ name: "ECDSA", hash: "SHA-256" }, key, text(`${header}.${claims}`));
  return `vapid t=${header}.${claims}.${b64url(new Uint8Array(signature))}, k=${applicationServerKey(vapid)}`;
}

async function hkdf(ikm: Uint8Array, salt: Uint8Array, info: Uint8Array, bytes: number): Promise<Uint8Array> {
  const key = await crypto.subtle.importKey("raw", ikm, "HKDF", false, ["deriveBits"]);
  return new Uint8Array(await crypto.subtle.deriveBits({ name: "HKDF", hash: "SHA-256", salt, info }, key, bytes * 8));
}

/** `plaintext` as one aes128gcm record for the subscription's key (RFC 8291 §3, RFC 8188 §2). */
export async function encrypt(subscription: Subscription, plaintext: Uint8Array): Promise<Uint8Array> {
  const phoneKey = unb64url(subscription.keys.p256dh);
  const auth = unb64url(subscription.keys.auth);
  const local = (await crypto.subtle.generateKey({ name: "ECDH", namedCurve: "P-256" }, true, ["deriveBits"])) as CryptoKeyPair;
  const localKey = new Uint8Array((await crypto.subtle.exportKey("raw", local.publicKey)) as ArrayBuffer);
  const remote = await crypto.subtle.importKey("raw", phoneKey, { name: "ECDH", namedCurve: "P-256" }, false, []);
  const shared = new Uint8Array(await crypto.subtle.deriveBits(ecdh(remote), local.privateKey, 256));
  const ikm = await hkdf(shared, auth, concat([text("WebPush: info\0"), phoneKey, localKey]), 32);
  const salt = crypto.getRandomValues(new Uint8Array(16));
  const cek = await hkdf(ikm, salt, text("Content-Encoding: aes128gcm\0"), 16);
  const nonce = await hkdf(ikm, salt, text("Content-Encoding: nonce\0"), 12);
  const key = await crypto.subtle.importKey("raw", cek, "AES-GCM", false, ["encrypt"]);
  const record = concat([plaintext, new Uint8Array([2])]); // 2: the last record's padding delimiter
  const ciphertext = new Uint8Array(await crypto.subtle.encrypt({ name: "AES-GCM", iv: nonce }, key, record));
  const header = new Uint8Array(16 + 4 + 1 + localKey.length);
  header.set(salt, 0);
  new DataView(header.buffer).setUint32(16, RECORD_SIZE);
  header[20] = localKey.length;
  header.set(localKey, 21);
  return concat([header, ciphertext]);
}

export type Delivery = { status: number; gone: boolean };

/**
 * Send `payload` to one subscription. `gone` means the subscription no longer exists (the user revoked it or
 * reinstalled the app): whoever holds it should forget it. `ttl` seconds: a phone that is off the network for
 * longer than that is not told at all, rather than rung about something old; `topic` makes a newer push replace
 * one still waiting.
 */
export async function send(
  vapid: Vapid,
  subscription: Subscription,
  payload: unknown,
  subject: string,
  options: { ttl?: number; topic?: string } = {},
): Promise<Delivery> {
  return deliver(vapid, subscription, await encrypt(subscription, text(JSON.stringify(payload))), subject, options);
}

/** The largest body a push service takes. */
export const MAX_BODY = RECORD_SIZE;

/**
 * Send a body already encrypted for the subscription (by the computer: backend/app/companion/webpush.py), so this
 * worker carries what a notification says without being able to read it.
 */
export async function deliver(
  vapid: Vapid,
  subscription: Subscription,
  body: Uint8Array,
  subject: string,
  { ttl = 600, topic = "" } = {},
): Promise<Delivery> {
  const headers: Record<string, string> = {
    Authorization: await vapidAuthorization(vapid, new URL(subscription.endpoint).origin, subject),
    "Content-Encoding": "aes128gcm",
    "Content-Type": "application/octet-stream",
    TTL: String(ttl),
    Urgency: "high",
  };
  if (topic) headers.Topic = topic;
  const response = await fetch(subscription.endpoint, { method: "POST", headers, body });
  await response.body?.cancel();
  return { status: response.status, gone: response.status === 404 || response.status === 410 };
}
