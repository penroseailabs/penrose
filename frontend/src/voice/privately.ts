import { api, type Message, type Run } from "../api";
import type { PrivateAsk, PrivateEntry } from "../components/PrivateFields";

type Detail = { messages: Message[]; runs: Run[] };

/** What the task in `chatId` waits to be given privately, for the keypad to open on its form: the orchestrator's
 * ask (a card, a password), or a phone step's code or password; undefined when it asks for nothing private. */
export async function privateAskOf(chatId: string | undefined): Promise<PrivateAsk | undefined> {
  if (!chatId) return undefined;
  const run = (await api<Detail>(`/chats/${chatId}`)).runs.at(-1);
  if (run?.status !== "waiting_user") return undefined;
  const step = run.questions.find((q) => q.sensitive);
  if (step) return { kind: /password/i.test(step.question) ? "password" : "code", label: step.question.slice(0, 80) };
  return run.private_asked;
}

/** Give values privately to the task in `chatId`; returns what to tell the user. One value for a phone step that
 * waits for a code or password goes straight into the phone; anything else is kept as private entries, which a
 * step types where the task needs them. Never handed to the desk: the models would see it. */
export async function sendPrivately(chatId: string | undefined, entries: PrivateEntry[]): Promise<string> {
  if (!chatId) return "Open the task this is for first, so I know where to send it. Nothing was sent.";
  const run = (await api<Detail>(`/chats/${chatId}`)).runs.at(-1);
  const step = run?.status === "waiting_user" ? run.questions.find((q) => q.sensitive) : undefined;
  if (run && step && entries.length === 1) {
    await api(`/runs/${run.id}/reply`, "POST", { content: entries[0].value, node_id: step.node_id, sensitive: true });
    return "Sending it to the phone.";
  }
  await api(`/chats/${chatId}/private`, "POST", { entries });
  return "Sent privately.";
}
