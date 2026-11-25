// Relay desktop: the local services (API, worker, device gateway), the Android emulator, and the app window,
// whose first-run setup (API keys, then the phone) is part of the React app. Everything listens on 127.0.0.1;
// the gateway requires a per-launch token and the API a per-launch sign-in, so other programs and web pages
// on the machine cannot use them. The one exception is the phone API for the `relay` command (Claude Code,
// Cowork), opened by a durable token the API keeps in the data dir (backend/app/api/phone.py).

const { app, BrowserWindow, Menu, Tray, dialog, ipcMain, nativeImage, nativeTheme, safeStorage, screen, session, shell } = require("electron");
const electronNet = require("electron").net; // fetch through the system's proxy settings; node:net is `net` below
const { spawn, execFile } = require("node:child_process");
const crypto = require("node:crypto");
const fs = require("node:fs");
const http = require("node:http");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const readline = require("node:readline");
const { Writable } = require("node:stream");
const autostart = require("./autostart");
const datafolder = require("./datafolder");

// RELAY_PHONE_PORT: another instance's phone (a second profile in RELAY_DATA_DIR runs beside this one's).
const SERIAL = `emulator-${Number(process.env.RELAY_PHONE_PORT) || 5570}`;
const DEV = !app.isPackaged;
// One ID for everything in this build (scripts/release.js); stamped into the packaged app's package.json.
const BUILD_ID = require("../package.json").relayBuildId || "dev";
const ROOT = path.resolve(__dirname, "..", ".."); // the repository, in development

// Development switches, set by `npm run dev` (scripts/dev.js); ignored in the packaged app.
const DEV_URL = DEV ? process.env.RELAY_DEV_URL || "" : ""; // load the Vite dev server instead of frontend/dist
const DEV_API_PORT = DEV ? Number(process.env.RELAY_API_PORT) || 0 : 0; // a fixed port the dev server proxies to
const DEV_WATCH = DEV && process.env.RELAY_DEV_WATCH === "1"; // restart the Python services on backend edits
const KEEP_PHONE = DEV && process.env.RELAY_KEEP_PHONE === "1"; // the phone outlives the app between restarts
const DEV_LOGIN = DEV ? process.env.RELAY_DEV_LOGIN || "" : ""; // "user:password", so a browser tab can sign in
if (DEV && process.env.RELAY_DEBUG_PORT) {
  // Chrome DevTools protocol on 127.0.0.1, for inspecting the window from outside (development only).
  app.commandLine.appendSwitch("remote-debugging-port", process.env.RELAY_DEBUG_PORT);
}

/** The data folder the user chose, or "": penrose/data-location.txt in the per-user app data folder, on every
 *  platform. The Windows installer writes it (desktop/build/installer.nsh); backend/app/cli.py reads it too. */
function chosenDataDir() {
  const base = process.platform === "win32" ? process.env.LOCALAPPDATA : app.getPath("appData");
  if (!app.isPackaged || !base) return "";
  try {
    const raw = fs.readFileSync(path.join(base, "penrose", "data-location.txt"));
    const text = raw.toString(raw.includes(0) ? "utf16le" : "utf8").replace(/^\uFEFF/, "").trim();
    return path.isAbsolute(text) ? path.resolve(text) : "";
  } catch {
    return "";
  }
}

// Big data (the Android SDK and phone disks) belongs in Local, not Roaming, on Windows.
// The data folder keeps its old name, "Relay", so data stays put now the product is shown as penrose: Electron's
// default would follow productName to a new, empty folder.
const CHOSEN_DATA = chosenDataDir();
const DEFAULT_DATA = process.platform === "win32" && process.env.LOCALAPPDATA
  ? path.join(process.env.LOCALAPPDATA, "Relay")
  : path.join(app.getPath("appData"), "Relay");
// The macOS and Linux builds have no install step, so their first start asks where the data goes (datafolder.js),
// unless data from before the question is in the default folder. Chromium's own files wait in a throwaway folder
// while it asks; penrose restarts in the chosen one.
const ASK_FOR_DATA = !DEV && process.platform !== "win32" && !process.env.RELAY_DATA_DIR && !CHOSEN_DATA &&
  !datafolder.hasData(DEFAULT_DATA);
const FIRST_RUN_DIR = path.join(app.getPath("temp"), "penrose-first-run");
if (process.env.RELAY_DATA_DIR) {
  app.setPath("userData", path.resolve(process.env.RELAY_DATA_DIR)); // another profile or drive
} else if (CHOSEN_DATA) {
  try {
    fs.mkdirSync(CHOSEN_DATA, { recursive: true });
    app.setPath("userData", CHOSEN_DATA);
  } catch {
    dialog.showErrorBox("penrose cannot open its data folder",
      `${CHOSEN_DATA}\n\nIf this folder is on a drive that is unplugged, plug it in and open penrose again.`);
    app.exit(1);
  }
} else if (ASK_FOR_DATA) {
  app.setPath("userData", FIRST_RUN_DIR);
} else {
  app.setPath("userData", DEFAULT_DATA);
}
if (!DEV && !ASK_FOR_DATA) fs.rm(FIRST_RUN_DIR, { recursive: true, force: true }, () => {});
const DATA = app.getPath("userData");
const ANDROID_HOME_DIR = path.join(DATA, "android");
const LOGS = path.join(DATA, "logs");
const CONFIG = path.join(DATA, "config.json");
fs.mkdirSync(LOGS, { recursive: true });

// --- configuration: API keys are encrypted with the OS keychain (safeStorage) -------------------------

const PROVIDERS = ["deepseek", "openai", "anthropic"];

function readConfig() {
  try {
    return JSON.parse(fs.readFileSync(CONFIG, "utf8"));
  } catch {
    return { keys: {} };
  }
}

function writeConfig(config) {
  fs.writeFileSync(CONFIG, JSON.stringify(config, null, 2));
}

function getKey(provider) {
  const stored = readConfig().keys?.[provider];
  if (!stored) return "";
  try {
    return safeStorage.decryptString(Buffer.from(stored, "base64"));
  } catch {
    return "";
  }
}

function setKey(provider, key) {
  const config = readConfig();
  config.keys = config.keys || {};
  if (key) config.keys[provider] = safeStorage.encryptString(key).toString("base64");
  else delete config.keys[provider];
  writeConfig(config);
}

function configuredProviders() {
  return PROVIDERS.filter((p) => getKey(p));
}

/** Each provider's key as its last four characters ("…abcd"), or "": all the window or a phone ever sees of a key. */
function maskedKeys() {
  return Object.fromEntries(PROVIDERS.map((p) => [p, getKey(p) ? `…${getKey(p).slice(-4)}` : ""]));
}

// --- where things are -------------------------------------------------------------------------------

function resource(...parts) {
  return path.join(process.resourcesPath, ...parts);
}

/** The Python services: the bundled relay-server, or `python -m app.desktop` from the repo in development. */
function serverCommand(args) {
  if (DEV) {
    const python =
      process.platform === "win32"
        ? path.join(ROOT, ".venv", "Scripts", "python.exe")
        : path.join(ROOT, ".venv", "bin", "python");
    return { command: python, args: ["-m", "app.desktop", ...args], cwd: path.join(ROOT, "backend") };
  }
  const name = process.platform === "win32" ? "relay-server.exe" : "relay-server";
  return { command: resource("server", name), args, cwd: resource("server") };
}

function androidAssets() {
  return DEV ? path.join(ROOT, "desktop", "assets", "android") : resource("android");
}

function frontendDir() {
  return DEV ? path.join(ROOT, "frontend", "dist") : resource("frontend");
}

function apkeepPath() {
  const name = process.platform === "win32" ? "apkeep.exe" : "apkeep";
  const file = DEV ? path.join(ROOT, "desktop", "vendor", "bin", name) : resource("bin", name);
  return fs.existsSync(file) ? file : "";
}

/** An Android SDK the user already has, so setup can skip the 3 GB download. */
function existingSdk() {
  const home = os.homedir();
  const candidates = [
    process.env.ANDROID_HOME,
    process.env.ANDROID_SDK_ROOT,
    process.platform === "win32" && process.env.LOCALAPPDATA && path.join(process.env.LOCALAPPDATA, "Android", "Sdk"),
    process.platform === "darwin" && path.join(home, "Library", "Android", "sdk"),
    process.platform === "linux" && path.join(home, "Android", "Sdk"),
    // An SDK elsewhere whose platform-tools (adb) are on PATH.
    ...(process.env.PATH || "").split(path.delimiter)
      .filter((dir) => path.basename(dir).toLowerCase() === "platform-tools")
      .map((dir) => path.dirname(dir)),
  ].filter(Boolean);
  const emulator = process.platform === "win32" ? "emulator.exe" : "emulator";
  return candidates.find((dir) => fs.existsSync(path.join(dir, "emulator", emulator))) || "";
}

function sdkDir() {
  return readConfig().sdk || path.join(ANDROID_HOME_DIR, "sdk");
}

/** adb in the SDK's platform-tools, found the way `relay-server provision status` finds it (app/provision Layout.adb),
 *  so the services can start without waiting for that check; adb on PATH when the SDK has none yet. */
function adbPath() {
  const dir = path.join(sdkDir(), "platform-tools");
  const names = process.platform === "win32" ? ["adb.exe", "adb.bat", "adb.cmd"] : ["adb"];
  const found = names.map((name) => path.join(dir, name)).find((file) => fs.existsSync(file));
  return found || "adb";
}

// --- child processes --------------------------------------------------------------------------------

const children = new Map(); // name -> ChildProcess
const crashes = new Map(); // name -> recent crash count, for the restart back-off
let quitting = false;

const LOG_LIMIT = 5 * 1024 * 1024;

/** A log to append to. Once it is over LOG_LIMIT it becomes <name>.old.log (replacing the previous one) and a new
 *  one begins, so a log never takes more than about twice the limit, however long penrose stays open (checked
 *  only when penrose started, a log grew without end on a computer that is never restarted). A log that cannot be
 *  written loses its lines: it never stops the app. */
function logStream(name) {
  const file = path.join(LOGS, `${name}.log`);
  let size = 0;
  try {
    size = fs.statSync(file).size;
  } catch {} // no log yet
  let out = null;
  const stream = new Writable({
    write(chunk, _encoding, done) {
      if (size > LOG_LIMIT) {
        out?.end();
        out = null;
        try {
          fs.renameSync(file, path.join(LOGS, `${name}.old.log`));
        } catch {} // held by another program: go on in the same file, and try again a limit later
        size = 0;
      }
      if (!out) {
        out = fs.createWriteStream(file, { flags: "a" });
        out.on("error", () => {});
      }
      size += chunk.length;
      out.write(chunk, () => done());
    },
    final(done) {
      if (out) out.end(() => done());
      else done();
    },
  });
  stream.on("error", () => {}); // a line written after the log was closed
  return stream;
}

/** `env`: the service's environment, or a function that gives it at each start (a crash restart too). */
function startService(name, args, env) {
  const { command, args: full, cwd } = serverCommand(args);
  const out = logStream(name);
  out.write(`\n--- ${new Date().toISOString()} ${name} ---\n`);
  const child = spawn(command, full, {
    cwd, env: { ...process.env, ...(typeof env === "function" ? env() : env) }, windowsHide: true,
  });
  const started = Date.now();
  child.stdout.pipe(out);
  child.stderr.pipe(out);
  child.on("exit", (code) => {
    out.end(`--- exited ${code} ---\n`);
    if (children.get(name) !== child) return; // stopped on purpose, or already replaced
    children.delete(name);
    if (quitting || code === 0) return;
    // A crashed service restarts after a pause that doubles with each quick crash (1 s up to a minute),
    // so a service that cannot start does not spin. A minute of running resets the count.
    const count = Date.now() - started > 60_000 ? 1 : (crashes.get(name) || 0) + 1;
    crashes.set(name, count);
    const delay = Math.min(60_000, 1000 * 2 ** (count - 1));
    setTimeout(() => !quitting && !children.has(name) && startService(name, args, env), delay);
  });
  children.set(name, child);
  return child;
}

/** Stop a service on purpose (no crash restart). Resolves once it has exited. */
function stopService(name) {
  const child = children.get(name);
  children.delete(name);
  if (!child || child.exitCode !== null) return Promise.resolve();
  return new Promise((resolve) => {
    child.once("exit", resolve);
    child.kill();
  });
}

/** Run `relay-server provision ...`, forwarding its JSON-line events. Resolves with the last event. */
function provision(args, onEvent) {
  return new Promise((resolve) => {
    const { command, args: full, cwd } = serverCommand(["provision", ...args]);
    const out = logStream("provision");
    const child = spawn(command, full, {
      cwd, env: { ...process.env, PYTHONUNBUFFERED: "1", RELAY_PARENT_PID: String(process.pid) }, windowsHide: true,
    });
    children.set("provision", child);
    let last = { event: "error", message: "Setup stopped unexpectedly." };
    readline.createInterface({ input: child.stdout }).on("line", (line) => {
      out.write(line + "\n");
      try {
        last = JSON.parse(line);
        onEvent?.(last);
      } catch {
        onEvent?.({ event: "log", text: line });
      }
    });
    child.stderr.pipe(out);
    child.on("exit", () => {
      out.end();
      children.delete("provision");
      resolve(last);
    });
  });
}

/** The setup doctor (backend/app/provision/doctor.py): the user's own model looks at the setup and answers in
 *  plain words, with one action from a fixed menu. Its own short process, so it never touches a running setup.
 *  Resolves { reply, state, action }, or null when it could not answer. */
function askDoctor(trigger, messages = []) {
  return new Promise((resolve) => {
    const { command, args, cwd } = serverCommand([
      "provision", "doctor", "--home", ANDROID_HOME_DIR, "--sdk", sdkDir(), "--logs", LOGS,
    ]);
    const out = logStream("doctor");
    const child = spawn(command, args, {
      cwd,
      env: {
        ...process.env, PYTHONUNBUFFERED: "1", ADB_PATH: adbPath(),
        DEEPSEEK_API_KEY: getKey("deepseek"), OPENAI_API_KEY: getKey("openai"), ANTHROPIC_API_KEY: getKey("anthropic"),
      },
      windowsHide: true,
    });
    let answer = null;
    const timer = setTimeout(() => child.kill(), 90_000);
    readline.createInterface({ input: child.stdout }).on("line", (line) => {
      out.write(line + "\n");
      try {
        const event = JSON.parse(line);
        if (event.event === "doctor") answer = { reply: event.reply, state: event.state, action: event.action };
      } catch {
        /* not an event */
      }
    });
    child.stderr.pipe(out);
    child.on("error", () => resolve(null));
    child.on("exit", () => {
      clearTimeout(timer);
      out.end();
      resolve(answer);
    });
    child.stdin.end(JSON.stringify({ trigger, messages, state: setupState() }));
  });
}

/** What the doctor is told about the setup, as this process sees it. */
function setupState() {
  const now = Date.now();
  const marks = setupRun?.marks || [];
  const step = [...marks].reverse().find((e) => e.event === "step");
  const progress = [...marks].reverse().find((e) => e.event === "progress");
  return {
    running: Boolean(setupRun && !setupRun.ended),
    step: step?.id || null,
    doing: progress?.doing || null,
    percent: progress?.percent ?? null,
    seconds_running: setupRun && !setupRun.ended ? Math.round((now - setupRun.startedAt) / 1000) : null,
    seconds_since_progress: setupRun && !setupRun.ended ? Math.round((now - setupRun.lastEventAt) / 1000) : null,
    error: setupResult && !setupResult.ok ? { message: setupResult.error, hint: setupResult.hint } : null,
    auto_retries: autoRetries,
  };
}

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

async function waitFor(url, headers = {}, seconds = 60) {
  const deadline = Date.now() + seconds * 1000;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url, { headers });
      if (response.ok) return true;
    } catch {}
    await new Promise((r) => setTimeout(r, 100)); // a local service: checking often costs nothing
  }
  return false;
}

// --- the phone ------------------------------------------------------------------------------------------

let androidStatus = null;
let statusRead = Promise.resolve(); // this launch's phone check, which the window's setup check waits for
let phoneError = null; // why the phone did not come up this launch, for the window
let phoneStep = null; // what the post-boot check is doing right now ("Updating your phone"), for the window
let phoneSetup = false; // device-ready is updating the phone: the gateway grants no lease until it is done
let stoppingEmulator = false; // this launch is stopping the phone on purpose (quit, reset)

async function readAndroidStatus() {
  const event = await provision(["status", "--home", ANDROID_HOME_DIR, "--sdk", sdkDir()]);
  androidStatus = event.event === "status" ? event : null;
  return androidStatus;
}

function adbState() {
  return new Promise((resolve) => {
    if (!androidStatus?.adb) return resolve("");
    execFile(androidStatus.adb, ["-s", SERIAL, "get-state"], { windowsHide: true, timeout: 5000 }, (error, stdout) =>
      resolve(error ? "" : stdout.trim()),
    );
  });
}

let adoptedPhone = false; // a phone left running by a launch that crashed, which this launch took over

// The phone's share of this computer: 20% of its memory (3 GB at least) and half its logical processors (4 to 6:
// the emulator takes no more than 6, and idle cores cost the computer almost nothing). Memory is rounded down to
// 512 MB so the same computer always asks for the same size: a different size makes the emulator drop its
// quick-boot snapshot and boot cold.
function phoneSize() {
  const memory = Math.max(3072, Math.floor((os.totalmem() / 2 ** 20) * 0.2 / 512) * 512);
  const cores = Math.min(6, Math.max(4, Math.floor(os.cpus().length / 2)));
  return { memory, cores };
}

// Windows: the phone's kernel is told not to compare its fast clock (tsc) with the emulated timer (hpet). The
// emulator's timer stops whenever the emulator stops the phone for a moment and the hypervisor's tsc does not, so a
// stop of a few tenths of a second made the kernel give up tsc for hpet until its next boot: every clock read
// then leaves the phone, apps start 1.5 to 2 times slower and stop responding. Why, what was tried and what
// device-ready does about a kernel already on hpet: backend/app/provision (fast_clock). Everything after -qemu
// goes to qemu itself, so these stay last.
const FAST_CLOCK_ARGS = process.platform === "win32" ? ["-qemu", "-append", "tsc=nowatchdog"] : [];

async function startEmulator() {
  if (!androidStatus?.phone_ready || children.has("emulator") || adoptedPhone) return;
  watchKeptPhone();
  const running = (await adbState()) === "device";
  if (KEEP_PHONE && running) return; // still running from the last dev restart
  if (running) {
    // Relay crashed or was killed and its phone kept running: use it rather than start a second one on the same
    // port, and stop it on quit as if this launch had started it.
    adoptedPhone = true;
    return phoneReady();
  }
  const port = SERIAL.split("-")[1];
  // Quick boot: the phone resumes from the snapshot saved when Relay last quit (about 2 s, against about 18 s
  // cold), root and Xposed included. After a crash the emulator finds that snapshot stale and boots cold
  // instead, keeping everything written since; quit (`emu kill`) saves a fresh one in under a second.
  // Windows: a hidden window, not -no-window. The headless build's UI thread spins on a wake event it never
  // resets, holding a whole CPU core while the phone sits idle (emulator 37.1); hidden, the phone idles at ~10%.
  // gRPC: the gateway's live view takes frames from the emulator itself (no encoding inside the phone). With a
  // token, the emulator serves it on 127.0.0.1 only and advertises port and token in its discovery file.
  // Audio: the phone has its sound device (calls need it: the gateway hears it over gRPC and relay-bridge feeds
  // penrose's voice in as the mic), but `-audio none` connects it to nothing on this computer: no sound plays
  // here and the computer's microphone is never opened.
  // No Vulkan inside the phone: the emulator will not save a snapshot while an app (Chrome, any WebView) holds a
  // Vulkan context ("Not saving state ... UNSUPPORTED_VK_APP"), deletes the old one, and the next start is a cold
  // boot (25-30 s instead of about 2 s). Without it apps draw with OpenGL ES, on the host GPU all the same.
  const display = process.platform === "win32" ? "-qt-hide-window" : "-no-window";
  const { memory, cores } = phoneSize();
  // Crash reports are never sent and never asked about. After an emulator crash the next start otherwise asks
  // whether to send the report, in the window this launch hides, and waits for the answer before it boots:
  // "The Android phone took too long to start" (24 minutes on 2026-11-24, also 2026-11-12 and 2026-10-30).
  // Detailed log: each line of emulator.log begins with its time of day (UTC) and the thread that wrote it, so a
  // freeze or a crash can be matched with what the phone and this computer were doing; the date is in the line
  // written at each start, below.
  const args = ["-avd", androidStatus.avd, "-port", port, display, "-audio", "none", "-no-boot-anim", "-gpu", "host",
    "-feature", "-Vulkan", "-memory", String(memory), "-cores", String(cores), "-dns-server", "1.1.1.1",
    "-grpc", String(Number(port) + 3000), "-grpc-use-token", "-crash-report-mode", "never", "-log-detailed",
    ...FAST_CLOCK_ARGS];
  const startLine = `\n--- ${new Date().toISOString()} emulator ---\n`;
  const env = { ...process.env, ANDROID_AVD_HOME: androidStatus.avd_home, ANDROID_HOME: androidStatus.sdk,
    ANDROID_SDK_ROOT: androidStatus.sdk };
  if (KEEP_PHONE) {
    // Not ours to stop: the next dev restart picks it up again, so it must not hold anything of this process's.
    // On Windows, Start-Process launches it in a hidden console that its helpers (qemu, netsimd) share, where
    // a detached process would have none and each helper would open a visible one; and unlike a plain child it
    // inherits none of Electron's handles, which would keep the DevTools port taken after a restart.
    const log = path.join(LOGS, "emulator.log");
    try {
      // The emulator writes this log itself, so the size is looked at here, before it starts.
      if (fs.statSync(log).size > LOG_LIMIT) fs.renameSync(log, path.join(LOGS, "emulator.old.log"));
    } catch {} // no log yet
    try {
      fs.appendFileSync(log, startLine);
    } catch {}
    if (process.platform === "win32") {
      const quote = (s) => `'${s.replace(/'/g, "''")}'`;
      const line = `/d /s /c ""${androidStatus.emulator}" ${args.join(" ")} >> "${log}" 2>&1"`;
      const script = `Start-Process -FilePath cmd.exe -ArgumentList ${quote(line)} -WindowStyle Hidden`;
      spawn("powershell.exe", ["-NoProfile", "-NonInteractive", "-EncodedCommand", Buffer.from(script, "utf16le").toString("base64")],
        { env, windowsHide: true, stdio: "ignore" });
    } else {
      const fd = fs.openSync(log, "a");
      spawn(androidStatus.emulator, args, { env, detached: true, stdio: ["ignore", fd, fd] }).unref();
      fs.closeSync(fd);
    }
  } else {
    const out = logStream("emulator");
    out.write(startLine);
    const child = spawn(androidStatus.emulator, args, { env, windowsHide: true });
    const started = Date.now();
    child.stdout.pipe(out);
    child.stderr.pipe(out);
    child.on("exit", (code) => {
      out.end();
      if (children.get("emulator") === child) children.delete("emulator");
      if (quitting || stoppingEmulator) return;
      // The phone crashed: say so rather than show "Starting" for ever. One that ran a while starts again (a cold
      // boot: the crash left no snapshot); one that cannot even start is left for the user to see.
      const again = Date.now() - started > 60_000;
      phoneError = {
        message: "The phone stopped unexpectedly.",
        hint: again ? "penrose is starting it again." : `It could not start (exit code ${code}). Quit penrose and open it again.`,
      };
      mainWindow?.webContents.send("phone:error", phoneError);
      if (again) setTimeout(() => !quitting && !stoppingEmulator && startEmulator(), 2000);
    });
    children.set("emulator", child);
  }
  phoneReady();
}

let phoneReadying = false; // device-ready is running: it waits for the phone to boot, however often it is started

function phoneReady() {
  // Three-button navigation, Chrome's first-run screens skipped, the Xposed framework up, and the phone
  // brought up to date with this build's shipped assets (the provisioner's manifest) when they changed.
  phoneError = null;
  // One at a time. An emulator that is started again while device-ready still waits for it (it did not come up
  // and the kept-phone watch started it once more, 2026-11-22) is the phone that run is waiting for: a second run
  // beside it did every step twice, the restarts too.
  if (phoneReadying) return;
  phoneReadying = true;
  const ready = ["device-ready", "--home", ANDROID_HOME_DIR, "--sdk", sdkDir(), "--serial", SERIAL,
    "--assets", androidAssets(), "--build-id", BUILD_ID];
  // Only for an emulator this launch started with the option: one adopted from an earlier build runs without it,
  // and restarting Android inside it would not add it.
  if (FAST_CLOCK_ARGS.length && !adoptedPhone) ready.push("--fast-clock");
  const sendStep = (title) => {
    phoneStep = title;
    mainWindow?.webContents.send("phone:step", phoneStep);
  };
  setPhoneSetup(true);
  provision(ready, (event) => {
    if (event.event === "step") sendStep(event.title);
    else if (event.event === "outdated") phoneOutdated(event);
    else if (event.event === "error") {
      phoneError = { message: event.message, hint: event.hint || "" };
      mainWindow?.webContents.send("phone:error", phoneError);
    }
  }).then(() => {
    phoneReadying = false;
    sendStep(null);
    setPhoneSetup(false);
  });
}

/** Tell the gateway that device-ready is (or is no longer) updating the phone, so no task takes it meanwhile. A
 *  gateway started meanwhile learns it from PHONE_SETUP (startServiceSet); one that is restarting is told again. */
async function setPhoneSetup(active) {
  phoneSetup = active;
  for (let attempt = 0; attempt < 120 && services && phoneSetup === active; attempt++) {
    try {
      const response = await fetch(`${services.env.EMULATOR_GATEWAY_URL}/devices/device-1/setup`, {
        method: "PUT",
        headers: { "Content-Type": "application/json", "X-Gateway-Token": services.env.GATEWAY_TOKEN },
        body: JSON.stringify({ active }),
      });
      if (response.ok) return;
    } catch {}
    await new Promise((r) => setTimeout(r, 500)); // the gateway is starting: it reads PHONE_SETUP then
  }
}

/** This build's base image is newer than the phone's: it keeps working, and a reset brings it up to date. */
async function phoneOutdated(event) {
  const { response } = await dialog.showMessageBox(mainWindow, {
    type: "info",
    buttons: ["Reset phone", "Later"],
    defaultId: 1,
    cancelId: 1,
    noLink: true,
    message: event.message,
    detail: `${event.hint}\n\nUntil then the phone keeps working as it is. "Reset phone…" in the penrose menu does it later.`,
  });
  if (response === 0) await performReset();
}

// `emu kill` saves the quick-boot snapshot, then the emulator gives itself 20 s to exit before it kills itself.
// Killing it sooner (this was 15 s) can leave a snapshot that fails to load, and the next start boots cold.
const EMULATOR_QUIT_MS = 30000;

function stopEmulator() {
  stoppingEmulator = true;
  return new Promise((resolve) => {
    if (KEEP_PHONE || !androidStatus?.adb || !(children.has("emulator") || adoptedPhone)) return resolve();
    // A hung adb must not hold up quitting: give up on it after 10 s and kill the emulator instead.
    execFile(androidStatus.adb, ["-s", SERIAL, "emu", "kill"], { windowsHide: true, timeout: 10000 }, () => {
      adoptedPhone = false;
      const child = children.get("emulator");
      if (!child) {
        // Adopted, so there is no process to wait for: wait until adb no longer sees the phone (at most 30 s).
        const deadline = Date.now() + EMULATOR_QUIT_MS;
        const gone = async () => {
          if (Date.now() > deadline || (await adbState()) !== "device") return resolve();
          setTimeout(gone, 500);
        };
        return gone();
      }
      const timer = setTimeout(() => {
        child?.kill();
        resolve();
      }, EMULATOR_QUIT_MS);
      child?.once("exit", () => {
        clearTimeout(timer);
        resolve();
      });
    });
  });
}

// --- a phone that stops answering -------------------------------------------------------------------------
// An emulator can freeze with its process still there: on 2026-11-09 this computer ran out of virtual memory, the
// emulator could not allocate, and it hung for good. Nothing exits, so nothing above notices. The gateway asks the
// phone whether it still answers (backend/app/gateway/watch.py) and, when it does not, asks here over the control
// server, because this app owns the emulator: the frozen one is ended and the phone starts again (a cold boot;
// everything written to the phone before it froze is kept).

const PHONE_RESTART_GAP_MS = 10 * 60 * 1000; // never a loop: a phone that freezes again this soon is left for the user
let phoneRestart = null; // the restart in flight
let lastPhoneRestart = 0;

/** The emulator processes running this phone (qemu, started with `-port <this phone's>`), whoever started them:
 *  this launch, an earlier one that crashed, or development's phone that outlives the app. */
function phoneProcesses() {
  const port = SERIAL.split("-")[1];
  const ours = (line) => /qemu-system/i.test(line) && new RegExp(`\\s-port\\s+${port}(\\s|$)`).test(line);
  return new Promise((resolve) => {
    const done = (error, stdout) =>
      resolve(error ? [] : String(stdout).split(/\r?\n/).filter(ours).map((line) => Number(/^\s*(\d+)\s/.exec(line)?.[1])).filter(Boolean));
    if (process.platform === "win32") {
      const script = `Get-CimInstance Win32_Process -Filter "Name like 'qemu-system%'" | ForEach-Object { "$($_.ProcessId) $($_.CommandLine)" }`;
      execFile("powershell.exe", ["-NoProfile", "-NonInteractive", "-Command", script], { windowsHide: true, timeout: 20000 }, done);
    } else {
      execFile("ps", ["-axo", "pid=,args="], { timeout: 10000, maxBuffer: 16 * 1024 * 1024 }, done);
    }
  });
}

/** End a process that no longer answers, with what it started. */
function endProcess(pid) {
  return new Promise((resolve) => {
    if (process.platform === "win32") {
      return execFile("taskkill", ["/F", "/T", "/PID", String(pid)], { windowsHide: true, timeout: 15000 }, () => resolve());
    }
    try {
      process.kill(pid, "SIGKILL");
    } catch {}
    resolve();
  });
}

/** The gateway found the phone frozen: end its emulator and start it again. Answers what was done. */
function restartFrozenPhone() {
  if (quitting || stoppingEmulator) return Promise.resolve({ restarted: false, reason: "penrose is stopping the phone itself" });
  if (phoneRestart) return phoneRestart;
  if (Date.now() - lastPhoneRestart < PHONE_RESTART_GAP_MS) {
    phoneError = {
      message: "The phone keeps freezing.",
      hint: "Quit penrose and open it again. If it goes on, restart this computer: it may be out of memory.",
    };
    mainWindow?.webContents.send("phone:error", phoneError);
    return Promise.resolve({ restarted: false, reason: "it was restarted less than ten minutes ago" });
  }
  phoneRestart = (async () => {
    const pids = await phoneProcesses();
    if (!pids.length) return { restarted: false, reason: "no emulator process runs this phone" };
    lastPhoneRestart = Date.now();
    console.error(`The phone stopped responding: ending its emulator (${pids.join(", ")}) and starting it again`);
    phoneError = { message: "The phone stopped responding.", hint: "penrose is restarting it." };
    mainWindow?.webContents.send("phone:error", phoneError);
    const child = children.get("emulator");
    for (const pid of pids) await endProcess(pid);
    if (child) {
      child.kill(); // its exit starts the phone again (startEmulator's exit handler)
      return { restarted: true, pids };
    }
    // A phone this launch did not start: adb drops it as its process ends, then it is started as at launch.
    adoptedPhone = false;
    for (let i = 0; i < 60 && (await adbState()) === "device"; i++) await new Promise((r) => setTimeout(r, 250));
    if ((await adbState()) === "device") return { restarted: false, reason: "adb still lists the phone", pids };
    await startEmulator();
    return { restarted: true, pids };
  })().finally(() => {
    phoneRestart = null;
  });
  return phoneRestart;
}

// A phone that is gone. Normally this app owns the emulator and starts it again when it exits (startEmulator's exit
// handler). Development's phone is started detached so it outlives restarts, so no exit handler sees it end: on
// 2026-11-18 it vanished at 02:20 (no crash report, no log line) and the chat sat on "Waiting for the phone" until
// someone started it by hand. Poll it instead.
const KEPT_PHONE_CHECK_MS = 20_000;
const KEPT_PHONE_MISSES = 2; // checks in a row that find neither the phone on adb nor its process (a boot shows a process)
let keptPhoneWatch = null;

function watchKeptPhone() {
  if (!KEEP_PHONE || keptPhoneWatch) return;
  let misses = 0;
  let busy = false;
  keptPhoneWatch = setInterval(async () => {
    if (busy || quitting || stoppingEmulator || phoneRestart) return;
    busy = true;
    try {
      if ((await adbState()) === "device") return void (misses = 0);
      // Not listed: booting, frozen (the gateway's watch deals with that) or gone. Only gone is ours.
      if ((await phoneProcesses()).length) return void (misses = 0);
      if (++misses < KEPT_PHONE_MISSES || quitting || stoppingEmulator) return;
      misses = 0;
      console.error("The phone's emulator is gone: starting it again");
      phoneError = { message: "The phone stopped unexpectedly.", hint: "penrose is starting it again." };
      mainWindow?.webContents.send("phone:error", phoneError);
      await startEmulator();
    } finally {
      busy = false;
    }
  }, KEPT_PHONE_CHECK_MS);
  keptPhoneWatch.unref?.();
}

// --- the services -----------------------------------------------------------------------------------------

let services = null; // { apiPort, env }

async function startServices() {
  const [apiPort, gatewayPort] = [DEV_API_PORT || (await freePort()), await freePort()];
  const [devUser, ...devPassword] = DEV_LOGIN.split(":");
  const login = DEV_LOGIN
    ? { user: devUser, password: devPassword.join(":") }
    : { user: crypto.randomBytes(9).toString("hex"), password: crypto.randomBytes(24).toString("hex") };
  const origin = `http://127.0.0.1:${apiPort}`;
  const env = {
    PYTHONUNBUFFERED: "1",
    DESKTOP: "true",
    RELAY_PARENT_PID: String(process.pid), // services exit if the app dies without stopping them
    DATA_DIR: path.join(DATA, "data"),
    USER_ID: "local",
    FRONTEND_DIR: frontendDir(),
    PUBLIC_ORIGIN: origin,
    LOGIN_USERNAME: login.user,
    LOGIN_PASSWORD: login.password,
    EMULATOR_GATEWAY_URL: `http://127.0.0.1:${gatewayPort}`,
    API_URL: origin, // the worker rings the API when a run has new events (backend/app/bell.py)
    GATEWAY_TOKEN: crypto.randomBytes(24).toString("hex"),
    // The companion phone app and its coordinator (backend/app/companion): deployed to the user's own Cloudflare
    // account from Settings (src/companion.js), which the API remembers; a developer may point at another.
    COMPANION_ORIGIN: process.env.RELAY_COMPANION_ORIGIN || "",
    ADB_TARGETS: SERIAL,
    ADB_PATH: adbPath(),
    APKEEP_PATH: apkeepPath(),
    // Set every key, even empty, so nothing is read from a developer's backend/.env.
    DEEPSEEK_API_KEY: getKey("deepseek"),
    OPENAI_API_KEY: getKey("openai"),
    ANTHROPIC_API_KEY: getKey("anthropic"),
  };
  services = { apiPort, gatewayPort, origin, env, control: await startControl() };
  startServiceSet();
  if (!(await waitFor(`${origin}/api/health`))) {
    throw new Error("penrose could not start its background service. Quit penrose and open it again.");
  }
  // Sign the window in with this launch's credentials.
  const response = await fetch(`${origin}/api/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username: login.user, password: login.password }),
  });
  const token = /relay_session=([^;]+)/.exec(response.headers.get("set-cookie") || "")?.[1];
  if (!token) throw new Error("Could not sign in to the penrose service.");
  // Cookies ignore the port, so in development this also covers the Vite dev server on the same host.
  await session.defaultSession.cookies.set({
    url: origin, name: "relay_session", value: token, httpOnly: true, sameSite: "strict",
  });
  if (DEV_WATCH) watchBackend();
  writeLauncher();
}

/** The `relay` command for other programs on this computer (Claude Code, Cowork): a launcher in <data>/bin that
 *  runs relay-server with the same arguments (backend/app/cli.py). The API writes the discovery file the command
 *  reads (<data>/data/cli.json). Putting the folder on the PATH is the user's step, shown by `relay setup`. */
function writeLauncher() {
  const bin = path.join(DATA, "bin");
  const { command, args, cwd } = serverCommand([]);
  try {
    fs.mkdirSync(bin, { recursive: true });
    // A shell script everywhere: on Windows it is what Git Bash (Claude Code's Bash tool) finds as `relay`.
    const slash = (s) => s.replace(/\\/g, "/");
    const script = [`#!/bin/sh`, `export RELAY_CWD="$PWD"`, `export PYTHONPATH='${slash(cwd)}'`,
      [`exec '${slash(command)}'`, ...args.map((a) => `'${a}'`), `"$@"`].join(" "), ``].join("\n");
    const file = path.join(bin, "relay");
    fs.writeFileSync(file, script);
    if (process.platform === "win32") {
      // And a .cmd for cmd.exe and PowerShell.
      const line = [`"${command}"`, ...args.map((a) => `"${a}"`), `%*`].join(" ");
      fs.writeFileSync(path.join(bin, "relay.cmd"), `@echo off\r\nset "RELAY_CWD=%CD%"\r\nset "PYTHONPATH=${cwd}"\r\n${line}\r\n`);
    } else {
      fs.chmodSync(file, 0o755);
    }
  } catch (error) {
    console.error("Could not write the relay launcher:", error);
  }
}

function startServiceSet() {
  const { apiPort, gatewayPort, env } = services;
  // The gateway's way back to this app is for one thing: a phone that froze (restartFrozenPhone).
  startService("gateway", ["gateway", "--port", String(gatewayPort)], () => ({
    ...env,
    PHONE_SETUP: phoneSetup ? "1" : "0",
    DESKTOP_CONTROL_URL: services.control.url,
    DESKTOP_PHONE_TOKEN: services.control.phoneToken,
  }));
  startService("api", ["api", "--port", String(apiPort)], apiEnv);
  startService("worker", ["worker"], env);
}

/** The API's environment: the services' own, and the way back to this app (only the API is given the token). */
function apiEnv() {
  return {
    ...services.env,
    DESKTOP_CONTROL_URL: services.control.url,
    DESKTOP_CONTROL_TOKEN: services.control.token,
  };
}

/** The services' way back to this app: loopback, with a per-launch token only the API is given. The keys are
 *  encrypted with the OS keychain, which only this process can open, so a key saved from a linked phone
 *  (backend/app/api/keys.py) is handed over here. The gateway has a token of its own that opens one route:
 *  restart a phone that froze (restartFrozenPhone). */
function startControl() {
  const token = crypto.randomBytes(24).toString("hex");
  const phoneToken = crypto.randomBytes(24).toString("hex"); // the gateway's: it opens /phone/restart and nothing else
  const server = http.createServer((request, response) => {
    const reply = (status, body) => {
      response.writeHead(status, { "Content-Type": "application/json" });
      response.end(JSON.stringify(body));
    };
    const allowed = (secret) => {
      const given = Buffer.from(request.headers.authorization || "");
      const want = Buffer.from(`Bearer ${secret}`);
      return given.length === want.length && crypto.timingSafeEqual(given, want);
    };
    if (request.method === "POST" && request.url === "/phone/restart") {
      if (!allowed(phoneToken)) return reply(401, { error: "Not allowed." });
      request.resume();
      return restartFrozenPhone().then(
        (result) => reply(200, result),
        (error) => reply(500, { restarted: false, reason: String(error?.message || error) }),
      );
    }
    if (!allowed(token)) return reply(401, { error: "Not allowed." });
    if (request.method !== "POST" || request.url !== "/keys") return reply(404, { error: "Not found." });
    let raw = "";
    request.on("data", (chunk) => {
      raw += chunk;
      if (raw.length > 4096) request.destroy();
    });
    request.on("end", async () => {
      try {
        const { provider, key } = JSON.parse(raw);
        const result = await applyKey(provider, key);
        if (result.ok) restartModelServicesSoon();
        reply(200, { ...result, keys: maskedKeys() });
      } catch {
        reply(400, { ok: false, error: "That request was not understood." });
      }
    });
  });
  return new Promise((resolve) =>
    server.listen(0, "127.0.0.1", () => resolve({ url: `http://127.0.0.1:${server.address().port}`, token, phoneToken })),
  );
}

/** Development: restart the Python services when backend code changes. */
function watchBackend() {
  let timer = null;
  const root = path.join(ROOT, "backend", "app");
  // Windows reports a file that was only read (its access time) as a change, so a grep or an editor opening a
  // file would restart the services and interrupt the task running on them. Only a new modification time counts.
  const stamps = new Map();
  const stamp = (file) => {
    try {
      return fs.statSync(path.join(root, file)).mtimeMs;
    } catch {
      return -1; // deleted
    }
  };
  for (const entry of fs.readdirSync(root, { recursive: true })) {
    if (String(entry).endsWith(".py")) stamps.set(String(entry), stamp(String(entry)));
  }
  fs.watch(root, { recursive: true }, (_event, file) => {
    if (!file || !file.endsWith(".py")) return;
    const now = stamp(file);
    if (stamps.get(file) === now) return;
    stamps.set(file, now);
    clearTimeout(timer);
    timer = setTimeout(async () => {
      console.log(`[dev] ${file} changed: restarting the Python services`);
      await Promise.all(["api", "gateway", "worker"].map(stopService));
      startServiceSet();
    }, 400);
  });
}

/** API keys changed: the API and the worker read them at start. */
async function restartModelServices() {
  if (!services) return;
  Object.assign(services.env, {
    DEEPSEEK_API_KEY: getKey("deepseek"),
    OPENAI_API_KEY: getKey("openai"),
    ANTHROPIC_API_KEY: getKey("anthropic"),
  });
  await Promise.all(["api", "worker"].map(stopService));
  startService("api", ["api", "--port", String(services.apiPort)], apiEnv);
  startService("worker", ["worker"], services.env);
  await waitFor(`${services.origin}/api/health`);
}

/** A key saved from a phone arrives through the API that is about to restart: let it send its answer first. */
let restartTimer = null;
function restartModelServicesSoon() {
  clearTimeout(restartTimer);
  restartTimer = setTimeout(() => restartModelServices().catch(() => {}), 1500);
}

/** The phone was just set up: the services learn where adb is, and the phone starts. */
async function phoneSetUp() {
  await readAndroidStatus();
  if (services) {
    services.env.ADB_PATH = adbPath();
    await Promise.all(["api", "gateway", "worker"].map(stopService));
    startServiceSet();
    await waitFor(`${services.origin}/api/health`);
  }
  startEmulator();
}

// --- the window -----------------------------------------------------------------------------------------------

let mainWindow = null;

// The window is drawn in Relay's canvas colour, title bar included; the native buttons sit on top of it.
const CANVAS = { light: { color: "#E7E8EB", symbol: "#6C6F77" }, dark: { color: "#1E1F23", symbol: "#A0A3AB" } };
const canvas = () => CANVAS[nativeTheme.shouldUseDarkColors ? "dark" : "light"];
const titleBarOverlay = () => ({ color: canvas().color, symbolColor: canvas().symbol, height: 38 });

nativeTheme.on("updated", () => {
  if (!mainWindow || process.platform === "darwin") return;
  mainWindow.setTitleBarOverlay(titleBarOverlay());
  mainWindow.setBackgroundColor(canvas().color);
});

const home = () => DEV_URL || services?.origin || "";

/** A window rectangle moved and shrunk to lie wholly inside a screen's work area. */
function fitTo(area, { x, y, width, height }) {
  width = Math.min(width, area.width);
  height = Math.min(height, area.height);
  x = Math.min(Math.max(x, area.x), area.x + area.width - width);
  y = Math.min(Math.max(y, area.y), area.y + area.height - height);
  return { x, y, width, height };
}

/**
 * Where the window was last, if that place is still on a screen; otherwise centred on the main screen. Either way
 * no bigger than that screen: a size saved on a large screen would hang off a small or highly scaled one.
 * `size` is the size the user chose, kept even when this screen cannot show all of it.
 */
function savedBounds() {
  const saved = readConfig().window || {};
  const size = { width: Math.max(saved.width || 1400, 760), height: Math.max(saved.height || 900, 520) };
  const maximized = !!saved.maximized;
  if (Number.isFinite(saved.x) && Number.isFinite(saved.y)) {
    const area = screen.getDisplayMatching({ x: saved.x, y: saved.y, ...size }).workArea;
    const onScreen =
      saved.x < area.x + area.width - 100 && saved.x + size.width > area.x + 100 &&
      saved.y >= area.y - 8 && saved.y < area.y + area.height - 100;
    if (onScreen) return { bounds: fitTo(area, { x: saved.x, y: saved.y, ...size }), size, maximized };
  }
  const area = screen.getPrimaryDisplay().workArea;
  const centred = { x: area.x + Math.round((area.width - size.width) / 2), y: area.y + Math.round((area.height - size.height) / 2) };
  return { bounds: fitTo(area, { ...centred, ...size }), size, maximized };
}

/** Dragged onto a smaller screen, or its screen got smaller (scaling, resolution, unplugged): shrink to fit it. */
function keepOnScreen() {
  if (!mainWindow || mainWindow.isMaximized() || mainWindow.isMinimized() || mainWindow.isFullScreen()) return;
  const bounds = mainWindow.getBounds();
  const area = screen.getDisplayMatching(bounds).workArea;
  // A pixel of slack: fractional scaling rounds window sizes.
  if (bounds.width <= area.width + 1 && bounds.height <= area.height + 1) return;
  mainWindow.setBounds(fitTo(area, bounds));
}

// The window's size as the user last set it. Only their own resizing changes it: Windows at fractional scaling
// opens a window a few pixels off the size asked for, and saving that would grow the window on every launch.
let normalSize = [1400, 900];
function trackSize() {
  if (mainWindow && !mainWindow.isMaximized() && !mainWindow.isMinimized() && !mainWindow.isFullScreen()) {
    normalSize = mainWindow.getSize();
  }
}

function rememberBounds() {
  if (!mainWindow) return;
  const config = readConfig();
  const { x, y } = mainWindow.getNormalBounds();
  const [width, height] = normalSize;
  config.window = { x, y, width, height, maximized: mainWindow.isMaximized() };
  writeConfig(config);
}

/** The WebSocket origin voice mode streams the microphone to: OpenAI's Realtime API, beside the REST base the
 *  services use (backend/app/config.py openai_base_url; backend/app/api/voice.py realtime_url). */
function voiceOrigin() {
  try {
    const base = new URL(process.env.OPENAI_BASE_URL || "https://api.openai.com/v1");
    return `${base.protocol === "https:" ? "wss" : "ws"}://${base.host}`;
  } catch {
    return "wss://api.openai.com";
  }
}

/** The window may run only Relay's own code and talk only to Relay's own service, plus the voice stream. */
function applyContentPolicy() {
  const devServer = DEV_URL ? new URL(DEV_URL).origin : "";
  const policy = [
    "default-src 'self'",
    // blob: is the microphone's audio processor (frontend/src/voice/speech.ts capture), built in the page
    `script-src 'self' blob:${devServer ? " 'unsafe-inline'" : ""}`, // the dev server's hot-reload preamble is inline
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self' data:",
    `connect-src 'self' ${voiceOrigin()}${devServer ? ` ${devServer.replace(/^http/, "ws")}` : ""}`,
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'none'",
    "frame-ancestors 'none'",
  ].join("; ");
  session.defaultSession.webRequest.onHeadersReceived((details, callback) => {
    if (details.resourceType !== "mainFrame" || !details.url.startsWith("http://127.0.0.1:")) return callback({});
    callback({ responseHeaders: { ...details.responseHeaders, "Content-Security-Policy": [policy] } });
  });
}

/** Right-click: spelling, editing and copying, as in any native app. */
function contextMenu(window, params) {
  const items = [];
  for (const word of (params.misspelledWord && params.dictionarySuggestions.slice(0, 4)) || []) {
    items.push({ label: word, click: () => window.webContents.replaceMisspelling(word) });
  }
  if (items.length) items.push({ type: "separator" });
  const flags = params.editFlags;
  if (params.isEditable) {
    items.push(
      { role: "undo", enabled: flags.canUndo },
      { role: "redo", enabled: flags.canRedo },
      { type: "separator" },
      { role: "cut", enabled: flags.canCut },
      { role: "copy", enabled: flags.canCopy },
      { role: "paste", enabled: flags.canPaste },
      { type: "separator" },
      { role: "selectAll" },
    );
  } else if (params.selectionText.trim()) {
    items.push({ role: "copy" });
  }
  if (params.linkURL && /^https?:/.test(params.linkURL)) {
    if (items.length) items.push({ type: "separator" });
    items.push({ label: "Copy link", click: () => require("electron").clipboard.writeText(params.linkURL) });
  }
  if (DEV) {
    if (items.length) items.push({ type: "separator" });
    items.push({ label: "Inspect", click: () => window.webContents.inspectElement(params.x, params.y) });
  }
  if (items.length) Menu.buildFromTemplate(items).popup({ window });
}

/** Bring the window forward, restoring it first when closing (or the user) minimised it. */
function showMain() {
  if (!mainWindow) return;
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  mainWindow.focus();
}

let tray = null;

/** The notification-area icon that keeps penrose reachable while its window is hidden (macOS uses the Dock). */
function setupTray() {
  if (tray || process.platform === "darwin") return;
  const file = DEV ? path.join(__dirname, "..", "build", "icon-win.png") : resource("tray.png");
  const image = nativeImage.createFromPath(file).resize({ width: 32, height: 32 });
  if (image.isEmpty()) return;
  tray = new Tray(image);
  tray.setToolTip("penrose");
  tray.setContextMenu(
    Menu.buildFromTemplate([
      { label: "Open penrose", click: showMain },
      ...(DEV ? [] : [{
        label: "Start when I log in",
        type: "checkbox",
        checked: autostart.isOn(app),
        click: (item) => setStartAtLogin(item.checked),
      }]),
      { type: "separator" },
      { label: "Quit penrose", click: () => app.quit() },
    ]),
  );
  tray.on("click", showMain);
}

// --- start at login -----------------------------------------------------------------------------------------
// penrose starts by itself when the user logs in, so after a reboot or a new boot it is already running (its
// phone and its watches with it). The packaged app turns this on the first time it runs on a computer; after
// that the user's choice (the tray menu, Task Manager's Startup apps, the Linux autostart folder) is kept.
// The Windows installer writes the entry too (desktop/build/installer.nsh); see autostart.js.

function setStartAtLogin(on) {
  try {
    autostart.set(app, on);
    writeConfig({ ...readConfig(), startAtLogin: on });
  } catch (error) {
    console.error("Could not change start at login:", error);
  }
}

function setupAutostart() {
  if (DEV) return;
  try {
    if (readConfig().startAtLogin === undefined) setStartAtLogin(true);
    else autostart.refresh(app);
  } catch (error) {
    console.error("Could not set up start at login:", error);
  }
}

// A login start keeps the window closed, in the tray, where penrose waits like it does after its window is
// closed. Without a tray (macOS has the Dock instead; some Linux desktops show none) the window opens as usual.
const LOGIN_START = !DEV && autostart.startedAtLogin(app);

let loginWindowShown = false; // only the first window of a login start stays hidden

function openMain() {
  if (mainWindow) return showMain();
  nativeTheme.themeSource = readConfig().theme || "system";
  const { bounds, size, maximized } = savedBounds();
  mainWindow = new BrowserWindow({
    ...bounds,
    minWidth: 760,
    minHeight: 520,
    title: "penrose",
    show: false, // shown once painted, so it never flashes white
    backgroundColor: canvas().color,
    titleBarStyle: "hidden",
    ...(process.platform === "darwin" ? { trafficLightPosition: { x: 14, y: 12 } } : { titleBarOverlay: titleBarOverlay() }),
    ...(DEV ? { icon: path.join(__dirname, "..", "build", "icon-win.png") } : {}),
    webPreferences: { preload: path.join(__dirname, "preload.js"), contextIsolation: true, sandbox: true, spellcheck: true },
  });
  const startHidden = LOGIN_START && !loginWindowShown && Boolean(tray);
  loginWindowShown = true;
  mainWindow.once("ready-to-show", () => {
    if (maximized) mainWindow?.maximize();
    if (!startHidden) mainWindow?.show();
  });
  normalSize = [size.width, size.height];
  mainWindow.on("resized", trackSize); // the end of a user's resize
  mainWindow.on("moved", keepOnScreen);
  screen.on("display-metrics-changed", keepOnScreen);
  screen.on("display-removed", keepOnScreen);
  // Closing the window hides it and leaves penrose (and the phone) running, like Slack: the tray icon (Windows,
  // Linux) or the Dock icon (macOS) brings it back. Quit, updates and reset set `quitting` through before-quit
  // first, so they still close it for real.
  mainWindow.on("close", (event) => {
    if (quitting) return;
    event.preventDefault();
    rememberBounds();
    if (mainWindow.isFullScreen()) {
      mainWindow.once("leave-full-screen", () => mainWindow?.hide());
      mainWindow.setFullScreen(false);
    } else {
      mainWindow.hide();
    }
  });
  mainWindow.webContents.on("context-menu", (_event, params) => contextMenu(mainWindow, params));
  // Links leave the app for the default browser; the window never navigates away from Relay.
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:/.test(url)) shell.openExternal(url);
    return { action: "deny" };
  });
  mainWindow.webContents.on("will-navigate", (event, url) => {
    if (!home() || !url.startsWith(home())) {
      event.preventDefault();
      if (/^https?:/.test(url)) shell.openExternal(url);
    }
  });
  // Until the services are up, a quiet boot screen in the same colours as the app.
  if (services) mainWindow.loadURL(home());
  else mainWindow.loadFile(path.join(__dirname, "boot.html"));
  mainWindow.on("closed", () => {
    screen.off("display-metrics-changed", keepOnScreen);
    screen.off("display-removed", keepOnScreen);
    mainWindow = null;
  });
}

/** Delete the phone and make a fresh one from the setup image (asks first). Relay restarts. */
async function resetPhone(window) {
  const { response } = await dialog.showMessageBox(window, {
    type: "warning",
    buttons: ["Reset phone", "Cancel"],
    defaultId: 1,
    cancelId: 1,
    noLink: true,
    message: "Reset the phone?",
    detail: "Its apps, sign-ins and data are erased, and a fresh phone is made from the setup image. penrose restarts.",
  });
  if (response !== 0) return false;
  await performReset();
  return true;
}

async function performReset() {
  await stopEmulator();
  if (KEEP_PHONE && androidStatus?.adb) {
    await new Promise((resolve) => execFile(androidStatus.adb, ["-s", SERIAL, "emu", "kill"], { windowsHide: true }, resolve));
    await new Promise((resolve) => setTimeout(resolve, 5000));
  }
  fs.rmSync(path.join(ANDROID_HOME_DIR, "avd"), { recursive: true, force: true });
  relaunch();
  app.quit();
}

/** Starts penrose again once this process exits. An AppImage runs from a mount that goes away with this process,
 *  so it restarts from the AppImage file itself. */
function relaunch() {
  app.relaunch(process.env.APPIMAGE ? { execPath: process.env.APPIMAGE, args: process.argv.slice(1) } : undefined);
}

// --- updates ------------------------------------------------------------------------------------------------
// electron-updater against the `publish` target in package.json (app-update.yml in the packaged app). An update
// is the whole installer, so the services, the UI and the phone-side assets come with it; the provisioner's
// post-boot check then updates the phone itself from those assets. Checked after launch and every six hours,
// downloaded in the background, installed on the restart the user accepts or on the next quit.
//
// A Mac build without a Developer ID (scripts/dist.js) cannot do that: macOS installs nothing into an app that is
// not signed by one. It checks the same way, then says a newer version exists and opens the download page.

const { autoUpdater } = require("electron-updater");
const { relayManualUpdates: MANUAL_UPDATES = false, relayDownloadPage: DOWNLOAD_PAGE = "" } = require("../package.json");
let updateReady = null; // the version downloaded and waiting for a restart
let updateOffered = null; // the version whose download was last offered, so the six-hourly check asks once

function setupUpdates() {
  if (DEV) return;
  const out = logStream("updates");
  const line = (level) => (...args) => out.write(`${new Date().toISOString()} ${level} ${args.join(" ")}\n`);
  autoUpdater.logger = { info: line("info"), warn: line("warn"), error: line("error"), debug: () => {} };
  autoUpdater.autoDownload = !MANUAL_UPDATES;
  autoUpdater.autoInstallOnAppQuit = !MANUAL_UPDATES;
  autoUpdater.on("update-downloaded", (info) => {
    updateReady = info.version;
    offerRestart(info.version);
  });
  setTimeout(() => checkForUpdates(false), 15_000);
  setInterval(() => checkForUpdates(false), 6 * 60 * 60 * 1000);
}

/** Check now; `manual` (the menu) also reports "up to date" and failures, which the timer keeps to the log. */
async function checkForUpdates(manual) {
  const tell = (message, detail, type = "info") =>
    manual && dialog.showMessageBox(mainWindow, { type, buttons: ["OK"], noLink: true, message, detail });
  if (DEV) return tell("Updates come with the installed app", `This is a development run (build ${BUILD_ID}).`);
  if (updateReady) return manual && offerRestart(updateReady);
  try {
    const result = await autoUpdater.checkForUpdates();
    if (MANUAL_UPDATES && result?.isUpdateAvailable) {
      if (manual || updateOffered !== result.updateInfo.version) offerDownload(result.updateInfo.version);
    } else if (result?.downloadPromise) tell(`Downloading penrose ${result.updateInfo.version}`, "You will be asked to restart when it is ready.");
    else tell("penrose is up to date", `Version ${app.getVersion()}, build ${BUILD_ID}.`);
  } catch (error) {
    tell("Could not check for updates", String(error.message || error), "warning");
  }
}

/** An update this copy cannot install itself (an unsigned Mac build): say so, and open the download page. */
async function offerDownload(version) {
  updateOffered = version;
  const { response } = await dialog.showMessageBox(mainWindow, {
    type: "info",
    buttons: ["Download", "Later"],
    defaultId: 0,
    cancelId: 1,
    noLink: true,
    message: `penrose ${version} is available`,
    detail: "Download it and drag it into Applications, replacing this copy. Your phone and your chats are kept.",
  });
  if (response === 0 && /^https:/.test(DOWNLOAD_PAGE)) shell.openExternal(DOWNLOAD_PAGE);
}

async function offerRestart(version) {
  const { response } = await dialog.showMessageBox(mainWindow, {
    type: "info",
    buttons: ["Restart now", "Later"],
    defaultId: 0,
    cancelId: 1,
    noLink: true,
    message: `penrose ${version} is ready to install`,
    detail: "penrose restarts to update. If the phone needs updating too, that happens as it starts, in about a minute. " +
      "\"Later\" installs it when you next quit penrose.",
  });
  if (response !== 0) return;
  // Our own shutdown first (the phone's disk stays consistent), then the installer takes over.
  quitting = true;
  rememberBounds();
  await stopEmulator();
  for (const child of children.values()) child.kill();
  autoUpdater.quitAndInstall(false, true);
}

// --- usage count --------------------------------------------------------------------------------------------
// Once a day an installed copy tells the usage-count worker (stats/src/index.ts, `relayStatsUrl` in package.json)
// that it ran: a random ID made on its first start (`installId` in config.json), the version and the OS. Nothing
// else, and nothing at all when the user turns it off in Settings (`usageCount: false`), DO_NOT_TRACK is set, or
// this is a development run. The worker keeps no IP address. Checked hourly, sent once per UTC day.

const { relayStatsUrl: STATS_URL = "" } = require("../package.json");
const DO_NOT_TRACK = /^(1|true|yes)$/i.test(process.env.DO_NOT_TRACK || "");

const usageCountOn = () => readConfig().usageCount !== false;

function sendUsagePing() {
  if (DEV || DO_NOT_TRACK || !/^https:\/\//.test(STATS_URL) || !usageCountOn()) return;
  const today = new Date().toISOString().slice(0, 10);
  let config = readConfig();
  if (config.usagePingDay === today) return;
  if (!config.installId) {
    config = { ...config, installId: crypto.randomUUID() };
    writeConfig(config);
  }
  const body = JSON.stringify({ id: config.installId, version: app.getVersion(), os: process.platform });
  electronNet
    .fetch(STATS_URL, { method: "POST", headers: { "Content-Type": "application/json" }, body })
    .then((response) => response.ok && writeConfig({ ...readConfig(), usagePingDay: today }))
    .catch(() => {}); // offline: the next hourly check tries again
}

function setupUsageCount() {
  if (DEV) return;
  setTimeout(sendUsagePing, 60_000);
  setInterval(sendUsagePing, 60 * 60 * 1000);
}

ipcMain.handle("usage:get", () => !DO_NOT_TRACK && usageCountOn());
ipcMain.handle("usage:set", (_event, on) => {
  if (typeof on !== "boolean") return;
  writeConfig({ ...readConfig(), usageCount: on });
  if (on) sendUsagePing();
});

function showAbout() {
  dialog.showMessageBox(mainWindow, {
    type: "info", buttons: ["OK"], noLink: true, message: `penrose ${app.getVersion()}`, detail: `Build ${BUILD_ID}`,
  });
}

function buildMenu() {
  const template = [
    ...(process.platform === "darwin" ? [{ role: "appMenu" }] : []),
    {
      label: "penrose",
      submenu: [
        { label: "AI models…", click: () => mainWindow?.webContents.send("app:open", "models") },
        ...(DEV ? [{ label: "Open logs folder", click: () => shell.openPath(LOGS) }] : []),
        { type: "separator" },
        { label: "Check for updates…", click: () => checkForUpdates(true) },
        ...(process.platform === "darwin" ? [] : [{ label: "About penrose", click: showAbout }]),
        { type: "separator" },
        { label: "Reset phone…", click: () => resetPhone(mainWindow) },
        { type: "separator" },
        { role: "quit" },
      ],
    },
    { role: "editMenu" },
    DEV
      ? { role: "viewMenu" }
      : {
          label: "View",
          submenu: [{ role: "resetZoom" }, { role: "zoomIn" }, { role: "zoomOut" }, { type: "separator" }, { role: "togglefullscreen" }],
        },
    { role: "windowMenu" },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

// --- the flow: window -> services (+ phone) -> the app, which asks for whatever setup is missing ---------------

async function launch() {
  openMain(); // at once, with its boot screen, while the rest starts
  // The phone check (a Python process) and the services start together: the services need only adb's path.
  // The phone starts only once the check finds it exists; otherwise the app sets it up first.
  statusRead = readAndroidStatus().catch((error) => console.error("Could not read the phone's status:", error));
  statusRead.then(() => {
    startEmulator();
    resumeSetup();
  });
  try {
    await startServices();
  } catch (error) {
    await dialog.showMessageBox(mainWindow, {
      type: "error",
      buttons: ["Quit"],
      defaultId: 0,
      noLink: true,
      message: "penrose could not start",
      detail: String(error.message || error),
    });
    return app.quit();
  }
  mainWindow?.loadURL(home());
}

// --- the window's API (preload.js) -------------------------------------------------------------------------

/** Check a key against the provider's model list (free; no tokens are used). */
async function checkKey(provider, key) {
  if (DEV && process.env.RELAY_DEV_SKIP_KEY_CHECK === "1") return { ok: true }; // test runs with dummy keys
  try {
    const [url, headers] =
      provider === "anthropic"
        ? ["https://api.anthropic.com/v1/models?limit=1", { "x-api-key": key, "anthropic-version": "2023-06-01" }]
        : [
            provider === "openai" ? "https://api.openai.com/v1/models" : "https://api.deepseek.com/models",
            { Authorization: `Bearer ${key}` },
          ];
    const response = await fetch(url, { headers });
    if (response.ok) return { ok: true };
    if (response.status === 401 || response.status === 403) return { ok: false, error: "This key was not accepted." };
    return { ok: false, error: `The provider answered with HTTP ${response.status}. Try again shortly.` };
  } catch {
    return { ok: false, error: "Could not reach the provider. Check the internet connection." };
  }
}

// "Relay on your phone": deploy the coordinator to the user's Cloudflare account (src/companion.js).
const companion = require("./companion")({
  DATA, DEV, ROOT, BUILD_ID, resource, log: (text) => logStream("app").write(`${new Date().toISOString()} ${text}
`),
});
companion.listen((event) => mainWindow?.webContents.send("companion:event", event));
ipcMain.handle("companion:setup", () => companion.setup());
ipcMain.handle("companion:state", () => companion.state());

/** Check a key with its provider and keep it (an empty key removes it). The services restart separately. */
async function applyKey(provider, key) {
  if (!PROVIDERS.includes(provider)) return { ok: false, error: "Unknown provider." };
  key = String(key || "").trim();
  if (key) {
    const check = await checkKey(provider, key);
    if (!check.ok) return check;
  }
  setKey(provider, key);
  return { ok: true };
}

ipcMain.handle("keys:get", () => maskedKeys());
ipcMain.handle("keys:save", async (_event, provider, key) => {
  const result = await applyKey(provider, key);
  if (result.ok) await restartModelServices();
  return result;
});
/** What the first run still needs, and whether the phone failed to come up. */
ipcMain.handle("setup:state", async () => {
  await statusRead; // the services can be up before the phone check is done
  return {
    keys: configuredProviders().length > 0,
    phone: Boolean(androidStatus?.phone_ready),
    phoneError,
    phoneStep,
  };
});
ipcMain.handle("android:info", async () => {
  const found = existingSdk();
  return {
    existingSdk: found,
    useExisting: readConfig().sdk ? readConfig().sdk === found : Boolean(found),
    freeGb: freeSpaceGb(DATA),
  };
});
ipcMain.handle("android:setup", (_event, options) => {
  const sdk = options?.useExisting && existingSdk() ? existingSdk() : path.join(ANDROID_HOME_DIR, "sdk");
  return runSetup(sdk, Boolean(options?.acceptLicenses), Boolean(options?.startOver));
});
/** The user asks the setup doctor; `messages` is the conversation so far, kept by the window only. */
ipcMain.handle("setup:ask", async (_event, messages) => {
  const kept = (Array.isArray(messages) ? messages : []).slice(-8);
  return (await askDoctor("question", kept)) || { reply: "I couldn’t look into it just now. Try again in a moment.", state: "working", action: "none" };
});
/** The setup run so far, for a window that opens (or reloads) while it runs, or after it ended. */
ipcMain.handle("android:progress", () => ({
  running: Boolean(setupRun),
  events: setupRun ? setupRun.marks : [],
  result: setupResult,
}));

// --- the phone setup ----------------------------------------------------------------------------------------
// One run at a time, owned here rather than by the window: it starts when the user accepts the licenses (the
// window then moves on to the API key while it runs) and again at launch when an earlier run did not finish,
// so the 3 GB download and the build overlap with whatever the user does meanwhile.

let setupRun = null; // { promise, marks: step and latest progress events, startedAt, lastEventAt } while it runs
let setupResult = null; // how the last run ended: { ok, error?, hint?, doctor? }
let autoRetries = 0; // runs the doctor started by itself this launch
const AUTO_RETRIES = 2;

function runSetup(sdk, acceptLicenses, startOver = false) {
  if (setupRun) return setupRun.promise;
  const config = readConfig();
  config.sdk = sdk;
  config.licensesAccepted = acceptLicenses;
  writeConfig(config);
  const args = ["setup", "--home", ANDROID_HOME_DIR, "--sdk", sdk, "--assets", androidAssets(), "--build-id", BUILD_ID];
  if (acceptLicenses) args.push("--accept-licenses");
  if (startOver) args.push("--start-over");
  const run = { marks: [], startedAt: Date.now(), lastEventAt: Date.now() };
  const send = (event) => mainWindow?.webContents.send("android:event", event);
  setupResult = null;
  run.promise = provision(args, (event) => {
    run.lastEventAt = Date.now();
    if (event.event === "log") return; // internal detail: kept out of the window
    // Replay needs every step but only the newest percent.
    if (event.event === "progress") run.marks = run.marks.filter((e) => e.event !== "progress");
    run.marks.push(event);
    send(event);
  })
    .then(async (last) => {
      if (last.event !== "done") return { ok: false, error: last.message, hint: last.hint };
      await phoneSetUp();
      return { ok: true };
    })
    .then(async (result) => {
      if (result.ok || result.hint === "licenses") return { result };
      // The doctor looks at what went wrong (the run still counts as going, so a Try again joins it). A plain
      // retry it may start by itself, a couple of times; anything else waits for the user, with the doctor's
      // words in place of the fixed hint.
      run.ended = true;
      setupResult = result;
      const doctor = await askDoctor("failed");
      if (doctor?.action === "try_again" && autoRetries < AUTO_RETRIES) {
        autoRetries += 1;
        send({ event: "retrying", reply: doctor.reply });
        setupRun = null;
        return { next: runSetup(sdk, acceptLicenses) }; // the retry reports its own end
      }
      return { result: { ...result, ...(doctor && { doctor }) } };
    })
    .then(({ result, next }) => {
      if (next) return next;
      setupRun = null;
      setupResult = result;
      send({ event: "finished", ...result });
      return result;
    });
  setupRun = run;
  return run.promise;
}

/** At launch: carry on a setup the user started on an earlier launch (licenses accepted, no phone yet). */
function resumeSetup() {
  const config = readConfig();
  if (androidStatus && !androidStatus.phone_ready && config.licensesAccepted && config.sdk) {
    runSetup(config.sdk, true);
  }
}
ipcMain.handle("open:logs", () => DEV && shell.openPath(LOGS));
// The Cowork skill the Connect Claude page exported (app/api/phone.py setup): shown in the file manager, for
// uploading in claude.ai. The path is fixed here, never taken from the window.
ipcMain.handle("reveal:claude-skill", () => {
  const zip = path.join(DATA, "claude", "android-phone.zip");
  if (fs.existsSync(zip)) shell.showItemInFolder(zip);
  return fs.existsSync(zip);
});
ipcMain.handle("theme:set", (_event, choice) => {
  if (!["system", "light", "dark"].includes(choice)) return;
  nativeTheme.themeSource = choice;
  const config = readConfig();
  config.theme = choice;
  writeConfig(config);
});
ipcMain.handle("phone:reset", (event) => resetPhone(BrowserWindow.fromWebContents(event.sender)));
/** A native confirmation: resolves true when the user chooses the action. */
ipcMain.handle("dialog:confirm", async (event, { message, detail, action }) => {
  const { response } = await dialog.showMessageBox(BrowserWindow.fromWebContents(event.sender), {
    type: "none",
    buttons: [String(action || "OK"), "Cancel"],
    defaultId: 1,
    cancelId: 1,
    noLink: true,
    message: String(message || ""),
    detail: detail ? String(detail) : undefined,
  });
  return response === 0;
});
/** A native popup menu of [{id, label}] or "-" items: resolves the chosen id, or null. */
ipcMain.handle(
  "menu:popup",
  (event, items) =>
    new Promise((resolve) => {
      const template = (Array.isArray(items) ? items : []).map((item) =>
        item === "-"
          ? { type: "separator" }
          : { label: String(item.label), enabled: item.enabled !== false, click: () => resolve(String(item.id)) },
      );
      Menu.buildFromTemplate(template).popup({
        window: BrowserWindow.fromWebContents(event.sender),
        callback: () => setTimeout(() => resolve(null), 150), // a click lands just after the menu closes
      });
    }),
);

// --- location -------------------------------------------------------------------------------------------------
// The window reads it like any browser (navigator.geolocation: Windows' location service, Core Location on a Mac),
// so the phone is where the user is. This only opens the OS's settings when location is off for Relay.

ipcMain.handle("open:location-settings", () =>
  shell.openExternal(
    process.platform === "darwin"
      ? "x-apple.systempreferences:com.apple.preference.security?Privacy_LocationServices"
      : "ms-settings:privacy-location",
  ),
);

function freeSpaceGb(dir) {
  try {
    const stat = fs.statfsSync(dir);
    return Math.round((stat.bavail * stat.bsize) / 1e9);
  } catch {
    return null;
  }
}

// --- lifecycle ----------------------------------------------------------------------------------------------

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", showMain);
  app.on("activate", showMain); // macOS: the dock icon
  app.whenReady().then(async () => {
    if (ASK_FOR_DATA) {
      const chosen = await datafolder.askDataFolder({ dialog, appData: app.getPath("appData"), defaultDir: DEFAULT_DATA });
      if (chosen) relaunch();
      app.exit(0);
      return;
    }
    app.setAboutPanelOptions({ applicationName: "penrose", applicationVersion: app.getVersion(), version: `build ${BUILD_ID}` });
    applyContentPolicy();
    buildMenu();
    setupAutostart();
    setupTray();
    setupUpdates();
    setupUsageCount();
    launch();
  });
  app.on("window-all-closed", () => app.quit());
  if (DEV) {
    // scripts/dev.js asks for a clean quit before restarting; Ctrl+C in its terminal does the same.
    process.on("message", (message) => message === "relay:quit" && app.quit());
    process.on("SIGINT", () => app.quit());
  }
  app.on("before-quit", (event) => {
    if (quitting) return;
    quitting = true;
    event.preventDefault();
    rememberBounds(); // app.exit() below skips the window's own close event
    (async () => {
      await stopEmulator(); // a clean shutdown keeps the phone's disk consistent
      for (const child of children.values()) child.kill();
      app.exit(0);
    })();
  });
}
