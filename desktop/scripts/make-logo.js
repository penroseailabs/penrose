// Draws Relay's mark, build/logo.svg (and the web favicon.svg): a Penrose rhomb tiling in graphite, pencil-lead greys
// with a cold cast, lit to silver at the heart and falling away to dark at the rim.
// The tiling comes from splitting a ten-triangle "sun" into Robinson triangles; each pair of triangles is one rhomb.
// Regenerate: node scripts/make-logo.js build   (then npx electron scripts/render-icon.js build for the PNGs)
const fs = require("node:fs");
const path = require("node:path");

const out = process.argv[process.argv.length - 1];
const SIZE = 1024;
const RADIUS = 228; // corner radius of the rounded square, the macOS icon shape at this size
const PHI = (1 + Math.sqrt(5)) / 2;

// Graphite shades, dark lead to the silver at the heart. The dark middle stops are near the app's --accent.
const GROUT = "#0b0d10";
const STOPS = ["#121418", "#1b1f25", "#272c34", "#363d47", "#4f5865", "#7a8594", "#c4ccd6"];

const add = (a, b) => [a[0] + b[0], a[1] + b[1]];
const sub = (a, b) => [a[0] - b[0], a[1] - b[1]];
const mul = (a, k) => [a[0] * k, a[1] * k];

/** The tiling's triangles, split `generations` times (more splits, smaller tiles). */
function tiling(generations) {
  // The sun: ten triangles around the centre, large enough to cover the square's corners.
  const c = [SIZE / 2, SIZE / 2];
  const sun = SIZE * 0.78;
  let tris = [];
  for (let i = 0; i < 10; i++) {
    let b = add(c, mul([Math.cos(((2 * i - 1) * Math.PI) / 10), Math.sin(((2 * i - 1) * Math.PI) / 10)], sun));
    let d = add(c, mul([Math.cos(((2 * i + 1) * Math.PI) / 10), Math.sin(((2 * i + 1) * Math.PI) / 10)], sun));
    if (i % 2 === 0) [b, d] = [d, b];
    tris.push({ thin: true, a: c, b, c: d });
  }
  // Split every triangle into smaller ones of the same two kinds (thin and thick rhomb halves).
  for (let g = 0; g < generations; g++) {
    const next = [];
    for (const t of tris) {
      if (t.thin) {
        const p = add(t.a, mul(sub(t.b, t.a), 1 / PHI));
        next.push({ thin: true, a: t.c, b: p, c: t.b }, { thin: false, a: p, b: t.c, c: t.a });
      } else {
        const q = add(t.b, mul(sub(t.a, t.b), 1 / PHI));
        const r = add(t.b, mul(sub(t.c, t.b), 1 / PHI));
        next.push({ thin: false, a: r, b: t.c, c: t.a }, { thin: false, a: q, b: r, c: t.b }, { thin: true, a: r, b: q, c: t.a });
      }
    }
    tris = next;
  }
  return tris;
}

const hex = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
const shade = (t) => {
  const x = Math.min(Math.max(t, 0), 1) * (STOPS.length - 1);
  const i = Math.min(Math.floor(x), STOPS.length - 2);
  const [p, q] = [hex(STOPS[i]), hex(STOPS[i + 1])];
  return "#" + p.map((v, k) => Math.round(v + (q[k] - v) * (x - i)).toString(16).padStart(2, "0")).join("");
};
// A small fixed jitter per rhomb so neighbouring tiles differ, like flakes of graphite do. Same input, same output.
const jitter = ([x, y]) => {
  const s = Math.sin(Math.round(x) * 12.9898 + Math.round(y) * 78.233) * 43758.5453;
  return s - Math.floor(s) - 0.5;
};

const f = (n) => n.toFixed(1);
// The light sits a little above centre, so the lower tiles fall away into the dark.
const heart = [SIZE * 0.5, SIZE * 0.44];

/** The mark: `generations` sets the tile size, `grout` the dark lines' width (in the 1024 square), `radius` the
 *  corners. Small icons use bigger tiles and heavier grout so the pattern still reads. */
function draw({ radius, generations, grout }) {
  const fills = [];
  const lines = [];
  for (const t of tiling(generations)) {
    // Both halves of a rhomb share the b–c edge, so its midpoint picks one shade for the whole rhomb.
    const mid = mul(add(t.b, t.c), 0.5);
    const dist = Math.hypot(mid[0] - heart[0], mid[1] - heart[1]) / (SIZE * 0.6);
    const color = shade(1 - dist + (t.thin ? 0.1 : 0) + jitter(mid) * 0.2);
    const pts = `${f(t.a[0])},${f(t.a[1])} ${f(t.b[0])},${f(t.b[1])} ${f(t.c[0])},${f(t.c[1])}`;
    fills.push(`<polygon points="${pts}" fill="${color}" stroke="${color}"/>`);
    // Grout along the rhomb's outer edges only, not across its middle.
    lines.push(`M${f(t.c[0])},${f(t.c[1])}L${f(t.a[0])},${f(t.a[1])}L${f(t.b[0])},${f(t.b[1])}`);
  }
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${SIZE} ${SIZE}">
<defs>
<clipPath id="tile"><rect width="${SIZE}" height="${SIZE}" rx="${radius}"/></clipPath>
<radialGradient id="sheen" cx="0.45" cy="0.4" r="0.55">
<stop offset="0" stop-color="#e6eef8" stop-opacity="0.28"/>
<stop offset="1" stop-color="#e6eef8" stop-opacity="0"/>
</radialGradient>
</defs>
<g clip-path="url(#tile)">
<rect width="${SIZE}" height="${SIZE}" fill="${GROUT}"/>
<g stroke-width="1" stroke-linejoin="round">${fills.join("")}</g>
<path d="${lines.join("")}" fill="none" stroke="${GROUT}" stroke-width="${grout}" stroke-linejoin="round" stroke-linecap="round"/>
<rect width="${SIZE}" height="${SIZE}" fill="url(#sheen)"/>
</g>
</svg>
`;
}
const full = { generations: 4, grout: 10 };
const small = { generations: 3, grout: 16 }; // reads at 16-64 px
const svg = draw({ radius: RADIUS, ...full });
fs.writeFileSync(path.join(out, "logo.svg"), svg);
// Square corners for the phone app: Android crops a maskable icon to its own shape, so it must fill the canvas.
fs.writeFileSync(path.join(out, "logo-square.svg"), draw({ radius: 0, ...full }));
// The small mark, for favicons; browsers draw favicon.svg sharp at any size.
const smallSvg = draw({ radius: RADIUS, ...small });
fs.writeFileSync(path.join(out, "logo-small.svg"), smallSvg);
fs.writeFileSync(path.join(__dirname, "..", "..", "frontend", "public", "favicon.svg"), smallSvg);
// Full bleed for the phone app's home-screen icon, which the launcher crops to its own shape: the full mark's tiles
// (so the star at the heart shows) with slightly heavier grout, as it is drawn at 48-192 px.
fs.writeFileSync(path.join(out, "logo-small-square.svg"), draw({ radius: 0, generations: 4, grout: 13 }));
console.log(path.join(out, "logo.svg"), `${(svg.length / 1024).toFixed(0)} KB, small ${(smallSvg.length / 1024).toFixed(0)} KB`);
