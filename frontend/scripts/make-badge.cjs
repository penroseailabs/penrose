// Draws the notification badge, public/companion/badge-96.png: the Penrose "star" (five thick rhombs meeting at
// a point) as a white silhouette on a transparent ground. Android's status bar paints a notification's badge as a
// single-colour mask from its alpha channel, so an opaque picture (the app icon) turns into a solid white square.
// Regenerate: node scripts/make-badge.cjs   (from frontend/)
const fs = require("node:fs");
const path = require("node:path");
const { PNG } = require("pngjs");

const SIZE = 96;
const SS = 4; // supersampling per axis
const PHI = (1 + Math.sqrt(5)) / 2;
const GAP = 0.055; // transparent seam between rhombs, in units of the star's inner radius s

const R = SIZE * 0.47; // outer tip radius (phi * s)
const s = R / PHI; // rhomb side
const c = SIZE / 2;
const tips = Array.from({ length: 5 }, (_, k) => {
  const a = (Math.PI * 2 * k) / 5 - Math.PI / 2 + Math.PI / 5; // a tip points straight up
  return [c + R * Math.cos(a), c + R * Math.sin(a)];
});
const valleys = Array.from({ length: 5 }, (_, k) => {
  const a = (Math.PI * 2 * k) / 5 - Math.PI / 2;
  return [c + s * Math.cos(a), c + s * Math.sin(a)];
});
// Ten-vertex outline, alternating valley and tip.
const outline = [];
for (let k = 0; k < 5; k++) outline.push(valleys[k], tips[k]);

function inside(x, y) {
  let hit = false;
  for (let i = 0, j = outline.length - 1; i < outline.length; j = i++) {
    const [xi, yi] = outline[i];
    const [xj, yj] = outline[j];
    if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) hit = !hit;
  }
  return hit;
}

/** Distance from a point to the segment centre -> valley, the seam between two neighbouring rhombs. */
function seam(x, y) {
  let best = Infinity;
  for (const [vx, vy] of valleys) {
    const dx = vx - c;
    const dy = vy - c;
    const t = Math.max(0, Math.min(1, ((x - c) * dx + (y - c) * dy) / (dx * dx + dy * dy)));
    best = Math.min(best, Math.hypot(x - (c + t * dx), y - (c + t * dy)));
  }
  return best;
}

const png = new PNG({ width: SIZE, height: SIZE });
for (let py = 0; py < SIZE; py++) {
  for (let px = 0; px < SIZE; px++) {
    let covered = 0;
    for (let sy = 0; sy < SS; sy++) {
      for (let sx = 0; sx < SS; sx++) {
        const x = px + (sx + 0.5) / SS;
        const y = py + (sy + 0.5) / SS;
        if (inside(x, y) && seam(x, y) > s * GAP) covered++;
      }
    }
    const i = (py * SIZE + px) * 4;
    png.data[i] = png.data[i + 1] = png.data[i + 2] = 255;
    png.data[i + 3] = Math.round((covered / (SS * SS)) * 255);
  }
}
const out = path.join(__dirname, "..", "public", "companion", "badge-96.png");
fs.writeFileSync(out, PNG.sync.write(png));
console.log("wrote", out);
