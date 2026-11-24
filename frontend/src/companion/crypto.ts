/**
 * The phone's side of the companion handshake and frames, in WebCrypto. The computer's side, and the protocol
 * itself, are in backend/app/companion/crypto.py; the two must stay in step.
 */

export const VERSION = 1;
const PAIR_HELLO = 1;
const SESSION_HELLO = 2;
const HELLO_OK = 3;
const ERROR = 4;
const DATA = 16;
const NONCE_ZERO = new Uint8Array(12);

export class HandshakeError extends Error {}

const text = new TextEncoder();
const untext = new TextDecoder();

export function b64(bytes: Uint8Array) {
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function unb64(value: string) {
  const padded = value.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (value.length % 4)) % 4);
  return Uint8Array.from(atob(padded), (c) => c.charCodeAt(0));
}

function concat(...parts: Uint8Array[]) {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let at = 0;
  for (const part of parts) {
    out.set(part, at);
    at += part.length;
  }
  return out;
}

/** The phone's key pair. Extractable, so it can be kept as plain data (exportIdentity): an iPhone's Home Screen app
 * could not read back a CryptoKey object it had kept in IndexedDB after the app reloaded, made a new key, and the
 * computer no longer knew the phone (2026-11-02). */
export async function generateKey() {
  return (await crypto.subtle.generateKey({ name: "X25519" }, true, ["deriveBits"])) as CryptoKeyPair;
}

/** The key pair as plain data to keep (a JWK: the private key with its public half). */
export async function exportIdentity(pair: CryptoKeyPair) {
  return crypto.subtle.exportKey("jwk", pair.privateKey);
}

export async function importIdentity(jwk: JsonWebKey): Promise<CryptoKeyPair> {
  const privateKey = await crypto.subtle.importKey("jwk", jwk, { name: "X25519" }, true, ["deriveBits"]);
  const publicKey = await crypto.subtle.importKey("jwk", { kty: jwk.kty, crv: jwk.crv, x: jwk.x }, { name: "X25519" }, true, []);
  return { privateKey, publicKey };
}

export async function publicBytes(pair: CryptoKeyPair) {
  return new Uint8Array(await crypto.subtle.exportKey("raw", pair.publicKey));
}

async function shared(privateKey: CryptoKey, publicRaw: Uint8Array) {
  const pub = await crypto.subtle.importKey("raw", publicRaw as BufferSource, { name: "X25519" }, false, []);
  return new Uint8Array(await crypto.subtle.deriveBits({ name: "X25519", public: pub }, privateKey, 256));
}

async function hkdf(ikm: Uint8Array, salt: Uint8Array, info: Uint8Array, bits = 256) {
  const key = await crypto.subtle.importKey("raw", ikm as BufferSource, "HKDF", false, ["deriveBits"]);
  return new Uint8Array(
    await crypto.subtle.deriveBits({ name: "HKDF", hash: "SHA-256", salt: salt as BufferSource, info: info as BufferSource }, key, bits),
  );
}

async function aesKey(raw: Uint8Array) {
  return crypto.subtle.importKey("raw", raw as BufferSource, "AES-GCM", false, ["encrypt", "decrypt"]);
}

async function seal(raw: Uint8Array, nonce: Uint8Array, data: Uint8Array, ad: Uint8Array) {
  const key = await aesKey(raw);
  return new Uint8Array(
    await crypto.subtle.encrypt({ name: "AES-GCM", iv: nonce as BufferSource, additionalData: ad as BufferSource }, key, data as BufferSource),
  );
}

async function open(raw: Uint8Array, nonce: Uint8Array, data: Uint8Array, ad: Uint8Array) {
  const key = await aesKey(raw);
  try {
    return new Uint8Array(
      await crypto.subtle.decrypt({ name: "AES-GCM", iv: nonce as BufferSource, additionalData: ad as BufferSource }, key, data as BufferSource),
    );
  } catch {
    throw new HandshakeError("bad ciphertext");
  }
}

async function sha256(data: Uint8Array) {
  return new Uint8Array(await crypto.subtle.digest("SHA-256", data as BufferSource));
}

function counterBytes(n: number) {
  const out = new Uint8Array(8);
  new DataView(out.buffer).setBigUint64(0, BigInt(n));
  return out;
}

/** The keys of one connection after the handshake: a key and a counter per direction. */
export class Session {
  private sent = 0;
  private received = 0;
  /** Shown on both screens while the user approves this phone on the computer. */
  readonly code: string;

  private constructor(
    private sendKey: Uint8Array,
    private recvKey: Uint8Array,
    code: string,
  ) {
    this.code = code;
  }

  static async from(root: Uint8Array) {
    const toPhone = await hkdf(root, new Uint8Array(), text.encode("relay-companion-d2p"));
    const toComputer = await hkdf(root, new Uint8Array(), text.encode("relay-companion-p2d"));
    const sas = await hkdf(root, new Uint8Array(), text.encode("relay-companion-sas"), 32);
    const code = String(new DataView(sas.buffer).getUint32(0) % 1_000_000).padStart(6, "0");
    return new Session(toComputer, toPhone, code);
  }

  async encrypt(payload: Uint8Array) {
    this.sent += 1;
    const counter = counterBytes(this.sent);
    const nonce = concat(new Uint8Array(4), counter);
    return concat(new Uint8Array([DATA]), counter, await seal(this.sendKey, nonce, payload, new Uint8Array()));
  }

  async decrypt(frame: Uint8Array) {
    if (frame.length < 9 || frame[0] !== DATA) throw new HandshakeError("not a data frame");
    const counter = Number(new DataView(frame.buffer, frame.byteOffset + 1, 8).getBigUint64(0));
    if (counter <= this.received) throw new HandshakeError("replayed frame");
    const payload = await open(this.recvKey, concat(new Uint8Array(4), frame.slice(1, 9)), frame.slice(9), new Uint8Array());
    this.received = counter;
    return payload;
  }
}

/** One handshake, from the hello to the keys. */
export class Handshake {
  private hello = new Uint8Array();
  private ephemeral: CryptoKeyPair | null = null;

  constructor(
    private identity: CryptoKeyPair,
    private computerKey: Uint8Array,
    private secret: Uint8Array,
    private info: Record<string, unknown>,
  ) {}

  /** The first message: a pairing hello with the QR code's secret, or a session hello without one. */
  async start() {
    this.ephemeral = await generateKey();
    const ePub = await publicBytes(this.ephemeral);
    const pairing = this.secret.length > 0;
    const head = pairing ? concat(new Uint8Array([PAIR_HELLO]), (await sha256(this.secret)).slice(0, 8)) : new Uint8Array([SESSION_HELLO]);
    const plain = concat(await publicBytes(this.identity), text.encode(JSON.stringify({ ...this.info, v: VERSION })));
    const k1 = await hkdf(await shared(this.ephemeral.privateKey, this.computerKey), this.secret, text.encode("relay-companion-hello-v1"));
    this.hello = concat(head, ePub, await seal(k1, NONCE_ZERO, plain, concat(head, ePub)));
    return this.hello;
  }

  /** The computer's answer: its reply and this connection's keys. */
  async finish(frame: Uint8Array): Promise<{ reply: Record<string, unknown>; session: Session }> {
    if (frame.length && frame[0] === ERROR) {
      let message = "declined";
      try {
        message = JSON.parse(untext.decode(frame.slice(1))).error || message;
      } catch {}
      throw new HandshakeError(message);
    }
    if (frame.length < 1 + 32 + 16 || frame[0] !== HELLO_OK || !this.ephemeral) throw new HandshakeError("unexpected reply");
    const eD = frame.slice(1, 33);
    const ss1 = await shared(this.ephemeral.privateKey, this.computerKey);
    const ss2 = await shared(this.identity.privateKey, eD);
    const ss3 = await shared(this.ephemeral.privateKey, eD);
    const transcript = await sha256(concat(this.hello, eD));
    const root = await hkdf(concat(ss1, ss2, ss3), this.secret, concat(text.encode("relay-companion-session-v1"), transcript));
    const reply = JSON.parse(untext.decode(await open(root, NONCE_ZERO, frame.slice(33), concat(text.encode("hello-ok"), eD))));
    return { reply, session: await Session.from(root) };
  }
}
