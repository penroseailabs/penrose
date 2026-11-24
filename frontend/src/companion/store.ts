/**
 * What the phone keeps: its own key pair (never exportable; the browser holds it) and the computer it is linked
 * to. In IndexedDB, which an installed app keeps until the user clears its data, after which the phone is simply
 * no longer linked and a new QR code links it again.
 */

import { exportIdentity, importIdentity } from "./crypto";

export type Link = {
  /** The companion app's origin, which is also the coordinator's. */
  origin: string;
  mailbox: string;
  /** The computer's public key, base64url. */
  computerKey: string;
  computerName: string;
  /** The id the computer gave this phone when it was linked. */
  phoneId: string;
  linked: number;
};

const DB = "relay-companion";
const STORE = "keep";

function db(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB, 1);
    request.onupgradeneeded = () => request.result.createObjectStore(STORE);
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

async function get<T>(key: string): Promise<T | undefined> {
  const d = await db();
  return new Promise((resolve, reject) => {
    const request = d.transaction(STORE, "readonly").objectStore(STORE).get(key);
    request.onsuccess = () => resolve(request.result as T | undefined);
    request.onerror = () => reject(request.error);
  });
}

async function put(key: string, value: unknown) {
  const d = await db();
  return new Promise<void>((resolve, reject) => {
    const tx = d.transaction(STORE, "readwrite");
    tx.objectStore(STORE).put(value, key);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

async function remove(key: string) {
  const d = await db();
  return new Promise<void>((resolve, reject) => {
    const tx = d.transaction(STORE, "readwrite");
    tx.objectStore(STORE).delete(key);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

/** The phone's key pair: kept as a JWK (crypto.ts exportIdentity). Phones linked before that kept the CryptoKey
 * objects themselves under "identity", which an iPhone's Home Screen app may fail to read back; those are used while
 * they still read. */
async function identity(): Promise<CryptoKeyPair | undefined> {
  const jwk = await get<JsonWebKey>("identity-jwk").catch(() => undefined);
  if (jwk) return importIdentity(jwk);
  const pair = await get<CryptoKeyPair>("identity").catch(() => undefined);
  return pair?.privateKey && pair?.publicKey ? pair : undefined;
}

async function saveIdentity(pair: CryptoKeyPair) {
  try {
    await put("identity-jwk", await exportIdentity(pair));
  } catch {
    await put("identity", pair); // a key made before keys could be exported
  }
}

export const keep = {
  identity,
  saveIdentity,
  link: () => get<Link>("link"),
  saveLink: (link: Link) => put("link", link),
  forgetLink: () => remove("link"),
};
