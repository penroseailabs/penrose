import { lazy, Suspense, useState } from "react";
import Trace from "../Trace";
import Workflow from "../Workflow";
import { ACTIVE, clock } from "../lib/status";
import type { Workspace } from "../lib/useWorkspace";
import "../styles/developer.css";
import Sheet from "./Sheet";

const Dag = lazy(() => import("../Dag"));

/** Developer mode only: the orchestrator's plan, the model calls, and the task graph, as they really are. */
export default function DeveloperSheet({ ws, onClose }: { ws: Workspace; onClose: () => void }) {
  const [tab, setTab] = useState<"trace" | "graph">("trace");
  const [selected, setSelected] = useState("");
  const run = ws.latest;
  const active = !!run && ACTIVE.includes(run.status);
  return (
    <Sheet title="Developer view" onClose={onClose} className="developer">
      <div className="seg" role="tablist">
        <button aria-pressed={tab === "trace"} onClick={() => setTab("trace")}>Trace</button>
        <button aria-pressed={tab === "graph"} onClick={() => setTab("graph")}>Task graph</button>
      </div>
      <div className="dev-view scroll">
        {!run ? (
          <p className="subtle">No task in this conversation yet.</p>
        ) : tab === "trace" ? (
          <>
            <p className="subtle">
              {run.id} · {run.status} · {Object.keys(run.nodes).length} nodes
            </p>
            <Workflow events={ws.events} active={active} />
            <Trace events={ws.events} active={active} clock={clock} runId={run.id} />
          </>
        ) : run.dag ? (
          <Suspense fallback={<p className="subtle">Loading…</p>}>
            <Dag run={run} selected={selected} onSelect={setSelected} />
            {selected && run.nodes[selected] && (
              <pre className="dev-node">{JSON.stringify(run.nodes[selected], null, 2)}</pre>
            )}
          </Suspense>
        ) : (
          <p className="subtle">This task has no graph.</p>
        )}
      </div>
    </Sheet>
  );
}
