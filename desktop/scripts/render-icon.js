// Renders Relay's icons from one master image, build/logo.svg (made by scripts/make-logo.js):
// icon.png for macOS and Linux, icon-win.png for Windows, the web favicon and the phone app's icons.
// The mark is itself a rounded square, so macOS gets it at its icon-grid size and nothing boxes it in grey.
// Regenerate: npx electron scripts/render-icon.js build
const { app, BrowserWindow } = require("electron");
const fs = require("node:fs");
const path = require("node:path");

const out = process.argv[process.argv.length - 1];
const master = path.join(out, "logo.svg");
const square = path.join(out, "logo-square.svg"); // full bleed, for the phone app's icons
const small = path.join(out, "logo-small.svg"); // bigger tiles, heavier lattice: favicons
const smallSquare = path.join(out, "logo-small-square.svg"); // the same, full bleed: the launcher's crop is filled edge to edge
const pub = path.join(__dirname, "..", "..", "frontend", "public");

/** file: where to write. canvas: output size in px. body: how much of the canvas the mark fills. shadow: soft drop shadow. from: which master. */
const variants = [
  { file: path.join(out, "logo.png"), canvas: 1024, body: 1024 },
  { file: path.join(out, "icon.png"), canvas: 1024, body: 824, shadow: true }, // macOS icon grid: 824 tile, 100 margin
  { file: path.join(out, "icon-win.png"), canvas: 1024, body: 980 }, // Windows shows icons fuller
  { file: path.join(pub, "favicon.png"), canvas: 64, body: 64, from: small },
  { file: path.join(pub, "companion", "icon-192.png"), canvas: 192, body: 192, from: square },
  { file: path.join(pub, "companion", "icon-512.png"), canvas: 512, body: 512, from: square },
  { file: path.join(pub, "companion", "icon-maskable-192.png"), canvas: 192, body: 192, from: smallSquare },
  { file: path.join(pub, "companion", "icon-maskable-512.png"), canvas: 512, body: 512, from: smallSquare },
];

app.disableHardwareAcceleration();
app.on("window-all-closed", () => {});
app.whenReady().then(async () => {
  const win = new BrowserWindow({ show: false, width: 200, height: 200 });
  await win.loadURL("about:blank");
  for (const { file, canvas, body, shadow = false, from = master } of variants) {
    const src = `data:image/svg+xml;base64,${fs.readFileSync(from).toString("base64")}`;
    const png = await win.webContents.executeJavaScript(`new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => {
        const c = document.createElement("canvas"); c.width = c.height = ${canvas};
        const g = c.getContext("2d"); g.imageSmoothingQuality = "high";
        const o = (${canvas} - ${body}) / 2;
        if (${shadow}) { g.shadowColor = "rgba(0, 0, 0, 0.28)"; g.shadowBlur = 28; g.shadowOffsetY = 12; }
        g.drawImage(img, o, o, ${body}, ${body});
        resolve(c.toDataURL("image/png"));
      };
      img.onerror = () => reject(new Error("logo.svg failed to load"));
      img.src = ${JSON.stringify(src)};
    })`);
    fs.writeFileSync(file, Buffer.from(png.split(",")[1], "base64"));
    console.log(path.relative(process.cwd(), file), "ok");
  }
  win.destroy();
  app.quit();
});
