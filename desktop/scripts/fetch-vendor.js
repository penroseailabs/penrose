// Download the platform's apkeep (direct APK installs without a Google sign-in) into vendor/bin.
const fs = require("node:fs");
const path = require("node:path");

const VERSION = "1.0.0";
const TARGETS = {
  "win32-x64": "apkeep-x86_64-pc-windows-msvc.exe",
  "darwin-arm64": "apkeep-aarch64-apple-darwin",
  "darwin-x64": "apkeep-x86_64-apple-darwin",
  "linux-x64": "apkeep-x86_64-unknown-linux-gnu",
  "linux-arm64": "apkeep-aarch64-unknown-linux-gnu",
};

async function main() {
  const asset = TARGETS[`${process.platform}-${process.arch}`];
  const bin = path.join(__dirname, "..", "vendor", "bin");
  fs.mkdirSync(bin, { recursive: true });
  if (!asset) {
    console.warn(`No apkeep build for ${process.platform}-${process.arch}; install_app will fall back to Play Store.`);
    return;
  }
  const dest = path.join(bin, process.platform === "win32" ? "apkeep.exe" : "apkeep");
  if (fs.existsSync(dest)) return console.log(`apkeep present: ${dest}`);
  const url = `https://github.com/EFForg/apkeep/releases/download/${VERSION}/${asset}`;
  console.log(`Downloading ${url}`);
  const response = await fetch(url);
  if (!response.ok) {
    // Upstream publishes no macOS builds; install_app falls back to Play Store there.
    if (process.platform === "darwin") return console.warn(`No apkeep for macOS (HTTP ${response.status}); skipping.`);
    throw new Error(`apkeep download failed: HTTP ${response.status}`);
  }
  fs.writeFileSync(dest, Buffer.from(await response.arrayBuffer()), { mode: 0o755 });
  console.log(`apkeep saved: ${dest}`);
}

main().catch((error) => {
  console.error(error.message);
  process.exit(1);
});
