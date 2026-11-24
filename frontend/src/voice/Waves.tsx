/**
 * The voice screen's waves: the user in green, Relay in electric blue, drawn from how loud each is now (echo.ts
 * `levels`: what the microphone sends, echo taken out, and everything played). Each voice is a ribbon of thin
 * strands across the whole width, twisting slowly and lifting a little as the voice gets louder; the colour runs
 * through the voice's shades from left to right. Muted, the green lies low and faint; while Relay works on something
 * the blue breathes a little. No library: one canvas, redrawn each frame while the screen is shown.
 *
 * `compact` is the same ribbon for the chat composer's voice input (Composer.tsx): only the user's, at the size of
 * the text box it replaces, swinging just enough to fill it.
 */

import { useEffect, useRef } from "react";
import { levels } from "./echo";

type Props = { muted: boolean; thinking: boolean; compact?: boolean };
type Voice = { colors: string[]; phase: number; speed: number };

const YOU: Voice = { colors: ["#14b8a6", "#22c55e", "#a3e635"], phase: 2.1, speed: 1.15 };
const RELAY: Voice = { colors: ["#22d3ee", "#2f7bff", "#6366f1"], phase: 0, speed: 1 };
const STRANDS = 22;

const COMPACT_SWING = 1.1;

export default function Waves({ muted, thinking, compact = false }: Props) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const now = useRef({ muted, thinking });
  now.current = { muted, thinking };

  useEffect(() => {
    const el = canvas.current;
    const g = el?.getContext("2d");
    if (!el || !g) return;
    const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
    let raf = 0;
    let you = 0;
    let relay = 0;
    let t = 0;
    let last = performance.now();
    // Quick to rise, slow to fall, so words do not flicker.
    const follow = (from: number, to: number) => from + (to - from) * (to > from ? 0.3 : 0.06);
    // Speech is quiet as RMS (about 0.01 to 0.2): lifted so ordinary talk reads clearly.
    const loud = (rms: number) => Math.min(1, Math.sqrt(rms * 8));

    const ribbon = (w: number, h: number, voice: Voice, level: number, faint: boolean) => {
      const mid = h / 2;
      const swing = compact ? COMPACT_SWING : 1;
      const lift = h * (0.05 + 0.18 * level) * swing; // how far the ribbon swings from the middle
      const spread = h * (0.09 + 0.1 * level) * swing; // how wide the strands fan out
      const tt = t * voice.speed + voice.phase;
      // TEST: plain lines with a very faint shadow just under each. One shadow is too faint to see; where many
      // strands overlap their shadows add up, and only then does a shade show. The dark screen is the light one
      // inverted: on light, white lines and a slate shadow; on dark, black lines and a pale shadow.
      const dark = document.documentElement.dataset.theme === "dark";
      const shadow = dark ? "235,240,255" : "70,80,100";
      for (let j = 0; j < STRANDS; j++) {
        const s = j / (STRANDS - 1) - 0.5; // -0.5..0.5 across the ribbon
        const path = new Path2D();
        for (let x = 0; x <= w; x += 3) {
          const u = x / w; // 0..1 across the screen
          const ends = 0.35 + 0.65 * Math.sin(Math.PI * u); // calmer towards the edges
          const centre =
            Math.sin(2 * Math.PI * (0.9 * u) - 1.1 * tt) * 0.7 + Math.sin(2 * Math.PI * (1.7 * u) + 0.7 * tt + 1) * 0.3;
          const twist = Math.sin(2 * Math.PI * (0.6 * u) + 0.5 * tt + s * 0.8);
          const y = mid + ends * (lift * centre + spread * s * 2 * twist);
          if (x === 0) path.moveTo(x, y);
          else path.lineTo(x, y);
        }
        // The middle strands are brightest, the edges of the ribbon fade out.
        const edge = 1 - Math.abs(s) * 1.6;
        const strength = 0.3 + 0.7 * edge;
        // The shadow first: soft and wide, a little below the line (the light is above), so faint it is invisible alone.
        g.save();
        g.translate(0, 3);
        g.lineWidth = 3.5;
        g.strokeStyle = `rgba(${shadow},${((faint ? 0.012 : 0.018 + 0.012 * level) * strength).toFixed(4)})`;
        g.stroke(path);
        g.restore();
        // Then the line itself: white (black on the dark screen).
        g.strokeStyle = dark ? "#000000" : "#ffffff";
        g.globalAlpha = (faint ? 0.15 : 0.4 + 0.5 * level) * strength;
        g.lineWidth = j === STRANDS >> 1 ? 1.6 : 1;
        g.stroke(path);
        g.globalAlpha = 1;
      }
    };

    const frame = (time: number) => {
      raf = requestAnimationFrame(frame);
      const dt = Math.min(0.05, (time - last) / 1000);
      last = time;
      const level = levels();
      you = follow(you, now.current.muted ? 0 : loud(level.you));
      relay = follow(relay, Math.max(loud(level.relay), now.current.thinking ? 0.12 + 0.06 * Math.sin(time / 300) : 0));
      if (!still) t += dt * (0.7 + 1.6 * Math.max(you, relay));

      const dpr = Math.min(2, window.devicePixelRatio || 1);
      const w = el.clientWidth;
      const h = el.clientHeight;
      if (el.width !== Math.round(w * dpr) || el.height !== Math.round(h * dpr)) {
        el.width = Math.round(w * dpr);
        el.height = Math.round(h * dpr);
      }
      g.setTransform(dpr, 0, 0, dpr, 0, 0);
      g.clearRect(0, 0, w, h);
      g.globalCompositeOperation = "source-over";
      if (!compact) ribbon(w, h, RELAY, relay, false);
      ribbon(w, h, YOU, you, now.current.muted);
    };
    raf = requestAnimationFrame(frame);
    return () => cancelAnimationFrame(raf);
  }, []);

  return <canvas ref={canvas} className={compact ? "scope" : "voice-waves"} aria-hidden="true" />;
}
