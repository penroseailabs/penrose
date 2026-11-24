import type { Health, NodeState, Run } from "../api";

/** The colour a status means: work = Relay at work, done, wait = needs you, err = a problem. */
export type Tone = "work" | "done" | "wait" | "err" | "none";

export const ACTIVE = ["queued", "running"];

/** A task (run) in plain words. */
export function taskStatus(status?: string | null): { text: string; tone: Tone } {
  switch (status) {
    case "queued":
      return { text: "Starting", tone: "work" };
    case "running":
      return { text: "Working", tone: "work" };
    case "waiting_user":
      return { text: "Needs you", tone: "wait" };
    case "stopped":
      return { text: "Paused", tone: "wait" };
    case "completed":
      return { text: "Done", tone: "done" };
    case "failed":
      return { text: "Couldn’t finish", tone: "err" };
    case "interrupted":
      return { text: "Interrupted", tone: "err" };
    case "cancelled":
      return { text: "Discarded", tone: "none" };
    default:
      return { text: "", tone: "none" };
  }
}

/** The mark beside one planned step. */
export type Mark = "ok" | "now" | "wait" | "err" | "later";
export function stepMark(node: NodeState, runActive: boolean): Mark {
  switch (node.status) {
    case "succeeded":
      return "ok";
    case "running":
    case "verifying":
      return runActive ? "now" : "later";
    case "waiting_user":
    case "waiting_device":
    case "blocked":
    case "unfinished":
      return "wait";
    case "failed":
    case "rejected":
    case "uncertain":
      return "err";
    default:
      return "later";
  }
}

export function cardTitle(run: Run): string {
  switch (run.status) {
    case "completed":
      return "Finished";
    case "waiting_user":
      return "Waiting for you";
    case "stopped":
      return "Paused";
    case "failed":
      return "Couldn’t finish";
    case "interrupted":
      return "Interrupted";
    case "cancelled":
      return "Discarded";
    default:
      return "Working on it";
  }
}

/** The phone and the services behind it, in one line. */
export function phoneStatus(health: Health | null, connected: boolean): { text: string; tone: Tone } {
  if (!health) return { text: "Connecting", tone: "none" };
  if (connected && health.worker_online) return { text: "Phone ready", tone: "done" };
  if (!health.worker_online) return { text: "penrose is starting", tone: "work" };
  return { text: "Phone starting", tone: "work" };
}

export const clock = (seconds: number) =>
  new Date(seconds * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });

export function dayLabel(seconds: number): string {
  const date = new Date(seconds * 1000);
  const now = new Date();
  const yesterday = new Date(now.getTime() - 86_400_000);
  if (date.toDateString() === now.toDateString()) return "Today";
  if (date.toDateString() === yesterday.toDateString()) return "Yesterday";
  return "Earlier";
}
