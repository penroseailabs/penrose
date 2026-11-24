/**
 * The ears, the mouth and the bell of voice mode (backend/app/api/voice.py).
 *
 * - `Listener`: the microphone, streamed as 24 kHz PCM straight to OpenAI's transcription session with the short-lived
 *   secret the API hands out. It says when the user starts and stops speaking (OpenAI's voice detection) and what they
 *   said, as it comes and once it is final. Nothing of the audio crosses Relay's own connection.
 * - `Speaker`: a reply as speech, fetched from the API (which has it made) and played through WebAudio, so it can be
 *   paused the instant the user speaks over it, resumed if that was only noise, or stopped for good.
 * - `Bell`: a ring and a vibration while a finished task waits to be picked up.
 */

import { api } from "../api";
import { canceller, noteHeard, playback, reference } from "./echo";

export const RATE = 24000;
const CHUNK_MS = 100;
const RECONNECT_MS = 1200;

export type ListenerEvents = {
  /** OpenAI heard the user begin to speak. */
  speech: Event;
  /** ...and stop. */
  silence: Event;
  /** Words so far, while the user speaks. */
  partial: CustomEvent<string>;
  /** What the user said, final. */
  final: CustomEvent<string>;
  /** "connecting", "listening", "muted", "off" or "error". */
  state: CustomEvent<ListenerState>;
};
export type ListenerState = "connecting" | "listening" | "muted" | "off" | "error";

const CHUNK = (RATE * CHUNK_MS) / 1000;

// The events a browser lets start sound from. An iPhone counts a touch only when the finger lifts (pointerup,
// touchend), never pointerdown, which is all a mouse needs.
const TAPS = ["pointerdown", "pointerup", "touchend", "keydown"] as const;

/** Calls `fn` on every tap or key press anywhere on the page, which is when a browser lets audio start. Returns the
 *  undo. */
export function onEveryTap(fn: () => void) {
  TAPS.forEach((t) => document.addEventListener(t, fn, { capture: true, passive: true }));
  return () => TAPS.forEach((t) => document.removeEventListener(t, fn, { capture: true }));
}

/** An iPhone starts an AudioContext made without a tap paused ("suspended"), and pauses it again ("interrupted")
 *  after a call or a trip to the background; paused, it reads nothing from the microphone. Resumes it now if the
 *  browser allows, and on each tap while it is paused. Returns the undo. */
function keepRunning(ctx: AudioContext) {
  const resume = () => {
    if (ctx.state !== "running" && ctx.state !== "closed") ctx.resume().catch(() => {});
  };
  resume();
  return onEveryTap(resume);
}
const ECHO_BLOCK = RATE / 50; // 20 ms

/** The worklet that turns the microphone's float frames into Int16 PCM blocks of CHUNK_MS. With `cancel`, it passes
 *  20 ms blocks of the microphone and of what was played at the same moment (its second input, echo.ts `reference`)
 *  on to the echo canceller instead. Inline, so the companion app needs no extra file: it is loaded from a blob URL. */
const WORKLET = `
class PCM extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.cancel = !!(options.processorOptions && options.processorOptions.cancel);
    this.buffer = new Int16Array(${CHUNK});
    this.mic = new Float32Array(${ECHO_BLOCK});
    this.ref = new Float32Array(${ECHO_BLOCK});
    this.at = 0;
  }
  process(inputs) {
    const input = inputs[0] && inputs[0][0];
    if (!input) return true;
    if (this.cancel) {
      const played = inputs[1] && inputs[1][0];
      for (let i = 0; i < input.length; i++) {
        this.mic[this.at] = input[i];
        this.ref[this.at++] = played ? played[i] : 0;
        if (this.at === this.mic.length) {
          this.port.postMessage({ mic: this.mic.slice(0), ref: this.ref.slice(0) });
          this.at = 0;
        }
      }
      return true;
    }
    for (let i = 0; i < input.length; i++) {
      const s = Math.max(-1, Math.min(1, input[i]));
      this.buffer[this.at++] = s < 0 ? s * 0x8000 : s * 0x7fff;
      if (this.at === this.buffer.length) {
        this.port.postMessage(this.buffer.buffer.slice(0));
        this.at = 0;
      }
    }
    return true;
  }
}
registerProcessor("relay-pcm", PCM);
`;

const withWorklet = new WeakSet<AudioContext>(); // contexts that already have the worklet (a name registers once)

/** The microphone `stream` as Int16 PCM blocks of CHUNK_MS at RATE, to `onChunk`. With `cancelEcho` (the
 *  loudspeaker, where the browser's own cancellation is off), Relay's own voice is taken out first (echo.ts).
 *  With `shared`, it reads in that AudioContext and leaves it open on `close` (the caller's, kept so that what plays
 *  in it carries on while the microphone is closed and opened again); else it makes its own and closes it. */
export async function capture(
  stream: MediaStream,
  cancelEcho: boolean,
  onPcm: (pcm: ArrayBuffer) => void,
  shared?: AudioContext,
) {
  // The voice screen's waves follow what is sent (Waves.tsx).
  const onChunk = (pcm: ArrayBuffer) => {
    noteHeard(pcm);
    onPcm(pcm);
  };
  const ctx = shared ?? new AudioContext({ sampleRate: RATE });
  const source = ctx.createMediaStreamSource(stream);
  const undo: (() => void)[] = [keepRunning(ctx), () => source.disconnect()];
  if (ctx.audioWorklet) {
    if (!withWorklet.has(ctx)) {
      const url = URL.createObjectURL(new Blob([WORKLET], { type: "application/javascript" }));
      try {
        await ctx.audioWorklet.addModule(url);
      } catch (e) {
        undo.forEach((u) => u());
        if (!shared) ctx.close().catch(() => {});
        throw e;
      } finally {
        URL.revokeObjectURL(url);
      }
      withWorklet.add(ctx);
    }
    const node = new AudioWorkletNode(ctx, "relay-pcm", {
      numberOfInputs: cancelEcho ? 2 : 1,
      numberOfOutputs: 0,
      processorOptions: { cancel: cancelEcho },
    });
    source.connect(node, 0, 0);
    if (cancelEcho) {
      const aec = canceller(RATE, CHUNK, onChunk);
      node.port.onmessage = (e) => aec.push(e.data.mic as Float32Array, e.data.ref as Float32Array);
      undo.push(reference(ctx, node, 1), () => aec.close());
    } else {
      node.port.onmessage = (e) => onChunk(e.data as ArrayBuffer);
    }
    undo.push(() => node.disconnect());
  } else {
    // Older WebViews: the deprecated processor, resampled by the context itself, and no echo cancellation.
    const node = ctx.createScriptProcessor(2048, 1, 1);
    node.onaudioprocess = (e) => {
      const input = e.inputBuffer.getChannelData(0);
      const out = new Int16Array(input.length);
      for (let i = 0; i < input.length; i++) {
        const s = Math.max(-1, Math.min(1, input[i]));
        out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
      }
      onChunk(out.buffer);
    };
    source.connect(node);
    node.connect(ctx.destination);
    undo.push(() => node.disconnect());
  }
  return {
    /** Where the microphone is read: what plays here is cancelled against exactly (echo.ts). */
    context: ctx,
    close() {
      undo.forEach((u) => u());
      noteHeard(null);
      if (!shared) ctx.close().catch(() => {});
    },
  };
}

/** Silence of `ms` milliseconds as base64 Int16 PCM, to end a turn the voice detection is still waiting on. */
export function silence(ms: number) {
  return b64(new ArrayBuffer(((RATE * ms) / 1000) * 2));
}

export function b64(bytes: ArrayBuffer) {
  const view = new Uint8Array(bytes);
  let out = "";
  for (let i = 0; i < view.length; i += 0x8000) out += String.fromCharCode(...view.subarray(i, i + 0x8000));
  return btoa(out);
}

/** Where the phone plays Relay's voice. On Android, Chrome runs the microphone in voice-call mode whenever the
 *  browser's echo cancellation is on, and then routes playback to the earpiece; without it the loudspeaker is used,
 *  and the page cancels Relay's own voice itself (echo.ts) so the user can speak over Relay ("speaker"). With an
 *  earpiece or headphones the browser's cancellation stays on ("earpiece"). */
export type Output = "speaker" | "earpiece";

export class Listener extends EventTarget {
  state: ListenerState = "off";
  /** While muted the microphone is not open at all (the phone's mic indicator is off): it is opened when the user
   *  unmutes and released when they mute. The screen opens muted. */
  muted = true;
  output: Output = "speaker";
  error = "";
  private ws: WebSocket | null = null;
  private stream: MediaStream | null = null;
  private mic: { close(): void } | null = null;
  private opening: Promise<boolean> | null = null;
  private wanted = false;
  private retry = 0;
  private partial = "";
  private speaking = false; // OpenAI heard the user begin to speak, and has not heard them stop

  private set(state: ListenerState, error = "") {
    this.state = state;
    this.error = error;
    this.dispatchEvent(new CustomEvent("state", { detail: state }));
  }

  /** Connect (and, unless muted, open the microphone, asking the first time). Keeps itself connected until `stop`. */
  async start() {
    this.wanted = true;
    if (!this.muted && !(await this.openMic())) return;
    await this.connect();
    // Already connected (a retry after an error): say so again.
    if (this.ws?.readyState === WebSocket.OPEN) this.set(this.muted ? "muted" : "listening");
  }

  /** Open the microphone if it is not open. False when it could not be (the state is then "error"). */
  private openMic(): Promise<boolean> {
    return (this.opening ??= this.tryOpenMic().finally(() => {
      this.opening = null;
    }));
  }

  private async tryOpenMic(): Promise<boolean> {
    if (this.stream) return true;
    const processed = this.output === "earpiece";
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: processed,
          noiseSuppression: processed,
          autoGainControl: processed,
          channelCount: 1,
        },
      });
    } catch (e) {
      this.muted = true;
      this.set(
        "error",
        /denied|permission/i.test(String(e))
          ? "penrose needs the microphone. Allow it in the browser's site settings."
          : "The microphone could not be opened.",
      );
      return false;
    }
    // Muted again (or stopped) while the browser was opening it: let it go.
    if (this.muted || !this.wanted) {
      stream.getTracks().forEach((t) => t.stop());
      return true;
    }
    try {
      this.mic = await capture(stream, this.output === "speaker", (pcm) => {
        if (this.muted || !this.ws || this.ws.readyState !== WebSocket.OPEN) return;
        this.ws.send(JSON.stringify({ type: "input_audio_buffer.append", audio: b64(pcm) }));
      });
    } catch {
      stream.getTracks().forEach((t) => t.stop());
      this.muted = true;
      this.set("error", "The microphone could not be opened.");
      return false;
    }
    this.stream = stream;
    if (this.muted || !this.wanted) this.closeMic();
    return true;
  }

  /** Release the microphone: its tracks stop, so the phone's indicator goes out. */
  private closeMic() {
    this.mic?.close();
    this.mic = null;
    this.stream?.getTracks().forEach((t) => t.stop());
    this.stream = null;
  }

  private async connect() {
    if (!this.wanted || this.ws) return;
    this.set("connecting");
    let secret: { secret: string; url: string };
    try {
      secret = await api<{ secret: string; url: string }>("/voice/listen", "POST");
    } catch (e) {
      this.set("error", (e as Error).message);
      this.later();
      return;
    }
    if (!this.wanted) return;
    const ws = new WebSocket(secret.url, ["realtime", `openai-insecure-api-key.${secret.secret}`]);
    this.ws = ws;
    ws.onopen = () => {
      this.retry = 0;
      this.set(this.muted ? "muted" : "listening");
    };
    ws.onmessage = (e) => this.receive(String(e.data));
    ws.onclose = () => {
      if (this.ws !== ws) return;
      this.ws = null;
      if (this.wanted) {
        this.set("connecting");
        this.later();
      }
    };
    ws.onerror = () => {};
  }

  private later() {
    if (!this.wanted) return;
    const wait = Math.min(RECONNECT_MS * 2 ** this.retry++, 15000);
    window.setTimeout(() => this.connect(), wait);
  }

  private receive(raw: string) {
    let event: { type?: string; delta?: string; transcript?: string; error?: { message?: string } };
    try {
      event = JSON.parse(raw);
    } catch {
      return;
    }
    switch (event.type) {
      case "input_audio_buffer.speech_started":
        this.partial = "";
        this.speaking = true;
        this.dispatchEvent(new Event("speech"));
        break;
      case "input_audio_buffer.speech_stopped":
        this.speaking = false;
        this.dispatchEvent(new Event("silence"));
        break;
      case "conversation.item.input_audio_transcription.delta":
        this.partial += event.delta || "";
        this.dispatchEvent(new CustomEvent("partial", { detail: this.partial }));
        break;
      case "conversation.item.input_audio_transcription.completed": {
        const text = (event.transcript || "").trim();
        this.partial = "";
        if (text) this.dispatchEvent(new CustomEvent("final", { detail: text }));
        break;
      }
      case "error":
        this.set("error", event.error?.message || "The transcription service refused.");
        break;
    }
  }

  /** Mute releases the microphone; unmute opens it again (a moment, and the first time the browser asks). The
   *  connection stays up either way: nothing is sent while muted, and it does not need audio to stay open. False when
   *  the microphone could not be opened (the listener is then muted, and its state "error"). */
  async setMuted(muted: boolean): Promise<boolean> {
    this.muted = muted;
    if (muted) {
      this.endTurn();
      this.closeMic();
    } else if (this.wanted && !(await this.openMic())) return false;
    if (this.ws?.readyState === WebSocket.OPEN) this.set(this.muted ? "muted" : "listening");
    return true;
  }

  /** Muted in the middle of a sentence: voice detection only ends a turn when it hears quiet, and none will come,
   *  so send a second of it. */
  private endTurn() {
    if (!this.speaking || this.ws?.readyState !== WebSocket.OPEN) return;
    const quiet = silence(100);
    for (let i = 0; i < 10; i++) this.ws.send(JSON.stringify({ type: "input_audio_buffer.append", audio: quiet }));
  }

  /** Close the connection and the microphone. */
  stop() {
    this.wanted = false;
    this.ws?.close();
    this.ws = null;
    this.closeMic();
    this.speaking = false;
    this.set("off");
  }
}

/** Speech from the API, as the bytes to decode: a real audio response in the window, or the tunnel's JSON with the
 *  bytes in base64 on the phone (backend/app/companion/tunnel.py). */
async function fetchSpeech(text: string): Promise<ArrayBuffer> {
  const response = await fetch("/api/voice/speak", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
  if (!response.ok) {
    const detail = await response.json().then(
      (e) => e.detail,
      () => "",
    );
    throw new Error(typeof detail === "string" && detail ? detail : `Speech failed (${response.status})`);
  }
  if ((response.headers.get("content-type") || "").startsWith("audio/")) return response.arrayBuffer();
  const body = (await response.json()) as { audio?: string };
  if (!body.audio) throw new Error("No speech came back");
  const bin = atob(body.audio);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes.buffer;
}

export class Speaker {
  private ctx: AudioContext | null = null;
  private source: AudioBufferSourceNode | null = null;
  private done: (() => void) | null = null;
  private generation = 0; // a `say` that is still fetching gives up when `stop` comes first
  /** The words being said, for telling the user's voice from the phone's own coming back through the microphone. */
  current = "";
  speaking = false;
  paused = false;

  /** Browsers only play sound after a tap: call this from one. */
  unlock() {
    if (!this.ctx) this.ctx = new AudioContext();
    if (this.ctx.state !== "running") this.ctx.resume().catch(() => {});
  }

  /** Say `text`; resolves when it has been said, or stopped. A long text is said in parts of a few sentences,
   *  each fetched while the one before plays: the first words come sooner, and each clip stays well within a
   *  companion tunnel frame (the API also cuts a single call at SPEAK_CHARS, backend/app/api/voice.py). */
  async say(text: string): Promise<void> {
    this.stop();
    this.unlock();
    const ctx = this.ctx!;
    const generation = ++this.generation;
    const parts = partsOf(text);
    const fetchPart = async (part: string) => ctx.decodeAudioData(await fetchSpeech(part));
    let next = fetchPart(parts[0]);
    for (let i = 0; i < parts.length; i++) {
      const buffer = await next;
      if (generation !== this.generation) return; // stopped meanwhile
      if (i + 1 < parts.length) next = fetchPart(parts[i + 1]);
      else next.catch(() => {});
      await this.play(ctx, buffer, text);
      if (generation !== this.generation) return;
    }
  }

  private play(ctx: AudioContext, buffer: AudioBuffer, text: string) {
    return new Promise<void>((resolve) => {
      const source = ctx.createBufferSource();
      source.buffer = buffer;
      source.connect(playback(ctx));
      this.source = source;
      this.current = text;
      this.speaking = true;
      this.paused = false;
      this.done = resolve;
      source.onended = () => {
        if (this.source === source) this.finish();
      };
      // A context the browser never lets run would never end: give up a little after the clip's own length,
      // counting only the time it is not held.
      let left = (buffer.duration + 1.5) * 1000;
      const watch = () => {
        if (this.source !== source) return;
        if (!this.paused) left -= 500;
        if (left <= 0) this.finish();
        else window.setTimeout(watch, 500);
      };
      window.setTimeout(watch, 500);
      if (ctx.state === "suspended") ctx.resume().catch(() => {});
      source.start();
    });
  }

  private finish() {
    this.source = null;
    this.speaking = false;
    this.paused = false;
    this.current = "";
    const done = this.done;
    this.done = null;
    done?.();
  }

  /** Hold the speech (the user began to speak over it). */
  pause() {
    if (!this.speaking || this.paused) return;
    this.paused = true;
    this.ctx?.suspend().catch(() => {});
  }

  /** Go on (it was only a noise). */
  resume() {
    if (!this.paused) return;
    this.paused = false;
    this.ctx?.resume().catch(() => {});
  }

  stop() {
    this.generation++;
    if (this.source) {
      try {
        this.source.stop();
      } catch {}
    }
    if (this.paused) this.ctx?.resume().catch(() => {});
    this.finish();
  }
}

const PART_CHARS = 700; // about a minute of speech a part

/** `text` in parts of up to PART_CHARS, broken at sentence ends (else at a space). */
export function partsOf(text: string): string[] {
  const parts: string[] = [];
  let rest = text.trim();
  while (rest.length > PART_CHARS) {
    const window = rest.slice(0, PART_CHARS);
    const ends = [...window.matchAll(/[.!?](?=\s)|\n/g)].map((m) => m.index + 1);
    let at = ends.length ? ends[ends.length - 1] : -1;
    if (at < PART_CHARS / 4) at = Math.max(window.lastIndexOf(" "), -1);
    if (at <= 0) at = PART_CHARS;
    parts.push(rest.slice(0, at).trim());
    rest = rest.slice(at).trim();
  }
  if (rest) parts.push(rest);
  return parts.length ? parts : [text];
}

/** Whether `heard` is the phone's own speech coming back through the microphone, rather than the user. */
export function isEcho(heard: string, saying: string) {
  const norm = (s: string) =>
    s
      .toLowerCase()
      .replace(/[^\p{L}\p{N} ]/gu, "")
      .replace(/\s+/g, " ")
      .trim();
  const h = norm(heard);
  if (!h) return true;
  if (h.split(" ").length <= 1 && h.length < 4) return true; // a cough, "uh"
  return norm(saying).includes(h);
}

/** A ring: two soft tones and a short vibration, once. It is up to `stop` to say the call is over; the sound is not repeated. */
export class Bell {
  private ctx: AudioContext | null = null;
  ringing = false;

  /** `soft`: one quiet note and no vibration, a nudge rather than a ring; it is over by itself. */
  start(soft = false) {
    if (this.ringing) return;
    this.ringing = true;
    this.ctx = this.ctx || new AudioContext();
    const ctx = this.ctx;
    if (ctx.state === "suspended") ctx.resume().catch(() => {});
    (soft ? [0] : [0, 0.35]).forEach((at, i) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = "sine";
      osc.frequency.value = i ? 1046 : 784; // G5, C6
      gain.gain.setValueAtTime(0.0001, ctx.currentTime + at);
      gain.gain.exponentialRampToValueAtTime(soft ? 0.1 : 0.25, ctx.currentTime + at + 0.03);
      gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + at + 0.5);
      osc.connect(gain).connect(playback(ctx));
      osc.start(ctx.currentTime + at);
      osc.stop(ctx.currentTime + at + 0.55);
    });
    if (soft) {
      setTimeout(() => (this.ringing = false), 600);
      return;
    }
    try {
      navigator.vibrate?.([200, 100, 200]);
    } catch {}
  }

  stop() {
    this.ringing = false;
    try {
      navigator.vibrate?.(0);
    } catch {}
  }
}

/** The sound of a call ringing at the other end: a soft double tone, a second on and two off, until `stop`. */
export class Ringback {
  private timer = 0;
  private gain: GainNode | null = null;

  /** `ctx`: the context the call's voice plays in (desk.ts `audio`), so the echo canceller hears the ring as it
   *  hears the voice. */
  start(ctx: AudioContext) {
    if (this.timer) return;
    if (ctx.state === "suspended") ctx.resume().catch(() => {});
    const ring = () => {
      if (ctx.state === "closed") return;
      const gain = ctx.createGain();
      const now = ctx.currentTime;
      gain.gain.setValueAtTime(0.0001, now);
      gain.gain.exponentialRampToValueAtTime(0.07, now + 0.05);
      gain.gain.setValueAtTime(0.07, now + 0.95);
      gain.gain.exponentialRampToValueAtTime(0.0001, now + 1);
      for (const hz of [440, 480]) {
        const osc = ctx.createOscillator();
        osc.type = "sine";
        osc.frequency.value = hz;
        osc.connect(gain);
        osc.start(now);
        osc.stop(now + 1.05);
      }
      gain.connect(playback(ctx));
      this.gain = gain;
    };
    ring();
    this.timer = window.setInterval(ring, 3000);
  }

  /** Answered, given up or cancelled: quiet at once, in the middle of a ring too. */
  stop() {
    window.clearInterval(this.timer);
    this.timer = 0;
    this.gain?.disconnect();
    this.gain = null;
  }
}
