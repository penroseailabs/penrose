import { useEffect, useRef, useState } from "react";
import { ChevronRight, Pause, Phone, Play } from "lucide-react";
import type { Attachment, Message } from "../api";
import { clock } from "../lib/status";

/** A call's recording, joined from its parts (api/uploads.py `audio`): the same on the computer and in the phone
 *  app, whose tunnel carries each part as base64 JSON. */
async function recording(file: Attachment): Promise<ArrayBuffer> {
  const whole = new Uint8Array(file.size);
  for (let part = 0, got = 0; got < file.size; part++) {
    const r = await fetch(`/api/uploads/${file.id}/audio?part=${part}`);
    if (!r.ok) throw new Error(String(r.status));
    let bytes: Uint8Array;
    if ((r.headers.get("content-type") || "").startsWith("audio/")) bytes = new Uint8Array(await r.arrayBuffer());
    else {
      const j = (await r.json()) as { audio?: string };
      if (!j.audio) throw new Error("no audio");
      bytes = Uint8Array.from(atob(j.audio), (c) => c.charCodeAt(0));
    }
    if (!bytes.length || got + bytes.length > file.size) throw new Error("bad part");
    whole.set(bytes, got);
    got += bytes.length;
  }
  return whole.buffer;
}

const mmss = (seconds: number) => `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, "0")}`;

/** The recording as it plays: decoded sound on the page's audio engine, not a media element, so it needs no file
 *  address (the window's content policy allows none made in the page) and plays the same in the phone app. */
type Sound = { ctx: AudioContext; buffer: AudioBuffer | null; source: AudioBufferSourceNode | null; from: number; began: number };

/** A call penrose made, once it is over (backend/app/runtime/call.py `show`): who it was with and how long, the
 *  recording to play, and what was said. The message's first line names the call ("Call with Grace · 1:42"); each
 *  line after it is one turn ("Grace: …", "penrose: …"). The recording loads when it is first played. */
export default function CallCard({ message }: { message: Message }) {
  const [head, ...said] = message.content.split("\n");
  const [name, length] = head.split(" · ");
  const file = message.attachments?.find((f) => f.kind === "audio");
  const sound = useRef<Sound | null>(null);
  const [state, setState] = useState<"idle" | "loading" | "playing" | "paused" | "failed">("idle");
  const [at, setAt] = useState(0);
  const [total, setTotal] = useState(0);
  const [open, setOpen] = useState(false);

  const stop = () => {
    const s = sound.current;
    if (!s?.source) return;
    s.from = Math.min(s.ctx.currentTime - s.began, s.buffer?.duration ?? 0);
    s.source.onended = null;
    s.source.stop();
    s.source = null;
  };
  const start = (from: number) => {
    const s = sound.current;
    if (!s?.buffer) return;
    stop();
    const source = s.ctx.createBufferSource();
    source.buffer = s.buffer;
    source.connect(s.ctx.destination);
    source.onended = () => {  // it played to its end
      s.source = null;
      s.from = 0;
      setAt(0);
      setState("paused");
    };
    source.start(0, from);
    s.source = source;
    s.began = s.ctx.currentTime - from;
    setState("playing");
  };

  useEffect(
    () => () => {
      stop();
      sound.current?.ctx.close().catch(() => {});
    },
    [],
  );
  // Where it is, while it plays.
  useEffect(() => {
    if (state !== "playing") return;
    const tick = window.setInterval(() => {
      const s = sound.current;
      if (s?.source) setAt(Math.min(s.ctx.currentTime - s.began, s.buffer?.duration ?? 0));
    }, 200);
    return () => window.clearInterval(tick);
  }, [state]);

  const toggle = async () => {
    if (!file || state === "loading") return;
    if (state === "playing") {
      stop();
      setState("paused");
      return;
    }
    // Made (and woken) in the tap itself: a browser lets sound start only from one.
    const s = (sound.current ||= { ctx: new AudioContext(), buffer: null, source: null, from: 0, began: 0 });
    s.ctx.resume().catch(() => {});
    if (!s.buffer) {
      setState("loading");
      try {
        s.buffer = await s.ctx.decodeAudioData(await recording(file));
        setTotal(s.buffer.duration);
      } catch {
        setState("failed");
        return;
      }
    }
    start(s.from);
  };
  const seek = (to: number) => {
    const s = sound.current;
    if (!s?.buffer) return;
    setAt(to);
    if (s.source) start(to);
    else s.from = to;
  };

  return (
    <div className="call surface appear">
      <div className="call-head">
        <Phone size={13} aria-hidden="true" />
        <b>{name}</b>
        <time>{clock(message.created)}</time>
      </div>
      {file && (
        <div className="call-player">
          <button
            type="button"
            className={"call-play" + (state === "loading" ? " loading" : "")}
            onClick={toggle}
            aria-label={state === "playing" ? "Pause the recording" : "Play the recording"}
          >
            {state === "playing" ? <Pause size={15} /> : <Play size={15} />}
          </button>
          <input
            type="range"
            className="call-seek"
            min={0}
            max={total || 1}
            step={0.1}
            value={at}
            disabled={!total}
            aria-label="Position in the recording"
            style={{ "--p": `${total ? (at / total) * 100 : 0}%` } as React.CSSProperties}
            onChange={(e) => seek(Number(e.target.value))}
          />
          <span className="call-time">
            {state === "failed" ? "Could not load" : total ? `${mmss(at)} / ${mmss(total)}` : length || ""}
          </span>
        </div>
      )}
      {said.length > 0 && (
        <>
          <button type="button" className="thinking-toggle" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
            <ChevronRight size={12} className={open ? "turned" : ""} />
            What was said
          </button>
          {open && (
            <div className="call-said">
              {said.map((line, i) => {
                const cut = line.indexOf(": ");
                const who = cut > 0 ? line.slice(0, cut) : "";
                return (
                  <p key={i} className={who === "penrose" ? "ours" : ""}>
                    {who && <b>{who}</b>}
                    {cut > 0 ? line.slice(cut + 2) : line}
                  </p>
                );
              })}
            </div>
          )}
        </>
      )}
    </div>
  );
}
