// `npm run dev`: the app with live reload. UI edits show instantly (Vite), backend edits restart the Python
// services, main-process edits restart Electron, and the phone keeps running through all of it.
//
// Optional settings go in desktop/.env.local (KEY=value per line), for example RELAY_DATA_DIR=D:\penrose-dev.
const { spawn } = require("node:child_process");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const desktop = path.resolve(__dirname, "..");
const frontend = path.join(desktop, "..", "frontend");
const UI_PORT = 8000;

const local = path.join(desktop, ".env.local");
if (fs.existsSync(local)) {
  for (const line of fs.readFileSync(local, "utf8").split(/\r?\n/)) {
    const match = /^\s*([A-Z0-9_]+)\s*=\s*(.*?)\s*$/.exec(line);
    if (match && !(match[1] in process.env)) process.env[match[1]] = match[2];
  }
}
const API_PORT = process.env.RELAY_API_PORT || "8001";

// A sign-in for this run, so a browser tab on the dev server can use the API too (see .dev-login.json).
const login = { username: "dev", password: crypto.randomBytes(18).toString("hex") };
fs.writeFileSync(path.join(desktop, ".dev-login.json"), JSON.stringify(login, null, 2));

const vite = spawn(
  process.execPath,
  [path.join(frontend, "node_modules", "vite", "bin", "vite.js"), "--host", "127.0.0.1", "--port", String(UI_PORT), "--strictPort"],
  { cwd: frontend, stdio: "inherit", env: { ...process.env, RELAY_API_PORT: API_PORT } },
);

let electron = null;
let restarting = false;
let stopping = false;

function startElectron() {
  electron = spawn(require("electron"), ["."], {
    cwd: desktop,
    stdio: ["inherit", "inherit", "inherit", "ipc"],
    env: {
      ...process.env,
      RELAY_DEV_URL: `http://127.0.0.1:${UI_PORT}`,
      RELAY_API_PORT: API_PORT,
      RELAY_DEV_WATCH: "1",
      RELAY_KEEP_PHONE: "1",
      RELAY_DEV_LOGIN: `${login.username}:${login.password}`,
      RELAY_DEBUG_PORT: process.env.RELAY_DEBUG_PORT || "9333",
    },
  });
  electron.on("exit", () => {
    electron = null;
    if (restarting) {
      restarting = false;
      startElectron();
    } else if (!stopping) {
      stop(); // the window was closed
    }
  });
}

/** Ask Electron to quit cleanly (it stops its services), forcing it after a few seconds. */
function quitElectron() {
  if (!electron) return;
  const child = electron;
  try {
    child.send("relay:quit");
  } catch {
    child.kill();
  }
  setTimeout(() => child.exitCode === null && child.kill(), 8000);
}

function stop() {
  if (stopping) return;
  stopping = true;
  quitElectron();
  const done = () => {
    vite.kill();
    process.exit(0);
  };
  if (electron) electron.once("exit", done);
  else done();
}

async function waitForUi() {
  for (let i = 0; i < 120; i++) {
    try {
      if ((await fetch(`http://127.0.0.1:${UI_PORT}/`)).ok) return;
    } catch {}
    await new Promise((r) => setTimeout(r, 250));
  }
  console.error("The Vite dev server did not start.");
  stop();
}

// Windows reports a file that was only read (its access time) as a change, so a grep, `git status` or an
// editor opening main.js would restart Electron and kill the task running on its services. Only a new
// modification time counts.
const src = path.join(desktop, "src");
const stamp = (file) => {
  try {
    return fs.statSync(path.join(src, file)).mtimeMs;
  } catch {
    return -1; // deleted
  }
};
const stamps = new Map(fs.readdirSync(src).map((file) => [file, stamp(file)]));
let timer = null;
fs.watch(src, (_event, file) => {
  if (!file || !/\.(js|cjs|mjs)$/.test(file)) return;
  const now = stamp(file);
  if (stamps.get(file) === now) return;
  stamps.set(file, now);
  clearTimeout(timer);
  timer = setTimeout(() => {
    if (!electron || restarting) return;
    console.log(`\n[dev] ${file} changed: restarting Electron`);
    restarting = true;
    quitElectron();
  }, 300);
});

process.on("SIGINT", stop);
process.on("SIGTERM", stop);
vite.on("exit", () => !stopping && stop());

waitForUi().then(() => !stopping && startElectron());
