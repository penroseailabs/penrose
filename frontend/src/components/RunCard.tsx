import { useState } from "react";
import { ChevronDown } from "lucide-react";
import type { Run } from "../api";
import { ACTIVE, cardTitle, clock, type Mark, stepMark } from "../lib/status";
import type { ActivityStep } from "../lib/timeline";
import { TileMark, TileSpinner, TileStar } from "./Tiles";

export function StepMark({ mark }: { mark: Mark }) {
  const glyph =
    mark === "ok" ? <TileMark shape="tick" /> :
    mark === "err" ? <TileMark shape="cross" /> :
    mark === "now" ? <TileSpinner /> :
    <TileStar outline={mark !== "wait"} />;
  return <span className={`sm ${mark}`}>{glyph}</span>;
}

/** One task's progress: its planned steps, how far along it is, and what Relay did, on request. */
export default function RunCard({ run, steps }: { run: Run; steps: ActivityStep[] }) {
  const [open, setOpen] = useState(false);
  const active = ACTIVE.includes(run.status);
  const nodes = Object.entries(run.nodes || {});
  const done = nodes.filter(([, n]) => n.status === "succeeded").length;
  const current = nodes.findIndex(([, n]) => !["succeeded", "failed", "rejected", "uncertain"].includes(n.status));
  const finished = run.status === "completed";
  const of = finished ? "Done" : `Step ${Math.min(nodes.length, current < 0 ? done : current + 1)} of ${nodes.length}`;
  const lastAt = (id: string) => steps.filter((s) => s.node === id).at(-1)?.at;

  return (
    <div className="card surface">
      <div className="card-head">
        <b>{cardTitle(run)}</b>
        <span className="of">{of}</span>
      </div>
      <div className="meter" role="progressbar" aria-valuemin={0} aria-valuemax={nodes.length} aria-valuenow={done}>
        <i style={{ width: `${finished ? 100 : nodes.length ? (done / nodes.length) * 100 : 0}%` }} />
      </div>
      <ul className={open ? "steps full" : "steps"}>
        {nodes.map(([id, node]) => {
          const mark = stepMark(node, active);
          const at = lastAt(id);
          return (
            <li key={id} className={mark}>
              <StepMark mark={mark} />
              <span className="t" title={node.spec.objective}>{node.spec.objective}</span>
              {mark === "now" ? <time>now</time> : mark === "ok" && at ? <time>{clock(at)}</time> : null}
            </li>
          );
        })}
      </ul>
      {run.error && !active && <p className="card-error">{run.error}</p>}
      {open && (
        <ol className="trace" aria-label="What penrose did">
          {steps.length ? (
            steps.map((step) => (
              <li key={step.id} className={step.ok === false ? "failed" : ""}>
                <time>{clock(step.at)}</time>
                <span>{step.summary}</span>
              </li>
            ))
          ) : (
            <li><span>Nothing done on the phone yet.</span></li>
          )}
        </ol>
      )}
      <button
        className={open ? "card-more open" : "card-more"}
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-label={open ? "Hide what penrose did" : "Show what penrose did"}
        title={open ? "Hide details" : "Details"}
      >
        <ChevronDown size={15} />
      </button>
    </div>
  );
}
