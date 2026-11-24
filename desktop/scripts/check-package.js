// Run after electron-builder packs the app (afterPack in dist.js): the app must start like a fresh copy, so it
// may carry no developer files and none of the developer's keys. Fails the build when it finds either.
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..", "..");
// Developer files that must never ship, by name, and folders that only tests use.
const FORBIDDEN_NAMES = [/^\.env(\..+)?$/, /^\.dev-login\.json$/, /\.sqlite3?$/, /\.db$/, /^config\.json$/];
const FORBIDDEN_DIRS = new Set(["tests", "__pycache__", ".pytest_cache", "node_modules/.cache", "data"]);
const SECRET_NAME = /KEY|TOKEN|SECRET|PASSWORD/i;

/** Values of secret-looking variables in the developer's own .env files: none may appear in the app. */
function localSecrets() {
  const files = [".env", "backend/.env", "frontend/.env", "desktop/.env.local"].map((f) => path.join(root, f));
  const values = new Set();
  for (const file of files) {
    if (!fs.existsSync(file)) continue;
    for (const line of fs.readFileSync(file, "utf8").split(/\r?\n/)) {
      const match = line.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*["']?([^"'#\s]+)/);
      if (match && SECRET_NAME.test(match[1]) && match[2].length >= 12) values.add(match[2]);
    }
  }
  return [...values];
}

function* walk(dir) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) yield* walk(full);
    else if (entry.isFile()) yield full;
  }
}

function checkPackage(appOutDir) {
  const problems = [];
  const secrets = localSecrets().map((value) => Buffer.from(value));
  for (const file of walk(appOutDir)) {
    const rel = path.relative(appOutDir, file).split(path.sep).join("/");
    const parts = rel.split("/");
    if (FORBIDDEN_NAMES.some((re) => re.test(parts.at(-1)))) problems.push(`developer file: ${rel}`);
    if (parts.slice(0, -1).some((part) => FORBIDDEN_DIRS.has(part))) problems.push(`test or dev folder: ${rel}`);
    if (secrets.length) {
      const bytes = fs.readFileSync(file);
      if (secrets.some((secret) => bytes.includes(secret))) problems.push(`contains a key from a local .env: ${rel}`);
    }
  }
  const unique = [...new Set(problems)];
  if (unique.length) throw new Error(`The packaged app is not clean:\n  ${unique.slice(0, 40).join("\n  ")}`);
  console.log(`Package check passed (${secrets.length} local key values looked for).`);
}

module.exports = { checkPackage };
if (require.main === module) checkPackage(path.resolve(process.argv[2] || path.join(__dirname, "..", "dist", "win-unpacked")));
