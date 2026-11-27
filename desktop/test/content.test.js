// Updates without an installer (src/content.js). Run: npm test (in desktop/).
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const contentStore = require("../src/content.js");

const SHELL = "a".repeat(16);
const OLD = "20260101000000";
const NEW = "20260201000000";
const NEWER = "20260301000000";

function folder(dir, build, shell = SHELL) {
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, "content.json"), JSON.stringify({ build, version: "2026.201.0", shell }));
}

/** A store in a temporary folder, with a release whose feed is `release(build)`. */
function setup({ feed, status = 200, check } = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "penrose-content-"));
  const home = path.join(root, "updates");
  const bundled = path.join(root, "bundled");
  folder(bundled, OLD);
  const zip = Buffer.from("the zip");
  const release = (build, extra = {}) => ({ build, version: "2026.201.0", shell: SHELL, file: "penrose-content.pack",
    size: zip.length, sha512: crypto.createHash("sha512").update(zip).digest("base64"), ...extra });
  const served = { feed: feed || release(NEW), status };
  const store = contentStore({
    home, bundled, feedBase: "https://example.com/releases/",
    fetch: async (url) => url.endsWith(".json")
      ? new Response(JSON.stringify(served.feed), { status: served.status })
      : new Response(zip),
    check: check || (async (dir) => folder(dir, served.feed.build, served.feed.shell)),
  });
  return { root, home, bundled, store, served, release };
}

test("a first start runs the installer's content", () => {
  const { store, bundled, home } = setup();
  const root = store.resolve();
  assert.equal(root.dir, bundled);
  assert.equal(root.trial, false);
  assert.equal(JSON.parse(fs.readFileSync(path.join(home, "active.json"))).dir, bundled);
});

test("a downloaded update is tried at the next start, then kept once confirmed", async () => {
  const { store, home } = setup();
  const running = store.resolve();
  const found = await store.look(running);
  assert.equal(found.kind, "content");
  await store.download(found.feed);
  assert.equal(fs.existsSync(path.join(home, `${NEW}.pack`)), false);
  const trial = store.resolve();
  assert.equal(trial.dir, path.join(home, NEW));
  assert.equal(trial.trial, true);
  store.confirm(trial);
  const after = store.resolve();
  assert.equal(after.build, NEW);
  assert.equal(after.trial, false);
  assert.deepEqual(await store.look(after), { kind: "current" });
});

test("an update that does not start is not tried again, and the installer is the way to that release", async () => {
  const { store, bundled } = setup();
  const running = store.resolve();
  await store.download((await store.look(running)).feed);
  const back = store.reject(store.resolve());
  assert.equal(back.dir, bundled);
  assert.equal(store.resolve().dir, bundled);
  assert.deepEqual(await store.look(back), { kind: "installer" });
});

test("a failed update goes back to the last one that worked", async () => {
  const { store, home, served, release } = setup();
  store.resolve();
  await store.download(served.feed);
  store.confirm(store.resolve());
  served.feed = release(NEWER);
  await store.download(served.feed);
  const back = store.reject(store.resolve());
  assert.equal(back.dir, path.join(home, NEW));
  assert.equal(back.trial, false);
});

test("two starts that ended without a word give the update up; a quit does not count", async () => {
  const { store, bundled, served } = setup();
  store.resolve();
  await store.download(served.feed);
  for (let i = 0; i < 4; i++) {
    assert.equal(store.resolve().build, NEW);
    store.abandon();
  }
  assert.equal(store.resolve().build, NEW);
  assert.equal(store.resolve().build, NEW);
  assert.equal(store.resolve().dir, bundled);
});

test("a release for another shell, or without content, needs the installer", async () => {
  const other = setup();
  other.served.feed = other.release(NEW, { shell: "b".repeat(16) });
  assert.deepEqual(await other.store.look(other.store.resolve()), { kind: "installer" });
  const none = setup({ status: 404 });
  assert.deepEqual(await none.store.look(none.store.resolve()), { kind: "installer" });
  const bad = setup();
  bad.served.feed = bad.release(NEW, { file: "../outside" });
  assert.deepEqual(await bad.store.look(bad.store.resolve()), { kind: "installer" });
});

test("a damaged download is refused and tried again later", async () => {
  const { store, release } = setup();
  const running = store.resolve();
  await assert.rejects(store.download(release(NEW, { sha512: "wrong" })), (error) => !error.unusable);
  assert.equal((await store.look(running)).kind, "content");
});

test("content this copy cannot load is removed and marked", async () => {
  const { store, home } = setup({ check: async () => Promise.reject(new Error("No module named x")) });
  const running = store.resolve();
  await assert.rejects(store.download((await store.look(running)).feed), (error) => error.unusable === true);
  assert.equal(fs.existsSync(path.join(home, NEW)), false);
  assert.deepEqual(await store.look(running), { kind: "installer" });
});

test("a new installer drops the updates made for the old one", async () => {
  const { store, home, bundled, served } = setup();
  store.resolve();
  await store.download(served.feed);
  store.confirm(store.resolve());
  folder(bundled, NEWER, "c".repeat(16));
  assert.equal(store.resolve().dir, bundled);
});

test("updates are fetched over HTTPS only, or from this computer", async () => {
  for (const feedBase of ["", "http://example.com/", "file:///tmp/"]) {
    const store = contentStore({ home: os.tmpdir(), bundled: os.tmpdir(), feedBase, fetch: () => assert.fail("fetched") });
    assert.deepEqual(await store.look({ build: OLD }), { kind: "installer" });
  }
});
