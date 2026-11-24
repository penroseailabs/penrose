import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowRight, ArrowUpRight, Moon, RotateCcw, Send, Sun } from "lucide-react";
import { useTheme } from "../lib/theme";
import { KeysFoot, ModelKeys, SUBSCRIPTION_NOTE } from "./Models";
import { StepMark } from "./RunCard";

const MAC = /Mac/.test(navigator.platform);

/**
 * First run: the phone, then an AI model's API key. The phone goes first because it takes longest (a 3 GB
 * download, then the build): once the user accepts the licenses it builds in the background, owned by the main
 * process, while they add a key. Google sign-in follows inside the app (Welcome). Each step shows only when it
 * is still needed.
 */
export default function Setup({ needs, onDone }: { needs: { keys: boolean; phone: boolean }; onDone: () => void }) {
  const build = useBuild();
  const [, setTheme, resolvedDark_] = useTheme();
  const resolvedDark = resolvedDark_ === "dark";
  const [step, setStep] = useState<"phone" | "keys">(needs.phone ? "phone" : "keys");
  const [keysDone, setKeysDone] = useState(!needs.keys);
  const phoneDone = !needs.phone || build.ready;
  const steps = [needs.phone && "phone", needs.keys && "keys"].filter(Boolean);

  // A build under way (just started here, or carried on from an earlier launch): add the key meanwhile.
  useEffect(() => {
    if (step === "phone" && build.running && !keysDone) setStep("keys");
  }, [step, build.running, keysDone]);
  useEffect(() => {
    if (keysDone && phoneDone) onDone();
  }, [keysDone, phoneDone, onDone]);

  return (
    <div className={`first-run ${MAC ? "mac" : ""}`}>
      <div className="titlebar">
        <span className="wordmark">penrose</span>
        <span className="grow" />
        <button
          className="ib"
          onClick={() => setTheme(resolvedDark ? "light" : "dark")}
          aria-label={resolvedDark ? "Light appearance" : "Dark appearance"}
          title={resolvedDark ? "Light appearance" : "Dark appearance"}
        >
          {resolvedDark ? <Sun size={14} /> : <Moon size={14} />}
        </button>
      </div>
      <main className="first-run-body scroll">
        {steps.length > 1 && (
          <div className="setup-dots" aria-hidden="true">
            {steps.map((s) => (
              <i key={String(s)} className={s === step ? "on" : ""} />
            ))}
          </div>
        )}
        {step === "keys" ? (
          <KeysStep
            build={needs.phone ? build : null}
            onNext={() => {
              setKeysDone(true);
              if (!phoneDone) setStep("phone");
            }}
          />
        ) : (
          <PhoneStep build={build} first={!keysDone} />
        )}
      </main>
    </div>
  );
}

function KeysStep({ build, onNext }: { build: Build | null; onNext: () => void }) {
  const [ready, setReady] = useState(false);
  return (
    <div className="setup appear">
      <span className="setup-eyebrow">{build ? "While your phone builds" : <>Welcome to <span className="wordmark">penrose</span></>}</span>
      <h2>{build ? "Now, connect an AI model." : "First, connect an AI model."}</h2>
      <p className="setup-lede">
        penrose’s agents think with a model you choose, on your own API key. One is enough to start; you can add the
        others any time in Settings.
      </p>
      <div className="card surface setup-card">
        <ModelKeys open="anthropic" onSaved={(saved) => setReady(Object.values(saved).some(Boolean))} />
        <KeysFoot />
      </div>
      <p className="setup-fine">{SUBSCRIPTION_NOTE}</p>
      {build && <BuildStatus build={build} />}
      <div className="setup-actions">
        <button className="btn primary" disabled={!ready} onClick={onNext}>
          Continue
          <ArrowRight size={14} />
        </button>
        {!ready && <span className="setup-hint">Add a key to continue.</span>}
      </div>
    </div>
  );
}

// The setup's own steps, grouped into what they mean to the user. `parts` are the steps inside a stage, in the
// order they run, with their share of it (a step already done on an earlier run is skipped, and counts as done).
const STAGES = [
  { id: "download", title: "Delivering your new phone", parts: { java: 5, "cmdline-tools": 5, sdk: 90 }, weight: 30 },
  { id: "check", title: "Checking compatibility with your computer", parts: { accel: 100 }, weight: 2 },
  { id: "build", title: "Recruiting your agents", parts: { tools: 8, golden: 92 }, weight: 60 },
  { id: "phone", title: "Finishing touches", parts: { phone: 100 }, weight: 8 },
];
const stageOf = (stepId: string) => STAGES.findIndex((s) => stepId in s.parts);

// What is going on right now, in the user's words: one line per step, and per part of the long image build
// (the backend's `doing`, see build_golden), which otherwise sits on one percent for minutes.
const STEP_NOTES: Record<string, string> = {
  java: "Fetching a helper the Android tools need",
  "cmdline-tools": "Picking up Google’s Android toolkit",
  sdk: "Downloading Android and the Play Store, about 3 GB",
  accel: "Making sure this computer can run a phone",
  tools: "Gathering the tools your agents work with",
  golden: "Assembling your agents’ phone",
  phone: "Making a copy of the phone that’s all yours",
};
const PART_NOTES: Record<string, string> = {
  image: "Unboxing a fresh copy of Android",
  boot: "Turning the phone on for the first time",
  root: "Handing your agents the keys to the phone",
  restart: "Restarting the phone with its new keys",
  access: "Asking Android to let your agents in",
  framework: "Teaching your agents to read what’s on screen",
  apps: "Letting your agents read every app, new ones too",
  chrome: "Checking that your agents can read apps",
  tidy: "Tidying up, so your phone starts fresh",
  save: "Packing up the finished phone",
};

type Progress = { stage: number; step: string; percent: number; doing?: string; done: boolean };
type Problem = { message: string; hint: string; doctor?: DoctorAnswer };

const START: Progress = { stage: 0, step: "", percent: 0, done: false };

/** How far through its stage a run is, 0 to 100, by the parts' shares. */
function stagePercent(progress: Progress) {
  const parts = Object.entries(STAGES[progress.stage]?.parts ?? {});
  const at = parts.findIndex(([id]) => id === progress.step);
  if (at < 0) return 0;
  const before = parts.slice(0, at).reduce((sum, [, share]) => sum + share, 0);
  return before + (parts[at][1] * progress.percent) / 100;
}

/** How far through the whole setup a run is, 0 to 100, by the stages' weights. */
function overallPercent(progress: Progress | null) {
  if (!progress) return 0;
  if (progress.done) return 100;
  const before = STAGES.slice(0, progress.stage).reduce((sum, s) => sum + s.weight, 0);
  return before + ((STAGES[progress.stage]?.weight ?? 0) * stagePercent(progress)) / 100;
}

/** The line saying what the run is doing right now, or "" when there is nothing to add. */
function noteOf(progress: Progress | null) {
  if (!progress || progress.done) return "";
  return (progress.doing && PART_NOTES[progress.doing]) || STEP_NOTES[progress.step] || "";
}

type Build = {
  running: boolean;
  progress: Progress | null;
  problem: Problem | null;
  /** The doctor started setup again by itself after a failure: what it said. */
  retrying: string;
  ready: boolean;
  start: (options: { acceptLicenses: boolean; useExisting: boolean; startOver?: boolean }) => void;
};

/** The phone setup run, which lives in the main process: joined on mount (a run carried on from an earlier
 *  launch, or a reloaded window), followed through its events, ended by its "finished" event. */
function useBuild(): Build {
  const [running, setRunning] = useState(false);
  const [progress, setProgress] = useState<Progress | null>(null);
  const [problem, setProblem] = useState<Problem | null>(null);
  const [ready, setReady] = useState(false);
  const [retrying, setRetrying] = useState("");

  const handle = useCallback((event: AndroidEvent) => {
    if (event.event === "step") {
      const stage = stageOf(event.id);
      // A retry runs the earlier steps and parts again, each skipping itself in seconds when already done: the
      // bar and its note stay where the run stopped until it catches up, so a retry never looks like a restart.
      if (stage >= 0)
        setProgress((p) =>
          p && (stage < p.stage || (stage === p.stage && p.step === event.id))
            ? p
            : { stage, step: event.id, percent: 0, done: false },
        );
    } else if (event.event === "progress") {
      if (stageOf(event.id) >= 0)
        setProgress((p) =>
          p && p.step === event.id && event.percent >= p.percent
            ? { ...p, percent: event.percent, doing: event.doing ?? p.doing }
            : p,
        );
    } else if (event.event === "retrying") {
      setRetrying(event.reply);
    } else if (event.event === "done") {
      setProgress({ stage: STAGES.length, step: "", percent: 0, done: true });
    } else if (event.event === "finished") {
      setRunning(false);
      setRetrying("");
      if (event.ok) setReady(true);
      else
        setProblem({
          message: event.error || "Setup stopped.",
          hint: event.hint === "licenses" ? "Accept the license above, then try again." : event.hint || "",
          doctor: event.doctor,
        });
    }
  }, []);

  useEffect(() => {
    const off = window.relay?.onAndroidEvent?.(handle);
    window.relay?.androidProgress?.().then(({ running, events, result }) => {
      if (running) {
        setRunning(true);
        setProgress((p) => p ?? START);
        events.forEach(handle);
      } else if (result) {
        handle({ event: "finished", ...result });
      }
    });
    return off;
  }, [handle]);

  const start = useCallback(
    (options: { acceptLicenses: boolean; useExisting: boolean; startOver?: boolean }) => {
      setProblem(null);
      setRetrying("");
      setRunning(true);
      setProgress((p) => (options.startOver ? START : (p ?? START))); // starting over does rebuild from the start
      // The run reports its end as a "finished" event; this covers a window with no desktop app behind it.
      const run = window.relay?.setupAndroid?.(options);
      if (!run) return handle({ event: "finished", ok: false });
      run.catch(() => handle({ event: "finished", ok: false }));
    },
    [handle],
  );

  return { running, progress, problem, retrying, ready, start };
}

/** On the API key step: the phone building behind it. */
function BuildStatus({ build }: { build: Build }) {
  const overall = Math.round(overallPercent(build.progress));
  const stage = build.progress && STAGES[build.progress.stage];
  const title = build.ready
    ? "Your phone is ready"
    : build.problem
      ? "Phone setup stopped"
      : build.progress?.done
        ? "Starting your phone"
        : "Building your phone in the background";
  const detail = build.ready
    ? ""
    : build.problem
      ? "After this step you’ll see what happened, and can try again."
      : stage
        ? `${noteOf(build.progress) || stage.title}. You can keep going; it carries on by itself.`
        : "";
  return (
    <div className="card surface setup-card">
      <div className="card-head">
        <b>{title}</b>
        {!build.ready && !build.problem && <span className="of">{overall}%</span>}
      </div>
      <div className="meter" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={overall}>
        <i style={{ width: `${build.ready ? 100 : overall}%` }} />
      </div>
      {detail && <p className="setup-hint">{detail}</p>}
    </div>
  );
}

function PhoneStep({ build, first }: { build: Build; first: boolean }) {
  const [info, setInfo] = useState<{ existingSdk: string; useExisting: boolean; freeGb: number | null } | null>(null);
  const [useExisting, setUseExisting] = useState(false);
  // A run only starts with the licenses accepted, so one that has started (Try again) needs no new tick.
  const [accepted, setAccepted] = useState(build.progress !== null);
  const { running, progress, problem } = build;

  useEffect(() => {
    window.relay?.androidInfo?.().then((i) => {
      setInfo(i);
      setUseExisting(i.useExisting);
    });
  }, []);

  // A run that has started already had its licenses accepted, even one resumed by itself after a restart.
  const licensed = accepted || progress !== null;
  const start = (startOver = false) => build.start({ acceptLicenses: licensed, useExisting, startOver });
  const overall = overallPercent(progress);

  return (
    <div className="setup appear">
      <span className="setup-eyebrow">{first ? <>Welcome to <span className="wordmark">penrose</span></> : "Almost ready"}</span>
      <h2>{progress ? "Building your phone." : first ? "First, build the phone." : "Now, build the phone."}</h2>
      <p className="setup-lede">
        penrose runs a real Android phone on this computer, prepared so its agents can read the apps they use. It’s
        built once, in about 15 to 30 minutes, and stays yours from then on.
        {first && " It builds in the background while you connect an AI model."}
      </p>

      {progress ? (
        <div className="card surface setup-card">
          <div className="card-head">
            <b>{progress.done ? "Starting your phone" : "Building your phone"}</b>
            <span className="of">{progress.done ? "" : `${Math.round(overall)}%`}</span>
          </div>
          <div className="meter" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(overall)}>
            <i style={{ width: `${progress.done ? 100 : overall}%` }} />
          </div>
          <ul className="steps">
            {STAGES.map((stage, i) => {
              const mark = i < progress.stage ? "ok" : i === progress.stage ? (problem ? "err" : running ? "now" : "later") : "later";
              const current = i === progress.stage && running;
              const note = current ? noteOf(progress) : "";
              const percent = current ? Math.round(stagePercent(progress)) : 0;
              return (
                <li key={stage.id} className={mark}>
                  <StepMark mark={mark} />
                  <span className="t">
                    {stage.title}
                    {note && (
                      <small key={note} className="note">
                        {note}
                      </small>
                    )}
                  </span>
                  {percent > 0 && <time>{percent}%</time>}
                </li>
              );
            })}
          </ul>
          <div className="card-foot">
            Keep penrose open while this runs. If it closes, setup carries on from the last finished step.
          </div>
        </div>
      ) : (
        <div className="card surface setup-card">
          <ul className="facts">
            <li>
              <b>About 12 GB of disk</b>
              <small>{info?.freeGb != null ? `${info.freeGb} GB free on this drive` : "On this computer’s drive"}</small>
            </li>
            <li>
              <b>About 3 GB to download</b>
              <small>
                Google’s Android emulator with the Play Store, a Java runtime, and the open-source Magisk, Vector and
                rootAVD tools
              </small>
            </li>
            <li>
              <b>Rooted, so agents can read apps</b>
              <small>A few apps refuse rooted phones; use their website in the phone’s browser instead</small>
            </li>
          </ul>
          {info?.existingSdk && (
            <label className="check">
              <input type="checkbox" checked={useExisting} onChange={(e) => setUseExisting(e.target.checked)} />
              <span>
                Use the Android SDK already on this computer instead of downloading it again
                <code>{info.existingSdk}</code>
              </span>
            </label>
          )}
          <label className="check">
            <input type="checkbox" checked={accepted} onChange={(e) => setAccepted(e.target.checked)} />
            <span>
              I accept the{" "}
              <a href="https://developer.android.com/studio/terms" target="_blank" rel="noreferrer">
                Android SDK License Agreement
                <ArrowUpRight size={11} />
              </a>{" "}
              and the licenses of the Google Play system image.
            </span>
          </label>
        </div>
      )}

      {build.retrying && running && (
        <div className="doctor-note" role="status">
          <b>Setup hit a snag and is trying again by itself.</b>
          <p>{build.retrying}</p>
        </div>
      )}

      {problem && (
        <div className="problem" role="alert">
          <b>{problem.message}</b>
          {/* The doctor's words, when it could look, in place of the fixed hint. */}
          {(problem.doctor?.reply || problem.hint) && <p>{problem.doctor?.reply || problem.hint}</p>}
        </div>
      )}

      <div className="setup-actions">
        {!running && (
          <button className="btn primary" disabled={!licensed} onClick={() => start()}>
            {problem && progress ? "Try again" : "Build the phone"}
            <ArrowRight size={14} />
          </button>
        )}
        {!running && problem && problem.doctor?.action === "start_over" && (
          <button className="btn" onClick={() => start(true)}>
            <RotateCcw size={13} />
            Start the phone over
          </button>
        )}
      </div>

      {progress && <SetupHelp running={running} onAction={(action) => !running && start(action === "start_over")} />}
    </div>
  );
}

/** Ask Relay's setup agent (the setup doctor) about the setup ("How long is left?"). The conversation lives in this component only:
 *  nothing is saved, and only its last few messages are sent with each question. */
function SetupHelp({ running, onAction }: { running: boolean; onAction: (action: "try_again" | "start_over") => void }) {
  const [messages, setMessages] = useState<(DoctorMessage & { action?: DoctorAnswer["action"] })[]>([]);
  const [text, setText] = useState("");
  const [asking, setAsking] = useState(false);
  const log = useRef<HTMLUListElement>(null);
  // The conversation scrolls inside its own box; each new message brings the box to its end.
  useEffect(() => {
    log.current?.scrollTo({ top: log.current.scrollHeight, behavior: "smooth" });
  }, [messages, asking]);
  if (!window.relay?.askSetup) return null;

  const ask = async (question: string) => {
    question = question.trim();
    if (!question || asking) return;
    const next = [...messages, { role: "user" as const, text: question }].slice(-8);
    setMessages(next);
    setText("");
    setAsking(true);
    try {
      const answer = await window.relay!.askSetup!(next.map(({ role, text }) => ({ role, text })));
      setMessages((m) => [...m, { role: "doctor" as const, text: answer.reply, action: answer.action }].slice(-8));
    } catch {
      setMessages((m) => [...m, { role: "doctor" as const, text: "I couldn’t look into it just now." }]);
    } finally {
      setAsking(false);
    }
  };

  return (
    <div className="setup-help">
      <p className="setup-help-intro">
        Setup takes a while. penrose’s setup agent looks after it for you and steps in if something goes wrong. Ask it
        anything along the way.
      </p>
      {messages.length > 0 && (
        <ul className="setup-help-log" aria-live="polite" ref={log}>
          {messages.map((m, i) => (
            <li key={i} className={m.role}>
              {m.text}
              {m.role === "doctor" && !running && (m.action === "try_again" || m.action === "start_over") &&
                i === messages.length - 1 && (
                  <button className="btn quiet" onClick={() => onAction(m.action as "try_again" | "start_over")}>
                    {m.action === "start_over" ? "Start the phone over" : "Try again"}
                    <ArrowRight size={12} />
                  </button>
                )}
            </li>
          ))}
          {asking && <li className="doctor pending">Checking on your phone…</li>}
        </ul>
      )}
      <form
        className="setup-help-ask"
        onSubmit={(e) => {
          e.preventDefault();
          ask(text);
        }}
      >
        <input
          className="input"
          value={text}
          maxLength={500}
          placeholder={running ? "Ask the setup agent, like “How long is left?”" : "Ask the setup agent what happened"}
          onChange={(e) => setText(e.target.value)}
        />
        <button className="btn" type="submit" disabled={!text.trim() || asking} aria-label="Ask">
          <Send size={13} />
        </button>
      </form>
    </div>
  );
}
