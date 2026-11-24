import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Activity, X } from "lucide-react";
import type { UsageRequest } from "../api";

/** The agents drawn as lines; every other stage (gate, memory, compaction, reflection) is a grey dot. */
const SERIES = [
  { stage: "Orchestrator", key: "orchestrator" },
  { stage: "Subagent", key: "subagent" },
  { stage: "Verifier", key: "verifier" },
] as const;
type Key = (typeof SERIES)[number]["key"] | "other";

const keyOf = (r: UsageRequest): Key => SERIES.find((s) => s.stage === r.stage)?.key ?? "other";

/** Token counts short: 812, 48.2k, 1.05M. */
function short(n: number) {
  if (n >= 1e6) return `${(n / 1e6).toPrecision(3)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(n >= 1e5 ? 0 : 1)}k`;
  return String(n);
}

/** A round step for about `count` gridlines up to `max`. */
function step(max: number, count = 3) {
  const raw = max / count;
  const pow = 10 ** Math.floor(Math.log10(raw || 1));
  return ([1, 2, 2.5, 5, 10].find((m) => m * pow >= raw) ?? 10) * pow;
}

/** How big each model call's prompt was (the provider's own count: the whole input, cached part included),
 * one tick per call: a small chip beside the cost that opens a chart floating over the chat. */
export default function ContextUse({ requests }: { requests: UsageRequest[] }) {
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (!open) return;
    const escape = (event: KeyboardEvent) => event.key === "Escape" && setOpen(false);
    // A press anywhere but the chip (which toggles it) or the popup closes it.
    const outside = (event: PointerEvent) => {
      if (!(event.target as Element | null)?.closest?.(".ctx-pop, .ctx-chip")) setOpen(false);
    };
    document.addEventListener("keydown", escape);
    document.addEventListener("pointerdown", outside);
    return () => {
      document.removeEventListener("keydown", escape);
      document.removeEventListener("pointerdown", outside);
    };
  }, [open]);
  const last = requests[requests.length - 1];
  const spark = requests.slice(-24).map((r) => r.tokens.input);
  return (
    <>
      <button
        className="ctx-chip"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-haspopup="dialog"
        title="Context per model call"
      >
        <Activity size={12} />
        <Spark values={spark} />
        <span>{last ? short(last.tokens.input) : "0"}</span>
      </button>
      {open && <Panel requests={requests} onClose={() => setOpen(false)} />}
    </>
  );
}

/** The last few calls as a tiny line, in the chip. */
function Spark({ values }: { values: number[] }) {
  const max = Math.max(1, ...values);
  const w = 34, h = 12;
  const points = values.map((v, i) => `${values.length > 1 ? (i / (values.length - 1)) * w : w / 2},${h - 1 - (v / max) * (h - 2)}`);
  return (
    <svg className="ctx-spark" width={w} height={h} viewBox={`0 0 ${w} ${h}`} aria-hidden>
      <polyline points={points.join(" ")} />
    </svg>
  );
}

function Panel({ requests, onClose }: { requests: UsageRequest[]; onClose: () => void }) {
  const present = SERIES.filter((s) => requests.some((r) => r.stage === s.stage));
  const others = requests.some((r) => keyOf(r) === "other");
  const orchestrator = requests.filter((r) => r.stage === "Orchestrator");
  // The active subagent: the session of the newest subagent call.
  const lastSub = [...requests].reverse().find((r) => r.stage === "Subagent");
  const session = lastSub
    ? requests.filter((r) => r.stage === "Subagent" && r.call_id === lastSub.call_id && r.run_id === lastSub.run_id)
    : [];
  return (
    <div className="ctx-pop" role="dialog" aria-label="Context per model call">
      <header>
        <strong>Context per model call</strong>
        <span className="ctx-sub">{requests.length} calls · input tokens, cached included</span>
        <button className="ib" aria-label="Close" onClick={onClose}>
          <X size={13} />
        </button>
      </header>
      <div className="ctx-stats">
        <Stat label="Orchestrator" series="orchestrator" calls={orchestrator} note="this chat" />
        <Stat label="Subagent" series="subagent" calls={session} note="its latest session" />
      </div>
      <Chart requests={requests} />
      <div className="ctx-legend">
        {present.map((s) => (
          <span key={s.key}><i className={`sw ${s.key}`} />{s.stage}</span>
        ))}
        {others && <span><i className="sw other dot" />Other agents</span>}
      </div>
    </div>
  );
}

function Stat({ label, series, calls, note }: { label: string; series: Key; calls: UsageRequest[]; note: string }) {
  const latest = calls[calls.length - 1];
  const peak = Math.max(0, ...calls.map((r) => r.tokens.input));
  return (
    <div className="ctx-stat">
      <span className="ctx-stat-label"><i className={`sw ${series}`} />{label}</span>
      <span className="ctx-stat-value">{latest ? short(latest.tokens.input) : "—"}</span>
      <span className="ctx-stat-note">
        {latest ? `peak ${short(peak)} · ${calls.length} ${calls.length === 1 ? "call" : "calls"} in ${note}` : "no calls yet"}
      </span>
    </div>
  );
}

const H = 170;
const PAD = { left: 36, right: 8, top: 8, bottom: 20 };

function Chart({ requests }: { requests: UsageRequest[] }) {
  const box = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  const [hover, setHover] = useState<number | null>(null);
  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    const observer = new ResizeObserver(() => setWidth(el.clientWidth));
    observer.observe(el);
    setWidth(el.clientWidth);
    return () => observer.disconnect();
  }, []);

  const n = requests.length;
  const w = Math.max(1, width - PAD.left - PAD.right);
  const h = H - PAD.top - PAD.bottom;
  const max = Math.max(1, ...requests.map((r) => r.tokens.input));
  const gap = step(max);
  const top = Math.ceil(max / gap) * gap;
  const x = (i: number) => PAD.left + (n > 1 ? (i / (n - 1)) * w : w / 2);
  const y = (v: number) => PAD.top + h - (v / top) * h;

  // One path per line: the orchestrator runs through the chat; a subagent or verifier line breaks between sessions.
  const { paths, dots, runs } = useMemo(() => {
    const segments = new Map<string, { key: Key; points: [number, number][] }>();
    const dots: [number, number][] = [];
    const runs: number[] = [];
    requests.forEach((r, i) => {
      if (i > 0 && r.run_id !== requests[i - 1].run_id) runs.push(i);
      const key = keyOf(r);
      if (key === "other") return void dots.push([i, r.tokens.input]);
      const id = key === "orchestrator" ? key : `${key}:${r.run_id}:${r.call_id ?? ""}`;
      if (!segments.has(id)) segments.set(id, { key, points: [] });
      segments.get(id)!.points.push([i, r.tokens.input]);
    });
    return { paths: [...segments.values()], dots, runs };
  }, [requests]);

  const ticks = [];
  for (let v = 0; v <= top; v += gap) ticks.push(v);
  const xTicks = n > 1 ? [...new Set([0, Math.round((n - 1) / 2), n - 1])] : [0];

  const pick = (event: React.PointerEvent<SVGRectElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    const at = n > 1 ? Math.round(((event.clientX - rect.left) / rect.width) * (n - 1)) : 0;
    setHover(Math.min(n - 1, Math.max(0, at)));
  };
  const shown = hover != null ? requests[hover] : null;

  return (
    <div className="ctx-chart" ref={box}>
      {width > 0 && (
        <svg width={width} height={H} role="img" aria-label={`Input tokens of each of ${n} model calls`}>
          {ticks.map((v) => (
            <g key={v}>
              <line className={v ? "grid" : "base"} x1={PAD.left} x2={PAD.left + w} y1={y(v)} y2={y(v)} />
              <text className="ax" x={PAD.left - 6} y={y(v)} dy="0.32em" textAnchor="end">{short(v)}</text>
            </g>
          ))}
          {runs.map((i) => (
            <line key={i} className="run" x1={(x(i - 1) + x(i)) / 2} x2={(x(i - 1) + x(i)) / 2} y1={PAD.top} y2={PAD.top + h} />
          ))}
          {xTicks.map((i) => (
            <text key={i} className="ax" x={x(i)} y={H - 5} textAnchor={i === 0 ? "start" : i === n - 1 ? "end" : "middle"}>
              {i + 1}
            </text>
          ))}
          {dots.map(([i, v]) => (
            <circle key={i} className="pt other" cx={x(i)} cy={y(v)} r={2.5} />
          ))}
          {paths.map((p, k) =>
            p.points.length > 1 ? (
              <polyline key={k} className={`ln ${p.key}`} points={p.points.map(([i, v]) => `${x(i)},${y(v)}`).join(" ")} />
            ) : (
              <circle key={k} className={`pt ${p.key}`} cx={x(p.points[0][0])} cy={y(p.points[0][1])} r={2.5} />
            ),
          )}
          {shown && hover != null && (
            <g pointerEvents="none">
              <line className="cross" x1={x(hover)} x2={x(hover)} y1={PAD.top} y2={PAD.top + h} />
              <circle className={`pt hot ${keyOf(shown)}`} cx={x(hover)} cy={y(shown.tokens.input)} r={4} />
            </g>
          )}
          <rect
            x={PAD.left - 4} y={0} width={w + 8} height={H} fill="transparent"
            onPointerMove={pick} onPointerDown={pick} onPointerLeave={() => setHover(null)}
          />
        </svg>
      )}
      {shown && hover != null && (
        <div
          className="ctx-tip"
          style={x(hover) > width / 2 ? { right: width - x(hover) + 10 } : { left: x(hover) + 10 }}
        >
          <div className="ctx-tip-head"><i className={`sw ${keyOf(shown)}`} />{shown.stage || "Model"} · call {hover + 1}</div>
          <div><b>{shown.tokens.input.toLocaleString()}</b> in · {shown.tokens.cached.toLocaleString()} cached</div>
          <div>{shown.tokens.output.toLocaleString()} out · {shown.model}</div>
        </div>
      )}
    </div>
  );
}
