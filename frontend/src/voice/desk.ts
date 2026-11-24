/**
 * The reception desk's line (backend/app/runtime/desk.py): one OpenAI Realtime session that hears the user, routes
 * their words to Relay's sessions with tools, and speaks. The desk keeps no account of the errands: every reply is
 * made from penrose as it is at that moment.
 *
 * - The microphone streams 24 kHz PCM to OpenAI; nothing of the audio crosses Relay's own connection.
 * - A reply is asked for when something calls for one (`wants`): the user finished speaking or typed, they opened an
 *   errand's card, the errand being talked about has news, or a tool finished with something left to say. Asking
 *   reads the state (`POST /api/voice/desk/state`, runtime/deskstate.py) and puts it last in the conversation in
 *   place of the one before, then asks for the response. So nothing is queued to be said later: what is said is
 *   decided when it is said, from what is true then.
 * - One voice, and the latest wins. A reply nobody has heard a sound of is dropped when the user starts to speak,
 *   and asked for again from the state once they have. In a chain of tool calls only the first acknowledgement and
 *   the last word are heard: the words a follow-up reply says before calling another tool are never played.
 * - Beside the state the conversation holds only the last lines said (a few, of the last minutes): older ones, the
 *   tool calls and their results are taken out, since the state says what they led to.
 * - News from the sessions (events) arrives through `inject`. It is a signal and a card, never words to play: about
 *   the errand being talked about it calls for a reply (a question, result or failure cuts in on the desk's own
 *   acknowledgement at its next pause; where a working errand is, only after a silence); about any other errand it
 *   is a card on the screen. An event is marked told once the reply that told it was heard to its end.
 * - Opening is silent. What the user has not heard comes with the session (`unheard`) and is emitted as "news" for
 *   the screen to show as cards. The user opening a card (`answer`) is the signal for what to talk about.
 * - A tool the desk calls goes to `POST /api/voice/desk/tool`; its result goes back into the session at once.
 * - What the user said and what the desk said are posted to `POST /api/voice/desk/log` as the session transcribes
 *   them: the tools read the user's own words there, and the next connect gets the last lines back.
 */

import { api } from "../api";
import type { Picks } from "../lib/models";
import { Captioner, type Pause } from "./captions";
import { playback } from "./echo";
import { b64, capture, type Output, RATE, silence } from "./speech";
import type { PrivateAsk } from "../components/PrivateFields";

export type DeskState = "off" | "connecting" | "ready" | "error";
export type Picture = { url: string; caption: string };
export type DeskEvent = {
  id: number;
  text: string;
  chat_id: string;
  title: string;
  kind: "progress" | "asks" | "finished" | "failed";
  /** What the desk is told of it before the user opens it: a headline, never the result. */
  news: string;
  /** The session's id as the desk sees it ("s:ab12cd"). */
  session: string;
  private: boolean;
  pictures?: Picture[];
  /** The question wants something private (a card, a password, a code): the screen opens its form as it is told. */
  ask?: PrivateAsk;
};
/** A private form the screen opens for a task's question. */
export type Asking = { chatId: string; ask: PrivateAsk };
export type Focus = { chatId: string; title: string };
type Item = Record<string, unknown>;
type Opened = {
  secret: string;
  url: string;
  items: Item[];
  /** What the user has not heard: the screen shows it as cards; opening is silent. */
  unheard: DeskEvent[];
  latest_event: number;
};
type ToolResult = {
  output: string;
  result: { error?: string; pictures?: Picture[] } & Record<string, unknown>;
  focus: { chat_id: string; title: string } | null;
};
type Realtime = {
  type?: string;
  delta?: string;
  transcript?: string;
  item_id?: string;
  item?: { id?: string; type?: string; name?: string; call_id?: string; arguments?: string };
  error?: { message?: string; code?: string; event_id?: string };
  response?: { status?: string };
  event_id?: string;
};
type Nods = { rate: number; clips: string[] };
/** What calls for a reply: the user spoke or typed; they opened an errand; the errand being talked about has
 *  news (a question, a result, a failure), or is still working after a silence; a tool finished with something left
 *  to say. */
type Why = "user" | "opened" | "news" | "progress" | "tool" | "answered";
/** penrose as it is now (runtime/deskstate.py): the block the desk reads, the errand it shows as the one being
 *  talked about, and the ids of that errand's events the user has not been told. */
type Now = { text: string; about: string; due: number[] };

/** Base64 Int16 PCM (the nods, as the desk's own audio) as samples. */
function decode(base64: string): Float32Array {
  const bin = atob(base64);
  const samples = new Float32Array(bin.length >> 1);
  for (let i = 0; i < samples.length; i++) {
    const v = (bin.charCodeAt(2 * i + 1) << 8) | bin.charCodeAt(2 * i);
    samples[i] = (v >= 0x8000 ? v - 0x10000 : v) / 0x8000;
  }
  return samples;
}

const RECONNECT_MS = 1200;
const DESK_KEY = "relay.voice.desk";
const EARLY_MS = 30_000; // a session opened as the app started is still the desk's latest within this long

/** Whether voice mode talks to the desk (health.voice_desk), as last heard, for a start before the API has said. */
export function deskRemembered() {
  try {
    return localStorage.getItem(DESK_KEY) === "1";
  } catch {
    return false;
  }
}
export function rememberDesk(on: boolean) {
  try {
    localStorage.setItem(DESK_KEY, on ? "1" : "0");
  } catch {}
}

// The phone app asks for the desk's first session as it starts (App.tsx), beside the rest of the start; the first
// connect takes it. Opening changes nothing on Relay's side, so one nobody takes is simply dropped.
let early: { at: number; opened: Promise<Opened> } | null = null;
export function openEarly() {
  if (!deskRemembered()) return;
  early = { at: Date.now(), opened: api<Opened>("/voice/desk/open", "POST") };
  early.opened.catch(() => {}); // the connect asks again, and reports a failure then
}
function openSession(): Promise<Opened> {
  const taken = early;
  early = null;
  const again = () => api<Opened>("/voice/desk/open", "POST");
  return taken && Date.now() - taken.at < EARLY_MS ? taken.opened.catch(again) : again();
}
const WAITING = 50; // blocks of microphone kept for a line still opening: five seconds of speech
// Errors the API sends for timing the desk cannot help: a cancel after the response ended, a second response asked
// for while one runs (the queue retries after it).
const QUIET_ERRORS = new Set(["response_cancel_not_active", "conversation_already_has_active_response"]);
// Turn-taking is the page's: the API says when the user starts and stops, the page cuts the desk off and asks for its
// reply. Sound over the desk's voice stops it only once BARGE_IN_MS of it is heard, counted from the microphone's own
// level (`listen`), not from the API's events: those say the user stopped only once the turn is over (a silence, or
// the semantic detector's judgement), so a timer that waited for them let nothing go. A "yeah", "mm-hm" or "right"
// (about 0.2-0.5 s), a click, a cough or what is left of the desk's own voice after the echo canceller is shorter, and
// is let go with no reply; two or three words are not. Sound from BARGE_LOOKBACK_MS before speech_started counts (the
// detector fires a little after the user began); if the meter never gets there while the detector still hears speech
// (a very soft voice), the desk stops anyway BARGE_FALLBACK_MS later, but only while the microphone still hears
// something (above SOFT within the last SOFT_RECENT_MS): a "yeah" long over is waiting for the detector to call its
// turn done, not still talking. Set when the line opens, so it does not depend on the backend's session
// (runtime/desk.py).
const BARGE_IN_MS = 600;
const BARGE_LOOKBACK_MS = 300;
const BARGE_FALLBACK_MS = 1000;
const SOFT = 0.004; // RMS: a soft voice (a third of QUIET)
const SOFT_RECENT_MS = 300;
// News that beats what the desk is saying waits for the end of its sentence (a pause at least SENTENCE_PAUSE
// long), for at most NEWS_WAIT_MS, so the desk is never cut off mid-word; its next reply then has the news.
const SENTENCE_PAUSE = 0.3;
const NEWS_WAIT_MS = 3000;
// Where a working errand is gets a word only after this long with nobody speaking: it fills a silence, it does not
// narrate. Whatever it is doing is in the state for when the user asks.
const PROGRESS_QUIET_MS = 8000;
// The dialogue kept in the conversation beside the state: the last few lines, and none older than this.
const LINES = 8;
const LINES_MS = 5 * 60_000;
// Tool calls one thing the user said may lead to, before the desk gets its last word without tools.
// The state is read while the user is still speaking (when they start, and at each pause), so the reply to them
// does not wait for it: one read at most this long before their turn ended is the state it is made from.
const STATE_FRESH_MS = 3000;
const STATE_AGAIN_MS = 1500; // ...and it is read again at a pause this long after the last read
const STATE_PAUSE_MS = 250;
const ROUNDS = 3;
// A reply that is stopped fades out over this long (seconds), as a voice trails off, instead of breaking off.
const FADE = 0.25;
// The words decide when the user's turn ends, not a timer: the API's semantic turn detection judges from what they
// have said whether they are done, and a pause in the middle of a thought keeps the turn open (`eagerness` low: a
// listener who lets them think). Replies stay the page's to ask for.
const TURNS = { type: "semantic_vad", eagerness: "low", create_response: false, interrupt_response: false };
// If the model refuses that (an older one), silence ends a turn, but more of it than the API's default 500 ms.
const PLAIN_TURNS = {
  type: "server_vad",
  threshold: 0.5,
  prefix_padding_ms: 300,
  silence_duration_ms: 1200,
  create_response: false,
  interrupt_response: false,
};
const TURNS_EVENT = "turns"; // the session.update's event_id, so a refusal is known for what it is
const TIDY = "tidy_"; // the event_ids of the deletes that keep the conversation short (`remove`)
// Nods: a short "mm-hm" (a clip from /voice/desk/nods, never a reply: no round trip, not a turn), so the user
// knows they are heard. One comes at a pause of NOD_AFTER_MS in anything they have said (NOD_SPEECH_MS of sound,
// so not after a cough): long enough that they may be done, too short to be sure, which is when the turn
// detector is still waiting and nothing else would be heard. And one comes NOD_REPLY_MS after their turn has
// ended, if the reply has not begun to sound by then and the pause had none. Nods come at most every NOD_GAP_MS
// within a turn. They used to come only in turns of four seconds or more: a short request met silence until the
// reply, which felt like not being heard (2026-11-24).
const NOD_AFTER_MS = 900;
const NOD_SPEECH_MS = 500;
const NOD_GAP_MS = 6000;
const NOD_REPLY_MS = 600;
const NOD_RECENT_MS = 2500; // a nod this recent stands for the end of the turn too
// A long turn transcribes after the desk has answered it: a tool that passes the user's words on waits this long
// for the transcript, so the session gets their words and not the desk's retelling (runtime/desk.py their_words).
const TRANSCRIPT_WAIT_MS = 2500;
const WORDS = new Set(["start_errand", "pass_on", "rewind_errand"]); // the tools that carry the user's words
// Tools whose result only confirms where the words went (the desk says that before calling, when it does).
const ROUTING = new Set(["start_errand", "pass_on", "stop_errand", "resume_errand", "rewind_errand"]);

/** The microphone as Int16 PCM blocks, to `onChunk`. The processing follows `processed` (the browser's echo
 *  cancellation for the earpiece; for the loudspeaker, the page's own: speech.ts Output, echo.ts).
 *
 *  Opened only while the user is unmuted: `open` asks the browser for the microphone and `close` gives it back (its
 *  tracks stop, so the phone's microphone indicator goes out). The AudioContext it reads in is kept apart from the
 *  stream, made by `prepare` and closed by `dispose`: the desk's voice plays in it (so the echo canceller hears
 *  exactly what plays), and must carry on playing when the user mutes or unmutes in the middle of it. */
class Microphone {
  private stream: MediaStream | null = null;
  private mic: Awaited<ReturnType<typeof capture>> | null = null;
  private ctx: AudioContext | null = null;
  private cancels = false;
  private opening: Promise<void> | null = null;
  constructor(private onChunk: (pcm: ArrayBuffer) => void) {}

  /** Make ready for a line with this processing: the context the microphone will read in. */
  prepare(processed: boolean) {
    this.cancels = !processed;
    if (!this.ctx || this.ctx.state === "closed") this.ctx = new AudioContext({ sampleRate: RATE });
  }

  /** The AudioContext the page cancels echo in (the loudspeaker), for the desk's voice to play in; else null. */
  get context(): AudioContext | null {
    return this.cancels ? this.ctx : null;
  }

  /** Open the microphone (asking the first time). Rejects when the browser will not give it. */
  open(processed: boolean): Promise<void> {
    return (this.opening ??= this.tryOpen(processed).finally(() => {
      this.opening = null;
    }));
  }

  private async tryOpen(processed: boolean) {
    if (this.stream) return;
    this.prepare(processed);
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: processed, noiseSuppression: processed, autoGainControl: processed, channelCount: 1 },
    });
    try {
      this.mic = await capture(stream, !processed, this.onChunk, this.ctx!);
      this.stream = stream;
    } catch (e) {
      stream.getTracks().forEach((t) => t.stop()); // so the next open starts over instead of keeping a stream nothing reads
      throw e;
    }
  }

  /** Open: the browser has given the microphone, and its sound is being read. */
  get on() {
    return !!this.stream;
  }

  /** Give the microphone back. The context stays. */
  close() {
    this.mic?.close();
    this.mic = null;
    this.stream?.getTracks().forEach((t) => t.stop());
    this.stream = null;
  }

  /** Close for good, the context too. */
  dispose() {
    this.close();
    this.ctx?.close().catch(() => {});
    this.ctx = null;
  }
}

const FRAME = RATE / 100; // 10 ms
const QUIET = 0.012; // a frame this soft (RMS) is silence
const PAUSE = 0.16; // seconds of silence that make a pause between words worth pinning captions to

/** The desk's voice: PCM chunks played as they stream, in order, through WebAudio; flushed the instant the user
 *  speaks over it or taps. It also keeps the reply's own timeline for the captions (captions.ts): how much has
 *  been heard, and where the pauses fell. */
class Player {
  private ctx: AudioContext | null = null;
  private own: AudioContext | null = null;
  private shared: AudioContext | null = null; // the microphone's (Microphone.context), while there is one
  private gain: GainNode | null = null; // what is playing now goes through it, so a stop can fade it out
  private next = 0;
  private sources = new Set<AudioBufferSourceNode>();
  private startedAt = 0;
  private replyStart = -1;
  private quietFrom = -1; // reply time a silence began, while in one
  private spoken = false; // the reply has made a sound (silence before it is not a pause)
  /** The reply's pauses so far, in seconds from its start. */
  pauses: Pause[] = [];
  playing = false;
  /** `natural`: it ran out of sound, rather than being flushed. */
  constructor(private onIdle: (natural: boolean) => void) {}

  /** A new reply: its timeline starts with its first sound. */
  beginReply() {
    this.replyStart = -1;
    this.quietFrom = -1;
    this.spoken = false;
    this.pauses = [];
  }

  /** Seconds of the current reply heard so far, gaps included. */
  heard() {
    if (!this.ctx || this.replyStart < 0) return 0;
    return Math.max(0, Math.min(this.ctx.currentTime, this.next) - this.replyStart);
  }

  private listen(samples: Float32Array, from: number) {
    for (let f = 0; f + FRAME <= samples.length; f += FRAME) {
      let sum = 0;
      for (let i = f; i < f + FRAME; i++) sum += samples[i] * samples[i];
      const t = from + f / RATE;
      if (Math.sqrt(sum / FRAME) < QUIET) {
        if (this.spoken && this.quietFrom < 0) this.quietFrom = t;
      } else {
        if (this.quietFrom >= 0 && t - this.quietFrom >= PAUSE) this.pauses.push({ start: this.quietFrom, end: t });
        this.quietFrom = -1;
        this.spoken = true;
      }
    }
  }

  /** Whether what is playing now is a pause between sentences (or the quiet at the end of what has come). */
  atPause() {
    const t = this.heard();
    if (this.quietFrom >= 0 && t >= this.quietFrom) return true;
    return this.pauses.some((p) => p.end - p.start >= SENTENCE_PAUSE && t >= p.start && t < p.end);
  }

  /** Play in `ctx` (the microphone's, so the echo canceller hears exactly what plays), or in its own (null). */
  use(ctx: AudioContext | null) {
    if (ctx === this.shared) return;
    this.flush();
    this.shared = ctx;
    this.ctx = null;
    this.gain = null;
  }

  private sinkId = ""; // the output device the user picked ("" for the phone's own choice)

  /** Play on this output device (Chrome's `AudioContext.setSinkId`; elsewhere nothing happens, and the phone's own
   *  audio settings decide). */
  setSink(id: string) {
    this.sinkId = id;
    for (const ctx of [this.ctx, this.own, this.shared]) this.applySink(ctx);
  }

  private applySink(ctx: AudioContext | null) {
    const sinkable = ctx as (AudioContext & { setSinkId?: (id: string) => Promise<void> }) | null;
    if (sinkable?.setSinkId && sinkable.state !== "closed") sinkable.setSinkId(this.sinkId).catch(() => {});
  }

  /** Browsers only play sound after a tap: call this from one. */
  unlock() {
    if (!this.ctx) {
      if (this.shared && this.shared.state !== "closed") this.ctx = this.shared;
      else this.ctx = this.own ||= new AudioContext({ sampleRate: RATE });
      if (this.sinkId) this.applySink(this.ctx);
    }
    if (this.ctx.state !== "running") this.ctx.resume().catch(() => {});
  }

  /** The context the voice plays in (the microphone's, on the loudspeaker): for other sounds of the call. */
  get context(): AudioContext {
    this.unlock();
    return this.ctx!;
  }

  /** The browser holds the sound back until the user taps (a page opened from a notification or the lock screen
   *  has no user activation yet): what is pushed waits, and plays on the first tap. */
  get suspended() {
    return !!this.ctx && this.ctx.state === "suspended";
  }

  push(base64: string) {
    this.unlock();
    const ctx = this.ctx!;
    const bin = atob(base64);
    const samples = new Float32Array(bin.length >> 1);
    for (let i = 0; i < samples.length; i++) {
      const lo = bin.charCodeAt(2 * i);
      const hi = bin.charCodeAt(2 * i + 1);
      const v = (hi << 8) | lo;
      samples[i] = (v >= 0x8000 ? v - 0x10000 : v) / 0x8000;
    }
    const buffer = ctx.createBuffer(1, samples.length, RATE);
    buffer.copyToChannel(samples, 0);
    const source = ctx.createBufferSource();
    source.buffer = buffer;
    if (!this.gain) {
      this.gain = ctx.createGain();
      this.gain.connect(playback(ctx));
    }
    source.connect(this.gain);
    const at = Math.max(ctx.currentTime + 0.02, this.next);
    if (!this.playing) {
      this.playing = true;
      this.startedAt = at;
    }
    if (this.replyStart < 0) this.replyStart = at;
    // Sound that came late leaves a gap of silence that is in no chunk.
    if (this.spoken && this.quietFrom < 0 && this.next && at - this.next > 0.05) this.quietFrom = this.next - this.replyStart;
    this.listen(samples, at - this.replyStart);
    source.onended = () => {
      this.sources.delete(source);
      if (!this.sources.size && this.playing) {
        this.playing = false;
        this.onIdle(true);
      }
    };
    this.sources.add(source);
    source.start(at);
    this.next = at + buffer.duration;
  }

  /** A short sound beside the reply (a nod while the user talks): through the same output, so the echo canceller
   *  hears it, but outside the reply's timeline: not a reply, not `playing`. Nothing plays before the first tap. */
  aside(samples: Float32Array) {
    this.unlock();
    const ctx = this.ctx!;
    if (ctx.state !== "running") return;
    const buffer = ctx.createBuffer(1, samples.length, RATE);
    buffer.copyToChannel(samples, 0);
    const source = ctx.createBufferSource();
    source.buffer = buffer;
    source.connect(playback(ctx));
    source.start();
  }

  /** Milliseconds of the current reply heard so far. */
  heardMs() {
    if (!this.ctx || !this.playing) return 0;
    return Math.max(0, Math.round((this.ctx.currentTime - this.startedAt) * 1000));
  }

  /** Stop what plays: at once, or fading out over `fade` seconds (what comes next starts after the fade). */
  flush(fade = 0) {
    const ctx = this.ctx;
    const end = ctx && fade && this.playing ? ctx.currentTime + fade : 0;
    if (ctx && end && this.gain) {
      const level = this.gain.gain;
      level.cancelScheduledValues(ctx.currentTime);
      level.setValueAtTime(level.value, ctx.currentTime);
      level.linearRampToValueAtTime(0, end);
      this.gain = null; // the next reply plays through a fresh one
    }
    for (const source of this.sources) {
      try {
        source.stop(end || undefined);
      } catch {}
    }
    this.sources.clear();
    this.next = end;
    if (this.playing) {
      this.playing = false;
      this.onIdle(false);
    }
  }
}

export class Desk extends EventTarget {
  state: DeskState = "off";
  error = "";
  /** Muted, as on a call: the microphone is not open at all (the phone's microphone indicator is off) and nothing is
   *  sent, while the line stays open and the desk is heard. It needs no audio to stay open, and nothing is billed
   *  for a line nobody speaks on. Unmuting opens the microphone; muting gives it back. Leaving the screen closes the
   *  line; the next open brings the desk's memory back. */
  muted = true;
  output: Output = "speaker";
  /** The user's picks for the tasks a tool starts or steers. */
  picks: () => { provider: string; models: Picks | null } = () => ({ provider: "deepseek", models: null });
  /** The id of the latest event this line has in its conversation (the API's `desk` update moves past it). */
  cursor = 0;

  private ws: WebSocket | null = null;
  private wanted = false;
  private retry = 0;
  private opening = false; // the backend is being asked for a line
  private micError = ""; // why the microphone could not be opened, while it cannot
  private waiting: ArrayBuffer[] = []; // what was said while the line was down in the middle of a call
  private up = false; // the line has been up since `start`: the call was answered (before that nothing said is kept)
  private active = false; // a response is in flight
  private spoke = false; // ...and has said something (a response may be a tool call alone)
  private called = false; // ...and has called a tool
  // Why a reply is wanted, until it is asked for. Nothing else waits to be said: what the reply says is decided
  // from the state read when it is asked for.
  private wants = new Set<Why>();
  private reading = false; // the state for a reply is being read
  private stateItem = ""; // the state's item in the conversation: each reply replaces it
  private seq = 0;
  private outbox: Item[] = []; // what the user typed, to add before the next reply
  private lines: { id: string; at: number }[] = []; // the dialogue's items in the conversation, oldest first
  // A chain: the replies and tool calls that follow one thing the user said.
  private toolItems: string[] = []; // its calls and their results in the conversation, taken out when it ends
  private rounds = 0; // tool calls made in it
  private calls = 0; // tools running now
  private acked = false; // the user has heard a word of it ("Checking.")
  private holding = false; // the reply in flight follows a tool: its sound waits for its end (it may only call again)
  private held: string[] = [];
  private superseded = false; // the reply in flight was dropped unheard: the user began to speak
  private lost = new Set<Why>(); // ...and why it had been asked for
  private why = new Set<Why>(); // why the reply in flight was asked for
  // Whom a reply answers. Only the user's words may start, steer or stop an errand: a reply that only tells news
  // once passed a task's own progress back to it as the user's words (2026-11-07). A tool's result inherits it.
  private toolFor = false; // a tool's result waits, from a reply that answered the user
  private forUser = false; // the reply in flight answers the user
  private telling = false; // the reply in flight tells an errand's news: other news follows it, never cuts it
  private due: number[] = []; // the events the reply in flight has to tell
  private toMark: number[] = []; // ...and those of a reply still playing: told once it is heard to its end
  private marking: Promise<unknown> = Promise.resolve(); // ...being marked told (the next state is read after it)
  private fresh = new Map<number, DeskEvent>(); // news of the errand being talked about that is not told yet
  private userSpeaking = false;
  private barge = 0; // while the user talks over the desk: the fallback timer that cuts it off (BARGE_FALLBACK_MS)
  private bargeFrom = 0; // ...and from when their sound counts towards BARGE_IN_MS
  private loud: [number, number][] = []; // the microphone's loud blocks in the last few seconds: [when, ms]
  private lastSound = 0; // when the microphone last heard anything above SOFT
  private blip = false; // the sound over the desk stopped before that: not a turn
  private overDesk = false; // the sound being heard began while the desk was speaking
  private ignored = new Set<string>(); // those turns' items: no reply, not logged
  private newsWait = 0; // the timer that stops the desk at its next pause for news
  private saying = "";
  private sayingItem = "";
  private about = ""; // the errand being talked about (the user opened it, or the desk last routed to it)
  private opened = false; // ...because the user opened its card,
  private openedAt = 0; // then
  private talkedAt = 0; // when the user last spoke or typed
  private quietSince = Date.now(); // when the desk last finished speaking
  private mic = new Microphone((pcm) => this.send(pcm));
  private player = new Player((natural) => this.idle(natural));
  // Captions: what the screen shows of the reply follows the audio (captions.ts), not the text's arrival.
  private captions = new Captioner();
  private textDone = false;
  private audioDone = false;
  private cut = false; // the user stopped this reply: the screen keeps what they heard
  private plainTurns = false; // the model refused semantic turn detection: silence ends a turn
  private turnStart = 0; // when the user's current turn began; 0 between turns
  private lastLoud = 0; // when the microphone last heard sound
  private nodDue = false; // the current pause has not had its nod
  private nodded = 0; // when the desk last nodded
  private nodWait = 0; // the timer for the nod that follows a turn the reply is slow to answer
  private nods: Float32Array[] = []; // the "mm-hm" clips, once fetched
  private nodsAsked = false;
  private pendingTranscripts = new Set<string>(); // the user's turns whose words have not come yet
  private logging: Promise<unknown>[] = []; // the user's words on their way to the log
  private logged = false; // the reply's words are in the desk's memory (runtime/desk.py)
  private cutBy: "" | "user" | "news" = ""; // who stopped the reply, once one did
  private cutHeard = ""; // ...and what the user had heard of it by then
  private cutHow: "tap" | "voice" = "tap"; // ...and, when it was the user, how (kept in the log, to tell a false cut)
  // The user stopped the desk's last reply, having heard this much of it: said to the desk with the next reply it
  // makes for them (`reason`), so it answers their words and does not start the same reply again. Null when not.
  private early: { at: number; key: string; got: Promise<Now | null> } | null = null; // the state, read ahead
  // When, for the reply being made: the user's turn ended, the reply was asked for, its first sound played; how long
  // the state took, and whether it had been read ahead. Kept in the log with the reply's words, to see where a slow
  // reply spent its time.
  private timing = { turn: 0, asked: 0, sound: 0, state: 0, ahead: false };
  private echoes = new Set<string>(); // the user's turns that began over the desk's voice: its own echo, maybe
  private lastSaid = ""; // what the desk last said aloud
  private stopped: string | null = null;
  private noted = false; // the reply in flight was told so
  private shown = "";
  private frame = 0;

  private show(text: string) {
    if (text === this.shown) return;
    this.shown = text;
    this.emit("saying", text);
  }

  private tick = () => {
    this.frame = 0;
    if (!this.player.playing) return;
    this.show(this.captions.at(this.player.heard(), this.player.pauses));
    this.frame = requestAnimationFrame(this.tick);
  };

  private idle(natural: boolean) {
    cancelAnimationFrame(this.frame);
    this.frame = 0;
    // Played to its end: the whole reply. Cut off: what was heard stays, as it is in the desk's memory.
    if (!natural) this.cut = true;
    else if (this.textDone && this.audioDone && !this.cut) this.show(this.saying.trim());
    this.quietSince = Date.now();
    this.settle(natural, this.cutBy !== "news");
    this.dispatchEvent(new Event("spoken"));
    this.drain();
  }

  /** The reply that had news to tell is over. Heard to its end: its events are told (their card goes). Stopped
   *  before that: they are not, and come back as cards (`cards`; not when news stopped it: the next reply tells
   *  them). */
  private settle(whole: boolean, cards = true) {
    const ids = this.toMark.splice(0);
    if (!ids.length) return;
    const events = ids.map((id) => this.fresh.get(id)).filter((e): e is DeskEvent => !!e);
    if (!whole) {
      if (cards && events.length) this.emit("news", events);
      return;
    }
    for (const id of ids) this.fresh.delete(id);
    this.early = null;
    this.marking = api("/voice/desk/announced", "POST", { ids }).catch(() => {});
    if (this.about) this.emit("told", this.about);
  }

  private set(state: DeskState, error = "") {
    this.state = state;
    this.error = error;
    this.dispatchEvent(new CustomEvent("state", { detail: state }));
  }

  private emit<T>(name: string, detail?: T) {
    this.dispatchEvent(detail === undefined ? new Event(name) : new CustomEvent(name, { detail }));
  }

  /** Open the line and the microphone (asking the first time) together: neither waits for the other. This is the
   *  call being placed (useDesk.ts `dial`): it is answered once the line is up and the microphone open (`hears`),
   *  and nothing said before that is sent. What is said while the line is down later, in the middle of the call,
   *  is kept (`waiting`). */
  async start() {
    this.wanted = true;
    const line = this.connect();
    // The context the microphone reads in (and the desk's voice plays in) exists from the start, muted or not.
    this.mic.prepare(this.output === "earpiece");
    this.player.use(this.mic.context);
    // Opens muted: the microphone is asked for when the user first unmutes.
    if (this.muted) {
      this.micError = "";
      if (this.state === "error" && this.connected) this.set("ready");
    } else await this.openMic();
    await line;
  }

  /** Open the microphone (the user has unmuted). If the browser will not give it, they are muted again. */
  private async openMic() {
    try {
      await this.mic.open(this.output === "earpiece");
      // Muted again, or the line closed, while the browser was opening it.
      if (this.muted || !this.wanted) {
        this.mic.close();
        return;
      }
      this.micError = "";
      if (this.state === "error" && this.connected) this.set("ready");
    } catch (e) {
      // Without a microphone the desk still answers what is typed (the keypad), so the line stays wanted.
      this.micError = /denied|permission/i.test(String(e))
        ? "penrose needs the microphone. Allow it in the browser's site settings."
        : "The microphone could not be opened.";
      this.muted = true;
      this.emit("muted", true);
      // While the line is still opening, it reports this when it is up (onopen).
      if (this.state !== "connecting") this.set("error", this.micError);
    }
  }

  /** Where the call's other sounds play (the ring: useDesk.ts): the context the desk's voice plays in, which on
   *  the loudspeaker is the microphone's own, so the echo canceller gets the very samples played, at once. A ring
   *  played in a context of its own reached the canceller late, through a MediaStream; the canceller settled on
   *  that delay, and the greeting that followed came back through the microphone as the user's words (2026-11-24). */
  get audio(): AudioContext {
    this.mic.prepare(this.output === "earpiece");
    this.player.use(this.mic.context);
    return this.player.context;
  }

  /** The microphone is open and what it hears is sent. */
  get hears() {
    return !this.muted && this.mic.on;
  }

  /** Why the microphone could not be opened, while it cannot. */
  get micProblem() {
    return this.micError;
  }

  /** Speaking, but the browser holds the sound until a tap (Player.suspended). */
  get silent() {
    return this.player.playing && this.player.suspended;
  }

  unlock() {
    this.player.unlock();
  }

  /** Play on this output device ("" for the phone's own choice): see outputs.ts. */
  setSink(id: string) {
    this.player.setSink(id);
  }

  get connected() {
    return !!this.ws && this.ws.readyState === WebSocket.OPEN;
  }

  get speaking() {
    return this.player.playing;
  }

  private send(pcm: ArrayBuffer) {
    if (this.muted) return;
    this.listen(pcm);
    if (!this.connected) {
      if (this.wanted && this.up && this.waiting.push(pcm) > WAITING) this.waiting.shift();
      return;
    }
    this.ws!.send(JSON.stringify({ type: "input_audio_buffer.append", audio: b64(pcm) }));
  }

  private post(message: Item) {
    if (!this.connected) return false;
    this.ws!.send(JSON.stringify(message));
    return true;
  }

  async connect() {
    if (!this.wanted || this.ws || this.opening) return;
    this.opening = true;
    this.set("connecting");
    let opened: Opened;
    try {
      opened = await openSession();
    } catch (e) {
      this.opening = false;
      this.set("error", (e as Error).message);
      this.later();
      return;
    }
    this.opening = false;
    if (!this.wanted || this.ws) return;
    const ws = new WebSocket(opened.url, ["realtime", `openai-insecure-api-key.${opened.secret}`]);
    this.ws = ws;
    ws.onopen = () => {
      this.retry = 0;
      this.up = true;
      this.cursor = opened.latest_event;
      this.setTurns();
      if (!this.nodsAsked) void this.fetchNods();
      // A new session knows nothing: the last lines said come back now, and the state before each reply.
      this.stateItem = "";
      this.toolItems = [];
      this.lines = [];
      this.endChain();
      for (const item of opened.items) this.post({ type: "conversation.item.create", item: { ...item, id: this.line() } });
      for (const pcm of this.waiting.splice(0)) this.send(pcm); // after the memory, so the words land in context
      if (this.micError) this.set("error", this.micError);
      else this.set("ready");
      // Opening is silent: what the user has not heard waits as cards until they open one (`answer`).
      if (opened.unheard?.length) this.emit("news", opened.unheard);
      this.drain();
    };
    ws.onmessage = (e) => this.receive(String(e.data));
    ws.onclose = () => {
      if (this.ws !== ws) return;
      this.ws = null;
      this.active = false;
      this.holding = false;
      this.superseded = false;
      this.held = [];
      this.waiting = [];
      this.player.flush();
      if (this.wanted) {
        this.set("connecting");
        this.later();
      }
    };
    ws.onerror = () => {};
  }

  private setTurns() {
    const turn_detection = this.plainTurns ? PLAIN_TURNS : TURNS;
    this.post({ type: "session.update", event_id: TURNS_EVENT, session: { type: "realtime", audio: { input: { turn_detection } } } });
  }

  private async fetchNods() {
    this.nodsAsked = true;
    try {
      const got = await api<Nods>("/voice/desk/nods");
      this.nods = got.clips.map(decode);
    } catch {
      this.nodsAsked = false; // the next connect asks again
    }
  }

  /** The microphone's level (speech.ts hands Int16 blocks): talking over the desk (BARGE_IN_MS) and the nods
   *  (NOD_AFTER_MS). */
  private listen(pcm: ArrayBuffer) {
    const now = Date.now();
    const samples = new Int16Array(pcm);
    if (!samples.length) return;
    let sum = 0;
    for (let i = 0; i < samples.length; i++) sum += samples[i] * samples[i];
    while (this.loud.length && now - this.loud[0][0] > 5000) this.loud.shift();
    const rms = Math.sqrt(sum / samples.length) / 32768;
    if (rms >= SOFT) this.lastSound = now;
    if (rms >= QUIET) {
      this.lastLoud = now;
      this.nodDue = this.turnStart > 0;
      this.loud.push([now, (samples.length / RATE) * 1000]);
      if (this.barge && this.heardSince(this.bargeFrom) >= BARGE_IN_MS) this.bargeIn();
      return;
    }
    // A pause in the user's turn: it may be its end. The state is read now, so the reply does not wait for it.
    if (this.turnStart && now - this.lastLoud > STATE_PAUSE_MS) this.readAhead();
    if (!this.nodDue || !this.turnStart || this.barge || this.player.playing || !this.nods.length) return;
    if (now - this.lastLoud < NOD_AFTER_MS || now - this.nodded < NOD_GAP_MS) return;
    if (this.heardSince(this.turnStart - BARGE_LOOKBACK_MS) < NOD_SPEECH_MS) return;
    this.nodDue = false;
    this.nod();
  }

  private nod() {
    this.nodded = Date.now();
    this.player.aside(this.nods[Math.floor(Math.random() * this.nods.length)]);
  }

  /** The user's turn ended a moment ago and nothing of the reply has been heard: a nod, unless the pause that
   *  ended the turn already had one. */
  private nodForReply(turn: number) {
    this.nodWait = 0;
    if (!this.connected || !this.nods.length || this.timing.turn !== turn) return;
    if (this.player.playing || this.userSpeaking || this.timing.sound >= turn) return;
    if (Date.now() - this.nodded < NOD_RECENT_MS) return;
    this.nod();
  }

  /** Whether what was transcribed as the user's words is what the desk itself was just saying (two words or more
   *  of it, in a row). */
  private isEcho(heard: string) {
    const norm = (s: string) => s.toLowerCase().replace(/[^\p{L}\p{N} ]/gu, "").replace(/\s+/g, " ").trim();
    const words = norm(heard);
    if (words.split(" ").length < 2) return false;
    return norm(`${this.lastSaid} ${this.saying}`).includes(words);
  }

  /** Milliseconds of loud sound the microphone sent since `since`. */
  private heardSince(since: number) {
    let ms = 0;
    for (const [at, length] of this.loud) if (at >= since) ms += length;
    return ms;
  }

  /** The user has talked over the desk for long enough: it stops where they heard it end. */
  private bargeIn() {
    window.clearTimeout(this.barge);
    this.barge = 0;
    this.interrupt("user", "voice");
    this.emit("speech");
  }

  /** The detector still has the user talking over the desk, long after: they are, if the microphone still hears
   *  them (a soft voice that never reached QUIET); otherwise look again shortly, until speech_stopped lets it go. */
  private bargeFallback() {
    if (Date.now() - this.lastSound < SOFT_RECENT_MS) return this.bargeIn();
    this.barge = window.setTimeout(() => this.bargeFallback(), 200);
  }

  /** The user's words of this turn into the log before a session is sent them: a long turn transcribes after the
   *  desk has answered it. At most TRANSCRIPT_WAIT_MS; a transcript that never comes is forgotten. */
  private async transcribed() {
    const until = Date.now() + TRANSCRIPT_WAIT_MS;
    while (this.pendingTranscripts.size && Date.now() < until) await new Promise((r) => setTimeout(r, 50));
    this.pendingTranscripts.clear();
    const left = Math.max(0, until - Date.now());
    await Promise.race([Promise.all(this.logging), new Promise((r) => setTimeout(r, left))]);
  }

  private later() {
    if (!this.wanted) return;
    const wait = Math.min(RECONNECT_MS * 2 ** this.retry++, 15000);
    window.setTimeout(() => this.connect(), wait);
  }

  /** Take an item out of the conversation. One that is already gone (a reply cancelled before it had an item) is
   *  refused by the API: that refusal is known by its `event_id` and let go (`receive`). */
  private remove(id: string) {
    if (id) this.post({ type: "conversation.item.delete", event_id: `${TIDY}${++this.seq}`, item_id: id });
  }

  /** A new item of the dialogue: its id, kept so old lines can be taken out again. */
  private line(id = `line_${++this.seq}`) {
    this.lines.push({ id, at: Date.now() });
    return id;
  }

  /** The conversation keeps only the last lines said: what came before is in the state. */
  private prune() {
    const now = Date.now();
    while (this.lines.length > LINES || (this.lines.length && now - this.lines[0].at > LINES_MS)) {
      this.remove(this.lines.shift()!.id);
    }
  }

  /** A chain is over: its tool calls and their results leave the conversation (the state shows what they did). */
  private endChain() {
    for (const id of this.toolItems.splice(0)) this.remove(id);
    this.rounds = 0;
    this.acked = false;
  }

  /** What called for the reply, as the desk reads it under the state. */
  private reason(why: Why) {
    const session = `s:${this.about.slice(0, 6)}`;
    if (why === "user") {
      const spoke = "the user spoke: their words are the last thing in the conversation.";
      if (this.stopped === null) return spoke;
      const heard = this.stopped ? `after hearing only "${this.stopped}"` : "before hearing any of it";
      return `${spoke} They stopped your last reply ${heard}. Answer what they said now, shortly; do not begin that reply again, and do not say again what they heard.`;
    }
    if (why === "opened") return `the user opened ${session} on their screen.`;
    if (why === "news") return `${session} has news the user has not been told, and they have said nothing since.`;
    if (why === "answered") return "the user called you and you have just picked up: greet them in two or three words, and nothing else.";
    if (why === "progress") return `${session} is still working and the user has heard nothing for a while: say where it is, in a few words.`;
    return "a tool you called has finished. If it reports no problem and you already said what you are doing, say nothing.";
  }

  private stateKey() {
    return `${this.about}|${this.opened}|${this.openedAt}`;
  }

  /** Read the state ahead of the reply it is for (`STATE_FRESH_MS`). Anything that changes it lets go of what was
   *  read: news from a session, a tool the desk called, an event marked told. */
  private readAhead() {
    if (!this.connected) return;
    const key = this.stateKey();
    if (this.early && this.early.key === key && Date.now() - this.early.at < STATE_AGAIN_MS) return;
    const body = { about: this.about, opened: this.opened, at: this.openedAt / 1000 };
    const got = this.marking.then(() => api<Now>("/voice/desk/state", "POST", body)).catch(() => null);
    this.early = { at: Date.now(), key, got };
  }

  /** Ask for a reply, from penrose as it is now: read the state, put it last in the conversation in place of the
   *  one before, and ask for the response. If the user starts to speak, or a reply begins, while the state is being
   *  read, nothing is asked: what was wanted stays wanted for whatever ends that. */
  private async reply() {
    const wants = new Set(this.wants);
    this.wants.clear();
    this.reading = true;
    let now: Now | null = null;
    const began = Date.now();
    // For the user's words alone, the state read while they were speaking, if it is recent and nothing has changed
    // it since (`readAhead`).
    const early = this.early;
    this.early = null;
    const ahead = !!early && wants.size === 1 && wants.has("user") && early.key === this.stateKey() && began - early.at < STATE_FRESH_MS;
    try {
      await this.marking; // what the last reply told is told, before the state says what is not
      if (ahead) now = await early!.got;
      now ||= await api<Now>("/voice/desk/state", "POST", { about: this.about, opened: this.opened, at: this.openedAt / 1000 });
    } catch (e) {
      this.emit("error", (e as Error).message || "The desk could not read where things are.");
    }
    this.reading = false;
    if (!now || !this.connected || this.active || this.userSpeaking) {
      for (const why of wants) this.wants.add(why);
      return;
    }
    if (!this.about) this.about = now.about;
    this.due = now.about === this.about ? now.due : [];
    // News that is told already (the reply before this one had it in its state), or that the user dismissed.
    if (!this.due.length) wants.delete("news");
    // The greeting on picking up is the last of these: said only when nothing else calls for a reply (the user
    // already spoke, or placed the call by opening a card, whose news is then the first thing said).
    const why = (["user", "opened", "news", "tool", "progress", "answered"] as Why[]).find((w) => wants.has(w));
    if (!why) return;
    if (why === "answered") this.due = []; // a greeting tells no news
    for (const item of this.outbox.splice(0)) this.post({ type: "conversation.item.create", item: { ...item, id: this.line() } });
    this.prune();
    this.remove(this.stateItem);
    this.stateItem = `state_${++this.seq}`;
    const text = `${now.text}\n\nWhy you speak now: ${this.reason(why)}`;
    this.post({
      type: "conversation.item.create",
      item: { id: this.stateItem, type: "message", role: "system", content: [{ type: "input_text", text }] },
    });
    this.why = wants;
    this.noted = why === "user" && this.stopped !== null;
    this.forUser = wants.has("user") || (wants.has("tool") && this.toolFor);
    this.toolFor = false;
    this.telling = wants.has("news") || wants.has("opened");
    if (this.telling) {
      // The screen shows the pictures of the news told now, and the form a question wants filled in privately, as
      // the desk starts telling it: never anything the user must go looking for.
      const news = this.due.map((id) => this.fresh.get(id)).filter((e): e is DeskEvent => !!e);
      const shown = news.filter((e) => e.pictures?.length).pop()?.pictures;
      const asking = news.filter((e) => e.ask).pop();
      if (shown) this.emit("show", shown);
      if (asking?.ask) this.emit("ask", { chatId: asking.chat_id, ask: asking.ask } as Asking);
    }
    // After a tool, once the user has heard a word of this chain, the reply's sound waits for its end: if it only
    // calls another tool, its words are never heard. Until they have heard one (the desk called its tool without a
    // word), it plays as it comes: holding it back was a second of silence on top of the tool's.
    this.holding = this.rounds > 0 && this.acked;
    this.timing = { ...this.timing, asked: Date.now(), sound: 0, state: Date.now() - began, ahead };
    this.held = [];
    this.active = true;
    // A chain that keeps calling tools gets one last word, without them.
    this.post(this.rounds >= ROUNDS ? { type: "response.create", response: { tool_choice: "none" } } : { type: "response.create" });
  }

  /** The sound of a reply that was held back (it followed a tool, and did not call another): heard now. */
  private release(audio: string[]) {
    const text = this.saying.trim();
    this.captions.setText(this.saying, true);
    for (const delta of audio) {
      const was = this.player.playing;
      this.player.push(delta);
      this.timing.sound ||= Date.now();
      if (!was) {
        this.emit("speaking");
        if (!this.frame) this.frame = requestAnimationFrame(this.tick);
      }
    }
    if (text) {
      this.logSaid(text);
      this.emit("said", text);
    }
  }

  private receive(raw: string) {
    let event: Realtime;
    try {
      event = JSON.parse(raw);
    } catch {
      return;
    }
    switch (event.type) {
      case "input_audio_buffer.speech_started":
        this.userSpeaking = true;
        this.blip = false;
        if (!this.turnStart) this.turnStart = Date.now();
        window.clearTimeout(this.barge);
        this.readAhead();
        this.overDesk = this.player.playing;
        if (this.player.playing) {
          // Over the desk's voice: it stops (where the user heard it end) only if they keep talking.
          this.bargeFrom = Date.now() - BARGE_LOOKBACK_MS;
          this.barge = window.setTimeout(() => this.bargeFallback(), BARGE_IN_MS + BARGE_FALLBACK_MS);
          if (this.heardSince(this.bargeFrom) >= BARGE_IN_MS) this.bargeIn();
        } else {
          if (this.active && !this.superseded) {
            // A reply is on its way and not a sound of it has been heard: what the user says now may change it. It
            // is dropped, and asked for again, from the state as it is then, once they have spoken.
            this.superseded = true;
            this.lost = new Set(this.why);
            this.post({ type: "response.cancel" });
          }
          this.emit("speech");
        }
        break;
      case "input_audio_buffer.speech_stopped":
        this.userSpeaking = false;
        if (this.barge) {
          window.clearTimeout(this.barge);
          this.barge = 0;
          this.blip = true;
        } else this.emit("silence");
        break;
      case "input_audio_buffer.committed":
        if (this.blip) {
          // Let go: out of the conversation, so the desk never answers it later either.
          this.blip = false;
          if (event.item_id) {
            this.ignored.add(event.item_id);
            this.post({ type: "conversation.item.delete", item_id: event.item_id });
          }
        } else {
          // The user's words begin a new chain: what follows them is heard at once.
          this.wants.add("user");
          this.rounds = 0;
          this.acked = false;
          this.timing.turn = Date.now();
          const turn = this.timing.turn;
          window.clearTimeout(this.nodWait);
          this.nodWait = window.setTimeout(() => this.nodForReply(turn), NOD_REPLY_MS);
          if (event.item_id) {
            this.pendingTranscripts.add(event.item_id);
            this.line(event.item_id);
            if (this.overDesk) this.echoes.add(event.item_id);
          }
        }
        // A reply dropped unheard when this sound began is asked for again, whatever the sound turned out to be.
        for (const why of this.lost) this.wants.add(why);
        this.lost.clear();
        this.turnStart = 0;
        this.nodDue = false;
        this.drain();
        break;
      case "conversation.item.input_audio_transcription.completed": {
        const text = (event.transcript || "").trim();
        if (event.item_id) this.pendingTranscripts.delete(event.item_id);
        if (event.item_id && this.ignored.delete(event.item_id)) break;
        if (event.item_id && this.echoes.delete(event.item_id) && this.isEcho(text)) {
          // A turn that began over the desk's voice and is the desk's own words: the microphone heard the
          // loudspeaker. It is nobody's words: out of the conversation, not logged, and not a reason for what the
          // desk says next. A reply to it that nobody has heard yet is dropped.
          this.remove(event.item_id);
          this.stopped = null;
          if (this.active && !this.superseded && !this.player.playing && this.why.size === 1 && this.why.has("user")) {
            this.superseded = true;
            this.post({ type: "response.cancel" });
          }
          break;
        }
        if (text) {
          this.talkedAt = Date.now();
          const logging = api("/voice/desk/log", "POST", { role: "user", text }).catch(() => {});
          this.logging.push(logging);
          void logging.finally(() => this.logging.splice(this.logging.indexOf(logging), 1));
          this.emit("heard", text);
        }
        break;
      }
      case "conversation.item.input_audio_transcription.failed":
        if (event.item_id) this.pendingTranscripts.delete(event.item_id);
        break;
      case "response.created":
        this.active = true;
        this.spoke = false;
        this.called = false;
        this.saying = "";
        this.sayingItem = "";
        this.textDone = false;
        this.audioDone = false;
        this.cut = false;
        this.logged = false;
        this.cutBy = "";
        this.cutHeard = "";
        this.captions.reset();
        this.player.beginReply();
        break;
      case "response.output_item.added":
        if (event.item?.type === "message" && event.item.id) this.sayingItem = event.item.id;
        break;
      case "response.output_audio.delta":
        // A reply that was stopped stays stopped: the sound still on its way when the cancel was sent (the model
        // writes ahead of what is heard) is not played. It used to be, so the desk went on talking, a few words
        // further on, over the user who had just stopped it.
        if (event.delta && !this.superseded && !this.cutBy) {
          this.spoke = true;
          if (this.holding) {
            this.held.push(event.delta);
            break;
          }
          const was = this.player.playing;
          this.player.push(event.delta);
          this.timing.sound ||= Date.now();
          if (!was) {
            this.emit("speaking");
            if (!this.frame) this.frame = requestAnimationFrame(this.tick);
          }
        }
        break;
      case "response.output_audio.done":
        this.audioDone = true;
        break;
      case "response.output_audio_transcript.delta":
        this.spoke = true;
        this.saying += event.delta || "";
        if (!this.holding && !this.superseded) this.captions.setText(this.saying);
        break;
      case "response.output_audio_transcript.done": {
        const text = (event.transcript || this.saying).trim();
        this.saying = event.transcript || this.saying;
        this.textDone = true;
        if (this.holding || this.superseded) break; // not heard yet, or never
        this.captions.setText(this.saying, true);
        // The audio may already have ended (it seldom does: the text runs ahead of it).
        if (!this.player.playing && this.audioDone && this.spoke && !this.cut) this.show(text);
        if (text) {
          this.logSaid(text);
          this.emit("said", text);
        }
        break;
      }
      case "response.output_item.done":
        if (event.item?.type === "function_call") {
          this.called = true;
          void this.call(event.item, this.forUser);
        }
        break;
      case "response.done": {
        const held = this.held.splice(0);
        // Whether the user hears (or heard) anything of this reply.
        let voiced = this.spoke && !this.superseded;
        if (this.superseded || (this.holding && this.called)) {
          // Dropped before a sound of it was heard (the user began to speak), or words said on the way to another
          // tool call: never said, so out of the conversation too.
          voiced = false;
          this.remove(this.sayingItem);
          this.saying = "";
        } else if (this.holding) {
          if (held.length) this.release(held);
        } else {
          // A cancelled reply (cut off) may end without its transcript's `done`: its words so far are logged here.
          this.logSaid(this.saying.trim());
        }
        const dropped = this.superseded;
        // The reply that was told the user had stopped the one before it has been made: that is said once.
        if (this.noted && !dropped) this.stopped = null;
        this.noted = false;
        this.superseded = false;
        this.holding = false;
        this.active = false;
        if (voiced) {
          this.lastSaid = this.saying;
          if (this.sayingItem) this.line(this.sayingItem);
          if (this.called) this.acked = true;
        }
        // What it had to tell is told once it has been heard to its end (`idle`), not when its text is done.
        const due = this.due.splice(0);
        if (due.length && voiced && !this.called) {
          this.toMark = due;
          if (this.cutBy) this.settle(false, this.cutBy !== "news");
          else if (!this.player.playing) this.settle(true);
        }
        if (!this.calls && !this.wants.has("tool")) this.endChain();
        // Nothing to say (a fragment, a "hold on"): the screen goes back to listening instead of waiting on a reply.
        if (!voiced && !this.called && !dropped) this.emit("quiet");
        this.drain();
        break;
      }
      case "error":
        if (event.error?.event_id === TURNS_EVENT || event.event_id === TURNS_EVENT) {
          // The model does not take semantic turn detection: silence ends a turn, on this and every later line.
          if (!this.plainTurns) {
            this.plainTurns = true;
            this.setTurns();
          }
          break;
        }
        if (event.error?.code && QUIET_ERRORS.has(event.error.code)) break;
        if ((event.error?.event_id || "").startsWith(TIDY)) break; // an item that was already gone
        this.emit("error", event.error?.message || "The desk's line failed.");
        break;
    }
  }

  /** A tool the desk called: Relay runs it, and the desk hears the result at once. It then speaks again only if
   *  something is left to say: a routing tool's result only confirms where the words went (the desk said that
   *  before calling, when it did), but a reply it read or a search it made is what the user asked for, and a
   *  problem must be told. */
  private async call(item: NonNullable<Realtime["item"]>, forUser: boolean) {
    this.emit("tool", item.name || "");
    this.calls++;
    // Whether the user has heard what the desk is doing: in this reply (unless its sound is held), or earlier in
    // the chain.
    const spoke = this.acked || (this.spoke && !this.holding);
    const { provider, models } = this.picks();
    if (WORDS.has(item.name || "")) await this.transcribed();
    let output = "";
    let failed = false;
    try {
      const done = await api<ToolResult>("/voice/desk/tool", "POST", {
        call_id: item.call_id,
        name: item.name,
        arguments: item.arguments || "{}",
        provider,
        models,
        for_user: forUser,
      });
      output = done.output;
      failed = !!done.result.error;
      if (done.focus) {
        if (done.focus.chat_id !== this.about) this.fresh.clear();
        this.about = done.focus.chat_id;
        this.opened = false;
        this.emit("focus", { chatId: done.focus.chat_id, title: done.focus.title } as Focus);
      }
      if (done.result.pictures?.length) this.emit("show", done.result.pictures);
    } catch (e) {
      output = JSON.stringify({ error: (e as Error).message });
      failed = true;
    }
    this.early = null;
    const result = `result_${++this.seq}`;
    this.post({ type: "conversation.item.create", item: { id: result, type: "function_call_output", call_id: item.call_id, output } });
    if (item.id) this.toolItems.push(item.id);
    this.toolItems.push(result);
    this.rounds++;
    this.calls--;
    this.emit("tooled");
    if (failed || !spoke || !ROUTING.has(item.name || "")) {
      this.toolFor = this.toolFor || forUser;
      this.wants.add("tool");
      this.drain();
    } else if (!this.active && !this.calls && !this.wants.has("tool")) this.endChain();
  }

  /** News from the sessions. It is never words to play: it says the state moved. About the errand being talked
   *  about, a question, result or failure calls for a reply (and cuts in on the desk's own acknowledgement at its
   *  next pause, never on the telling of other news); where a working errand is calls for one only when nobody has
   *  said anything for a while. About any other errand it is a card on the screen, and the desk says nothing. */
  inject(events: DeskEvent[]) {
    let news = false;
    let progress = false;
    if (events.length) this.early = null;
    const others: DeskEvent[] = [];
    for (const event of events) {
      if (event.id <= this.cursor) continue;
      this.cursor = event.id;
      if (event.chat_id !== this.about) {
        if (event.kind !== "progress") others.push(event);
      } else if (event.kind === "progress") progress = true;
      else {
        news = true;
        this.fresh.set(event.id, event);
      }
    }
    if (others.length) this.emit("news", others);
    if (news) {
      this.wants.add("news");
      // Cut in only on a reply being said, and only on the desk's own word ("Got it, let me see…"): a response that
      // has not spoken yet may be calling a tool, which must complete. The cancelled response ends with
      // `response.done`, which asks for the next one.
      if (this.active && !this.telling && this.stopAtPause()) return;
    } else if (progress) {
      const quiet = !this.active && !this.reading && !this.player.playing && !this.userSpeaking;
      if (quiet && Date.now() - Math.max(this.quietSince, this.talkedAt) > PROGRESS_QUIET_MS) this.wants.add("progress");
    }
    this.drain();
  }

  /** The call the user placed has been answered (useDesk.ts `answered`): the desk says hello, as a person picking
   *  up does, so the user knows it is there and listening. Nothing if they are already speaking. */
  greet() {
    // Not after something else: a reply already wanted, being made or being said is what the user hears first.
    if (this.wants.size || this.active || this.reading || this.userSpeaking || this.player.playing) return;
    this.wants.add("answered");
    this.drain();
  }

  /** The user opened a session's card (or answered its notification): that session is now the one being talked
   *  about, and the desk tells what it has. */
  answer(focus: Focus & { session: string }, events: DeskEvent[]) {
    if (focus.chatId !== this.about) this.fresh.clear();
    this.about = focus.chatId;
    this.opened = true;
    this.openedAt = Date.now();
    this.talkedAt = Date.now();
    for (const event of events) if (event.kind !== "progress") this.fresh.set(event.id, event);
    this.wants.add("opened");
    this.drain();
  }

  /** Stop the reply being said at its next pause (or after NEWS_WAIT_MS): what was heard stays in the desk's memory,
   *  and the next reply, made from the state with the news in it, follows on from it. False when nothing is being
   *  said. */
  private stopAtPause(): boolean {
    if (!this.player.playing) return false;
    const item = this.sayingItem;
    const until = Date.now() + NEWS_WAIT_MS;
    window.clearInterval(this.newsWait);
    this.newsWait = window.setInterval(() => {
      // It ended, or the next reply began (the news is told in it): nothing to stop.
      const over = !this.player.playing || this.sayingItem !== item;
      if (!over && !this.player.atPause() && Date.now() < until) return;
      window.clearInterval(this.newsWait);
      this.newsWait = 0;
      if (!over) this.interrupt("news");
    }, 50);
    return true;
  }

  /** Typed instead of spoken (nothing transcribes it: logged here). */
  type(text: string) {
    this.talkedAt = Date.now();
    this.timing.turn = Date.now();
    void api("/voice/desk/log", "POST", { role: "user", text }).catch(() => {});
    this.outbox.push({ type: "message", role: "user", content: [{ type: "input_text", text }] });
    this.wants.add("user");
    this.rounds = 0;
    this.acked = false;
    if (!this.ws) void this.connect();
    this.drain();
  }

  /** Ask for the reply that is wanted, when one can be: the API allows one response at a time, and nothing is
   *  asked while the user is speaking (their turn's end asks). */
  private drain() {
    if (!this.connected || this.active || this.userSpeaking || this.reading || !this.wants.size) return;
    // News waits for the desk to finish what it is saying (`idle` asks again): what that told is then known, and
    // nothing is told twice. The user's words, a card they opened and a tool's result do not wait.
    const news = [...this.wants].every((why) => why === "news" || why === "progress");
    if (news && this.player.playing) return;
    void this.reply();
  }

  /** The reply's words into the desk's memory (runtime/desk.py), once; with what the user heard of them if the
   *  reply was cut off by then (a cut after this goes by `/cut`). */
  private logSaid(text: string) {
    if (this.logged || !text) return;
    this.logged = true;
    const cut = this.cutBy ? { heard: this.cutHeard, by: this.cutBy, ...(this.cutBy === "user" ? { how: this.cutHow } : {}) } : {};
    // Where the time went, for a reply to the user: from the end of their turn to its first sound (`total`), of
    // which reading the state (`state`; `ahead` when it had been read while they spoke) and the model (`model`).
    const { turn, asked, sound, state, ahead } = this.timing;
    const timing = this.forUser && turn && sound ? { timing: { total: sound - turn, state, model: sound - asked, ahead, round: this.rounds } } : {};
    void api("/voice/desk/log", "POST", { role: "assistant", text, ...cut, ...timing }).catch(() => {});
  }

  /** A tap or speech while the desk speaks (`by` the user), or news that may not wait: it stops, and listens.
   *  The reply is remembered as heard, both by the desk (the API's truncation) and in penrose's memory, where the
   *  session the user's next words go to reads it too ("add that" means what they heard). True when it was
   *  speaking. */
  interrupt(by: "user" | "news" = "user", how: "tap" | "voice" = "tap"): boolean {
    if (!this.player.playing) return false;
    // What they heard of it: the caption at this moment (captions.ts paces the words to the audio).
    this.cutHeard = this.captions.at(this.player.heard(), this.player.pauses).trim();
    this.cutBy = by;
    this.cutHow = how;
    if (by === "user") this.stopped = this.cutHeard;
    const which = by === "user" ? { how } : {};
    if (this.logged) void api("/voice/desk/cut", "POST", { heard: this.cutHeard, by, ...which }).catch(() => {});
    if (this.sayingItem) {
      this.post({ type: "conversation.item.truncate", item_id: this.sayingItem, content_index: 0, audio_end_ms: this.player.heardMs() });
    }
    this.post({ type: "response.cancel" });
    this.player.flush(FADE);
    return true;
  }

  /** Muting gives the microphone back; unmuting opens it (see `muted`). Resolves once it is open or refused (then
   *  muted again, and a "muted" event says so). */
  async setMuted(muted: boolean) {
    this.muted = muted;
    if (muted) {
      this.waiting = [];
      this.endTurn();
      this.mic.close();
      return;
    }
    if (!this.wanted) return; // `start` opens it
    if (!this.ws) void this.connect();
    await this.openMic();
  }

  /** Muted in the middle of a sentence: voice detection only ends a turn when it hears quiet, and none will come, so
   *  send a second of it. */
  private endTurn() {
    if (!this.userSpeaking || !this.connected) return;
    const quiet = silence(100);
    for (let i = 0; i < 10; i++) this.ws!.send(JSON.stringify({ type: "input_audio_buffer.append", audio: quiet }));
  }

  /** The line and the microphone, closed. */
  stop() {
    this.wanted = false;
    this.up = false;
    this.stopped = null;
    this.early = null;
    this.echoes.clear();
    this.lastSaid = "";
    window.clearTimeout(this.nodWait);
    this.nodWait = 0;
    this.ws?.close();
    this.ws = null;
    this.active = false;
    this.wants.clear();
    this.lost.clear();
    this.outbox = [];
    this.held = [];
    this.holding = false;
    this.superseded = false;
    this.due = [];
    this.toMark = [];
    this.waiting = [];
    this.player.flush();
    window.clearTimeout(this.barge);
    this.barge = 0;
    window.clearInterval(this.newsWait);
    this.newsWait = 0;
    this.turnStart = 0;
    this.pendingTranscripts.clear();
    this.player.use(null); // before its context closes with the microphone
    this.mic.dispose();
    this.set("off");
  }
}
