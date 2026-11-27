// Package the app: `node scripts/dist.js [--dir] [--publish]`, after `npm run prepare:all`.
//
// Every build is stamped with one build ID (scripts/release.js): the installer's version is derived from it,
// and the ID itself goes into the app's package.json as `relayBuildId`, where the main process reads it for
// About and hands it to the provisioner for the phone's record. package.json in the repository keeps its
// placeholder version; only the packaged app carries the stamped one.
//
// The app's content (its code, web apps and phone files) is staged into desktop/content, which the installer
// carries, and zipped into dist/content with its feed, which installed copies update from without an installer when
// the shell ID matches (scripts/content.js, scripts/shell-id.js).
//
// Updates: the app checks the `publish` target in package.json, this repository's GitHub releases.
// `RELAY_UPDATE_URL` switches to a plain HTTPS folder that serves the files electron-builder puts in dist
// (latest.yml, latest-mac.yml, latest-linux.yml, the installers) instead. `--publish` uploads there (GitHub needs
// GH_TOKEN); otherwise the installers stay in dist/, which is what the release workflow uploads
// (.github/workflows/release.yml).
//
// A Mac build without a Developer ID certificate (CSC_LINK or CSC_NAME) is signed ad hoc: Apple Silicon starts no
// app without a signature. macOS installs no update into such an app, so that build only tells the user a newer
// version exists and opens the download page (`relayManualUpdates`, read in main.js).
const { execFileSync } = require("child_process");
const fs = require("fs");
const path = require("path");
const { build } = require("electron-builder");
const { release } = require("./release");
const { checkPackage } = require("./check-package");
const { STAGE, stage, pack } = require("./content");
const { shellId } = require("./shell-id");
const { build: { productName, publish } } = require("../package.json");

async function main() {
  const { buildId, version } = release();
  const args = process.argv.slice(2);
  const feed = process.env.RELAY_UPDATE_URL
    ? process.env.RELAY_UPDATE_URL.replace(/\/?$/, "/")
    : `https://github.com/${publish.owner}/${publish.repo}/releases/latest/download/`;
  const content = stage({ buildId, version, shell: shellId() });
  // Before anything is packed: the bundled Python can load every module of the content it ships with.
  const backend = path.join(__dirname, "..", "..", "backend");
  const server = path.join(backend, "dist", "relay-server", process.platform === "win32" ? "relay-server.exe" : "relay-server");
  const scratch = path.join(backend, "build", "check-data");
  execFileSync(server, ["check-content", STAGE], { stdio: "inherit", env: { ...process.env, DATA_DIR: scratch } });
  fs.rmSync(scratch, { recursive: true, force: true });
  checkPackage(STAGE);
  const config = {
    extraMetadata: { version, relayBuildId: buildId, relayContentFeed: feed },
    // Before the installer is made: no developer files or keys in what ships.
    afterPack: async (context) => checkPackage(context.appOutDir),
  };
  // A list: it replaces package.json's `publish`, where an object would be merged into it and be neither.
  if (process.env.RELAY_UPDATE_URL) config.publish = [{ provider: "generic", url: process.env.RELAY_UPDATE_URL }];
  if (process.platform === "darwin" && !process.env.CSC_LINK && !process.env.CSC_NAME) {
    config.mac = { identity: "-", hardenedRuntime: false };
    config.extraMetadata.relayManualUpdates = true;
    config.extraMetadata.relayDownloadPage = `https://github.com/${publish.owner}/${publish.repo}/releases/latest`;
  }
  console.log(`${productName} build ${buildId} (version ${version}, shell ${content.shell})`);
  // Only this build's installers stay in dist/: earlier builds pile up at about 300 MB each. electron-builder names
  // them after productName ("<name> Setup <version>.exe", "<name>-<version>.dmg", ...); "Relay" is the name before.
  const dist = path.join(__dirname, "..", "dist");
  const prefixes = [productName, "Relay"].flatMap((name) => [`${name}-`, `${name} Setup `]);
  for (const name of fs.existsSync(dist) ? fs.readdirSync(dist) : [])
    if (prefixes.some((prefix) => name.startsWith(prefix))) fs.rmSync(path.join(dist, name), { force: true });
  const files = await build({
    config,
    dir: args.includes("--dir"),
    publish: args.includes("--publish") ? "always" : "never",
  });
  // The venv build-server.js made, for its Python.
  const python = path.join(backend, "build", "server-venv", ...(process.platform === "win32" ? ["Scripts", "python.exe"] : ["bin", "python"]));
  for (const file of [...files, ...pack(content, python)]) console.log(file);
}

main().catch((error) => {
  console.error(error.stack || error.message);
  process.exit(1);
});
