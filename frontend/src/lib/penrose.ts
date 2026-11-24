// Penrose rhomb (P3) tilings, the same construction as Relay's logo (desktop/scripts/make-logo.js): a ten-triangle
// "sun" split into Robinson triangles, each pair of triangles one rhomb. Used by the icons and the background.

export type Pt = [number, number];
/** One rhomb: its four corners, whether it is the thin kind, and its centre. */
export type Rhomb = { pts: Pt[]; thin: boolean; mid: Pt };

const PHI = (1 + Math.sqrt(5)) / 2;
type Tri = { thin: boolean; a: Pt; b: Pt; c: Pt };

const add = (p: Pt, q: Pt): Pt => [p[0] + q[0], p[1] + q[1]];
const sub = (p: Pt, q: Pt): Pt => [p[0] - q[0], p[1] - q[1]];
const mul = (p: Pt, k: number): Pt => [p[0] * k, p[1] * k];

/** The rhombs of a tiling centred on `centre`, reaching `radius` out, split `generations` times (each split makes
 *  the tiles 1.618 times smaller). `keep` drops rhombs early, by their centre, to save work. */
export function rhombs(centre: Pt, radius: number, generations: number, keep?: (mid: Pt) => boolean): Rhomb[] {
  let tris: Tri[] = [];
  for (let i = 0; i < 10; i++) {
    let b = add(centre, mul([Math.cos(((2 * i - 1) * Math.PI) / 10), Math.sin(((2 * i - 1) * Math.PI) / 10)], radius));
    let c = add(centre, mul([Math.cos(((2 * i + 1) * Math.PI) / 10), Math.sin(((2 * i + 1) * Math.PI) / 10)], radius));
    if (i % 2 === 0) [b, c] = [c, b];
    tris.push({ thin: true, a: centre, b, c });
  }
  for (let g = 0; g < generations; g++) {
    const next: Tri[] = [];
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
  // Both halves of a rhomb share their b–c side (its short diagonal): one rhomb per diagonal, the fourth corner
  // the first mirrored through the diagonal's middle.
  const seen = new Set<string>();
  const out: Rhomb[] = [];
  for (const t of tris) {
    const mid = mul(add(t.b, t.c), 0.5);
    const key = `${Math.round(mid[0] * 64)},${Math.round(mid[1] * 64)}`;
    if (seen.has(key)) continue;
    seen.add(key);
    if (keep && !keep(mid)) continue;
    out.push({ pts: [t.a, t.b, sub(add(t.b, t.c), t.a), t.c], thin: t.thin, mid });
  }
  return out;
}

/** A fixed number in [0, 1) for a point: the same tile always gets the same shade. */
export function hash([x, y]: Pt) {
  const s = Math.sin(Math.round(x * 10) * 12.9898 + Math.round(y * 10) * 78.233) * 43758.5453;
  return s - Math.floor(s);
}

/** The rhomb drawn a little smaller about its centre, so neighbours show a hairline of grout between them. */
export function inset(r: Rhomb, by: number): Pt[] {
  return r.pts.map((p) => {
    const d = sub(p, r.mid);
    const len = Math.hypot(d[0], d[1]) || 1;
    return add(r.mid, mul(d, Math.max(0, len - by) / len));
  });
}

/** Distance from a point to the segment p–q. */
export function toSegment(x: Pt, p: Pt, q: Pt) {
  const d = sub(q, p);
  const t = Math.max(0, Math.min(1, ((x[0] - p[0]) * d[0] + (x[1] - p[1]) * d[1]) / (d[0] * d[0] + d[1] * d[1] || 1)));
  return Math.hypot(x[0] - p[0] - t * d[0], x[1] - p[1] - t * d[1]);
}

export const points = (pts: Pt[]) => pts.map(([x, y]) => `${x.toFixed(2)},${y.toFixed(2)}`).join(" ");
