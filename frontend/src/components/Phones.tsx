/**
 * "Relay on your phone": the steps to link the user's own phone, the QR code that does it, the phones asking to
 * be allowed, and the phones already linked (backend/app/api/companion.py). The first time, the desktop app sets
 * the companion up: it deploys the coordinator to the user's own Cloudflare account (desktop/src/companion.js).
 */

import { useEffect, useRef, useState } from "react";
import { Smartphone, Trash2 } from "lucide-react";
import QRCode from "qrcode";
import { api } from "../api";
import { TileSpinner } from "./Tiles";
import { onUpdate } from "../lib/updates";
import Sheet from "./Sheet";

type Phone = { id: string; name: string; created: number; last_seen: number };
type Pending = { id: string; name: string; code: string; created: number; expires: number };
type Status = {
  enabled: boolean;
  origin: string;
  connected: boolean;
  error: string;
  name: string;
  online: string[];
  pending: Pending[];
  phones: Phone[];
};
type Pairing = { url: string; expires: number };

function ago(seconds: number) {
  const gap = Date.now() / 1000 - seconds;
  if (gap < 90) return "just now";
  if (gap < 3600) return `${Math.round(gap / 60)} min ago`;
  if (gap < 86400) return `${Math.round(gap / 3600)} h ago`;
  return `${Math.round(gap / 86400)} d ago`;
}

function Code({ url }: { url: string }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    if (canvas.current) QRCode.toCanvas(canvas.current, url, { width: 232, margin: 1, errorCorrectionLevel: "M" }).catch(() => {});
  }, [url]);
  return <canvas ref={canvas} className="qr" aria-label="QR code to scan with your phone" />;
}

export default function PhonesSheet({ onClose }: { onClose: () => void }) {
  const [status, setStatus] = useState<Status | null>(null);
  const [pairing, setPairing] = useState<Pairing | null>(null);
  const [problem, setProblem] = useState("");
  // The one-time setup: what it is doing right now, and whether the browser was sent to Cloudflare.
  const [setup, setSetup] = useState<{ running: boolean; step: string; login: boolean }>({ running: false, step: "", login: false });
  const canSetUp = Boolean(window.relay?.companionSetup);

  // A phone may ask to be allowed at any moment: the API pushes the companion's status as it changes.
  useEffect(() => onUpdate<Status | null>("companion", (s) => s && setStatus(s)), []);

  // A setup already going (the sheet was closed and opened again) shows its progress here too.
  useEffect(() => {
    window.relay?.companionState?.().then((s) => s.running && setSetup({ running: true, step: s.step, login: false }));
    return window.relay?.onCompanionEvent?.((event) => {
      if (event.event === "step") setSetup((s) => ({ ...s, running: true, step: event.text }));
      if (event.event === "login") setSetup((s) => ({ ...s, login: true }));
      if (event.event === "error") setSetup({ running: false, step: "", login: false });
      if (event.event === "done") setSetup({ running: false, step: "", login: false });
    });
  }, []);

  // Once the computer is connected, the code shows by itself and renews when it runs out.
  const ready = Boolean(status?.enabled && status.connected);
  useEffect(() => {
    if (!ready || pairing || status?.pending.length) return;
    api<Pairing>("/companion/pairings", "POST").then(setPairing).catch((e) => setProblem(e.message));
  }, [ready, pairing, status?.pending.length]);
  useEffect(() => {
    if (!pairing) return;
    const timer = window.setTimeout(() => setPairing(null), Math.max(0, pairing.expires * 1000 - Date.now()));
    return () => window.clearTimeout(timer);
  }, [pairing]);

  async function setUp() {
    setProblem("");
    setSetup({ running: true, step: "Starting", login: false });
    const result = await window.relay!.companionSetup!();
    setSetup({ running: false, step: "", login: false });
    if (!result.ok) return setProblem(result.error || "Setup did not finish.");
    try {
      setStatus({ ...(await api<Status>("/companion/origin", "PUT", { origin: result.origin })), phones: status?.phones || [] });
    } catch (e) {
      setProblem(e instanceof Error ? e.message : String(e));
    }
  }

  async function decide(id: string, allow: boolean) {
    setProblem("");
    try {
      await api(`/companion/pending/${id}`, "POST", { allow });
      if (allow) setPairing(null);
      setStatus(await api<Status>("/companion"));
    } catch (e) {
      setProblem(e instanceof Error ? e.message : String(e));
    }
  }

  async function forget(id: string) {
    try {
      await api(`/companion/phones/${id}`, "DELETE");
      setStatus(await api<Status>("/companion"));
    } catch (e) {
      setProblem(e instanceof Error ? e.message : String(e));
    }
  }

  const pending = status?.pending || [];
  return (
    <Sheet title="penrose on your phone" onClose={onClose}>
      <p className="phones-intro">
        Use penrose from your own phone, wherever you are. Everything between the phone and this computer is encrypted
        end to end; nothing is kept anywhere else.
      </p>

      {status && !status.enabled && (
        <div className="phones-setup">
          <ol className="phones-steps">
            <li>
              <b>Sign in to Cloudflare</b>
              <small>A free account; the sign-in opens in your browser. penrose puts a tiny relay on it that only ever sees
              encrypted messages, so nobody but you runs anything.</small>
            </li>
            <li>
              <b>Scan the code with your phone</b>
              <small>It appears here once the setup is done, and stays valid for good.</small>
            </li>
            <li>
              <b>Allow the phone</b>
              <small>Both show the same six digits; one click links them.</small>
            </li>
          </ol>
          {setup.running ? (
            <p className="phones-progress">
              <TileSpinner size={14} className="accent" /> {setup.step}…
              {setup.login && <small>Finish signing in in your browser, then come back here.</small>}
            </p>
          ) : canSetUp ? (
            <button className="btn primary" onClick={setUp}>Set up with Cloudflare</button>
          ) : (
            <p className="phones-problem">Set COMPANION_ORIGIN to the address of a deployed coordinator (docs/COMPANION.md).</p>
          )}
        </div>
      )}
      {status?.enabled && !status.connected && (
        <p className="phones-problem">
          {/SSL|TLS|certificate/i.test(status.error)
            ? "Cloudflare is still setting up your new address; this takes a few minutes the first time…"
            : `Waiting for a connection to the coordinator${status.error ? ` (${status.error})` : ""}…`}
        </p>
      )}
      {problem && <p className="phones-problem">{problem}</p>}

      {pending.map((p) => (
        <div key={p.id} className="phones-pending">
          <Smartphone size={18} />
          <div>
            <b>{p.name} wants to link to this computer</b>
            <small>Allow it if the phone shows this code</small>
            <div className="phones-code">{p.code.slice(0, 3)} {p.code.slice(3)}</div>
          </div>
          <div className="phones-actions">
            <button className="btn small primary" onClick={() => decide(p.id, true)}>Allow</button>
            <button className="btn small" onClick={() => decide(p.id, false)}>Deny</button>
          </div>
        </div>
      ))}

      {ready && !pending.length && (
        <div className="phones-qr">
          {pairing ? <Code url={pairing.url} /> : <div className="qr phones-qr-wait" />}
          <p>
            Scan with your phone’s camera and open the link. On Android, Chrome offers to install penrose; on an iPhone, add
            it to the home screen when asked. Then allow the phone here.
          </p>
        </div>
      )}

      {!!status?.phones.length && (
        <div className="set-group">
          {status.phones.map((phone) => (
            <div key={phone.id} className="set-row">
              <Smartphone size={16} className="phones-icon" />
              <div>
                <b>{phone.name}</b>
                <small>{status.online.includes(phone.id) ? "Connected now" : `Last seen ${ago(phone.last_seen)}`}</small>
              </div>
              <button className="ib" aria-label={`Unlink ${phone.name}`} title="Unlink" onClick={() => forget(phone.id)}>
                <Trash2 size={14} />
              </button>
            </div>
          ))}
        </div>
      )}
    </Sheet>
  );
}
