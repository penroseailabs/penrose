// Updates without an installer. penrose's own code, web apps and phone files are "content", a folder the installer
// ships as resources/content (scripts/content.js says what is in it). A release also publishes that folder as one
// zip with a small feed, content-latest.json. An installed copy takes it when the release's shell ID is its own,
// that is, when nothing in the release needs a new installer (scripts/shell-id.js); otherwise main.js goes to the
// installer, as before.
//
// Downloaded content lives in <data>/updates/<build>. Its first starts are a trial: main.js confirms it once the
// services run on it, and it goes back to the last good content (or the installer's) when they do not, or when two
// starts in a row ended before saying either. state.json keeps that: the build to try (`next`), the one that last
// worked (`good`), the starts tried, and the builds that failed (`bad`). active.json names the folder in use, for
// the programs that start relay-server without this app (backend/relay_server.py).
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const FEED = "content-latest.json";
const TRIES = 2;
const MAX_BYTES = 500 * 1024 * 1024;

function readJson(file) {
  try {
    const value = JSON.parse(fs.readFileSync(file, "utf8"));
    return value && typeof value === "object" ? value : null;
  } catch {
    return null;
  }
}

/** A content folder as { dir, build, version, shell, companion }, or null when there is none there. */
function readContent(dir) {
  const info = readJson(path.join(dir, "content.json"));
  return info && /^\d{14}$/.test(info.build) ? { ...info, dir } : null;
}

/** Updates come over HTTPS; plain HTTP only from this computer, to try an update out. */
function safeBase(base) {
  try {
    const url = new URL(base);
    return url.protocol === "https:" || (url.protocol === "http:" && ["127.0.0.1", "localhost"].includes(url.hostname));
  } catch {
    return false;
  }
}

function validFeed(feed) {
  return Boolean(feed) && /^\d{14}$/.test(feed.build) && /^\d+\.\d+\.\d+$/.test(feed.version) &&
    /^[0-9a-f]{16}$/.test(feed.shell) && /^[\w][\w.-]*$/.test(feed.file) && typeof feed.sha512 === "string" &&
    Number.isInteger(feed.size) && feed.size > 0 && feed.size <= MAX_BYTES;
}

/**
 * `home` is <data>/updates, `bundled` the installer's content folder, `feedBase` the URL the release's files are
 * under ("" for none). `fetch` is the app's; `check(dir, zip)` unpacks the zip into dir and rejects when this
 * installed copy cannot run what is in it.
 */
module.exports = function contentStore({ home, bundled, feedBase, fetch, check }) {
  const stateFile = path.join(home, "state.json");
  const shipped = () => {
    const found = readContent(bundled);
    if (!found) throw new Error("This copy of penrose is incomplete. Download it again and reinstall it.");
    return found;
  };

  function state() {
    const saved = readJson(stateFile) || {};
    return { next: saved.next || null, good: saved.good || null, tries: saved.tries || 0,
      bad: Array.isArray(saved.bad) ? saved.bad : [] };
  }

  function write(file, value) {
    fs.mkdirSync(home, { recursive: true });
    fs.writeFileSync(`${file}.new`, JSON.stringify(value));
    fs.renameSync(`${file}.new`, file); // whole or not at all
  }

  /** Downloaded content that this installed copy may run: made for its shell, newer than the installer's own. */
  function usable(build, saved) {
    if (!build || saved.bad.includes(build)) return null;
    const found = readContent(path.join(home, build));
    const base = shipped();
    return found && found.build === build && found.shell === base.shell && build > base.build ? found : null;
  }

  /** Marks `root` as the content in use, counting a start of content that has not worked yet. */
  function use(root) {
    const saved = state();
    const trial = root.dir !== bundled && root.build !== saved.good;
    if (trial) write(stateFile, { ...saved, next: root.build, tries: (saved.next === root.build ? saved.tries : 0) + 1 });
    write(path.join(home, "active.json"), { dir: root.dir });
    return { ...root, trial };
  }

  /** Downloads nothing refers to any more (older updates, half-finished ones, all of them after a new installer). */
  function clean(keep) {
    let names = [];
    try {
      names = fs.readdirSync(home);
    } catch {
      return;
    }
    const folders = [
      ...names.filter((name) => /^\d{14}(\.pack)?$/.test(name) && !keep.has(name)),
      ...(names.includes("check-data") ? ["check-data"] : []),
    ].map((name) => path.join(home, name));
    try {
      const cache = path.join(home, "pycache");
      folders.push(...fs.readdirSync(cache).filter((name) => !keep.has(name)).map((name) => path.join(cache, name)));
    } catch {}
    for (const folder of folders) {
      try {
        fs.rmSync(folder, { recursive: true, force: true });
      } catch {} // held open: the next start removes it
    }
  }

  /** The content this start runs: the update being tried, else the last good one, else the installer's. */
  function resolve() {
    let saved = state();
    if (saved.next && saved.tries >= TRIES) {
      saved = { ...saved, bad: [...saved.bad, saved.next], next: null, tries: 0 };
      write(stateFile, saved);
    }
    const found = usable(saved.next, saved) || usable(saved.good, saved) || shipped();
    clean(new Set([found.build, saved.good].filter(Boolean)));
    return use(found);
  }

  /** The services run on `root`: it is good from now on. */
  function confirm(root) {
    if (root.dir !== bundled) write(stateFile, { ...state(), good: root.build, next: null, tries: 0 });
    return { ...root, trial: false };
  }

  /** The services do not run on `root`: it is not tried again. Answers the content to run instead. */
  function reject(root) {
    const saved = state();
    const good = saved.good === root.build ? null : saved.good;
    write(stateFile, { ...saved, good, next: null, tries: 0, bad: [...saved.bad, root.build] });
    return use(usable(good, state()) || shipped());
  }

  /** penrose was quit during a trial, before it could say: that start does not count. */
  function abandon() {
    const saved = state();
    if (saved.next && saved.tries > 0) write(stateFile, { ...saved, tries: saved.tries - 1 });
  }

  /** What the latest release is to this copy: { kind: "current" }, { kind: "content", feed } or
   *  { kind: "installer" } (it needs one, or publishes no content, or its content failed here). */
  async function look(running) {
    if (!safeBase(feedBase)) return { kind: "installer" };
    const response = await fetch(new URL(FEED, feedBase).href, { cache: "no-store" });
    if (response.status === 404) return { kind: "installer" };
    if (!response.ok) throw new Error(`The update server answered with HTTP ${response.status}.`);
    const feed = await response.json();
    if (!validFeed(feed)) return { kind: "installer" };
    if (feed.build <= running.build) return { kind: "current" };
    if (feed.shell !== shipped().shell || state().bad.includes(feed.build)) return { kind: "installer" };
    return { kind: "content", feed };
  }

  /** Fetches, verifies and unpacks `feed`'s content; the next start runs it. An error marked `unusable` means this
   *  copy cannot run it, and the installer is the way. */
  async function download(feed) {
    const response = await fetch(new URL(feed.file, feedBase).href);
    if (!response.ok) throw new Error(`The update could not be downloaded (HTTP ${response.status}).`);
    const bytes = Buffer.from(await response.arrayBuffer());
    if (bytes.length !== feed.size || crypto.createHash("sha512").update(bytes).digest("base64") !== feed.sha512) {
      throw new Error("The downloaded update is damaged. penrose will try again later.");
    }
    const dir = path.join(home, feed.build);
    const zip = `${dir}.pack`;
    fs.mkdirSync(home, { recursive: true });
    fs.rmSync(dir, { recursive: true, force: true });
    fs.writeFileSync(zip, bytes);
    try {
      await check(dir, zip);
      const found = readContent(dir);
      if (!found || found.build !== feed.build || found.shell !== feed.shell) throw new Error("It is not what the feed says.");
      write(stateFile, { ...state(), next: feed.build, tries: 0 });
      return found;
    } catch (error) {
      const saved = state();
      write(stateFile, { ...saved, bad: [...saved.bad, feed.build] });
      fs.rmSync(dir, { recursive: true, force: true });
      throw Object.assign(error, { unusable: true });
    } finally {
      fs.rmSync(zip, { force: true });
    }
  }

  return { abandon, confirm, download, look, reject, resolve, use };
};

module.exports.readContent = readContent;
