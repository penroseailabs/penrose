// A harness for src/voice/desk.ts: the Desk class driven by a fake Realtime socket and a fake Relay API, so the
// desk's reply logic (one reply made from the state, tool chains heard once, news told once) can be checked without
// a microphone or OpenAI.
//
//   node scripts/desk-sim.mjs            (from frontend/; exits 1 when a check fails)
//   node scripts/desk-sim.mjs <a copy of frontend/ to check instead>
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import os from "node:os";
import path from "node:path";

const here = path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1"));
const front = path.resolve(process.argv[2] || path.join(here, ".."));
const require = createRequire(pathToFileURL(path.join(front, "package.json")));
const esbuild = require(path.join(front, "node_modules/esbuild"));

// --- the browser, as far as the desk uses it -------------------------------------------------------------------
const t0 = performance.now();
class FakeContext {
  sampleRate = 24000;
  state = "running";
  get currentTime() {
    return (performance.now() - t0) / 1000;
  }
  createBuffer(_c, length, rate) {
    return { duration: length / rate, copyToChannel() {} };
  }
  createBufferSource() {
    const ctx = this;
    const source = {
      buffer: null,
      onended: null,
      timer: 0,
      connect() {},
      start(at = 0) {
        played.push(source);
        const ms = Math.max(0, (at - ctx.currentTime + source.buffer.duration) * 1000);
        source.timer = setTimeout(() => source.onended?.(), ms);
      },
      stop() {
        clearTimeout(source.timer);
      },
    };
    return source;
  }
  createGain() {
    return { gain: { value: 1, cancelScheduledValues() {}, setValueAtTime() {}, linearRampToValueAtTime() {} }, connect() {} };
  }
  resume() {
    return Promise.resolve();
  }
  close() {
    return Promise.resolve();
  }
}
const played = []; // every chunk of sound that reached the loudspeaker
let socket = null;
class FakeSocket {
  static OPEN = 1;
  readyState = 0;
  sent = [];
  constructor() {
    socket = this;
    setTimeout(() => {
      this.readyState = 1;
      this.onopen?.();
    }, 0);
  }
  send(raw) {
    const message = JSON.parse(raw);
    if (message.type !== "input_audio_buffer.append") this.sent.push(message);
  }
  close() {
    this.readyState = 3;
  }
  say(event) {
    this.onmessage?.({ data: JSON.stringify(event) });
  }
}
Object.assign(globalThis, {
  window: globalThis,
  AudioContext: FakeContext,
  WebSocket: FakeSocket,
  requestAnimationFrame: (fn) => setTimeout(fn, 16),
  cancelAnimationFrame: (id) => clearTimeout(id),
  localStorage: { getItem: () => null, setItem() {} },
});

// --- Relay's API ---------------------------------------------------------------------------------------------------
const relay = { calls: [], state: { text: "[penrose now]", about: "", due: [] }, tools: [] };
globalThis.__api = async (url, method, body) => {
  relay.calls.push({ url, body });
  if (url === "/voice/desk/open") return { secret: "ek", url: "wss://x", items: relay.items || [], unheard: [], latest_event: 0 };
  if (url === "/voice/desk/state") return structuredClone(relay.state);
  if (url === "/voice/desk/tool") return relay.tools.shift() || { output: "{}", result: {}, focus: null };
  if (url === "/voice/desk/nods") return { rate: 24000, clips: relay.nods || [] };
  return null;
};

const stubs = {
  name: "stubs",
  setup(build) {
    build.onResolve({ filter: /^\.\.\/api$/ }, () => ({ path: "api", namespace: "stub" }));
    build.onResolve({ filter: /^\.\/echo$/ }, () => ({ path: "echo", namespace: "stub" }));
    build.onResolve({ filter: /^\.\/speech$/ }, () => ({ path: "speech", namespace: "stub" }));
    build.onLoad({ filter: /.*/, namespace: "stub" }, (args) => ({
      contents: {
        api: "export const api = (...a) => globalThis.__api(...a);",
        echo: "export const playback = () => ({});",
        speech: "export const RATE = 24000; export const b64 = () => ''; export const silence = () => ''; export const capture = async () => ({ close() {} });",
      }[args.path],
    }));
  },
};
const out = path.join(os.tmpdir(), `desk-sim-${process.pid}.mjs`);
await esbuild.build({ entryPoints: [path.join(front, "src/voice/desk.ts")], bundle: true, format: "esm", outfile: out, plugins: [stubs], logLevel: "silent" });
const { Desk } = await import(pathToFileURL(out).href + `?${Date.now()}`);

// --- helpers -------------------------------------------------------------------------------------------------------
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const sound = (ms) => Buffer.alloc(Math.round(24 * ms) * 2, 1).toString("base64"); // PCM of `ms`, as the API sends it
let failures = 0;
function check(name, ok, detail = "") {
  if (!ok) failures++;
  console.log(`${ok ? "  ok  " : "  FAIL"} ${name}${ok || !detail ? "" : `\n         ${detail}`}`);
}
const sent = (type) => socket.sent.filter((m) => m.type === type);
const stateTexts = () => sent("conversation.item.create").filter((m) => m.item.role === "system").map((m) => m.item.content[0].text);
const logged = () => relay.calls.filter((c) => c.url === "/voice/desk/log" && c.body.role === "assistant").map((c) => c.body.text);
let n = 0;

async function open(items = []) {
  relay.calls = [];
  relay.tools = [];
  relay.items = items;
  played.length = 0;
  const desk = new Desk();
  desk.events = [];
  for (const name of ["quiet", "told", "news", "said", "show", "ask", "error", "focus"]) {
    desk.addEventListener(name, (e) => desk.events.push([name, e.detail]));
  }
  await desk.start();
  await wait(20);
  return desk;
}

/** The user says something: the detector's events, then their transcript. */
async function userSays(text, { over = false } = {}) {
  const id = `user_${++n}`;
  socket.say({ type: "input_audio_buffer.speech_started" });
  if (!over) await wait(5);
  socket.say({ type: "input_audio_buffer.speech_stopped" });
  socket.say({ type: "input_audio_buffer.committed", item_id: id });
  socket.say({ type: "conversation.item.input_audio_transcription.completed", item_id: id, transcript: text });
  await wait(30);
  return id;
}

/** The model's response: words (with `ms` of sound), and maybe a tool call. Returns the ids it used. */
async function modelReplies({ says = "", ms = 60, calls = null, cancelled = false } = {}) {
  const item = `msg_${++n}`;
  socket.say({ type: "response.created" });
  if (says) {
    socket.say({ type: "response.output_item.added", item: { type: "message", id: item } });
    socket.say({ type: "response.output_audio_transcript.delta", delta: says });
    socket.say({ type: "response.output_audio.delta", delta: sound(ms) });
    if (!cancelled) {
      socket.say({ type: "response.output_audio.done" });
      socket.say({ type: "response.output_audio_transcript.done", transcript: says });
    }
  }
  let call = "";
  if (calls) {
    call = `fc_${++n}`;
    socket.say({ type: "response.output_item.done", item: { type: "function_call", id: call, name: calls, call_id: `call_${n}`, arguments: "{}" } });
  }
  socket.say({ type: "response.done", response: { status: cancelled ? "cancelled" : "completed" } });
  await wait(40);
  return { item, call };
}

// --- 1. the 14:20 chain: one thing said, three tool calls -------------------------------------------------------------
console.log("\n1. A reply that keeps calling tools is heard once, then gets one last word");
{
  const desk = await open();
  relay.state = { text: "[penrose now] nothing is running", about: "", due: [] };
  relay.tools = [
    { output: '{"error": "No errand has that id"}', result: { error: "No errand has that id" }, focus: null },
    { output: '{"sessions": []}', result: { sessions: [] }, focus: null },
    { output: '{"sessions": []}', result: { sessions: [] }, focus: null },
  ];
  await userSays("Hold on. Order the usual pad thai.");
  check("the state is read and put in the conversation before the reply is asked for",
    stateTexts().length === 1 && stateTexts()[0].includes("Why you speak now: the user spoke") && sent("response.create").length === 1);
  const first = await modelReplies({ says: "Okay, pausing it.", calls: "stop_errand" });
  check("the first words are heard at once", played.length === 1);
  check("a failed tool asks for one more reply, from a fresh state", sent("response.create").length === 2 && stateTexts().length === 2);
  check("the old state item is taken out", sent("conversation.item.delete").some((m) => m.item_id === "state_1"));
  const second = await modelReplies({ says: "Let me check which order is active.", calls: "find_errand" });
  check("words on the way to another tool call are never played", played.length === 1, `played ${played.length}`);
  check("...and leave the conversation", sent("conversation.item.delete").some((m) => m.item_id === second.item));
  await modelReplies({ says: "Got it, let me check the right order.", calls: "find_errand" });
  check("still only one reply heard after three tool calls", played.length === 1);
  const last = sent("response.create").at(-1);
  check("after three calls the desk gets its last word without tools", last.response?.tool_choice === "none", JSON.stringify(last));
  await modelReplies({ says: "There's no order running. Starting a new one." });
  check("the last word is heard", played.length === 2);
  check("two replies are remembered as said, not five", logged().length === 2, JSON.stringify(logged()));
  const gone = sent("conversation.item.delete").map((m) => m.item_id);
  check("the tool calls and their results leave the conversation when the chain ends",
    [first.call, second.call].every((id) => gone.includes(id)) && gone.filter((id) => id.startsWith("result_")).length === 3, gone.join(","));
  desk.stop();
}

// --- 2. a routing tool that worked: nothing more is asked ----------------------------------------------------------
console.log("\n2. A tool that only confirms where the words went asks for no second reply");
{
  const desk = await open();
  relay.tools = [{ output: '{"session": "s:aaaaaa", "status": "starting"}', result: { session: "s:aaaaaa" }, focus: { chat_id: "aaaaaa1111", title: "Tea" } }];
  await userSays("Order the usual pad thai.");
  await modelReplies({ says: "Ordering your groceries.", calls: "start_errand" });
  check("one reply asked for, one heard", sent("response.create").length === 1 && played.length === 1);
  check("the errand becomes the one being talked about", desk.events.some(([name, d]) => name === "focus" && d.chatId === "aaaaaa1111"));
  check("the call and its result are taken out", sent("conversation.item.delete").filter((m) => /^(fc_|result_)/.test(m.item_id)).length === 2);
  desk.stop();
}

// --- 3. the user starts to speak before a sound of the reply is heard --------------------------------------------------
console.log("\n3. A reply nobody has heard is dropped when the user speaks, and asked for again from the new state");
{
  const desk = await open();
  relay.state = { text: "[penrose now] A", about: "", due: [] };
  await userSays("Order the");
  check("a reply is on its way", sent("response.create").length === 1);
  socket.say({ type: "response.created" });
  socket.say({ type: "response.output_item.added", item: { type: "message", id: "msg_dropped" } });
  socket.say({ type: "input_audio_buffer.speech_started" }); // the user goes on
  check("it is cancelled", sent("response.cancel").length === 1);
  socket.say({ type: "response.output_audio_transcript.delta", delta: "Sure, let me" });
  socket.say({ type: "response.output_audio.delta", delta: sound(60) });
  socket.say({ type: "response.done", response: { status: "cancelled" } });
  check("nothing of it is played or remembered", played.length === 0 && logged().length === 0);
  check("its item leaves the conversation", sent("conversation.item.delete").some((m) => m.item_id === "msg_dropped"));
  relay.state = { text: "[penrose now] B", about: "", due: [] };
  socket.say({ type: "input_audio_buffer.speech_stopped" });
  socket.say({ type: "input_audio_buffer.committed", item_id: "user_more" });
  await wait(30);
  check("the next reply is made from the state as it is then", sent("response.create").length === 2 && stateTexts().at(-1).includes("B"));
  await modelReplies({ says: "Ordering your groceries." });
  check("and that one is heard", played.length === 1 && logged().length === 1);
  desk.stop();
}

// --- 4. news of the errand being talked about --------------------------------------------------------------------
console.log("\n4. News is a signal: the reply is made from the state, and told once it is heard to the end");
{
  const desk = await open();
  relay.tools = [{ output: '{"session": "s:aaaaaa"}', result: { session: "s:aaaaaa" }, focus: { chat_id: "aaaaaa1111", title: "Tea" } }];
  await userSays("Order the usual pad thai.");
  await modelReplies({ says: "Ordering your groceries.", ms: 40, calls: "start_errand" });
  await wait(80);
  const event = { id: 7, chat_id: "aaaaaa1111", title: "Tea", kind: "asks", text: "Place it for 255?", news: "", session: "s:aaaaaa", private: false };
  relay.state = { text: "[penrose now] tea asks: place it for 255? not told yet", about: "aaaaaa1111", due: [7] };
  const before = sent("response.create").length;
  desk.inject([event]);
  await wait(30);
  check("a question about it asks for a reply", sent("response.create").length === before + 1);
  check("the event's own words are never put in the conversation",
    !sent("conversation.item.create").some((m) => JSON.stringify(m).includes("[event]")) && stateTexts().at(-1).includes("has news the user has not been told"));
  await modelReplies({ says: "It's two fifty-five. Place it?", ms: 50 });
  check("not marked told while it is still being heard", !relay.calls.some((c) => c.url === "/voice/desk/announced"));
  await wait(120);
  const marked = relay.calls.filter((c) => c.url === "/voice/desk/announced");
  check("marked told once heard to its end", marked.length === 1 && marked[0].body.ids[0] === 7, JSON.stringify(marked));
  check("and its card goes", desk.events.some(([name, d]) => name === "told" && d === "aaaaaa1111"));
  // The same news again (a late copy), with nothing untold in the state: no reply.
  relay.state = { text: "[penrose now] tea waiting", about: "aaaaaa1111", due: [] };
  const count = sent("response.create").length;
  desk.inject([{ ...event, id: 8 }]);
  await wait(30);
  check("news the state shows as told asks for nothing", sent("response.create").length === count);
  // News of another errand: a card, no reply.
  desk.inject([{ ...event, id: 9, chat_id: "bbbbbb2222", kind: "finished" }]);
  await wait(30);
  check("another errand's news is a card, and the desk says nothing",
    sent("response.create").length === count && desk.events.some(([name, d]) => name === "news" && d[0].id === 9));
  desk.stop();
}

// --- 5. news told, but the user cuts in ----------------------------------------------------------------------------
console.log("\n5. A reply the user cuts off has told nothing");
{
  const desk = await open();
  const event = { id: 3, chat_id: "aaaaaa1111", title: "Tea", kind: "finished", text: "Ordered.", news: "", session: "s:aaaaaa", private: false };
  relay.state = { text: "[penrose now] tea finished, not told yet", about: "aaaaaa1111", due: [3] };
  desk.answer({ chatId: "aaaaaa1111", title: "Tea", session: "s:aaaaaa" }, [event]);
  await wait(30);
  check("opening a card asks for a reply", sent("response.create").length === 1 && stateTexts()[0].includes("the user opened s:aaaaaa"));
  await modelReplies({ says: "Your tea is ordered, arriving in thirty-four minutes.", ms: 400 });
  desk.interrupt();
  await wait(40);
  check("not marked told", !relay.calls.some((c) => c.url === "/voice/desk/announced"));
  check("it is a card again", desk.events.some(([name, d]) => name === "news" && d[0].id === 3));
  check("what the user heard of it is remembered as cut", relay.calls.some((c) => c.url === "/voice/desk/cut"));
  await userSays("Which three");
  check("the next reply is told they stopped the last one", stateTexts().at(-1).includes("They stopped your last reply"), stateTexts().at(-1));
  await modelReplies({ says: "The Lane Linen, the AmazonBasics and the Encasa.", ms: 5 });
  await wait(300); // the reply has been heard to its end
  await userSays("Thanks");
  check("and only that one", stateTexts().length === 3 && !stateTexts().at(-1).includes("They stopped your last reply"));
  desk.stop();
}

// --- 6. nothing to say ---------------------------------------------------------------------------------------------
console.log("\n6. A reply with nothing in it lets the screen go back to listening");
{
  const desk = await open();
  await userSays("Hold on");
  await modelReplies({});
  check("the screen is told", desk.events.some(([name]) => name === "quiet"));
  check("nothing is remembered as said", logged().length === 0);
  desk.stop();
}

// --- 7. the conversation stays short -------------------------------------------------------------------------------
console.log("\n7. Only the last lines said stay in the conversation");
{
  const desk = await open([
    { type: "message", role: "user", content: [{ type: "input_text", text: "earlier" }] },
    { type: "message", role: "assistant", content: [{ type: "output_text", text: "Earlier reply." }] },
  ]);
  const start = sent("conversation.item.create").filter((m) => m.item.id?.startsWith("line_")).length;
  check("a new line starts with the last lines said, and no state", start === 2 && stateTexts().length === 0);
  for (let i = 0; i < 5; i++) {
    await userSays(`turn ${i}`);
    await modelReplies({ says: `Reply ${i}.`, ms: 5 });
  }
  const gone = sent("conversation.item.delete").map((m) => m.item_id);
  check("the oldest lines are taken out as new ones come", gone.includes("line_1") && gone.includes("line_2"), gone.join(","));
  check("one state item at a time", stateTexts().length === 5 && gone.filter((id) => id.startsWith("state_")).length === 4);
  desk.stop();
}

// --- 8. a call: nothing said before it is answered is sent -----------------------------------------------------------
console.log("\n8. What is said before the call is answered is not kept");
{
  const desk = new Desk();
  const pcm = new Int16Array(240).fill(3000).buffer;
  const starting = desk.start();
  desk.muted = false; // as if the microphone had opened before the line
  desk.send(pcm);
  check("said while it rings: dropped", desk.waiting.length === 0);
  await starting;
  await wait(20);
  socket.onclose(); // the line drops in the middle of the call
  desk.send(pcm);
  check("said while a call's line is down: kept for when it is back", desk.waiting.length === 1);
  desk.stop();
}

// --- 9. a reply that is stopped stays stopped ------------------------------------------------------------------------
console.log("\n9. Sound still on its way when a reply is stopped is not played");
{
  const desk = await open();
  await userSays("Which lamps did we find?");
  socket.say({ type: "response.created" });
  socket.say({ type: "response.output_item.added", item: { type: "message", id: "msg_cut" } });
  socket.say({ type: "response.output_audio_transcript.delta", delta: "I found three sets: " });
  socket.say({ type: "response.output_audio.delta", delta: sound(400) });
  await wait(20);
  const before = played.length;
  desk.interrupt("user", "voice");
  socket.say({ type: "response.output_audio_transcript.delta", delta: "the Lane Linen six-pack" });
  socket.say({ type: "response.output_audio.delta", delta: sound(400) });
  socket.say({ type: "response.output_audio.delta", delta: sound(400) });
  socket.say({ type: "response.done", response: { status: "cancelled" } });
  await wait(40);
  check("nothing more of it is heard", played.length === before && !desk.speaking, `played ${played.length - before} more`);
  await wait(300);
  await userSays("Never mind");
  await modelReplies({ says: "Okay.", ms: 5 });
  check("the next reply is heard", played.length === before + 1, `played ${played.length - before}`);
  desk.stop();
}

// --- 10. picking up -------------------------------------------------------------------------------------------------
console.log("\n10. The desk greets when it picks up a call, unless something else is to be said first");
{
  let desk = await open();
  relay.state = { text: "[penrose now] tea finished, not told yet", about: "aaaaaa1111", due: [3] };
  desk.greet();
  await wait(30);
  check("a greeting is asked for", sent("response.create").length === 1 && stateTexts()[0].includes("you have just picked up"));
  await modelReplies({ says: "Hello.", ms: 5 });
  await wait(100);
  check("it tells no news", !relay.calls.some((c) => c.url === "/voice/desk/announced"));
  desk.stop();

  desk = await open();
  desk.answer({ chatId: "aaaaaa1111", title: "Tea", session: "s:aaaaaa" }, []);
  desk.greet();
  await wait(30);
  check("a call placed by opening a card says that card's news instead",
    sent("response.create").length === 1 && stateTexts()[0].includes("the user opened s:aaaaaa"));
  await modelReplies({ says: "Your tea is ordered.", ms: 5 });
  await wait(100);
  check("and no greeting follows", sent("response.create").length === 1);
  desk.stop();
}

// --- 11. the state is read while the user speaks ---------------------------------------------------------------------
console.log("\n11. The reply does not wait for the state: it was read while the user was speaking");
{
  const desk = await open();
  const reads = () => relay.calls.filter((c) => c.url === "/voice/desk/state").length;
  socket.say({ type: "input_audio_buffer.speech_started" });
  await wait(20);
  check("read when they start to speak", reads() === 1);
  socket.say({ type: "input_audio_buffer.speech_stopped" });
  socket.say({ type: "input_audio_buffer.committed", item_id: "user_ahead" });
  await wait(30);
  check("the reply is made from that read", reads() === 1 && sent("response.create").length === 1 && stateTexts().length === 1);
  relay.tools = [{ output: "{}", result: {}, focus: null }];
  await modelReplies({ calls: "read_reply" });
  check("after a tool it is read again", reads() === 2 && stateTexts().length === 2, `reads ${reads()}`);
  const before = played.length;
  socket.say({ type: "response.created" });
  socket.say({ type: "response.output_audio_transcript.delta", delta: "It says the order is placed." });
  socket.say({ type: "response.output_audio.delta", delta: sound(60) });
  await wait(10);
  check("a tool called without a word: what follows is heard as it comes, not held to its end", played.length === before + 1);
  socket.say({ type: "response.done", response: { status: "completed" } });
  await wait(40);
  const said = relay.calls.filter((c) => c.url === "/voice/desk/log" && c.body.role === "assistant").at(-1);
  check("the reply's timing is kept with its words", said?.body.timing?.total >= 0 && said.body.timing.round === 1, JSON.stringify(said?.body));
  desk.stop();
}

// --- 12. the desk hears itself ---------------------------------------------------------------------------------------
console.log("\n12. The desk's own voice, back through the microphone, is nobody's words");
{
  const desk = await open();
  desk.greet();
  await wait(30);
  socket.say({ type: "response.created" });
  socket.say({ type: "response.output_item.added", item: { type: "message", id: "msg_hello" } });
  socket.say({ type: "response.output_audio_transcript.delta", delta: "Hi, I'm here." });
  socket.say({ type: "response.output_audio.delta", delta: sound(600) });
  socket.say({ type: "response.output_audio_transcript.done", transcript: "Hi, I'm here." });
  socket.say({ type: "response.done", response: { status: "completed" } });
  await wait(30);
  socket.say({ type: "input_audio_buffer.speech_started" }); // the loudspeaker, heard by the microphone
  desk.bargeIn();
  socket.say({ type: "input_audio_buffer.speech_stopped" });
  socket.say({ type: "input_audio_buffer.committed", item_id: "user_echo" });
  socket.say({ type: "conversation.item.input_audio_transcription.completed", item_id: "user_echo", transcript: "Hi, I'm here." });
  await wait(40);
  check("not logged as the user's words", !relay.calls.some((c) => c.url === "/voice/desk/log" && c.body.role === "user"));
  check("taken out of the conversation", sent("conversation.item.delete").some((m) => m.item_id === "user_echo"));
  check("a reply to it that nobody heard is dropped", sent("response.cancel").length >= 1);
  socket.say({ type: "response.done", response: { status: "cancelled" } });
  await wait(300);
  await userSays("Which lamps did we find?");
  check("the next reply is not told the user stopped the desk", !stateTexts().at(-1).includes("They stopped your last reply"));
  check("their real words are logged", relay.calls.some((c) => c.url === "/voice/desk/log" && c.body.role === "user"));
  desk.stop();
}

// --- 13. heard, before the reply -------------------------------------------------------------------------------------
console.log("\n13. A nod says the user was heard when the reply is slow to begin");
{
  relay.nods = [sound(200)];
  const desk = await open();
  await userSays("Which lamps did we find?");
  const before = played.length;
  await wait(700);
  check("a turn the reply has not begun to answer gets a nod", played.length === before + 1, `played ${played.length - before}`);
  await modelReplies({ says: "Three sets.", ms: 5 });
  await wait(300);
  await userSays("And the tea?");
  const then = played.length;
  await modelReplies({ says: "Ordered at noon.", ms: 900 });
  await wait(700);
  check("one that is answered at once gets none", played.length === then + 1, `played ${played.length - then}`);
  desk.stop();
  relay.nods = [];
}

console.log(failures ? `\n${failures} check(s) failed` : "\nall checks passed");
process.exit(failures ? 1 : 0);
