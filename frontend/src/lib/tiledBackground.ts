import { hash, type Pt, rhombs } from "./penrose";

// The canvas behind everything: a Penrose tiling, each tile a slightly different shade between white and grey,
// with faint grout lines. A tiling never repeats, so it is drawn once for the window's size (no seams) and handed
// to CSS as --tiles; the conversation panel lays a fainter copy, --tiles-faint, in the same place.

const EDGE = 34; // a tile's side, in CSS pixels
const STRENGTH = 0.5; // how strongly the shades and grout show; 1 was too busy

function draw(width: number, height: number, dark: boolean, strength: number): Promise<Blob | null> {
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const w = Math.ceil(width * dpr);
  const h = Math.ceil(height * dpr);
  const canvas = document.createElement("canvas");
  canvas.width = w;
  canvas.height = h;
  const g = canvas.getContext("2d");
  if (!g) return Promise.resolve(null);
  // A centre off to the upper left, so the tiling's five-fold star sits out of the way.
  const centre: Pt = [w * 0.18, h * 0.1];
  const radius = Math.hypot(Math.max(centre[0], w - centre[0]), Math.max(centre[1], h - centre[1])) * 1.12;
  const generations = Math.max(1, Math.round(Math.log(radius / (EDGE * dpr)) / Math.log((1 + Math.sqrt(5)) / 2)));
  const margin = EDGE * dpr * 2;
  const tiles = rhombs(centre, radius, generations, ([x, y]) => x > -margin && y > -margin && x < w + margin && y < h + margin);
  g.lineJoin = "round";
  g.lineWidth = Math.max(1, 0.75 * dpr);
  g.strokeStyle = dark ? `rgba(255,255,255,${0.045 * strength})` : `rgba(40,44,52,${0.045 * strength})`;
  for (const tile of tiles) {
    g.beginPath();
    tile.pts.forEach(([x, y], i) => (i ? g.lineTo(x, y) : g.moveTo(x, y)));
    g.closePath();
    // From a touch lighter than the canvas to a touch darker: white to grey, never a pattern of its own.
    const s = hash(tile.mid) * 2 - 1;
    g.fillStyle = dark
      ? s > 0 ? `rgba(255,255,255,${s * 0.03 * strength})` : `rgba(0,0,0,${-s * 0.12 * strength})`
      : s > 0 ? `rgba(255,255,255,${s * 0.38 * strength})` : `rgba(30,34,44,${-s * 0.026 * strength})`;
    g.fill();
    g.stroke();
  }
  return new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
}

/** Draws the tiling just after the first paint and again whenever the window's width, the theme, or the height it was
 *  drawn for changes. A shorter window keeps the drawing, cut off at the bottom (--tiles-size): the keyboard shortens
 *  the phone app's window each time it opens (interactive-widget=resizes-content) and must not cost two redraws. */
export function startTiledBackground() {
  const root = document.documentElement;
  const urls: string[] = [];
  let timer = 0;
  let drawn = "";
  let tall = 0;
  let generation = 0;
  const render = async () => {
    const dark = root.dataset.theme === "dark";
    const width = window.innerWidth;
    const key = `${width}:${dark}:${window.devicePixelRatio}`;
    if (key === drawn && window.innerHeight <= tall) return;
    // The screen's height where it is known, so the window growing back (the keyboard going) needs nothing either.
    const height = Math.max(window.innerHeight, Math.min(window.screen?.availHeight || 0, window.innerHeight * 2));
    drawn = key;
    tall = height;
    const mine = ++generation;
    const [full, faint] = await Promise.all([
      draw(width, height, dark, STRENGTH),
      draw(width, height, dark, STRENGTH * 0.45),
    ]);
    if (!full || !faint || mine !== generation) return;
    const next = [URL.createObjectURL(full), URL.createObjectURL(faint)];
    root.style.setProperty("--tiles", `url("${next[0]}")`);
    root.style.setProperty("--tiles-faint", `url("${next[1]}")`);
    root.style.setProperty("--tiles-size", `100% ${height}px`);
    // The old images go once the new ones are in place.
    urls.splice(0).forEach((url) => setTimeout(() => URL.revokeObjectURL(url), 1000));
    urls.push(...next);
  };
  const later = () => {
    window.clearTimeout(timer);
    timer = window.setTimeout(render, 250);
  };
  // Not in the way of the first paint: the window shows on the plain canvas a frame before the tiling joins it.
  requestAnimationFrame(() => window.setTimeout(render));
  window.addEventListener("resize", later);
  new MutationObserver(render).observe(root, { attributes: true, attributeFilter: ["data-theme"] });
}
