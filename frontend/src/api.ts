export type Chat = {
  id: string;
  title: string;
  created: number;
  /** The latest task's status and the last activity, for the task list. */
  status?: string | null;
  updated?: number;
};
/** A file on a message (backend/app/attachments.py): a photo or document penrose reads, a file put on the phone, or
 *  what penrose kept for the chat (a call's recording). */
export type Attachment = {
  id: number;
  /** How the agents name it: `file:12`. */
  ref: string;
  name: string;
  kind: "image" | "document" | "file" | "audio";
  mime: string;
  size: number;
  /** "read": for penrose to read. "phone": uploaded to the phone's Download/penrose folder. "call": the recording
   *  of a call penrose made. */
  purpose: "read" | "phone" | "call";
  phone_path: string | null;
  w: number;
  h: number;
  /** What kind of document it is, or a note on it ("PDF, 6 pages"). */
  detail: string;
};
export type Message = {
  id: string;
  /** "user", "assistant", or "notification": what an app on the phone showed, passed to this chat
   *  (backend/app/notifications.py). */
  role: string;
  content: string;
  created: number;
  source?: string | null;
  /** The reasoning behind a Relay reply, shown collapsed above it. */
  thinking?: string | null;
  /** What Relay says aloud for this reply, when it was written for a listening user (voice mode). */
  spoken?: string | null;
  streaming?: boolean;
  /** Shown at once while it is still on its way to the server. */
  sending?: boolean;
  /** The files it carries: what the user sent with it, or a call's recording on a call's card. */
  attachments?: Attachment[];
};
/** What the chat keeps of one of penrose's turns besides its message (backend/app/api/chats.py `thoughts`): the
 *  notes it kept for its own later turns, and its reasoning when it said nothing. Shown while penrose is being
 *  tested; `id` is the turn's call, and the id of the message it wrote when it wrote one. */
export type Thought = { id: string; created: number; thinking: string; notes: string[] };
/** One decision a subagent made, as recorded on its node (the UI agent's own taps are not steps). */
export type Step = {
  step?: boolean;
  /** Unix seconds; steps recorded before the orchestrator have none. */
  at?: number;
  kind?: string;
  summary: string;
  goal?: string;
  command?: string;
  action?: string;
  status?: string;
  result?: { status?: string; ok?: boolean; reason?: string };
};
/** The Google account signed in on the phone; null when the phone cannot be read. */
export type GooglePhone = { accounts: string[] | null };
/** "Simplify sign-ins on this device" (backend/app/gateway/simple_signins.py): the Settings switch (`on`), and what
 * the phone has: Relay's screen lock (`enabled`) and Chrome signed in with sync. Null fields: the phone is away.
 * `chrome` after turning it on: "consent" when the user still presses “Yes, I’m in” in Chrome on the phone. */
export type SimpleSignins = {
  on: boolean;
  account: string | null;
  enabled: boolean | null;
  chrome_signed_in: boolean | null;
  foreign_lock: boolean;
  chrome?: "signed_in" | "consent" | null;
};
/** Relay's guided Google sign-in on the phone (backend/app/gateway/signin.py). "phone": the user taps Yes on their
 * own phone (and `number`, when Google shows one); "need_phone": Google asks to confirm the account's phone number,
 * which the user gives in the window; "password": the user types their password on the phone; "agree": Google's
 * terms wait on the phone; "manual": the user finishes on the phone; "failed": try again. */
export type GoogleSignIn = {
  state: "idle" | "working" | "phone" | "need_phone" | "need_password" | "agree" | "done" | "manual" | "failed" | "stopped";
  message: string;
  number: string | null;
  /** need_phone: the last digits of the account's phone number, as Google shows them. */
  hint: string | null;
  account: string | null;
  email: string | null;
  /** How the user chose to sign in: the "Tap Yes" prompt, or their password typed on the phone. */
  method: SignInMethod | null;
};
export type SignInMethod = "prompt" | "password";
export type NodeState = {
  status: string;
  attempt: number;
  slot?: string;
  error?: string;
  /** Why the step waits for the phone (status waiting_device), as the run put it. */
  waiting?: string;
  output: string;
  turns: Step[];
  /** The orchestrator's messages to this subagent: relayed answers and corrections. */
  guidance?: string[];
  verification?: { passed: boolean; reason: string; evidence: string[] };
  spec: {
    id: string;
    objective: string;
    steps: string[];
    depends_on: string[];
    system_prompt: string;
    tools: string[];
    verifier: string;
  };
  /** kind "login": the user signs in to the app on the phone themselves. */
  question?: { question: string; sensitive: boolean; kind?: "login" | null };
};
export type Run = {
  id: string;
  status: string;
  request: string;
  /** Unix seconds; runs started before this was recorded have none. */
  created?: number;
  provider?: string;
  error?: string;
  nodes: Record<string, NodeState>;
  context?: { summary: string };
  dag?: { summary: string } | null;
  /** User messages the orchestrator has not read yet. */
  inbox?: string[];
  /** What the orchestrator's question asks the user to give privately: the form the window shows for it. */
  private_asked?: { kind: "card" | "password" | "code" | "other"; label?: string };
  /** The answers the orchestrator's question offers: buttons under it, each sent as the user's own message. */
  options?: AnswerOption[];
  questions: {
    node_id: string | null;
    question: string;
    sensitive: boolean;
    kind?: "login" | null;
  }[];
};
/** One request's tokens in the same terms for every provider: the whole prompt, what was read from and
 * written to the provider's cache, and the output. */
export type UsageTokens = { input: number; cached: number; written: { short: number; long: number }; output: number };
/** One model request and what it cost: credits are US cents (null: no known price). `usage` is the provider's
 * usage JSON as it came; `peak` is DeepSeek's peak price (the others have none). */
export type UsageRequest = {
  id: number;
  run_id: string;
  /** The agent session that made it (a subagent's own session), when there is one. */
  call_id: string | null;
  stage: string | null;
  provider: string;
  model: string;
  created: number;
  peak: boolean;
  credits: number | null;
  usage: Record<string, unknown>;
  tokens: UsageTokens;
};
/** A chat's usage: the requests and credits, the tokens summed, and each provider's usage JSON summed field by field. */
export type ChatUsage = {
  requests: number;
  peak_requests: number;
  credits: number;
  tokens?: UsageTokens;
  usage: Record<string, Record<string, unknown>>;
};
/** A message the user sent while the task worked. steer (Enter): in the chat, read at the task's next step.
 * next (Ctrl+Enter): queued, sent as the next message once the task ends. */
export type Pending = {
  id: number;
  run_id: string;
  mode: "steer" | "next";
  content: string;
  message_id: string | null;
  created: number;
};
export type AnswerOption = { label: string; message: string };
/** One briefing in the "For you" list (backend/app/briefings): what arrived from one conversation of an app the
 * user chose, in penrose's words. Once opened it carries its chat, its task's status and, while it waits for the
 * user, what it found and offers. */
export type Briefing = {
  id: number;
  app: { package: string; label: string; site?: string };
  name: string;
  headline: string;
  why: string;
  /** What is asked of the user; "" when nothing. */
  asks: string;
  /** The points that matter, from the sort; may be empty. */
  gist: string[];
  count: number;
  latest: number;
  urgent: boolean;
  /** The user has opened it. */
  seen: boolean;
  chat_id?: string;
  status?: string;
  found?: string;
  options?: AnswerOption[];
};
/** A briefing with its details (POST /for-you/{id}/look): the points that matter, the notifications it came from
 * (oldest first; `earlier` more are not shown) and, once penrose has prepared it, what it found as markdown. */
export type BriefingDetail = Briefing & {
  arrived: { from: string; text: string; at: number }[];
  earlier: number;
  prepared?: { content: string; at: number };
};
/** What penrose has for the user when they open it: briefings, the chats that wait for their answer, and what
 * else came in on the apps they are briefed on (`more`: how many of those are not shown). */
export type ForYou = {
  ready: Briefing[];
  waiting: { chat_id: string; title: string; updated: number; asks: string; needs: string }[];
  came_in: Briefing[];
  more: number;
  unseen: number;
  apps_on: number;
};
/** An app the phone had notifications from, for choosing which to be briefed on; and a muted conversation. */
/** `people`: its notifications name who wrote (a chat or mail app), not just what the app says. */
export type BriefedApp = { package: string; label: string; count: number; people: boolean; on: boolean };
export type MutedConversation = { package: string; key: string; name: string; label?: string };
/** What a chat waits for among the phone's notifications (backend/app/watches.py). With `then`, the first one
 * that shows it has happened starts a task on that instruction, limited to `apps`. */
export type Watch = {
  id: number;
  what: string;
  match: string[];
  then?: string;
  apps?: string[];
  until: string;
  expires: number;
  created: number;
};
export type Event = {
  id: number;
  kind: string;
  created: number;
  payload: {
    stage?: string;
    node_id?: string;
    output?: unknown;
    description?: string;
    [key: string]: unknown;
  };
};
export type Device = {
  id: string;
  connected: boolean;
  owner: string | null;
  fps: number;
  /** Its screen is off (and soon in Doze) after a while unused; any use wakes it (gateway/sleep.py). */
  asleep?: boolean;
  width?: number;
  height?: number;
  /** offline, starting, hooking, setup (being updated), ready or broken (backend/app/gateway/watch.py). */
  state?: string;
  detail?: string;
  /** Google accounts on the phone, read when they may have changed; null until read. */
  accounts?: string[] | null;
  /** Relay's guided Google sign-in on this phone, while there is one. */
  signin?: GoogleSignIn | null;
};
/** One thing Relay remembers: a named, typed note (see backend/app/runtime/memory.py). */
export type Memory = {
  id: string;
  name: string;
  type: "user" | "person" | "thing" | "reference" | "feedback" | "open" | "app";
  description: string;
  content: string;
  app: string;
  /** user: added in the panel; chat: learned from a conversation or asked for in one; reflection: seen across tasks. */
  source: "user" | "chat" | "reflection" | null;
  chat_id: string | null;
  created: number;
  updated: number;
  expires: string;
};
/** The few paragraphs Relay writes about the user from the memories. */
export type Provider = "deepseek" | "openai" | "anthropic";
export const PROVIDERS: { id: Provider; label: string }[] = [
  { id: "deepseek", label: "DeepSeek" },
  { id: "openai", label: "GPT-6" },
  { id: "anthropic", label: "Claude" },
];
/** Whether a provider has an API key, per /api/health. */
export function providerReady(health: Health | null, id: Provider) {
  if (!health) return true;
  if (id === "deepseek") return health.deepseek_configured;
  if (id === "openai") return !!health.openai_configured;
  return !!health.anthropic_configured;
}
export type Health = {
  status: string;
  deepseek_configured: boolean;
  openai_configured?: boolean;
  anthropic_configured?: boolean;
  /** Set when the API runs inside the desktop app. */
  desktop?: boolean;
  gateway_connected: boolean;
  gateway_outdated?: boolean;
  worker_online: boolean;
  /** Voice mode talks to the reception desk, one Realtime model (backend/app/runtime/desk.py), not the front desk. */
  voice_desk?: boolean;
  fast_model: string;
  heavy_model: string;
  stream_fps: number;
};
export async function api<T>(
  path: string,
  method = "GET",
  body?: unknown,
): Promise<T> {
  const send = () =>
    fetch("/api" + path, {
      method,
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
  let response = await send();
  // The dev proxy marks a reply when the API is restarting after a code change. Reads are safe to repeat,
  // so wait up to ~10 s for it to come back.
  const restarting = (r: Response) => r.status === 503 && r.headers.get("X-Relay-Restarting") === "1";
  for (let wait = 0; restarting(response) && method === "GET" && wait < 10; wait++) {
    await new Promise((resolve) => setTimeout(resolve, 1000));
    response = await send();
  }
  if (!response.ok) {
    if (response.status === 401 && path !== "/auth/login") {
      window.dispatchEvent(new Event("relay-session-expired"));
    }
    const error = await response.json().catch(() => ({}));
    // FastAPI reports validation problems as a list of {msg}.
    const detail = Array.isArray(error.detail)
      ? error.detail
          .map((d: { msg?: string }) => (d.msg || "").replace(/^Value error, /, ""))
          .filter(Boolean)
          .join(" ")
      : error.detail;
    throw new Error(
      typeof detail === "string" && detail
        ? detail
        : `Request failed (${response.status})`,
    );
  }
  return response.json();
}
