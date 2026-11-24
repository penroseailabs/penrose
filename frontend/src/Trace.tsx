import {
  AlertTriangle,
  Brain,
  Check,
  ChevronRight,
  Info,
  LoaderCircle,
  MousePointerClick,
  Sparkles,
  Terminal,
  X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import Rich from "./components/Rich";
import { api, type Event } from "./api";

type Output = Record<string, unknown>;
type Status = "running" | "done" | "error" | "info";

type CallEntry = {
  type: "call";
  id: string;
  node?: string;
  stage: string;
  created: number;
  ended?: number;
  thinking: string;
  /** Screenshots the model was shown (ids of stored images). */
  images: number[];
  output?: Output;
  preview?: Output;
  error?: string;
};
type AdbEntry = {
  type: "adb";
  id: string;
  node?: string;
  created: number;
  ended?: number;
  command: string;
  description: string;
  result?: Event["payload"];
};
type UiAction = {
  kind: string;
  description: string;
  status: string;
  error?: string;
  /** What the action did and what changed on screen, as the UI agent hears it next turn. */
  outcome?: string;
};
/** One tool call of the UI agent: an element and an interaction, or a device-level command. */
type UiCall = { name: string; arguments: Record<string, unknown> };
/** One UI agent decision: the summary streams live; the full exchange is fetched on demand. */
type UiTurn = {
  callId: number;
  seconds: number;
  calls: UiCall[];
  screenshot: boolean;
  tokens?: number;
  error?: string;
};
type UiExchange = {
  seconds: number;
  error: string | null;
  request: {
    model: string;
    /** System instructions, then the state: goal, recent actions, the bridge screen, what each element accepts. */
    messages: [{ role: string; content: string }, { role: string; content: string | { type: string; text?: string }[] }];
    tools: { function: { name: string } }[];
  };
  response: unknown;
};
type UiEntry = {
  type: "ui";
  id: string;
  node?: string;
  created: number;
  ended?: number;
  goal: string;
  actions: UiAction[];
  turns: UiTurn[];
  result?: Event["payload"];
};
type NoticeEntry = {
  type: "notice";
  id: string;
  node?: string;
  created: number;
  level: "error" | "info";
  title: string;
  text: string;
};
type Entry = CallEntry | AdbEntry | UiEntry | NoticeEntry;

const text = (value: unknown) => (typeof value === "string" ? value : "");

/** Fold the event journal into one entry per model call, command, UI routine, or notice. */
export function buildTrace(events: Event[]): Entry[] {
  const entries: Entry[] = [];
  const calls = new Map<string, CallEntry>();
  const adb = new Map<string, AdbEntry>();
  const ui = new Map<string, UiEntry>();
  const loose = new Map<string, UiEntry>();
  const notice = (
    event: Event,
    level: NoticeEntry["level"],
    title: string,
    body: string,
  ) =>
    entries.push({
      type: "notice",
      id: `n${event.id}`,
      node: event.payload.node_id,
      created: event.created,
      level,
      title,
      text: body,
    });

  for (const event of events) {
    const p = event.payload;
    const node = p.node_id || "";
    const callId = text(p.call_id);
    switch (event.kind) {
      case "stage_started": {
        const entry: CallEntry = {
          type: "call",
          id: `c${callId || event.id}`,
          node: p.node_id,
          stage: text(p.stage),
          created: event.created,
          thinking: "",
          images: [],
        };
        if (callId) calls.set(callId, entry);
        entries.push(entry);
        break;
      }
      case "stage_thinking": {
        const entry = calls.get(callId);
        if (entry) entry.thinking = p.reset ? "" : entry.thinking + text(p.text);
        break;
      }
      case "stage_image": {
        const entry = calls.get(callId);
        if (entry) entry.images.push(Number(p.image_id));
        break;
      }
      case "stage_delta": {
        const entry = calls.get(callId);
        if (entry) entry.preview = p.output as Output;
        break;
      }
      case "stage_output": {
        let entry = calls.get(callId);
        if (!entry) {
          // Older runs recorded outputs without a start event.
          entry = {
            type: "call",
            id: `c${event.id}`,
            node: p.node_id,
            stage: text(p.stage),
            created: event.created,
            thinking: "",
            images: [],
          };
          entries.push(entry);
        }
        entry.output = p.output as Output;
        entry.ended = event.created;
        break;
      }
      case "stage_error": {
        const entry = calls.get(callId);
        if (entry) {
          entry.error = text(p.description) || "Failed";
          entry.ended = event.created;
        } else notice(event, "error", `${p.stage} failed`, text(p.description));
        break;
      }
      case "ui_started": {
        const entry: UiEntry = {
          type: "ui",
          id: `u${event.id}`,
          node: p.node_id,
          created: event.created,
          goal: text(p.goal),
          actions: [],
          turns: [],
        };
        ui.set(node, entry);
        entries.push(entry);
        break;
      }
      case "ui_result": {
        const entry = ui.get(node) || loose.get(node);
        if (entry) {
          entry.result = p;
          entry.ended = event.created;
          ui.delete(node);
          loose.delete(node);
        } else
          notice(event, p.status === "completed" ? "info" : "error", `UI ${p.status}`, text(p.reason));
        break;
      }
      case "ui_call": {
        let entry = ui.get(node) || loose.get(node);
        if (!entry) {
          entry = {
            type: "ui",
            id: `u${event.id}`,
            node: p.node_id,
            created: event.created,
            goal: text(p.goal) || "UI action",
            actions: [],
            turns: [],
          };
          loose.set(node, entry);
          entries.push(entry);
        }
        const usage = p.usage as { prompt_tokens?: number; completion_tokens?: number } | null | undefined;
        entry.turns.push({
          callId: Number(p.call_id),
          seconds: Number(p.seconds) || 0,
          calls: Array.isArray(p.calls) ? (p.calls as UiCall[]) : [],
          screenshot: p.screenshot === true,
          tokens: usage ? (usage.prompt_tokens || 0) + (usage.completion_tokens || 0) : undefined,
          error: text(p.error) || undefined,
        });
        break;
      }
      case "action_started": {
        if (p.kind === "adb_command" || p.kind === "install_app" || p.kind === "device_ops") {
          const entry: AdbEntry = {
            type: "adb",
            id: `a${event.id}`,
            node: p.node_id,
            created: event.created,
            command: text(p.command),
            description: text(p.description),
          };
          adb.set(node, entry);
          entries.push(entry);
          break;
        }
        let entry = ui.get(node) || loose.get(node);
        if (!entry) {
          entry = {
            type: "ui",
            id: `u${event.id}`,
            node: p.node_id,
            created: event.created,
            goal: "UI action",
            actions: [],
            turns: [],
          };
          loose.set(node, entry);
          entries.push(entry);
        }
        entry.actions.push({
          kind: text(p.kind),
          description: text(p.description),
          status: "started",
        });
        break;
      }
      case "action_completed":
      case "action_failed":
      case "action_rejected":
      case "action_uncertain": {
        const status = event.kind.replace("action_", "");
        const pending = adb.get(node);
        // Older adb events carried no kind; they pair with the pending command.
        if (p.kind === "adb_command" || p.kind === "install_app" || p.kind === "device_ops" || (!p.kind && pending && !pending.result)) {
          if (pending) {
            pending.result = { ...p, status };
            pending.ended = event.created;
            adb.delete(node);
          }
          break;
        }
        const entry = ui.get(node) || loose.get(node);
        const action = entry?.actions.at(-1);
        if (action) {
          action.status = status;
          action.error = text(p.error) || undefined;
        } else if (status !== "completed")
          notice(event, "error", `Action ${status}`, text(p.error) || text(p.description));
        if (entry === loose.get(node) && status !== "completed") loose.delete(node);
        break;
      }
      case "ui_outcome": {
        const action = (ui.get(node) || loose.get(node))?.actions.at(-1);
        if (action) action.outcome = text(p.outcome);
        break;
      }
      case "node_feedback":
        notice(event, "error", `Attempt ${p.attempt} failed`, text(p.feedback));
        break;
      case "node_status": {
        const status = text(p.status);
        if (status === "failed" || status === "blocked")
          notice(event, "error", `Node ${status}`, text(p.error) || text(p.description));
        else if (status === "waiting_user")
          notice(event, "info", "Waiting for you", text((p.question as { question?: string })?.question));
        else if (status === "succeeded")
          notice(event, "info", "Node verified", "");
        else if (status === "waiting_device")
          notice(event, "info", "Waiting for device", text(p.description));
        break;
      }
      case "error":
      case "failed":
        notice(event, "error", "Run failed", text(p.message));
        break;
      case "completed":
        notice(event, "info", "Run completed", "");
        break;
      case "memories_updated": {
        const added = (p.added as { description?: string; content: string }[] | undefined) || [];
        notice(event, "info", p.reflection ? "Reflected on recent tasks" : "Memory updated", added.map((m) => m.description || m.content).join(" · "));
        break;
      }
      case "memory_failed":
        notice(event, "error", p.reflection ? "Reflection failed" : "Memory not updated", text(p.description));
        break;
      case "chats_searched": {
        const found = (p.found as { title: string; date: string }[] | undefined) || [];
        const queries = ((p.queries as string[] | undefined) || []).join(", ");
        notice(event, "info", "Searched earlier chats", `${queries || `${p.after || "…"} to ${p.before || "now"}`}: ${found.map((c) => `${c.title} (${c.date})`).join(" · ") || "nothing found"}`);
        break;
      }
      case "compacted":
        notice(event, "info", "Conversation summarised", text(p.description));
        break;
      case "compaction_failed":
        notice(event, "error", "Conversation not summarised", text(p.description));
        break;
    }
  }
  return entries;
}

function statusOf(entry: Entry, active: boolean): Status {
  switch (entry.type) {
    case "notice":
      return entry.level;
    case "call":
      return entry.error ? "error" : entry.output ? "done" : active ? "running" : "error";
    case "adb":
      return !entry.result ? (active ? "running" : "error") : entry.result.ok ? "done" : "error";
    case "ui": {
      if (entry.actions.some((a) => a.status === "rejected" || a.status === "uncertain"))
        return "error";
      if (!entry.result) return active ? "running" : "error";
      return entry.result.status === "completed" ? "done" : "error";
    }
  }
}

function duration(entry: Entry) {
  if (!("ended" in entry) || !entry.ended) return "";
  const seconds = entry.ended - entry.created;
  return seconds < 60 ? `${seconds.toFixed(1)}s` : `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
}

function headline(entry: Entry): { icon: ReactNode; title: string; line: string } {
  switch (entry.type) {
    case "call": {
      const output = entry.output || entry.preview || {};
      const line =
        entry.error ||
        text(output.summary) ||
        text(output.reason) ||
        text(output.reply) ||
        (entry.thinking ? "Thinking…" : "Waiting for the model…");
      return {
        icon: <Sparkles size={12} />,
        title: entry.images.length ? `${entry.stage} · saw screen` : entry.stage,
        line,
      };
    }
    case "adb":
      return {
        icon: <Terminal size={12} />,
        title: "ADB",
        line:
          entry.result && !entry.result.ok
            ? `${entry.command || entry.description} — ${adbFailure(entry.result)}`
            : entry.command || entry.description,
      };
    case "ui": {
      const failed = entry.actions.find((a) => a.error);
      const result = entry.result;
      return {
        icon: <MousePointerClick size={12} />,
        title: `UI agent · ${entry.actions.length} action${entry.actions.length === 1 ? "" : "s"}`,
        line: failed?.error
          ? `${failed.description} — ${failed.error}`
          : result
            ? `${result.status}: ${text(result.reason)}`
            : entry.goal,
      };
    }
    case "notice":
      return {
        icon: entry.level === "error" ? <AlertTriangle size={12} /> : <Info size={12} />,
        title: entry.title,
        line: entry.text,
      };
  }
}

function adbFailure(result: Event["payload"]) {
  if (text(result.error)) return text(result.error);
  if (result.exit_code != null) return `exit code ${result.exit_code}`;
  return `${text(result.status) || "failed"} (no details recorded)`;
}

function Thinking({ value, live }: { value: string; live: boolean }) {
  const box = useRef<HTMLPreElement>(null);
  useEffect(() => {
    if (live && box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [value, live]);
  return (
    <details className="trace-thinking" open={live}>
      <summary>
        <Brain size={11} /> Thinking {live && <LoaderCircle size={10} className="spin" />}
        <small>{value.length.toLocaleString()} chars</small>
      </summary>
      <pre ref={box}>{value}</pre>
    </details>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="trace-field">
      <span>{label}</span>
      <div>{children}</div>
    </div>
  );
}

function pretty(value: unknown) {
  return JSON.stringify(value, null, 2);
}

function Copy({ value }: { value: string }) {
  return (
    <button
      type="button"
      className="trace-copy"
      onClick={(event) => {
        event.preventDefault();
        void navigator.clipboard?.writeText(value);
      }}
    >
      Copy
    </button>
  );
}

const describeCall = (call: UiCall) =>
  `${call.name}(${Object.entries(call.arguments || {})
    .map(([key, value]) => `${key}=${JSON.stringify(value)}`)
    .join(", ")})`;

/** One UI agent turn. The summary is live; opening it fetches the exact request and raw reply. */
function UiTurnRow({ runId, turn, index }: { runId: string; turn: UiTurn; index: number }) {
  const [exchange, setExchange] = useState<UiExchange | null>(null);
  const [failure, setFailure] = useState("");
  const load = () => {
    if (exchange || failure || !runId) return;
    api<UiExchange>(`/runs/${runId}/ui/${turn.callId}`)
      .then(setExchange)
      .catch((error: Error) => setFailure(error.message));
  };
  const user = exchange?.request.messages[1].content;
  const state = typeof user === "string" ? user : (user || []).map((part) => part.text || "").join("\n");
  const instructions = exchange?.request.messages[0].content || "";
  const shown = turn.calls.length ? turn.calls.map(describeCall).join("  +  ") : "no tool call";
  return (
    <details
      className={`ui-turn ${turn.error ? "error" : "acted"}`}
      onToggle={(event) => (event.target as HTMLDetailsElement).open && load()}
    >
      <summary>
        <b>#{index + 1}</b>
        <span className="ui-pick">{turn.error || shown}</span>
        <small>
          {turn.screenshot ? "with screenshot · " : ""}
          {turn.seconds.toFixed(1)}s{turn.tokens ? ` · ${turn.tokens.toLocaleString()} tokens` : ""}
        </small>
      </summary>
      {failure && <p className="error-text">{failure}</p>}
      {!exchange && !failure && <p className="subtle">Loading…</p>}
      {exchange && (
        <div className="ui-body">
          <details className="trace-raw" open>
            <summary>
              Input · what the agent read · {state.length.toLocaleString()} chars
              <Copy value={state} />
            </summary>
            <pre>{state}</pre>
          </details>
          <Field label={`Output · ${turn.calls.length} tool call${turn.calls.length === 1 ? "" : "s"}`}>
            <ol className="trace-actions">
              {turn.calls.map((call, i) => (
                <li key={i}>
                  <code>{describeCall(call)}</code>
                </li>
              ))}
            </ol>
          </Field>
          <details className="trace-raw">
            <summary>Static instructions · {instructions.length.toLocaleString()} chars</summary>
            <pre>{instructions}</pre>
          </details>
          <details className="trace-raw">
            <summary>
              Full request JSON · {exchange.request.tools.length} tools
              <Copy value={pretty(exchange.request)} />
            </summary>
            <pre>{pretty(exchange.request)}</pre>
          </details>
          <Field label="Output · raw response">
            <pre className={`trace-code ${exchange.error ? "error" : ""}`}>
              {exchange.error || pretty(exchange.response)}
            </pre>
          </Field>
        </div>
      )}
    </details>
  );
}

function Body({ entry, status, runId }: { entry: Entry; status: Status; runId: string }) {
  if (entry.type === "call") {
    const output = entry.output || entry.preview || {};
    const shown = ["summary", "reason", "kind", "command", "goal", "inputs", "question", "output", "reply"];
    const rest = Object.fromEntries(Object.entries(output).filter(([key]) => !shown.includes(key)));
    const inputs = Array.isArray(output.inputs) ? (output.inputs as string[]) : [];
    return (
      <>
        {entry.images.length > 0 && (
          <Field label="Screenshot the agent was shown">
            {entry.images.map((id) => (
              <a key={id} href={`/api/runs/${runId}/images/${id}`} target="_blank" rel="noreferrer">
                <img
                  className="trace-shot"
                  src={`/api/runs/${runId}/images/${id}`}
                  alt="Emulator screenshot the agent was shown"
                  loading="lazy"
                />
              </a>
            ))}
          </Field>
        )}
        {entry.thinking && <Thinking value={entry.thinking} live={status === "running"} />}
        {entry.error && <p className="error-text">{entry.error}</p>}
        {text(output.summary || output.reason) && (
          <Field label="Decision">
            <Rich>{text(output.summary || output.reason)}</Rich>
          </Field>
        )}
        {text(output.kind) && (
          <Field label="Tool call">
            <code className="trace-kind">{text(output.kind)}</code>
            {text(output.command) && <pre className="trace-code">{text(output.command)}</pre>}
            {text(output.goal) && <p>Goal: {text(output.goal)}</p>}
            {inputs.length > 0 && <p>Inputs: {inputs.join(", ")}</p>}
            {text(output.question) && <p>Question: {text(output.question)}</p>}
          </Field>
        )}
        {text(output.output || output.reply) && (
          <Field label="Output">
            <Rich>{text(output.output || output.reply)}</Rich>
          </Field>
        )}
        {Object.keys(rest).length > 0 && (
          <details className="trace-raw">
            <summary>Structured output</summary>
            <pre>{JSON.stringify(rest, null, 2)}</pre>
          </details>
        )}
      </>
    );
  }
  if (entry.type === "adb") {
    const r = entry.result;
    return (
      <>
        {entry.description && <p className="subtle">{entry.description}</p>}
        {entry.command && <pre className="trace-code">$ {entry.command}</pre>}
        {!r && <p className="subtle">Running…</p>}
        {r && !r.ok && <p className="error-text">{adbFailure(r)}</p>}
        {r?.exit_code != null && <Field label="Exit code">{String(r.exit_code)}</Field>}
        {text(r?.output) && (
          <Field label="stdout">
            <pre className="trace-code">{text(r?.output)}</pre>
          </Field>
        )}
        {text(r?.stderr) && (
          <Field label="stderr">
            <pre className="trace-code error">{text(r?.stderr)}</pre>
          </Field>
        )}
      </>
    );
  }
  if (entry.type === "ui") {
    const r = entry.result;
    return (
      <>
        <Field label="Goal">{entry.goal}</Field>
        {entry.turns.length > 0 && (
          <Field label={`UI agent turns · ${entry.turns.length}`}>
            {entry.turns.map((turn, index) => (
              <UiTurnRow key={turn.callId} runId={runId} turn={turn} index={index} />
            ))}
          </Field>
        )}
        {entry.actions.length > 0 && (
          <ol className="trace-actions">
            {entry.actions.map((action, index) => (
              <li key={index} className={action.error ? "failed" : ""}>
                {action.description}
                {action.status !== "completed" && action.status !== "started" && (
                  <strong> · {action.status}</strong>
                )}
                {action.error && <span className="error-text"> — {action.error}</span>}
                {action.outcome && <small className="trace-outcome">{action.outcome}</small>}
              </li>
            ))}
          </ol>
        )}
        {r && (
          <Field label={`Result: ${r.status}`}>
            <p>{text(r.reason)}</p>
            {text(r.evidence) && (
              <details className="trace-raw">
                <summary>Screen evidence</summary>
                <pre>{text(r.evidence)}</pre>
              </details>
            )}
          </Field>
        )}
      </>
    );
  }
  return entry.text ? <p className={entry.level === "error" ? "error-text" : "subtle"}>{entry.text}</p> : null;
}

function Row({
  entry,
  active,
  clock,
  runId,
}: {
  entry: Entry;
  active: boolean;
  clock: (t: number) => string;
  runId: string;
}) {
  const status = statusOf(entry, active);
  // Live work opens; errors stay compact because the header already shows the message.
  const [open, setOpen] = useState(status === "running");
  const { icon, title, line } = headline(entry);
  const expandable = entry.type !== "notice" || !!entry.text;
  return (
    <details
      className={`trace-row ${status} ${entry.type}`}
      open={open}
      onToggle={(event) => setOpen((event.target as HTMLDetailsElement).open)}
    >
      <summary>
        <span className="trace-status">
          {status === "running" ? (
            <LoaderCircle size={11} className="spin" />
          ) : status === "done" ? (
            <Check size={11} />
          ) : status === "error" ? (
            <X size={11} />
          ) : (
            icon
          )}
        </span>
        {entry.type !== "notice" && <span className="trace-icon">{icon}</span>}
        <div className="trace-heading">
          <strong>
            {title}
            {entry.node && <em>{entry.node}</em>}
          </strong>
          {line && <small>{line}</small>}
        </div>
        <time>
          {duration(entry) || clock(entry.created)}
        </time>
        {expandable && <ChevronRight size={11} className="trace-chevron" />}
      </summary>
      {expandable && (
        <div className="trace-body">
          <Body entry={entry} status={status} runId={runId} />
        </div>
      )}
    </details>
  );
}

export default function Trace({
  events,
  active,
  clock,
  runId,
}: {
  events: Event[];
  active: boolean;
  clock: (t: number) => string;
  runId: string;
}) {
  const [errorsOnly, setErrorsOnly] = useState(false);
  const [node, setNode] = useState("");
  const entries = useMemo(() => buildTrace(events), [events]);
  const nodes = [...new Set(entries.map((e) => e.node).filter(Boolean))] as string[];
  const shown = entries
    .filter((entry) => !node || entry.node === node)
    .filter((entry) => !errorsOnly || statusOf(entry, active) === "error")
    .reverse();
  const errors = entries.filter((entry) => statusOf(entry, active) === "error").length;
  if (!entries.length)
    return (
      <div className="empty-activity">
        <span />
        <p>
          Agent thinking, tool calls, and errors will appear here.
        </p>
      </div>
    );
  return (
    <div className="trace">
      <div className="trace-filters">
        <button className={!errorsOnly ? "selected" : ""} onClick={() => setErrorsOnly(false)}>
          All {entries.length}
        </button>
        <button className={errorsOnly ? "selected" : ""} onClick={() => setErrorsOnly(true)}>
          Errors {errors}
        </button>
        {nodes.length > 0 && (
          <select value={node} onChange={(e) => setNode(e.target.value)} aria-label="Filter by agent">
            <option value="">All agents</option>
            {nodes.map((id) => (
              <option key={id} value={id}>
                {id}
              </option>
            ))}
          </select>
        )}
      </div>
      {shown.map((entry) => (
        <Row key={entry.id} entry={entry} active={active} clock={clock} runId={runId} />
      ))}
      {!shown.length && <p className="subtle">Nothing matches this filter.</p>}
    </div>
  );
}
