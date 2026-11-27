// The shell ID: a hash of everything only an installer can change (the Electron app, the installer itself, the
// Python runtime and its libraries, bundled programs). It is stamped into the packaged app and into each release's
// content (scripts/content.js). An installed app takes a release's content on its own when the two IDs match, and
// asks for the new installer when they differ (src/content.js). Nothing is bumped by hand: change one of these
// files and the ID changes.
//
// Everything else ships as content: backend/app, the agents' prompts and skills, the two web apps, the
// coordinator's source and the phone-side files in assets/android.
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..", "..");

// Files and folders, from the repository root.
const SHELL = [
  "desktop/src",
  "desktop/stubs",
  "desktop/build",
  "desktop/package.json",
  "desktop/package-lock.json",
  "desktop/scripts/build-server.js",
  "desktop/scripts/check-package.js",
  "desktop/scripts/content.js",
  "desktop/scripts/dist.js",
  "desktop/scripts/fetch-vendor.js",
  "desktop/scripts/shell-id.js",
  "backend/relay_server.py",
  "backend/server_build.py",
  "backend/requirements.txt",
  "coordinator/package-lock.json",
];

function* files(relative) {
  const full = path.join(root, relative);
  if (!fs.statSync(full).isDirectory()) return yield relative;
  for (const name of fs.readdirSync(full).sort()) {
    if (name !== ".DS_Store") yield* files(`${relative}/${name}`);
  }
}

/** A file's bytes, text with Unix line endings: a Windows checkout must give the same ID as a Linux one. */
function normalised(relative) {
  const bytes = fs.readFileSync(path.join(root, relative));
  return bytes.includes(0) ? bytes : Buffer.from(bytes.toString("latin1").replace(/\r\n/g, "\n"), "latin1");
}

/** The Python the release workflow bundles: a new one is a new runtime. */
function pythonVersion() {
  const workflow = fs.readFileSync(path.join(root, ".github", "workflows", "release.yml"), "utf8");
  return /python-version:\s*["']?([\d.]+)/.exec(workflow)?.[1] || "";
}

function shellId() {
  const hash = crypto.createHash("sha256").update(`python ${pythonVersion()}\n`);
  for (const entry of SHELL) {
    for (const file of files(entry)) {
      const bytes = normalised(file);
      hash.update(`${file}\n${bytes.length}\n`).update(bytes);
    }
  }
  return hash.digest("hex").slice(0, 16);
}

module.exports = { SHELL, shellId };

if (require.main === module) console.log(shellId());
