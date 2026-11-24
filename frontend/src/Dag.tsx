import { useMemo } from "react";
import {
  ReactFlow,
  Background,
  Controls,
  MarkerType,
  Position,
} from "@xyflow/react";
import type { Run } from "./api";

const colors: Record<string, string> = {
  succeeded: "#147461",
  running: "#3071dd",
  failed: "#c74d4d",
  waiting_user: "#b68022",
  blocked: "#9b9ba5",
  waiting_device: "#8d6dbb",
};
export default function Dag({
  run,
  selected,
  onSelect,
}: {
  run: Run;
  selected: string;
  onSelect: (id: string) => void;
}) {
  const { nodes, edges } = useMemo(() => {
    const entries = Object.entries(run.nodes),
      depths: Record<string, number> = {},
      rows: Record<number, number> = {};
    function depth(id: string): number {
      if (depths[id] !== undefined) return depths[id];
      const deps = run.nodes[id]?.spec.depends_on || [];
      return (depths[id] = deps.length ? Math.max(...deps.map(depth)) + 1 : 0);
    }
    const nodes = entries.map(([id, node]) => {
      const col = depth(id),
        row = rows[col] || 0;
      rows[col] = row + 1;
      return {
        id,
        position: { x: col * 235, y: row * 135 },
        data: {
          label: (
            <div className="dag-label">
              <span className="eyebrow">
                {node.status.replaceAll("_", " ")}
              </span>
              <strong>{node.spec.objective}</strong>
              <small>
                {node.spec.steps.length} steps ·{" "}
                {node.attempt
                  ? `attempt ${node.attempt}`
                  : "ready when dependencies pass"}
              </small>
            </div>
          ),
        },
        style: {
          width: 205,
          borderRadius: 12,
          border: `1.5px solid ${colors[node.status] || "#dce2e7"}`,
          boxShadow:
            selected === id ? "0 0 0 4px #116b5922" : "0 3px 10px #152c4010",
          padding: 14,
          textAlign: "left" as const,
        },
        sourcePosition: Position.Right,
        targetPosition: Position.Left,
      };
    });
    const edges = entries.flatMap(([id, node]) =>
      node.spec.depends_on.map((source) => ({
        id: source + "-" + id,
        source,
        target: id,
        animated: node.status === "running",
        style: {
          stroke:
            run.nodes[source]?.status === "succeeded" ? "#147461" : "#b9c5cb",
          strokeWidth: 1.5,
        },
        markerEnd: { type: MarkerType.ArrowClosed },
      })),
    );
    return { nodes, edges };
  }, [run, selected]);
  return (
    <div className="dag-canvas">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        fitView
        fitViewOptions={{ padding: 0.25 }}
        minZoom={0.2}
        maxZoom={1.5}
        nodesDraggable={false}
        nodesConnectable={false}
        onNodeClick={(_, node) => onSelect(node.id)}
      >
        <Background gap={18} color="#d5dfe3" />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}
