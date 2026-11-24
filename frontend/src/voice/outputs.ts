/**
 * Where the phone may play penrose's voice (OutputMenu.tsx): the routes the user can pick, as on a call.
 *
 * Two things set where sound comes out, and which of them the browser lets the page control differs by platform:
 *
 * - The microphone's processing, which the page always controls (speech.ts `Output`): "speaker" runs without the
 *   browser's echo cancellation (the page cancels echo itself, echo.ts) and plays on the loudspeaker; "earpiece" keeps
 *   the browser's own, and the phone then plays in call mode, on the earpiece, or on a Bluetooth headset or wired
 *   headphones if one is connected. Every platform has these two, so every route carries one of them.
 * - The output device itself (`AudioContext.setSinkId`), which only some browsers have (Chrome on Android and on
 *   computers). Where it is there, the routes are the devices it lists, by name (a Bluetooth headset, say). Where it
 *   is not (Safari on iPhone and iPad), the phone's own audio settings choose between headphones, Bluetooth and
 *   AirPlay: a connected one is used on its own in "earpiece" mode, and the menu says so.
 */

import type { Output } from "./speech";

export type Icon = "speaker" | "earpiece" | "bluetooth" | "headphones";
/** One entry of the menu: `mode` is the processing it implies, `sink` the output device ("" for the phone's own choice). */
export type Route = { key: string; label: string; icon: Icon; mode: Output; sink: string };

const SINK_KEY = "relay.voice.sink";

export function storedSink(): string {
  try {
    return localStorage.getItem(SINK_KEY) || "";
  } catch {
    return "";
  }
}

const CHOSEN_KEY = "relay.voice.route.chosen";

/** The user's own pick in the output menu (`chosen`): remembered, and it ends the phone's following of a car or headset
 *  (useDesk.ts follow). Without `chosen` nothing is kept. */
export function rememberSink(id: string, chosen = false) {
  try {
    localStorage.setItem(SINK_KEY, id);
    if (chosen) localStorage.setItem(CHOSEN_KEY, "1");
  } catch {}
}

export function hasChosenRoute() {
  try {
    return localStorage.getItem(CHOSEN_KEY) === "1";
  } catch {
    return false;
  }
}

/** Whether the page may pick an output device (Chrome; not Safari). */
export function sinkSupported() {
  return typeof AudioContext !== "undefined" && "setSinkId" in AudioContext.prototype;
}

export function onApple() {
  return /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
}

const SPEAKER: Route = { key: "speaker", label: "Speaker", icon: "speaker", mode: "speaker", sink: "" };
const EARPIECE: Route = { key: "earpiece", label: "Phone earpiece", icon: "earpiece", mode: "earpiece", sink: "" };

function iconOf(label: string): Icon {
  if (/bluetooth|airpods|buds|\bbt\b|hands-?free|headset|android ?auto|\bcar\b/i.test(label) && !/earpiece/i.test(label)) return "bluetooth";
  if (/headphone|wired|usb|line out/i.test(label)) return "headphones";
  if (/earpiece|receiver/i.test(label)) return "earpiece";
  return "speaker";
}

/** The routes to offer now: the devices the browser lists (named, once it has the microphone's permission), else the
 *  two modes every platform has. A mode the list lacks is still offered, since it needs no device. */
export async function listRoutes(): Promise<Route[]> {
  const routes: Route[] = [];
  if (sinkSupported() && navigator.mediaDevices?.enumerateDevices) {
    try {
      const seen = new Set<string>();
      for (const d of await navigator.mediaDevices.enumerateDevices()) {
        // "default" and "communications" are aliases of one of the others, and an empty label means no permission yet.
        if (d.kind !== "audiooutput" || !d.label || d.deviceId === "default" || d.deviceId === "communications") continue;
        const icon = iconOf(d.label);
        const label = d.label.replace(/^(default|communications)\s*-\s*/i, "");
        if (seen.has(label)) continue;
        seen.add(label);
        routes.push({ key: d.deviceId, label, icon, mode: icon === "speaker" ? "speaker" : "earpiece", sink: d.deviceId });
      }
    } catch {
      // not listed: the two modes
    }
  }
  if (!routes.some((r) => r.mode === "speaker")) routes.unshift(SPEAKER);
  if (!routes.some((r) => r.icon === "earpiece") && !routes.some((r) => r.icon === "bluetooth" || r.icon === "headphones")) {
    routes.splice(1, 0, EARPIECE);
  }
  const order: Record<Icon, number> = { speaker: 0, earpiece: 1, headphones: 2, bluetooth: 3 };
  return routes.sort((a, b) => order[a.icon] - order[b.icon]);
}

/** The route in force: the one for the chosen device, else the first of the mode. */
export function current(routes: Route[], output: Output, sink: string): Route {
  return (
    (sink && routes.find((r) => r.sink === sink)) ||
    routes.find((r) => !r.sink && r.mode === output) ||
    routes.find((r) => r.mode === output) ||
    routes[0] ||
    SPEAKER
  );
}
