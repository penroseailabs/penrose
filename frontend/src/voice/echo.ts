/**
 * Echo cancellation in the page, for the loudspeaker (speech.ts Output). With the browser's own cancellation off,
 * Android plays through the loudspeaker but the microphone hears Relay's voice, which then interrupts Relay. So the
 * page cancels it itself, with WebRTC's AEC3 (the canceller inside Chrome) compiled to WebAssembly: everything Relay
 * plays goes through `playback`, which also hands a copy to the microphone's audio graph as the reference, and
 * echo.worker.ts takes that out of what the microphone heard before it is sent. AEC3 finds the delay between the two
 * by itself, but only if the copy comes before the echo: played in the microphone's own AudioContext (the desk does),
 * the copy is the very samples sent to the loudspeaker, at once; from another context it goes through a MediaStream,
 * which adds a delay of its own that varies.
 */

import aecScript from "@ennuicastr/webrtcaec3.js?url";

const outs = new Map<AudioContext, AudioNode>(); // each context's playback node
const joiners = new Set<(played: AudioContext, out: AudioNode) => void>();
const meters = new Set<AnalyserNode>();
let heard = 0; // loudness of the last block the microphone sent, echo taken out

/** Where a player connects its sound: the loudspeaker, plus the canceller's copy of what is playing. */
export function playback(ctx: AudioContext): AudioNode {
  let out = outs.get(ctx);
  if (!out) {
    for (const old of outs.keys()) if (old.state === "closed") outs.delete(old);
    const node = ctx.createGain();
    node.connect(ctx.destination);
    const meter = ctx.createAnalyser();
    meter.fftSize = 512;
    node.connect(meter);
    meters.add(meter);
    outs.set(ctx, node);
    joiners.forEach((join) => join(ctx, node));
    out = node;
  }
  return out;
}

/** Note the loudness of a block of Int16 PCM the microphone sends (speech.ts capture); `null` when it closes. */
export function noteHeard(pcm: ArrayBuffer | null) {
  if (!pcm) return void (heard = 0);
  const samples = new Int16Array(pcm);
  let sum = 0;
  for (let i = 0; i < samples.length; i++) sum += samples[i] * samples[i];
  heard = Math.sqrt(sum / Math.max(1, samples.length)) / 0x8000;
}

const scratch = new Float32Array(512);
/** How loud the user (what the microphone sends) and Relay (everything played) are now, as RMS 0–1. */
export function levels() {
  let relay = 0;
  for (const meter of meters) {
    meter.getFloatTimeDomainData(scratch);
    let sum = 0;
    for (let i = 0; i < scratch.length; i++) sum += scratch[i] * scratch[i];
    relay = Math.max(relay, Math.sqrt(sum / scratch.length));
  }
  return { you: heard, relay };
}

/** Everything played, now and from players made later, summed into `input` of `node` (in the microphone's
 *  context). Returns the undo. */
export function reference(ctx: AudioContext, node: AudioNode, input: number): () => void {
  const bus = ctx.createGain();
  bus.connect(node, 0, input);
  const joined: AudioNode[] = [];
  const join = (played: AudioContext, out: AudioNode) => {
    if (played.state === "closed") return;
    try {
      if (played === ctx) {
        out.connect(bus); // the same samples the loudspeaker gets, at the same moment
        joined.push(out);
        return;
      }
      const tap = played.createMediaStreamDestination();
      out.connect(tap);
      ctx.createMediaStreamSource(tap.stream).connect(bus);
    } catch {
      // no MediaStream output there: nothing to cancel against
    }
  };
  outs.forEach((out, played) => join(played, out));
  joiners.add(join);
  return () => {
    joiners.delete(join);
    joined.forEach((out) => {
      try {
        out.disconnect(bus);
      } catch {}
    });
    bus.disconnect();
  };
}

/** The canceller, in a worker: 20 ms blocks of microphone and reference in, Int16 PCM blocks of `chunk` samples out.
 *  Until the WebAssembly is loaded (a moment), the microphone goes through as it is. */
export function canceller(rate: number, chunk: number, onChunk: (pcm: ArrayBuffer) => void) {
  const worker = new Worker(new URL("./echo.worker.ts", import.meta.url));
  worker.onmessage = (e) => onChunk(e.data as ArrayBuffer);
  worker.postMessage({ t: "load", url: new URL(aecScript, location.href).href, rate, chunk });
  return {
    push(mic: Float32Array, ref: Float32Array) {
      worker.postMessage({ t: "frame", mic, ref }, [mic.buffer, ref.buffer]);
    },
    close() {
      worker.terminate();
    },
  };
}
