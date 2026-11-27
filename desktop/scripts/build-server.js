// Bundle the Python services into backend/dist/relay-server (one folder) with PyInstaller.
// Its Python comes from the repository's .venv, or RELAY_PYTHON. Run on each OS you ship for.
const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..", "..");
const backend = path.join(root, "backend");
const base =
  process.env.RELAY_PYTHON ||
  (process.platform === "win32"
    ? path.join(root, ".venv", "Scripts", "python.exe")
    : path.join(root, ".venv", "bin", "python"));
if (!fs.existsSync(base)) {
  console.error(`No Python at ${base}. Create .venv (see README) or set RELAY_PYTHON.`);
  process.exit(1);
}
// A clean venv with only backend/requirements.txt, so nothing a developer installed (test tools, old
// experiments) can be pulled into the bundle by a PyInstaller hook.
const venv = path.join(backend, "build", "server-venv");
const python = process.platform === "win32" ? path.join(venv, "Scripts", "python.exe") : path.join(venv, "bin", "python");
if (!fs.existsSync(python)) execFileSync(base, ["-m", "venv", venv], { stdio: "inherit" });
const run = (args) => execFileSync(python, args, { cwd: backend, stdio: "inherit" });

run(["-m", "pip", "install", "--quiet", "-r", "requirements.txt", "pyinstaller"]);
// Python and the libraries only: penrose's own code ships beside it as content (backend/server_build.py).
run(["server_build.py"]);
console.log(`Built ${path.join(backend, "dist", "relay-server")}`);
