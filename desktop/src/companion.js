// Setting up "Relay on your phone" with one click: the coordinator (coordinator/) and the companion app are
// deployed to the user's own Cloudflare account, so nobody but the user runs anything. The user signs in to
// Cloudflare once in their browser (wrangler's OAuth flow, with the token kept in Relay's data dir); then
// wrangler, bundled with the app, deploys the Worker and the API is told the resulting workers.dev address
// (backend/app/companion). An update with a newer companion deploys again by itself.

const { shell } = require("electron");
const { spawn } = require("node:child_process");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const WORKER = "relay-companion"; // wrangler.toml's name: the address is https://relay-companion.<subdomain>.workers.dev
const SCOPES = ["account:read", "user:read", "workers:write", "workers_scripts:write"]; // wrangler adds offline_access itself
const API = "https://api.cloudflare.com/client/v4";
const LOGIN_MINUTES = 10;

module.exports = function companion({ DATA, DEV, source, assets, modules, version, log }) {
  const home = path.join(DATA, "coordinator"); // wrangler's project dir: its config and its scratch files
  const cloudflare = path.join(DATA, "cloudflare"); // XDG_CONFIG_HOME: the OAuth token lives in .wrangler/config
  const tokenFile = path.join(cloudflare, ".wrangler", "config", "default.toml");
  const deployedFile = path.join(home, "deployed"); // what was last deployed (version()) and its address

  const wrangler = () => path.join(modules(), "wrangler", "wrangler-dist", "cli.js");

  let run = null; // the setup under way: { step, log: [] }
  let onEvent = () => {};

  function step(text) {
    if (run) run.step = text;
    log?.(`companion: ${text}`);
    onEvent({ event: "step", text });
  }

  function deployed() {
    try {
      return JSON.parse(fs.readFileSync(deployedFile, "utf8"));
    } catch {
      return null;
    }
  }

  function token() {
    try {
      return /oauth_token\s*=\s*"([^"]+)"/.exec(fs.readFileSync(tokenFile, "utf8"))?.[1] || "";
    } catch {
      return "";
    }
  }

  /** Run wrangler with Electron's own Node, collecting its output; `onLine` sees each line as it comes. */
  function wranglerRun(args, { env = {}, onLine, minutes = 5 } = {}) {
    return new Promise((resolve, reject) => {
      fs.mkdirSync(home, { recursive: true });
      const child = spawn(process.execPath, [path.join(__dirname, "wrangler.js"), ...args], {
        cwd: home,
        windowsHide: true,
        env: {
          ...process.env,
          ELECTRON_RUN_AS_NODE: "1",
          RELAY_WRANGLER_CLI: wrangler(),
          XDG_CONFIG_HOME: cloudflare,
          WRANGLER_SEND_METRICS: "false",
          WRANGLER_LOG: "log",
          NO_COLOR: "1",
          CI: "1", // never wait on a question nobody can answer
          ...env,
        },
      });
      let out = "";
      let rest = "";
      const feed = (chunk) => {
        out += chunk;
        rest += chunk;
        const lines = rest.split(/\r?\n/);
        rest = lines.pop();
        for (const line of lines) onLine?.(line);
      };
      child.stdout.on("data", (d) => feed(String(d)));
      child.stderr.on("data", (d) => feed(String(d)));
      const timer = setTimeout(() => child.kill(), minutes * 60_000);
      child.on("error", reject);
      child.on("exit", (code) => {
        clearTimeout(timer);
        if (rest) onLine?.(rest);
        if (code === 0) resolve(out);
        else reject(new Error(lastError(out) || `wrangler ${args[0]} failed (${code})`));
      });
    });
  }

  function lastError(out) {
    const lines = out.split(/\r?\n/).map((l) => l.replace(/^\s*[✘X]\s*\[ERROR\]\s*/, "").trim()).filter(Boolean);
    const marked = lines.filter((l, i) => /\[ERROR\]/.test(out.split(/\r?\n/)[i] || ""));
    return (marked[0] || lines.at(-1) || "").slice(0, 300);
  }

  async function cloudflareApi(method, route, body) {
    const response = await fetch(API + route, {
      method,
      headers: { Authorization: `Bearer ${token()}`, "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await response.json().catch(() => ({}));
    return { ok: response.ok && data.success !== false, data };
  }

  /** The user's sign-in to Cloudflare, in their browser; nothing to do when the kept token still works. */
  async function signIn() {
    if (token()) {
      const check = await cloudflareApi("GET", "/user/tokens/verify");
      if (check.ok) return;
    }
    step("Sign in to Cloudflare in your browser");
    let opened = false;
    await wranglerRun(["login", "--browser=false", ...SCOPES.map((scope) => `--scopes=${scope}`)], {
      minutes: LOGIN_MINUTES,
      onLine: (line) => {
        const url = /(https:\/\/dash\.cloudflare\.com\/oauth2\/auth\S+)/.exec(line)?.[1];
        if (url && !opened) {
          opened = true;
          shell.openExternal(url);
          onEvent({ event: "login", url });
        }
      },
    });
    if (!token()) throw new Error("Cloudflare did not finish signing in.");
  }

  async function accountId() {
    const { ok, data } = await cloudflareApi("GET", "/accounts?per_page=50");
    const accounts = ok ? data.result || [] : [];
    if (!accounts.length) throw new Error("Your Cloudflare sign-in has no account to deploy to.");
    return accounts[0].id; // the account the user signed in as; the one they made with the sign-up, for nearly everyone
  }

  /** The account's workers.dev subdomain, registered on first use with a name of Relay's choosing. */
  async function subdomain(account) {
    const have = await cloudflareApi("GET", `/accounts/${account}/workers/subdomain`);
    if (have.ok && have.data.result?.subdomain) return have.data.result.subdomain;
    step("Choosing an address for your phone");
    for (let attempt = 0; attempt < 5; attempt++) {
      const name = `relay-${crypto.randomBytes(4).toString("hex")}`;
      const made = await cloudflareApi("PUT", `/accounts/${account}/workers/subdomain`, { subdomain: name });
      if (made.ok) return made.data.result?.subdomain || name;
    }
    throw new Error("Could not register a workers.dev address for your account.");
  }

  /** wrangler's project directory: the coordinator's config and source, copied where wrangler may write. */
  function stageProject() {
    fs.mkdirSync(path.join(home, "src"), { recursive: true });
    for (const name of fs.readdirSync(path.join(source(), "src")).filter((name) => name.endsWith(".ts"))) {
      fs.copyFileSync(path.join(source(), "src", name), path.join(home, "src", name));
    }
    const config = fs
      .readFileSync(path.join(source(), "wrangler.toml"), "utf8")
      .replace(/directory\s*=\s*"[^"]*"/, `directory = ${JSON.stringify(assets().replace(/\\/g, "/"))}`);
    fs.writeFileSync(path.join(home, "wrangler.toml"), config);
  }

  async function deploy(account) {
    if (!fs.existsSync(path.join(assets(), "index.html"))) {
      throw new Error(DEV ? "Build the companion app first: npm run build:companion in frontend/." : "The companion app is missing from this build.");
    }
    stageProject();
    step("Deploying to your Cloudflare account");
    await wranglerRun(["deploy"], { env: { CLOUDFLARE_ACCOUNT_ID: account } });
  }

  /** A new workers.dev subdomain answers with TLS failures until Cloudflare has issued its certificate, which
   *  takes a few minutes; the address is handed over only once it answers. */
  async function waitForAddress(origin) {
    step("Waiting for Cloudflare to finish setting up the address (a few minutes the first time)");
    for (let attempt = 0; attempt < 60; attempt++) {
      try {
        if ((await fetch(`${origin}/health`, { signal: AbortSignal.timeout(10_000) })).ok) return;
      } catch {}
      await new Promise((r) => setTimeout(r, 10_000));
    }
    throw new Error(`${origin} is not answering yet. Try again in a few minutes.`);
  }

  /** Everything, start to finish; resolves with the companion's address. */
  async function setup() {
    if (run) return run.promise;
    run = { step: "", promise: null };
    run.promise = (async () => {
      try {
        await signIn();
        const account = await accountId();
        const sub = await subdomain(account);
        await deploy(account);
        const origin = `https://${WORKER}.${sub}.workers.dev`;
        await waitForAddress(origin);
        fs.writeFileSync(deployedFile, JSON.stringify({ version: version(), origin, account, at: Date.now() }));
        onEvent({ event: "done", origin });
        return { ok: true, origin };
      } catch (error) {
        const message = error.message || String(error);
        log?.(`companion: setup failed: ${message}`);
        onEvent({ event: "error", message });
        return { ok: false, error: message };
      } finally {
        run = null;
      }
    })();
    return run.promise;
  }

  /** An update shipped a newer companion: deploy it again, quietly, with the sign-in already kept. Never signs
   *  in (that opens the browser, which only the user's own click may do): wrangler renews the kept sign-in itself,
   *  and where it cannot, the next start tries again. */
  async function refresh() {
    const last = deployed();
    if (DEV || run || !last?.account || last.version === version() || !token()) return;
    run = { step: "", promise: null };
    run.promise = (async () => {
      try {
        await deploy(last.account);
        fs.writeFileSync(deployedFile, JSON.stringify({ ...last, version: version(), at: Date.now() }));
        onEvent({ event: "done", origin: last.origin });
        return { ok: true, origin: last.origin };
      } catch (error) {
        const message = error.message || String(error);
        log?.(`companion: could not redeploy after the update: ${message}`);
        onEvent({ event: "error", message });
        return { ok: false, error: message };
      } finally {
        run = null;
      }
    })();
    await run.promise;
  }

  return {
    setup,
    refresh,
    state: () => ({ running: Boolean(run), step: run?.step || "", deployed: deployed() }),
    listen: (callback) => (onEvent = callback),
  };
};
