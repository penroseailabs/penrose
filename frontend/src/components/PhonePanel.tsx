import { useEffect, useRef, useState } from "react";
import { ChevronLeft, Circle, Square, X } from "lucide-react";
import { api } from "../api";
import { stepsOf } from "../lib/timeline";
import { useLiveFrames } from "../lib/useLiveFrames";
import type { Workspace } from "../lib/useWorkspace";
import { StepMark } from "./RunCard";

/**
 * The phone, molded into the canvas: its live screen, the three Android buttons, and what Relay is doing
 * on it. Click the screen to tap, scroll to swipe and type into it, whenever Relay is not using it.
 * `watchOnly` (the companion phone app): the screen and what Relay is doing, with nothing that acts on the phone.
 */
/** Why the phone did not come up this launch, and what the desktop app is doing to it before it is ready
 *  (starting it, or updating it after an update): read once, then kept current by the app's messages. */
function usePhoneState(connected: boolean) {
  const [error, setError] = useState<{ message: string; hint: string } | null>(null);
  const [step, setStep] = useState<string | null>(null);
  useEffect(() => {
    window.relay?.setupState?.().then((s) => {
      setError(s.phoneError);
      setStep(s.phoneStep ?? null);
    });
    const offError = window.relay?.onPhoneError?.(setError);
    const offStep = window.relay?.onPhoneStep?.(setStep);
    return () => {
      offError?.();
      offStep?.();
    };
  }, []);
  return { error: connected ? null : error, step };
}

export default function PhonePanel({ ws, watchOnly = false, onClose }: { ws: Workspace; watchOnly?: boolean; onClose?: () => void }) {
  const { device, latest } = ws;
  const connected = !!device?.connected;
  const busy = ws.running;
  // Claude Code holds the phone through the `relay` command (owner cli:<session>). The user may still tap.
  const claude = !busy && !!device?.owner?.startsWith("cli:");
  const acts = !watchOnly;

  const steps = latest ? stepsOf(latest) : [];
  const lastStep = steps.at(-1);
  const currentNode = Object.values(latest?.nodes || {}).find((n) => ["running", "verifying"].includes(n.status));
  // The step waits for the phone (another task or Claude Code holds it): say so, not that it is still planning.
  const waitingNode = Object.values(latest?.nodes || {}).find((n) => n.status === "waiting_device");
  const { error: failed, step } = usePhoneState(connected);
  const updating = !connected && !!step && /updat/i.test(step);
  const status = !ws.health ? "Connecting" : connected ? "Connected" : failed ? "Stopped" : updating ? "Updating" : "Starting";

  return (
    <aside className={`phone ${watchOnly ? "watch" : ""}`} aria-label="Phone">
      <div className="phone-head">
        <b>Phone</b>
        <span className="status">
          <span className={`sd ${connected ? "done" : failed ? "err" : "work"}`} />
          {status}
        </span>
        {onClose && (
          <button className="ib" onClick={onClose} aria-label="Close" title="Close">
            <X size={15} />
          </button>
        )}
      </div>
      <div className="stage">
        <div className="handset" style={device?.width && device.height ? { aspectRatio: `${device.width} / ${device.height}` } : undefined}>
          <div className="screen">
            {connected && device ? (
              <Screen ws={ws} locked={busy} watchOnly={watchOnly} />
            ) : failed ? (
              <div className="screen-empty failed">
                <StepMark mark="err" />
                <span>{failed.message}</span>
                {failed.hint && <small>{failed.hint}</small>}
              </div>
            ) : (
              <div className="screen-empty">
                <span className="sm now" />
                <span>{step || "The phone is starting"}</span>
              </div>
            )}
          </div>
        </div>
      </div>
      {acts && <div className="pctl">
        <button className="ib solid" disabled={!connected || busy} onClick={() => ws.phoneAction({ kind: "back" })} aria-label="Back" title="Back">
          <ChevronLeft size={16} />
        </button>
        <button className="ib solid" disabled={!connected || busy} onClick={() => ws.phoneAction({ kind: "home" })} aria-label="Home" title="Home">
          <Circle size={14} />
        </button>
        <button className="ib solid" disabled={!connected || busy} onClick={() => ws.phoneAction({ kind: "recents" })} aria-label="Recent apps" title="Recent apps">
          <Square size={13} />
        </button>
      </div>}
      {busy && waitingNode ? (
        <div className="doing surface">
          <StepMark mark="wait" />
          <span className="txt">
            <b>Waiting for the phone</b>
            <small>{waitingNode.waiting || "Work resumes automatically when it is free."}</small>
          </span>
          {acts && (
            <button className="btn small" onClick={ws.stop} title="Pause penrose and use the phone yourself">
              Take over
            </button>
          )}
        </div>
      ) : busy ? (
        <div className="doing surface">
          <StepMark mark="now" />
          <span className="txt">
            <b>{lastStep?.summary || "Getting started"}</b>
            <small>{currentNode?.spec.objective || "Planning the steps"}</small>
          </span>
          {acts && (
            <button className="btn small" onClick={ws.stop} title="Pause penrose and use the phone yourself">
              Take over
            </button>
          )}
        </div>
      ) : claude ? (
        <div className="doing surface">
          <StepMark mark="now" />
          <span className="txt">
            <b>Claude Code is using the phone</b>
            <small>{acts ? "You can still tap; taking it back ends Claude’s turn" : "Watching only"}</small>
          </span>
          {acts && <button className="btn small" onClick={() => api("/phone/sessions", "DELETE").catch((e) => ws.setError((e as Error).message))} title="Free the phone; Claude Code is told on its next command">
            Take back
          </button>}
        </div>
      ) : ws.loginAsk ? (
        <div className="doing surface">
          <StepMark mark="wait" />
          <span className="txt">
            <b>Sign in on the phone</b>
            <small>{acts ? "Click to tap, type once it’s selected" : "On your computer"}</small>
          </span>
          {acts && <button
            className="btn small primary"
            disabled={ws.busy}
            onClick={() => ws.send("I signed in on the phone myself. Continue from the screen.", { instead: true })}
            title="Tell penrose you are signed in"
          >
            I’m signed in
          </button>}
        </div>
      ) : latest?.status === "waiting_user" ? (
        <div className="doing surface">
          <StepMark mark="wait" />
          <span className="txt">
            <b>Waiting for you</b>
            <small>Answer in the conversation</small>
          </span>
        </div>
      ) : (
        connected && acts && <p className="phone-hint">Click to tap, scroll to swipe, and type once it’s selected.</p>
      )}
    </aside>
  );
}

/** The live screen: video while it changes, and an exact frame once it settles (the gateway does both). */
function Screen({ ws, locked: busy, watchOnly }: { ws: Workspace; locked: boolean; watchOnly: boolean }) {
  const locked = busy || watchOnly;
  const device = ws.device!;
  const image = useRef<HTMLImageElement>(null);
  const live = useLiveFrames(`/api/devices/${device.id}/stream`, image);
  // Asleep, the screen is dark: the first click only wakes it (the gateway takes it as a wake, not a tap).
  const [woken, setWoken] = useState(false);
  useEffect(() => setWoken(false), [device.asleep]);
  const wheel = useRef(0);
  // Keys typed while the screen has focus go to the phone, in order: letters in short bursts.
  const queue = useRef(Promise.resolve());
  const typed = useRef("");
  const burst = useRef<ReturnType<typeof setTimeout>>(undefined);
  const send = (body: object) => {
    queue.current = queue.current.then(() => ws.phoneAction(body));
  };
  const flush = () => {
    clearTimeout(burst.current);
    if (typed.current) send({ kind: "type_text", text: typed.current });
    typed.current = "";
  };
  const printable = (text: string) => text.replace(/[^ -~]/g, "");

  const point = (e: React.MouseEvent<HTMLElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    return {
      x: Math.round(((e.clientX - rect.left) / rect.width) * (device.width || 1080)),
      y: Math.round(((e.clientY - rect.top) / rect.height) * (device.height || 1920)),
    };
  };

  return (
    <div
      className={`live ${locked ? "locked" : ""}`}
      tabIndex={locked ? -1 : 0}
      aria-label={watchOnly ? "The phone’s screen" : "The phone’s screen. Click to tap; while it has focus, typing goes to the phone."}
      title={watchOnly ? undefined : locked ? "penrose is using the phone. Take over to use it yourself." : undefined}
      onClick={(e) => {
        if (locked) return;
        flush();
        if (device.asleep) setWoken(true);
        send({ kind: "tap", ...point(e) });
      }}
      onKeyDown={(e) => {
        if (locked || e.ctrlKey || e.metaKey || e.altKey) return;
        if (e.key.length === 1 && printable(e.key)) {
          e.preventDefault();
          typed.current += e.key;
          clearTimeout(burst.current);
          burst.current = setTimeout(flush, 140);
        } else if (e.key === "Backspace" || e.key === "Enter") {
          e.preventDefault();
          flush();
          send({ kind: e.key === "Enter" ? "enter" : "delete" });
        }
      }}
      onPaste={(e) => {
        const text = printable(e.clipboardData.getData("text"));
        if (locked || !text) return;
        e.preventDefault();
        flush();
        send({ kind: "type_text", text });
      }}
      onWheel={(e) => {
        if (locked) return;
        // One swipe per wheel gesture, not one per wheel tick.
        const now = Date.now();
        if (now - wheel.current < 450) return;
        wheel.current = now;
        const w = device.width || 1080;
        const h = device.height || 1920;
        const down = e.deltaY > 0;
        ws.phoneAction({
          kind: "swipe",
          x: Math.round(w / 2),
          y: Math.round(h * (down ? 0.72 : 0.32)),
          x2: Math.round(w / 2),
          y2: Math.round(h * (down ? 0.32 : 0.72)),
        });
      }}
    >
      <img ref={image} className={live ? "" : "waiting"} alt="The phone’s screen" draggable={false} />
      {device.asleep && !woken && !busy && <span className="asleep-note">{watchOnly ? "Asleep" : "Asleep · click to wake"}</span>}
    </div>
  );
}
