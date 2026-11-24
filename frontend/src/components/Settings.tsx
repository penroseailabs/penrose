import { useEffect, useState, type ReactNode } from "react";
import { api, type SimpleSignins } from "../api";
import { COMPANION } from "../companion/flag";
import { useBriefedApps } from "../lib/forYou";
import { canManageKeys } from "../lib/keys";
import { picksLabel } from "../lib/models";
import type { ThemeChoice } from "../lib/theme";
import type { Workspace } from "../lib/useWorkspace";
import ClaudeMark from "./ClaudeMark";
import { ModelPicker } from "./ModelsMenu";
import Sheet from "./Sheet";

type Props = {
  ws: Workspace;
  theme: ThemeChoice;
  onTheme: (choice: ThemeChoice) => void;
  devMode: boolean;
  onDevMode: (on: boolean) => void;
  onMemory: () => void;
  onModels: () => void;
  onClaude: () => void;
  onPhones: () => void;
  onClose: () => void;
};

function Row({ title, detail, note, children }: { title: string; detail?: string; note?: string; children: ReactNode }) {
  return (
    <div className="set-row">
      <div>
        <b>{title}</b>
        {detail && <small>{detail}</small>}
        {note && <small className="set-note" aria-live="polite">{note}</small>}
      </div>
      {children}
    </div>
  );
}

function Switch({ on, onChange, label, disabled }: { on: boolean; onChange: (v: boolean) => void; label: string; disabled?: boolean }) {
  return (
    <button className="switch" role="switch" aria-checked={on} aria-label={label} disabled={disabled} onClick={() => onChange(!on)} />
  );
}

/** Passwords and passkeys from Google Password Manager for Relay's sign-ins: a screen lock Relay keeps out of sight,
 * and Chrome signed in with sync, whose “Yes, I’m in” the user presses on the phone. Shown once Google is signed in. */
function SimpleSigninsRow({ slot }: { slot: string }) {
  const [state, setState] = useState<SimpleSignins | null>(null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  const path = `/devices/${slot}/simple-signins`;

  useEffect(() => {
    let alive = true;
    api<SimpleSignins>(path).then((s) => alive && setState(s)).catch(() => {});
    return () => {
      alive = false;
    };
  }, [path]);

  async function change(on: boolean) {
    setBusy(true);
    setProblem("");
    setState((s) => s && { ...s, on });
    try {
      setState(await api<SimpleSignins>(path, "POST", { enabled: on }));
    } catch (e) {
      setProblem(e instanceof Error ? e.message : String(e));
      api<SimpleSignins>(path).then(setState).catch(() => {});
    } finally {
      setBusy(false);
    }
  }

  const note = problem
    || (busy && state?.on ? "Setting up the phone…" : "")
    || (state?.on && state.enabled && state.chrome_signed_in === false
      ? "On the phone, press “Yes, I’m in” in Chrome to finish"
      : "");
  return (
    <Row
      title="Simplify Sign-ins on this Device"
      detail="Enable and sync Google Password Manager so that penrose can log in to websites on your behalf"
      note={note}
    >
      <Switch on={!!state?.on} onChange={change} label="Simplify sign-ins on this device" disabled={!state || busy} />
    </Row>
  );
}

/** Which apps penrose briefs the user on (backend/app/briefings): the models it sorts and prepares with; the apps
 * the phone has had notifications from, the busiest first, each a switch; and the conversations the user muted from
 * a briefing, to bring back. */
function BriefingApps({ ws }: { ws: Workspace }) {
  const { choices, problem, setApp, unmute, setModels } = useBriefedApps();
  const [all, setAll] = useState(false);
  const [picking, setPicking] = useState(false);
  if (!choices?.apps.length) return null;
  const shown = all ? choices.apps : choices.apps.filter((app, index) => app.on || index < 6);
  // Until the user chooses, what their tasks run on: the picker starts from those.
  const picks = choices.models ?? ws.models ?? ws.catalog?.defaults[ws.provider] ?? null;
  return (
    <div className="set-group">
      <Row
        title="Have things ready for you"
        detail="penrose looks at what these apps show as it arrives and gets ready what you may need. It only reads until you say what to do."
        note={problem}
      >
        {shown.length < choices.apps.length && (
          <button className="btn small" onClick={() => setAll(true)}>All apps</button>
        )}
      </Row>
      {ws.catalog && picks && (
        <>
          <Row
            title="Models"
            detail={`For sorting what arrives and preparing it: ${choices.models ? picksLabel(ws.catalog, choices.models) : "the same as your tasks"}`}
          >
            <button className="btn small" aria-expanded={picking} onClick={() => setPicking((open) => !open)}>
              {picking ? "Done" : "Change"}
            </button>
          </Row>
          {picking && (
            <div className="set-models">
              <ModelPicker catalog={ws.catalog} picks={picks} health={ws.health} onChange={setModels} />
              {choices.models && (
                <button className="btn small" onClick={() => setModels(null)}>Same as my tasks</button>
              )}
            </div>
          )}
        </>
      )}
      {shown.map((app) => (
        <Row key={app.package} title={app.label} detail={app.count ? `${app.count} notification${app.count === 1 ? "" : "s"} this week` : "Quiet this week"}>
          <Switch on={app.on} onChange={(on) => setApp(app, on)} label={`Brief me on ${app.label}`} />
        </Row>
      ))}
      {choices.muted.map((muted) => (
        <Row key={`${muted.package}:${muted.key}`} title={muted.name} detail={`Muted${muted.label ? ` · ${muted.label}` : ""}`}>
          <button className="btn small" onClick={() => unmute(muted)}>Unmute</button>
        </Row>
      ))}
    </div>
  );
}

function locationDetail(where: Workspace["where"]) {
  // The desktop window asks its OS; a browser tab, the browser (which asks the OS of whatever device it is on).
  const platform = window.relay?.platform;
  const os = platform === "darwin" ? "macOS" : platform === "win32" ? "Windows" : platform ? "your computer" : "your device";
  switch (where.source) {
    case "phone":
      if (where.permission === "denied")
        return "Location is off for penrose in this browser, so the phone stays where it was last placed";
      if (where.permission === "unsupported") return "This browser can’t share its location, so the phone stays where it was last placed";
      return where.permission === "granted"
        ? "Tasks you send from here run where this phone is"
        : "Tasks you send from here run where this phone is. Your browser asks once, the first time you send";
    case "browser":
      // Without a Wi-Fi scan a device only knows its area; turning Wi-Fi on (no need to connect) fixes that.
      if (where.accuracy > 2000)
        return `Within about ${Math.round(where.accuracy / 1000)} km. Turning on Wi-Fi lets ${os} place it precisely, even unconnected`;
      return "The phone is where you are";
    case "denied":
      return platform
        ? `Location is off for penrose, so the phone stays where it was last placed. Turn it on in ${os}’s settings`
        : "Location is blocked for this site, so the phone stays where it was last placed. Allow it in your browser’s site settings";
    case "unavailable":
      return `Couldn’t find where you are, so the phone stays where it was last placed. Turn on location services in ${os}`;
    default:
      return "Finding where you are…";
  }
}

export default function Settings({ ws, theme, onTheme, devMode, onDevMode, onMemory, onModels, onClaude, onPhones, onClose }: Props) {
  const desktop = window.relay;
  return (
    <Sheet title="Settings" onClose={onClose}>
      <div className="set-group">
        <Row title="Appearance">
          <div className="seg" role="group" aria-label="Appearance">
            {(["system", "light", "dark"] as const).map((choice) => (
              <button key={choice} aria-pressed={theme === choice} onClick={() => onTheme(choice)}>
                {choice === "system" ? "Auto" : choice[0].toUpperCase() + choice.slice(1)}
              </button>
            ))}
          </div>
        </Row>
      </div>
      <div className="set-group">
        <Row title="Location" detail={locationDetail(ws.where)}>
          {(ws.where.source === "denied" || ws.where.source === "unavailable") && desktop?.openLocationSettings && (
            <button className="btn small" onClick={() => desktop.openLocationSettings?.()}>
              Settings
            </button>
          )}
        </Row>
        {ws.device?.id && ws.googleAccount && <SimpleSigninsRow slot={ws.device.id} />}
        <Row title="What penrose remembers" detail="Preferences and details it uses across tasks">
          <button className="btn small" onClick={onMemory}>Open</button>
        </Row>
        {canManageKeys() && (
          <Row title="AI models" detail="API keys for Claude, GPT and DeepSeek">
            <button className="btn small" onClick={onModels}>Manage</button>
          </Row>
        )}
        {!COMPANION && (
          <Row title="penrose on your phone" detail="Use penrose from your own phone, wherever you are">
            <button className="btn small" onClick={onPhones}>Link a phone</button>
          </Row>
        )}
        {desktop && (
          <Row title="Connect Claude" detail="Let Claude Code on this computer use the phone">
            <button className="btn small" onClick={onClaude}>
              <ClaudeMark />
              Set up
            </button>
          </Row>
        )}
      </div>
      <BriefingApps ws={ws} />
      {desktop?.resetPhone && (
        <div className="set-group">
          <Row title="Reset phone" detail="Erases its apps, sign-ins and data, and starts it fresh">
            <button className="btn small" onClick={() => desktop.resetPhone?.()}>Reset…</button>
          </Row>
        </div>
      )}
      <div className="set-group">
        <Row title="Developer mode" detail="Shows penrose’s internal plan and trace">
          <Switch on={devMode} onChange={onDevMode} label="Developer mode" />
        </Row>
        {devMode && desktop?.openLogs && (
          <Row title="Logs" detail="The services’ log files">
            <button className="btn small" onClick={() => desktop.openLogs?.()}>Open folder</button>
          </Row>
        )}
      </div>
    </Sheet>
  );
}
