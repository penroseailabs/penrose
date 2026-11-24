import type { Event, Message, Run, Step, Thought } from "../api";

/** Add a batch of streamed events, keeping one preview per model call (they replace each other). One new array
 *  per batch, however many events it holds. */
export function collectEvents(previous: Event[], batch: Event[]): Event[] {
  if (!batch.length) return previous;
  const seen = new Set(previous.map((event) => event.id));
  const fresh = batch.filter((item) => !seen.has(item.id) && seen.add(item.id));
  if (!fresh.length) return previous;
  const next = [...previous];
  // Where each call's preview sits, so a newer one replaces it in place.
  const deltas = new Map<unknown, number>();
  next.forEach((event, i) => event.kind === "stage_delta" && deltas.set(event.payload.call_id, i));
  for (const item of fresh) {
    const at = item.kind === "stage_delta" ? deltas.get(item.payload.call_id) : undefined;
    if (at !== undefined) next[at] = item;
    else {
      if (item.kind === "stage_delta") deltas.set(item.payload.call_id, next.length);
      next.push(item);
    }
  }
  return next;
}

/** The newest event id seen, to resume a stream after it. */
export function lastEventId(events: Event[]) {
  return events.reduce((max, event) => Math.max(max, event.id), 0);
}

/** Stages that speak to the user. Older chats also hold "Final response" replies. */
const PUBLIC_STAGES = ["Fast response", "Orchestrator", "Final response"];
/** Step results that count as done; "answered" is a question the orchestrator answered. */
const SUCCEEDED = ["completed", "installed", "already_installed", "answered"];

/** One thing Relay did on the phone, in its own words. */
export type ActivityStep = {
  id: string;
  node: string;
  at: number;
  summary: string;
  outcome?: string;
  ok?: boolean;
};

/** A search of earlier conversations (the gate's recall, the orchestrator's look_up): what was searched, and the
 *  conversations it found, each a link. */
export type ChatSearch = { queries: string[]; after: string; before: string; found: { chat_id: string; title: string; date: string }[] };

export type TimelineItem =
  /** `earlier`: the turns just before it that wrote nothing to the user, shown in one fold with its own reasoning. */
  | { type: "message"; id: string; created: number; message: Message; thinking: string; notes: string[]; earlier: Thought[] }
  | { type: "search"; id: string; created: number; search: ChatSearch }
  | { type: "thinking"; id: string; created: number; thinking: string }
  /** Finished turns in a row that wrote nothing to the user: each one's reasoning and the notes it kept for itself. */
  | { type: "thought"; id: string; created: number; turns: Thought[] }
  | { type: "run"; id: string; created: number; run: Run; steps: ActivityStep[] };

/** The source of the message that shows a finished call (backend/app/runtime/call.py): drawn as a card. */
export const CALL = "Call";

const NO_NOTES: string[] = [];
const NO_TURNS: Thought[] = [];

const isPublic = (message: Message) =>
  message.role === "user" || message.role === "notification" || !message.source ||
  PUBLIC_STAGES.includes(message.source) || message.source === CALL;

export function stepsOf(run: Run): ActivityStep[] {
  const steps: ActivityStep[] = [];
  for (const [node, state] of Object.entries(run.nodes || {}))
    (state.turns || []).forEach((turn: Step, index) => {
        if (!turn.step || !turn.at) return;
        const result = turn.result;
        steps.push({
          id: `${run.id}:${node}:${index}`,
          node,
          at: turn.at,
          summary: turn.summary,
          outcome: result?.status || (result ? (result.ok ? "ok" : "failed") : undefined),
          ok: !result ? undefined : SUCCEEDED.includes(result.status || "") || result.ok === true,
        });
    });
  return steps.sort((a, b) => a.at - b.at);
}

/**
 * The conversation as the user sees it: their messages, Relay's replies (each with its reasoning),
 * and one progress card per task, placed where the task began. `thoughts`: what the chat keeps of the turns
 * besides their messages (notes to itself, the reasoning of a turn that said nothing), shown while penrose is
 * being tested: on the turn's message, or as an entry of its own where the turn ended.
 */
export function conversation(
  messages: Message[],
  runs: Run[],
  events: Event[],
  active: boolean,
  thoughts: Thought[] = [],
): TimelineItem[] {
  // By turn: the chat's own, then the ones still only in the stream (the chat is reloaded a moment after).
  const kept = new Map(thoughts.map((thought) => [thought.id, thought]));
  for (const event of events)
    if (event.kind === "turn_thought" && event.payload.call_id && !kept.has(String(event.payload.call_id)))
      kept.set(String(event.payload.call_id), {
        id: String(event.payload.call_id),
        created: event.created,
        thinking: String(event.payload.thinking || ""),
        notes: Array.isArray(event.payload.notes) ? event.payload.notes.map(String) : [],
      });
  const thinking = new Map<string, string>();
  const finished = new Set<string>();
  const started = new Map<string, Event>();
  const drafts = new Map<string, string>();
  for (const event of events) {
    const call = String(event.payload.call_id ?? "");
    if (!call) continue;
    if (event.kind === "stage_started" && PUBLIC_STAGES.includes(event.payload.stage || "")) started.set(call, event);
    if (event.kind === "stage_thinking")
      thinking.set(call, event.payload.reset ? "" : (thinking.get(call) || "") + String(event.payload.text || ""));
    if (event.kind === "stage_delta" && typeof event.payload.message === "string") drafts.set(call, event.payload.message);
    if (["stage_output", "stage_error", "agent_message"].includes(event.kind)) finished.add(call);
  }
  const saved = new Map(messages.filter(isPublic).map((message) => [message.id, message]));
  for (const event of events)
    if (event.kind === "agent_message" || event.kind === "call_recorded") {
      const message = event.payload.message as Message;
      if (!saved.has(message.id)) saved.set(message.id, message);
    }
  const items: TimelineItem[] = [...saved.values()].map((message) => ({
    type: "message",
    id: message.id,
    created: message.created,
    message,
    thinking: message.thinking || thinking.get(message.id) || "",
    notes: kept.get(message.id)?.notes || NO_NOTES,
    earlier: NO_TURNS,
  }));
  for (const thought of kept.values())
    if (!saved.has(thought.id)) items.push({ type: "thought", id: thought.id, created: thought.created, turns: [thought] });
  if (active)
    for (const [call, event] of started) {
      if (finished.has(call) || saved.has(call) || kept.has(call)) continue;
      const draft = drafts.get(call) || "";
      // A turn for one subagent works in the background; it shows only once it writes to you.
      if (event.payload.node_id && !draft) continue;
      if (draft)
        items.push({
          type: "message",
          id: call,
          created: event.created,
          message: { id: call, role: "assistant", content: draft, created: event.created, source: event.payload.stage, streaming: true },
          thinking: thinking.get(call) || "",
          notes: NO_NOTES,
          earlier: NO_TURNS,
        });
      else items.push({ type: "thinking", id: call, created: event.created, thinking: thinking.get(call) || "" });
    }

  for (const event of events)
    if (event.kind === "chats_searched")
      items.push({ type: "search", id: `search:${event.id}`, created: event.created, search: event.payload as unknown as ChatSearch });

  const userMessages = messages.filter((m) => m.role === "user");
  for (const run of runs) {
    if (!Object.keys(run.nodes || {}).length) continue; // answered directly: nothing to track
    const steps = stepsOf(run);
    const request = userMessages.find((m) => m.content === run.request);
    // Just after the request (its first reply usually comes before the work starts).
    const anchor = run.created ?? request?.created ?? steps[0]?.at ?? Number.MAX_SAFE_INTEGER;
    items.push({ type: "run", id: run.id, created: anchor + 0.5, run, steps });
  }
  return gatherThoughts(items.sort((a, b) => a.created - b.created));
}

/** Finished turns in a row that said nothing become one entry, and the reply of penrose's that follows them takes
 *  them into its own fold. Anything else between them (a card, a search, a turn still running) ends the row. */
function gatherThoughts(items: TimelineItem[]): TimelineItem[] {
  const out: TimelineItem[] = [];
  let row: Thought[] = [];
  const flush = () => {
    if (row.length) out.push({ type: "thought", id: row[0].id, created: row[0].created, turns: row });
    row = [];
  };
  for (const item of items) {
    if (item.type === "thought") {
      row = [...row, ...item.turns];
      continue;
    }
    const reply = item.type === "message" && !["user", "notification"].includes(item.message.role) && item.message.source !== CALL;
    if (row.length && reply) {
      out.push({ ...item, earlier: row });
      row = [];
      continue;
    }
    flush();
    out.push(item);
  }
  flush();
  return out;
}
