// The app's content: everything an installed copy can replace without a new installer (scripts/shell-id.js says
// what it cannot). `stage` gathers it from the repository into desktop/content, which the installer ships as
// resources/content; `pack` turns that folder into the release's two update files, which an installed copy reads
// (src/content.js):
//
//   dist/content/penrose-content-<version>.pack   the folder, zipped
//   dist/content/content-latest.json              what it is: build, version, shell ID, size and SHA-512
//
// Run on its own after `npm run build:frontend`: `node scripts/content.js` (stages only).
const { execFileSync } = require("node:child_process");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const desktop = path.resolve(__dirname, "..");
const root = path.join(desktop, "..");
const STAGE = path.join(desktop, "content");
const FEED = "content-latest.json";

// [from the repository root, to the content root]
const PARTS = [
  ["backend/app", "backend/app"],
  ["backend/agent_prompts", "backend/agent_prompts"],
  ["backend/skills", "backend/skills"],
  ["frontend/dist", "frontend"],
  ["frontend/dist-companion", "companion"],
  ["coordinator/src", "coordinator/src"],
  ["coordinator/wrangler.toml", "coordinator/wrangler.toml"],
  ["desktop/assets/android", "android"],
];

const wanted = (file) => !/(^|[\\/])(__pycache__|\.pytest_cache|\.DS_Store)$|\.py[co]$/.test(file);

/** A hash of some folders of the staged content: the same files give the same hash. */
function hashOf(...folders) {
  const hash = crypto.createHash("sha256");
  const walk = (relative) => {
    const full = path.join(STAGE, relative);
    if (!fs.statSync(full).isDirectory()) return void hash.update(`${relative}\n`).update(fs.readFileSync(full));
    for (const name of fs.readdirSync(full).sort()) walk(`${relative}/${name}`);
  };
  folders.forEach(walk);
  return hash.digest("hex").slice(0, 16);
}

/** Gathers desktop/content and writes its content.json. */
function stage({ buildId, version, shell }) {
  fs.rmSync(STAGE, { recursive: true, force: true });
  for (const [from, to] of PARTS) {
    const source = path.join(root, from);
    if (!fs.existsSync(source)) throw new Error(`Nothing at ${from}: run \`npm run build:frontend\` first.`);
    fs.cpSync(source, path.join(STAGE, to), { recursive: true, filter: wanted });
  }
  const info = { build: buildId, version, shell, companion: hashOf("companion", "coordinator") };
  fs.writeFileSync(path.join(STAGE, "content.json"), JSON.stringify(info, null, 2) + "\n");
  return info;
}

/** Zips the staged content into `out` and writes the feed beside it. `python` is any Python 3. */
function pack(info, python, out = path.join(desktop, "dist", "content")) {
  fs.rmSync(out, { recursive: true, force: true });
  fs.mkdirSync(out, { recursive: true });
  const file = `penrose-content-${info.version}.pack`;
  const zip = path.join(out, file);
  execFileSync(python, ["-c",
    "import sys, zipfile, pathlib\n" +
    "root = pathlib.Path(sys.argv[1])\n" +
    "with zipfile.ZipFile(sys.argv[2], 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as z:\n" +
    "    for p in sorted(root.rglob('*')):\n" +
    "        if p.is_file(): z.write(p, p.relative_to(root).as_posix())\n",
    STAGE, zip], { stdio: "inherit" });
  const bytes = fs.readFileSync(zip);
  const feed = { build: info.build, version: info.version, shell: info.shell, file, size: bytes.length,
    sha512: crypto.createHash("sha512").update(bytes).digest("base64") };
  fs.writeFileSync(path.join(out, FEED), JSON.stringify(feed, null, 2) + "\n");
  return [zip, path.join(out, FEED)];
}

module.exports = { FEED, STAGE, stage, pack };

if (require.main === module) {
  const { release } = require("./release");
  const { shellId } = require("./shell-id");
  console.log(stage({ ...release(), shell: shellId() }));
}
