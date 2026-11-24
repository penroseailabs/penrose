// Developer view only: the orchestrator's stages and each model call's structured output.
import { ChevronDown } from "lucide-react";
import { TileMark, TileSpinner } from "./components/Tiles";
import Rich from "./components/Rich";
import type { Event } from "./api";

export const stages = [
  ["Fast response", "Answer directly, or hand the request to the orchestrator"],
  ["Orchestrator", "Plan, correct agents, and talk to you"],
  ["Subagent", "Act, check, and adapt"],
];

function belongs(event: Event, stage: string) {
  // The orchestrator's turns for a single subagent sit with its other decisions.
  if (event.payload.stage === "Orchestrator") return stage === "Orchestrator";
  if (stage === "Subagent")
    return (
      !!event.payload.node_id ||
      ["Subagent", "Verifier"].includes(
        event.payload.stage || "",
      )
    );
  const legacy = stage === "Orchestrator" && ["Planner", "Final response"].includes(event.payload.stage || "");
  return (event.payload.stage === stage || legacy) && !event.payload.node_id;
}

export default function Workflow({
  events,
  active,
}: {
  events: Event[];
  active: boolean;
}) {
  return (
    <div className="pipeline">
      {stages.map(([stage, description], index) => {
        const items = events.filter((event) => belongs(event, stage));
        const calls = items.filter(
          (event) =>
            event.kind === "stage_started" ||
            (event.kind === "stage_output" &&
              !event.payload.call_id &&
              !items.some(
                (started) =>
                  started.kind === "stage_started" &&
                  started.payload.stage === event.payload.stage &&
                  started.payload.node_id === event.payload.node_id &&
                  started.id < event.id,
              )),
        );
        const relatedTo = (call: Event) =>
          items.filter((event) => {
            if (call.payload.call_id)
              return event.payload.call_id === call.payload.call_id;
            if (call.kind === "stage_output") return event.id === call.id;
            const next = calls.find(
              (entry) =>
                entry.id > call.id &&
                entry.payload.stage === call.payload.stage &&
                entry.payload.node_id === call.payload.node_id,
            );
            return (
              event.payload.stage === call.payload.stage &&
              event.payload.node_id === call.payload.node_id &&
              event.id >= call.id &&
              (!next || event.id < next.id)
            );
          });
        const pending = calls.some(
          (call) =>
            call.kind === "stage_started" &&
            !relatedTo(call).some((event) =>
              ["stage_output", "stage_error"].includes(event.kind),
            ),
        );
        const tools = items.filter(
          (event) =>
            event.kind.startsWith("action_") ||
            ["node_feedback", "node_status", "ui_result"].includes(event.kind),
        );
        return (
          <details
            className={`pipeline-item ${items.length ? "reached" : ""}`}
            key={stage}
          >
            <summary className="pipeline-header">
              <span className="pipeline-marker">
                {items.length && !pending ? <TileMark shape="tick" size={14} /> : index + 1}
              </span>
              <div>
                <strong>
                  {stage === "Subagent" ? "Execution & verification" : stage}
                </strong>
                <small>{description}</small>
              </div>
              {pending && active ? (
                <TileSpinner size={14} className="accent" />
              ) : (
                <ChevronDown size={14} className="workflow-chevron" />
              )}
            </summary>
            <div className="workflow-body">
              {!items.length && <p className="subtle">No activity yet.</p>}
              {calls.map((call) => {
                const related = relatedTo(call);
                const result = related.find(
                  (event) => event.kind === "stage_output",
                );
                const failure = related.find(
                  (event) => event.kind === "stage_error",
                );
                const preview = related.find(
                  (event) => event.kind === "stage_delta",
                );
                const output = (result?.payload.output ||
                  preview?.payload.output ||
                  {}) as Record<string, unknown>;
                const summary = output.summary || output.reason;
                const message =
                  output.reply || output.output || output.question;
                const remainder = Object.fromEntries(
                  Object.entries(output).filter(
                    ([key]) =>
                      ![
                        "summary",
                        "reason",
                        "reply",
                        "output",
                        "question",
                      ].includes(key),
                  ),
                );
                return (
                  <article className="workflow-call" key={call.id}>
                    <div className="workflow-call-heading">
                      <strong>{call.payload.stage}</strong>
                      <small>
                        {call.payload.node_id ||
                          `Update ${calls.indexOf(call) + 1}`}
                      </small>
                      <span>
                        {failure
                          ? "Failed"
                          : result
                            ? "Complete"
                            : active
                              ? "Live"
                              : "Interrupted"}
                      </span>
                    </div>
                    {typeof summary === "string" && summary && (
                      <section>
                        <h4>Decision summary</h4>
                        <Rich>{summary}</Rich>
                      </section>
                    )}
                    {typeof message === "string" && message && (
                      <section>
                        <h4>Message</h4>
                        <Rich>{message}</Rich>
                      </section>
                    )}
                    {!result && !failure && !summary && !message && (
                      <p className="subtle">
                        {active
                          ? "Preparing a response…"
                          : "No completed response."}
                      </p>
                    )}
                    {failure && (
                      <p className="error-text">
                        {failure.payload.description}
                      </p>
                    )}
                    {!!Object.keys(remainder).length && (
                      <details className="structured-output">
                        <summary>Structured result</summary>
                        <pre>{JSON.stringify(remainder, null, 2)}</pre>
                      </details>
                    )}
                  </article>
                );
              })}
              {!!tools.length && (
                <details className="workflow-tools">
                  <summary>Tool calls & execution ({tools.length})</summary>
                  {tools.map((event) => (
                    <div className="workflow-tool" key={event.id}>
                      <strong>
                        {event.payload.node_id} ·{" "}
                        {event.kind.replaceAll("_", " ")}
                      </strong>
                      <p>
                        {String(
                          event.payload.description ||
                            (event.kind === "ui_result"
                              ? `${event.payload.status}: ${event.payload.reason}`
                              : "") ||
                            event.payload.feedback ||
                            event.payload.error ||
                            event.payload.status ||
                            "",
                        )}
                      </p>
                      {event.payload.kind != null && (
                        <code>{String(event.payload.kind)}</code>
                      )}
                    </div>
                  ))}
                </details>
              )}
            </div>
          </details>
        );
      })}
    </div>
  );
}
