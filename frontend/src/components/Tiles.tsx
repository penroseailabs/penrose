import { hash, inset, points, type Pt, rhombs, toSegment } from "../lib/penrose";

// Relay's small marks: one Penrose tiling, the template, under every icon on a 24-unit grid, cut to a rounded square. An icon is the
// template's tiles coloured where its shape falls (a tick, a cross, the star at the centre); the rest stay as a
// faint ground, the same from icon to icon. They take the colour (currentColor) of where they sit.

const PHI = (1 + Math.sqrt(5)) / 2;
const EDGE = 3.1; // a template tile's side
type Tile = { pts: Pt[]; mid: Pt; spots: Pt[]; shade: number; angle: number; r: number };
const TEMPLATE: Tile[] = rhombs([12, 12], EDGE * PHI ** 4, 4, ([x, y]) => x > -1 && y > -1 && x < 25 && y < 25).map((t) => ({
  pts: inset(t, 0.22),
  mid: t.mid,
  // Where it is sampled against a stroke: its centre and halfway out to each corner.
  spots: [t.mid, ...t.pts.map(([x, y]): Pt => [(x + t.mid[0]) / 2, (y + t.mid[1]) / 2])],
  shade: hash(t.mid),
  angle: (Math.atan2(t.mid[1] - 12, t.mid[0] - 12) / (2 * Math.PI) + 1.25) % 1, // 0 at the top, then clockwise
  r: Math.hypot(t.mid[0] - 12, t.mid[1] - 12),
}));

type Stroke = [Pt, Pt][];
const TICK: Stroke = [[[5.6, 13.2], [9.9, 17.8]], [[9.9, 17.8], [18.8, 6.6]]];
const CROSS: Stroke = [[[6.5, 6.5], [17.5, 17.5]], [[17.5, 6.5], [6.5, 17.5]]];
const HALF = 2.3; // half the stroke's width: a tile mostly within it is coloured

type Lit = { tile: Tile; delay: string };

/** The template's tiles mostly under a stroke, each set in by how far along the stroke it lies. */
function under(stroke: Stroke): Lit[] {
  const lengths = stroke.map(([p, q]) => Math.hypot(q[0] - p[0], q[1] - p[1]));
  const total = lengths.reduce((a, b) => a + b, 0);
  return TEMPLATE.flatMap((tile) => {
    let best = Infinity;
    let along = 0;
    let before = 0;
    stroke.forEach(([p, q], i) => {
      const d = toSegment(tile.mid, p, q);
      const t = Math.max(0, Math.min(1, ((tile.mid[0] - p[0]) * (q[0] - p[0]) + (tile.mid[1] - p[1]) * (q[1] - p[1])) / lengths[i] ** 2));
      if (d < best) [best, along] = [d, (before + t * lengths[i]) / total];
      before += lengths[i];
    });
    const covered = tile.spots.filter((spot) => stroke.some(([p, q]) => toSegment(spot, p, q) < HALF)).length;
    return covered * 2 > tile.spots.length ? [{ tile, delay: `${Math.round(along * 280)}ms` }] : [];
  });
}

/** The star at the heart of the tiling and the ring about it, lit in turn clockwise. */
const STAR: Lit[] = TEMPLATE.filter((t) => t.r < EDGE * 2.05).map((tile) => ({ tile, delay: `${(-tile.angle * 1.5).toFixed(2)}s` }));
const LIT = { tick: under(TICK), cross: under(CROSS), spin: STAR, wait: STAR, later: STAR };

function Mark({ kind, size, className = "" }: { kind: keyof typeof LIT; size: number; className?: string }) {
  const on = LIT[kind];
  const lit = new Set(on.map((l) => l.tile));
  return (
    <svg className={`pz pz-${kind} ${className}`} width={size} height={size} viewBox="0 0 24 24" aria-hidden="true">
      <g className="pz-ground">
        {TEMPLATE.map((t, i) => !lit.has(t) && <polygon key={i} points={points(t.pts)} />)}
      </g>
      {on.map(({ tile, delay }, i) => (
        <polygon key={i} className="pz-on" points={points(tile.pts)} style={{ fillOpacity: 0.5 + tile.shade * 0.45, animationDelay: delay }} />
      ))}
    </svg>
  );
}

/** Working: the star's tiles light in turn, round and round. */
export function TileSpinner({ size = 16, className = "" }: { size?: number; className?: string }) {
  return <Mark kind="spin" size={size} className={className} />;
}

/** Waiting for you (filled, breathing) or not started yet (outline only). */
export function TileStar({ size = 16, outline = false }: { size?: number; outline?: boolean }) {
  return <Mark kind={outline ? "later" : "wait"} size={size} />;
}

/** Done or failed: a tick (or a cross) of coloured tiles, set in along the stroke. */
export function TileMark({ shape, size = 18 }: { shape: "tick" | "cross"; size?: number }) {
  return <Mark kind={shape} size={size} />;
}
