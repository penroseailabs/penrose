/**
 * What the "For you" screens share (components/ForYou.tsx, the settings, the voice tray): how a briefing's origin
 * reads, and the user's choice of apps to be briefed on (backend/app/briefings, api/foryou.py).
 */

import { useCallback, useEffect, useState } from "react";
import { api, type BriefedApp, type Briefing, type MutedConversation } from "../api";
import type { Picks } from "./models";

/** Where a briefing is from, in a line: the app (and its website), then the conversation when it says more. */
export function origin(item: Briefing) {
  const app = item.app.site ? `${item.app.label} (${item.app.site})` : item.app.label;
  return item.name && item.name !== item.app.label ? `${app} · ${item.name}` : app;
}

/** `models`: what sorting and preparing run on; null, the same as the user's tasks. */
type Choices = { apps: BriefedApp[]; muted: MutedConversation[]; models: Picks | null };

/**
 * The apps the phone has had notifications from, each with whether the user is briefed on it, the conversations
 * they muted, and the models for what notifications start. `choices` is null until loaded. A choice shows at once
 * and goes back if the server refuses; `problem` then says why.
 */
export function useBriefedApps() {
  const [choices, setChoices] = useState<Choices | null>(null);
  const [problem, setProblem] = useState("");

  const load = useCallback(async () => {
    try {
      setChoices(await api<Choices>("/for-you/apps"));
    } catch (e) {
      setProblem((e as Error).message);
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);

  const setApp = useCallback(
    async (app: BriefedApp, on: boolean) => {
      setProblem("");
      setChoices((c) => c && { ...c, apps: c.apps.map((a) => (a.package === app.package ? { ...a, on } : a)) });
      try {
        setChoices(await api<Choices>(`/for-you/apps/${encodeURIComponent(app.package)}`, "PUT", { on }));
      } catch (e) {
        setProblem((e as Error).message);
        await load();
      }
    },
    [load],
  );

  const unmute = useCallback(
    async (muted: MutedConversation) => {
      setProblem("");
      try {
        setChoices(await api<Choices>("/for-you/unmute", "POST", { package: muted.package, key: muted.key }));
      } catch (e) {
        setProblem((e as Error).message);
        await load();
      }
    },
    [load],
  );

  const setModels = useCallback(
    async (models: Picks | null) => {
      setProblem("");
      setChoices((c) => c && { ...c, models });
      try {
        setChoices(await api<Choices>("/for-you/models", "PUT", { models }));
      } catch (e) {
        setProblem((e as Error).message);
        await load();
      }
    },
    [load],
  );

  return { choices, problem, setApp, unmute, setModels };
}
