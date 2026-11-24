/**
 * The push module on Node's WebCrypto (the same API Workers give it): `npm test`. The phone's side of RFC 8291 is
 * played here to open what `encrypt` made, and a local server stands in for the push service.
 */

import assert from "node:assert/strict";
import { createServer } from "node:http";
import { test } from "node:test";
import {
  applicationServerKey,
  asSubscription,
  b64url,
  deliver,
  encrypt,
  makeVapid,
  send,
  type Subscription,
  unb64url,
  vapidAuthorization,
} from "../src/push.ts";

const text = (s: string) => new TextEncoder().encode(s);

/** A phone's subscription keys, as the browser makes them, with the private half kept to decrypt. */
async function phone() {
  const pair = await crypto.subtle.generateKey({ name: "ECDH", namedCurve: "P-256" }, true, ["deriveBits"]);
  const auth = crypto.getRandomValues(new Uint8Array(16));
  const subscription: Subscription = {
    endpoint: "https://push.example/send/abc",
    keys: { p256dh: b64url(new Uint8Array(await crypto.subtle.exportKey("raw", pair.publicKey))), auth: b64url(auth) },
  };
  return { pair, auth, subscription };
}

async function hkdf(ikm: Uint8Array, salt: Uint8Array, info: Uint8Array, bytes: number) {
  const key = await crypto.subtle.importKey("raw", ikm, "HKDF", false, ["deriveBits"]);
  return new Uint8Array(await crypto.subtle.deriveBits({ name: "HKDF", hash: "SHA-256", salt, info }, key, bytes * 8));
}

/** RFC 8291 from the receiving side. */
async function decrypt(me: Awaited<ReturnType<typeof phone>>, body: Uint8Array) {
  const salt = body.slice(0, 16);
  const rs = new DataView(body.buffer, body.byteOffset).getUint32(16);
  const idlen = body[20];
  const serverKey = body.slice(21, 21 + idlen);
  const ciphertext = body.slice(21 + idlen);
  assert.equal(rs, 4096);
  assert.equal(idlen, 65);
  const remote = await crypto.subtle.importKey("raw", serverKey, { name: "ECDH", namedCurve: "P-256" }, false, []);
  const shared = new Uint8Array(await crypto.subtle.deriveBits({ name: "ECDH", public: remote }, me.pair.privateKey, 256));
  const myKey = new Uint8Array(await crypto.subtle.exportKey("raw", me.pair.publicKey));
  const info = new Uint8Array([...text("WebPush: info\0"), ...myKey, ...serverKey]);
  const ikm = await hkdf(shared, me.auth, info, 32);
  const cek = await hkdf(ikm, salt, text("Content-Encoding: aes128gcm\0"), 16);
  const nonce = await hkdf(ikm, salt, text("Content-Encoding: nonce\0"), 12);
  const key = await crypto.subtle.importKey("raw", cek, "AES-GCM", false, ["decrypt"]);
  const record = new Uint8Array(await crypto.subtle.decrypt({ name: "AES-GCM", iv: nonce }, key, ciphertext));
  assert.equal(record.at(-1), 2);
  return new TextDecoder().decode(record.slice(0, -1));
}

test("the phone can open what the coordinator encrypts, and nobody else can", async () => {
  const me = await phone();
  const body = await encrypt(me.subscription, text('{"t":"ring"}'));
  assert.equal(await decrypt(me, body), '{"t":"ring"}');
  const other = await phone();
  await assert.rejects(decrypt({ ...other, auth: me.auth }, body));
  // Fresh keys and salt each time: two pushes of the same thing never look alike.
  assert.notDeepEqual(await encrypt(me.subscription, text('{"t":"ring"}')), body);
});

test("the VAPID header carries a JWT the public key verifies, for the push service's origin", async () => {
  const vapid = await makeVapid();
  const header = await vapidAuthorization(vapid, "https://push.example", "https://relay.example");
  const match = header.match(/^vapid t=([^,]+), k=([^,]+)$/);
  assert.ok(match);
  const [, jwt, k] = match;
  assert.equal(k, applicationServerKey(vapid));
  const point = unb64url(k);
  assert.equal(point.length, 65);
  assert.equal(point[0], 4);
  const [head, claims, signature] = jwt.split(".");
  assert.deepEqual(JSON.parse(new TextDecoder().decode(unb64url(head))), { typ: "JWT", alg: "ES256" });
  const body = JSON.parse(new TextDecoder().decode(unb64url(claims)));
  assert.equal(body.aud, "https://push.example");
  assert.equal(body.sub, "https://relay.example");
  assert.ok(body.exp > Date.now() / 1000 + 3600 && body.exp <= Date.now() / 1000 + 24 * 3600);
  const key = await crypto.subtle.importKey("raw", point, { name: "ECDSA", namedCurve: "P-256" }, false, ["verify"]);
  const ok = await crypto.subtle.verify({ name: "ECDSA", hash: "SHA-256" }, key, unb64url(signature), text(`${head}.${claims}`));
  assert.equal(ok, true);
  const bad = await crypto.subtle.verify({ name: "ECDSA", hash: "SHA-256" }, key, unb64url(signature), text(`${head}.x`));
  assert.equal(bad, false);
});

test("only a subscription with an https endpoint and both keys is one", async () => {
  const me = await phone();
  assert.deepEqual(asSubscription(me.subscription), me.subscription);
  assert.deepEqual(asSubscription({ ...me.subscription, expirationTime: null }), me.subscription);
  assert.equal(asSubscription(null), null);
  assert.equal(asSubscription({ endpoint: "http://push.example/x", keys: me.subscription.keys }), null);
  assert.equal(asSubscription({ endpoint: me.subscription.endpoint, keys: { p256dh: "AAAA", auth: me.subscription.keys.auth } }), null);
  assert.equal(asSubscription({ endpoint: me.subscription.endpoint, keys: { p256dh: me.subscription.keys.p256dh } }), null);
  assert.equal(asSubscription({ endpoint: "nonsense", keys: me.subscription.keys }), null);
});

test("send posts an encrypted body with the push headers, and reports a subscription that is gone", async () => {
  const me = await phone();
  const seen: { headers: Record<string, string | string[] | undefined>; body: Uint8Array }[] = [];
  let status = 201;
  const server = createServer((request, response) => {
    const chunks: Buffer[] = [];
    request.on("data", (chunk) => chunks.push(chunk));
    request.on("end", () => {
      seen.push({ headers: request.headers, body: new Uint8Array(Buffer.concat(chunks)) });
      response.writeHead(status).end();
    });
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const port = (server.address() as { port: number }).port;
  const subscription = { ...me.subscription, endpoint: `http://127.0.0.1:${port}/send/abc` };
  const vapid = await makeVapid();
  try {
    const first = await send(vapid, subscription, { t: "ring" }, "https://relay.example", { topic: "ring" });
    assert.deepEqual(first, { status: 201, gone: false });
    const { headers, body } = seen[0];
    assert.equal(headers["content-encoding"], "aes128gcm");
    assert.equal(headers["content-type"], "application/octet-stream");
    assert.equal(headers.ttl, "600");
    assert.equal(headers.urgency, "high");
    assert.equal(headers.topic, "ring");
    assert.match(String(headers.authorization), /^vapid t=.+, k=.+$/);
    assert.equal(await decrypt(me, body), '{"t":"ring"}');
    status = 410;
    assert.deepEqual(await send(vapid, subscription, { t: "ring" }, "https://relay.example"), { status: 410, gone: true });
    status = 404;
    assert.deepEqual(await send(vapid, subscription, { t: "ring" }, "https://relay.example"), { status: 404, gone: true });
    status = 400;
    assert.deepEqual(await send(vapid, subscription, { t: "ring" }, "https://relay.example"), { status: 400, gone: false });
  } finally {
    server.close();
  }
});

test("deliver posts a body the computer encrypted as it is, under the topic it asked for", async () => {
  const me = await phone();
  let got: { body: Uint8Array; topic?: string } | null = null;
  const server = createServer((request, response) => {
    const chunks: Buffer[] = [];
    request.on("data", (chunk) => chunks.push(chunk));
    request.on("end", () => {
      got = { body: new Uint8Array(Buffer.concat(chunks)), topic: request.headers.topic as string | undefined };
      response.writeHead(201).end();
    });
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const port = (server.address() as { port: number }).port;
  const subscription = { ...me.subscription, endpoint: `http://127.0.0.1:${port}/send/abc` };
  try {
    const sealed = await encrypt(subscription, text('{"t":"ring","body":"Approve the sign-in"}'));
    const result = await deliver(await makeVapid(), subscription, sealed, "https://relay.example", { topic: "c0ffee" });
    assert.deepEqual(result, { status: 201, gone: false });
    assert.deepEqual(got!.body, sealed);
    assert.equal(got!.topic, "c0ffee");
    assert.equal(await decrypt(me, got!.body), '{"t":"ring","body":"Approve the sign-in"}');
  } finally {
    server.close();
  }
});
