import { useEffect, useState } from "react";
import { ArrowRight, KeyRound, LockKeyhole, Smartphone } from "lucide-react";
import { api, type GoogleSignIn, type SignInMethod } from "../api";
import { COMPANION } from "../companion/flag";
import type { Workspace } from "../lib/useWorkspace";
import { StepMark } from "./RunCard";
import { TileSpinner } from "./Tiles";

const LIVE: GoogleSignIn["state"][] = ["working", "phone", "need_phone", "need_password", "agree"];

/**
 * First run: what Relay is, then the one thing it needs, the user's Google account on the phone. Shown in the window
 * and in the phone app, so a user who is never at the computer signs in from their own phone.
 * The user gives their email and picks how to sign in. Relay types the email on the phone (backend/app/gateway/signin.py),
 * then asks Google for the “Tap Yes” prompt on their own phone. When Google offers no prompt, or the user chose their
 * password, it asks for the password here and types it on the phone once, keeping it nowhere. The account's phone
 * number (when Google wants it confirmed) and Google's terms are answered here too. Anything else (a CAPTCHA, a code)
 * they finish on the phone. Tasks stay locked until Android itself reports an account (the gateway reads it again
 * after each sign-in).
 */
export default function Welcome({ ws }: { ws: Workspace }) {
  const slot = ws.device?.id || "";
  const ready = !!ws.device?.connected;
  const [email, setEmail] = useState("");
  const [job, setJob] = useState<GoogleSignIn | null>(null);
  const [problem, setProblem] = useState("");
  const [sending, setSending] = useState(false);
  const [onPhone, setOnPhone] = useState(false);
  const [phone, setPhone] = useState("");
  const [password, setPassword] = useState("");
  const [method, setMethod] = useState<SignInMethod>("prompt");
  const live = !!job && LIVE.includes(job.state);

  // A sign-in already under way (the window reloaded), and each step of one: the gateway pushes it with the phone.
  const pushed = ws.device?.signin;
  useEffect(() => {
    if (!pushed || !ready) return;
    setJob(pushed.state === "idle" ? null : pushed);
    if (pushed.email) setEmail((e) => e || pushed.email || "");
    if (pushed.method && LIVE.includes(pushed.state)) setMethod(pushed.method);
  }, [pushed, ready]);

  async function start() {
    setProblem("");
    setSending(true);
    try {
      setJob(await api<GoogleSignIn>(`/devices/${slot}/google/signin`, "POST", { email: email.trim(), method }));
      setOnPhone(false);
    } catch (e) {
      setProblem((e as Error).message);
    } finally {
      setSending(false);
    }
  }

  async function answerPhone() {
    setProblem("");
    setSending(true);
    try {
      setJob(await api<GoogleSignIn>(`/devices/${slot}/google/signin/phone`, "POST", { phone: phone.trim() }));
      setPhone("");
    } catch (e) {
      setProblem((e as Error).message);
    } finally {
      setSending(false);
    }
  }

  async function answerPassword() {
    setProblem("");
    setSending(true);
    try {
      setJob(await api<GoogleSignIn>(`/devices/${slot}/google/signin/password`, "POST", { password }));
    } catch (e) {
      setProblem((e as Error).message);
    } finally {
      setPassword(""); // typed on the phone once; nothing here keeps it
      setSending(false);
    }
  }

  async function agree() {
    setProblem("");
    setSending(true);
    try {
      setJob(await api<GoogleSignIn>(`/devices/${slot}/google/signin/agree`, "POST"));
    } catch (e) {
      setProblem((e as Error).message);
    } finally {
      setSending(false);
    }
  }

  async function cancel() {
    try {
      await api<GoogleSignIn>(`/devices/${slot}/google/signin`, "DELETE");
      setJob(null);
      setOnPhone(true);
    } catch (e) {
      setProblem((e as Error).message);
    }
  }

  function signInMyself() {
    ws.phoneAction({ kind: "google_signin" });
    setOnPhone(true);
    setJob(null);
  }

  const handedOver = job?.state === "manual" || onPhone;
  const mark = job?.state === "done" ? "ok" : job?.state === "failed" ? "err" : live || handedOver ? "now" : "later";

  return (
    <div className="setup appear">
      <span className="setup-eyebrow">Welcome to <span className="wordmark">penrose</span></span>
      <h2>A second phone, run by your agents.</h2>
      <p className="setup-lede">
        penrose is a phone of your own that lives on this computer. Your agents pick it up, open the apps you
        already use, and get things done in them: a table for Saturday, the weekly groceries, a bill before
        it’s due. Hundreds of apps, none of the tapping.
      </p>
      <p className="setup-lede quiet">You stay in charge. penrose checks with you before anything you didn’t ask for.</p>

      <div className="card surface setup-card">
        <div className="card-head">
          <b>Set up the phone</b>
          <span className="of">One step</span>
        </div>
        <div className="setup-step">
          <StepMark mark={mark} />
          <div className="signin">
            <b>Sign in with your Google account</b>
            <p>
              Your agents use it just as you would: Play Store for installing apps, and “Continue with Google”
              to sign in to them, so your orders, addresses and history come along.
            </p>

            {job?.state === "phone" ? (
              <div className="signin-phone" aria-live="polite">
                <Smartphone size={22} />
                <div>
                  <b>Check your phone and tap Yes</b>
                  <p>
                    Google sent a notification to the phone you already use with {job.email || "this account"}.
                    {job.number ? " Then tap this number on it:" : " Open it and tap Yes."}
                  </p>
                  {job.number && <div className="signin-number">{job.number}</div>}
                </div>
              </div>
            ) : job?.state === "need_password" ? (
              <form
                className="signin-ask"
                aria-live="polite"
                onSubmit={(e) => {
                  e.preventDefault();
                  answerPassword();
                }}
              >
                <p>
                  <KeyRound size={14} /> {job.message}
                </p>
                <div className="signin-form">
                  <input
                    className="input"
                    type="password"
                    autoComplete="current-password"
                    autoFocus
                    placeholder="Your Google password"
                    aria-label="Google password"
                    value={password}
                    maxLength={200}
                    onChange={(e) => setPassword(e.target.value)}
                  />
                  <button className="btn primary" type="submit" disabled={sending || !password}>
                    Continue
                    <ArrowRight size={14} />
                  </button>
                </div>
              </form>
            ) : job?.state === "agree" ? (
              <div className="signin-ask" aria-live="polite">
                <p>{job.message}</p>
                <div className="setup-actions">
                  <button className="btn primary" disabled={sending} onClick={agree}>
                    I agree
                    <ArrowRight size={14} />
                  </button>
                </div>
              </div>
            ) : job?.state === "need_phone" ? (
              <form
                className="signin-ask"
                aria-live="polite"
                onSubmit={(e) => {
                  e.preventDefault();
                  answerPhone();
                }}
              >
                <p>{job.message}</p>
                <div className="signin-form">
                  <input
                    className="input"
                    type="tel"
                    autoComplete="tel"
                    autoFocus
                    placeholder={job.hint ? `Your phone number, ending in ${job.hint}` : "Your phone number"}
                    aria-label="Phone number on your Google account"
                    value={phone}
                    maxLength={40}
                    onChange={(e) => setPhone(e.target.value)}
                  />
                  <button className="btn primary" type="submit" disabled={sending || !phone.trim()}>
                    Continue
                    <ArrowRight size={14} />
                  </button>
                </div>
              </form>
            ) : live ? (
              <p className="signin-status" aria-live="polite">
                <TileSpinner size={14} className="accent" /> {job?.message}
              </p>
            ) : job?.state === "done" ? (
              <p className="signin-status" aria-live="polite">{job.message}</p>
            ) : handedOver ? (
              <p className="signin-status" aria-live="polite">
                {job?.state === "manual"
                  ? job.message
                  : "Continue on the phone beside this. Click to tap, and type with your keyboard."}
              </p>
            ) : null}

            {!live && job?.state !== "done" && (
              <div className="signin-choice" role="radiogroup" aria-label="How to sign in">
                {(
                  [
                    ["prompt", "Tap Yes on my phone", "No password: Google asks the phone you already use to confirm."],
                    ["password", "Use my password", "Type it here. penrose enters it on the phone once and keeps it nowhere; no AI sees it."],
                  ] as const
                ).map(([value, title, detail]) => (
                  <label key={value} className={method === value ? "on" : ""}>
                    <input
                      type="radio"
                      name="signin-method"
                      value={value}
                      checked={method === value}
                      onChange={() => setMethod(value)}
                    />
                    <span>
                      <b>{title}</b>
                      {detail}
                    </span>
                  </label>
                ))}
              </div>
            )}
            {!live && job?.state !== "done" && (
              <form
                className="signin-form"
                onSubmit={(e) => {
                  e.preventDefault();
                  start();
                }}
              >
                <input
                  className="input"
                  type="email"
                  autoComplete="email"
                  spellCheck={false}
                  placeholder="Your Google email, like name@gmail.com"
                  aria-label="Google email address"
                  value={email}
                  maxLength={254}
                  onChange={(e) => setEmail(e.target.value)}
                />
                <button className="btn primary" type="submit" disabled={!ready || ws.running || sending || !email.trim()}>
                  {handedOver || job?.state === "failed" ? "Try again" : "Sign in"}
                  <ArrowRight size={14} />
                </button>
              </form>
            )}

            <div className="setup-actions">
              {COMPANION ? (
                // The phone app has no phone panel: there is no signing in on the phone by hand from here.
                live && (
                  <button className="btn quiet" onClick={cancel}>
                    Stop
                  </button>
                )
              ) : live ? (
                <button className="btn quiet" onClick={cancel}>
                  Stop and sign in on the phone myself
                </button>
              ) : (
                job?.state !== "done" && (
                  <button className="btn quiet" disabled={!ready || ws.running} onClick={signInMyself}>
                    {onPhone ? "Open sign-in on the phone again" : "Or sign in on the phone yourself"}
                  </button>
                )
              )}
              <span className="setup-hint">
                {!ready
                  ? "The phone is starting…"
                  : problem || (job?.state === "failed" ? job.message : "")}
              </span>
            </div>
          </div>
        </div>
        <div className="card-foot">
          <LockKeyhole size={13} />
          Anything you type here goes straight into the phone, never to an AI model, and penrose keeps none of it.
        </div>
      </div>
    </div>
  );
}
