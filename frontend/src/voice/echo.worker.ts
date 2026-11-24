/**
 * The echo canceller's worker (echo.ts): WebRTC's AEC3 from @ennuicastr/webrtcaec3.js, loaded as the classic script
 * it is (it sets a global, so it cannot be bundled as a module). Each 20 ms block: what was played is analysed, then
 * its echo is taken out of what the microphone heard; the result goes back as Int16 PCM in blocks of `chunk`.
 */

/// <reference lib="webworker" />

type Aec = {
  analyze(data: Float32Array[], opts?: { sampleRateIn?: number }): void;
  processSize(data: Float32Array[], opts?: { sampleRateIn?: number; sampleRateOut?: number }): number;
  process(out: Float32Array[], data: Float32Array[], opts?: { sampleRateIn?: number; sampleRateOut?: number }): void;
};
type Module = { AEC3: new (rate: number, render: number, capture: number) => Aec };

const scope = self as unknown as DedicatedWorkerGlobalScope & { WebRtcAec3?: () => Promise<Module> };
let aec: Aec | null = null;
let rate = 24000;
let out = new Int16Array(2400);
let at = 0;

function emit(samples: Float32Array) {
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    out[at++] = s < 0 ? s * 0x8000 : s * 0x7fff;
    if (at === out.length) {
      const block = out.buffer.slice(0);
      scope.postMessage(block, [block]);
      at = 0;
    }
  }
}

scope.onmessage = async (e: MessageEvent) => {
  const m = e.data as { t: "load"; url: string; rate: number; chunk: number } | { t: "frame"; mic: Float32Array; ref: Float32Array };
  if (m.t === "load") {
    rate = m.rate;
    out = new Int16Array(m.chunk);
    try {
      scope.importScripts(m.url);
      const mod = await scope.WebRtcAec3!();
      aec = new mod.AEC3(48000, 1, 1); // AEC3 runs at 48 kHz; the library resamples in and out
    } catch {
      // not loaded: the microphone goes through as it is
    }
    return;
  }
  if (!aec) return emit(m.mic);
  const opts = { sampleRateIn: rate, sampleRateOut: rate };
  aec.analyze([m.ref], { sampleRateIn: rate });
  const size = aec.processSize([m.mic], opts); // whole 10 ms frames; the library keeps the rest for the next block
  const cleaned = [new Float32Array(size)];
  aec.process(cleaned, [m.mic], opts);
  emit(cleaned[0]);
};
