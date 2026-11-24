import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import Sheet from "./Sheet";

/** What the API knows about this computer's Claude installation (backend/app/cli.py installation). */
type Setup = {
  claude_code: string;
  claude_desktop: boolean;
  claude_app_config: string;
  claude_app_registered: boolean;
  skill: "installed" | "outdated" | "missing";
  skill_path: string;
  launcher: string;
  launcher_present: boolean;
  on_path: boolean;
  platform: string;
  sessions: string[];
  path_note?: string;
  skill_zip?: string;
};

type Step = "skill" | "path" | "app" | "skill_zip";

function Row({ ok, title, detail, children }: { ok: boolean | null; title: string; detail: string; children?: React.ReactNode }) {
  return (
    <div className="key">
      <div className="key-head">
        <div className="who">
          <b>{title}</b>
          <small>
            <span className={`sd ${ok === null ? "work" : ok ? "done" : "err"}`} />
            {detail}
          </small>
        </div>
        {children}
      </div>
    </div>
  );
}

/**
 * Connect Claude: Claude Code (the CLI, and the Code tab of the Claude app) can use Relay's phone through the
 * `relay` command; Cowork through Relay's MCP server (`relay mcp`), which the Claude app starts on this computer
 * because Cowork's own sandbox cannot reach Relay. The page finds what is installed, puts the command on the
 * PATH, writes the Claude Code skill, adds the MCP server to the Claude app and exports the Cowork skill.
 */
export default function ClaudeSheet({ onClose }: { onClose: () => void }) {
  const [state, setState] = useState<Setup | null>(null);
  const [busy, setBusy] = useState<"" | Step>("");
  const [zip, setZip] = useState("");
  const [error, setError] = useState("");
  const [note, setNote] = useState("");

  const load = useCallback(() => api<Setup>("/phone/setup").then(setState).catch((e) => setError((e as Error).message)), []);
  useEffect(() => {
    load();
  }, [load]);

  async function apply(what: Step) {
    setBusy(what);
    setError("");
    try {
      const next = await api<Setup>("/phone/setup", "POST", { [what]: true });
      setState(next);
      if (next.path_note) setNote(next.path_note);
      if (what === "app") setNote("Added. Quit and reopen the Claude app (from its tray or menu bar icon) so Cowork sees the phone.");
      if (next.skill_zip) {
        setZip(next.skill_zip);
        window.relay?.revealClaudeSkill?.();
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy("");
    }
  }

  const ready = !!state && !!state.claude_code && state.on_path && state.skill === "installed";
  return (
    <Sheet title="Connect Claude" onClose={onClose}>
      <p className="sheet-lede">
        Claude Code and Cowork on this computer can use penrose’s phone for anything done in apps: Claude Code through the{" "}
        <code>relay</code> command, Cowork through phone tools the Claude app runs. penrose’s own tasks and Claude never act on
        the phone at the same time: whoever asks second is told to wait.
      </p>
      {state ? (
        <div className="keys">
          <Row
            ok={!!state.claude_code}
            title="Claude Code"
            detail={state.claude_code ? `Found at ${state.claude_code}` : "Not found. Install it from claude.com/code, then come back."}
          />
          <Row
            ok={state.on_path}
            title="The relay command"
            detail={
              state.on_path
                ? "On your PATH, so Claude can run it"
                : state.launcher_present
                  ? `Not on your PATH yet (the launcher is ${state.launcher})`
                  : "The launcher has not been written yet; restart penrose"
            }
          >
            {!state.on_path && state.launcher_present && (
              <button className="btn small primary" disabled={busy !== ""} onClick={() => apply("path")}>
                {busy === "path" ? "Adding…" : "Add to PATH"}
              </button>
            )}
          </Row>
          <Row
            ok={state.skill === "installed"}
            title="The android-phone skill"
            detail={
              state.skill === "installed"
                ? `Installed, so Claude knows when and how to use the phone (${state.skill_path})`
                : state.skill === "outdated"
                  ? "Installed, but older than this penrose’s"
                  : "Not installed. It tells Claude when and how to use the phone."
            }
          >
            {state.skill !== "installed" && (
              <button className="btn small primary" disabled={busy !== ""} onClick={() => apply("skill")}>
                {busy === "skill" ? "Writing…" : state.skill === "outdated" ? "Update" : "Install"}
              </button>
            )}
          </Row>
          <Row
            ok={state.claude_app_registered}
            title="Cowork in the Claude app"
            detail={
              state.claude_app_registered
                ? "penrose’s phone tools are in the Claude app, so Cowork can use the phone"
                : state.claude_app_config
                  ? "Adds penrose’s phone tools to the Claude app. Cowork’s sandbox cannot reach penrose by itself; the app runs the tools on this computer."
                  : "The Claude desktop app was not found."
            }
          >
            {state.claude_app_config && !state.claude_app_registered && (
              <button className="btn small primary" disabled={busy !== ""} onClick={() => apply("app")}>
                {busy === "app" ? "Adding…" : "Add to Claude app"}
              </button>
            )}
          </Row>
          {state.claude_app_config && (
            <Row
              ok={null}
              title="Cowork skill (optional)"
              detail={
                zip
                  ? `Saved as ${zip}. Upload it in claude.ai under Customize › Skills.`
                  : "Helps Cowork decide when to reach for the phone. Export it, then upload it in claude.ai under Customize › Skills."
              }
            >
              <button className="btn small" disabled={busy !== ""} onClick={() => (zip && window.relay?.revealClaudeSkill ? window.relay.revealClaudeSkill() : apply("skill_zip"))}>
                {busy === "skill_zip" ? "Exporting…" : zip && window.relay?.revealClaudeSkill ? "Show file" : "Export skill"}
              </button>
            </Row>
          )}
        </div>
      ) : error ? null : (
        <p className="key-note">Looking at this computer…</p>
      )}
      {note && <p className="key-note">{note}</p>}
      {error && (
        <p className="key-note">
          <span className="err">{error}</span>
        </p>
      )}
      {state?.sessions.length ? (
        <p className="key-note">Claude Code is using the phone right now (session {state.sessions.join(", ")}).</p>
      ) : null}
      {ready && (
        <p className="key-note">
          Try it: in Claude Code{state?.claude_app_registered ? " or Cowork" : ""}, ask for something done on the phone, such as “Open YouTube on the phone and tell me what’s trending”.
          If Claude Code was already open, restart it first so it finds the command.
        </p>
      )}
    </Sheet>
  );
}
