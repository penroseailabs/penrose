/**
 * Where penrose's voice plays, as in a video call's audio menu: one button showing the route in force, and a frosted
 * list above it with a tick on that route (speaker, the phone's earpiece, headphones, a Bluetooth headset by name).
 * See outputs.ts for what each route does and where the page may pick a device itself.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Bluetooth, Check, Headphones, Smartphone, Volume2 } from "lucide-react";
import { current, type Icon, listRoutes, onApple, type Route, sinkSupported } from "./outputs";
import type { Output } from "./speech";

const ICONS = { speaker: Volume2, earpiece: Smartphone, bluetooth: Bluetooth, headphones: Headphones } satisfies Record<Icon, unknown>;

type Props = { output: Output; sink: string; onPick: (route: Route) => void; onOpen?: (open: boolean) => void };

export default function OutputMenu({ output, sink, onPick, onOpen }: Props) {
  const [open, setOpenState] = useState(false);
  const [routes, setRoutes] = useState<Route[]>([]);
  const box = useRef<HTMLDivElement>(null);
  const setOpen = useCallback(
    (next: boolean) => {
      setOpenState(next);
      onOpen?.(next);
    },
    [onOpen],
  );

  const refresh = useCallback(() => void listRoutes().then(setRoutes), []);
  // Listed at once, and again when a headset connects or goes (and when the list is opened, after the microphone's
  // permission may have given the devices their names).
  useEffect(() => {
    refresh();
    navigator.mediaDevices?.addEventListener?.("devicechange", refresh);
    return () => navigator.mediaDevices?.removeEventListener?.("devicechange", refresh);
  }, [refresh]);
  useEffect(() => {
    if (!open) return;
    refresh();
    const away = (e: Event) => !box.current?.contains(e.target as Node) && setOpen(false);
    const key = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("pointerdown", away);
    window.addEventListener("keydown", key);
    return () => {
      document.removeEventListener("pointerdown", away);
      window.removeEventListener("keydown", key);
    };
  }, [open, refresh, setOpen]);

  const now = current(routes, output, sink);
  const Now = ICONS[now.icon];
  return (
    <div className="voice-route" ref={box}>
      <button
        className={`ib solid ${open ? "is-on" : ""}`}
        onClick={() => setOpen(!open)}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={`Audio output: ${now.label}`}
        title={`Audio output: ${now.label}`}
      >
        <Now size={16} />
      </button>
      {open && (
        <div className="voice-route-menu" role="menu" aria-label="Audio output">
          {routes.map((route) => {
            const Icon = ICONS[route.icon];
            const on = route.key === now.key;
            return (
              <button
                key={route.key}
                className={`voice-route-item ${on ? "is-on" : ""}`}
                role="menuitemradio"
                aria-checked={on}
                onClick={() => {
                  onPick(route);
                  setOpen(false);
                }}
              >
                <Icon size={18} aria-hidden="true" />
                <span>{route.label}</span>
                {on && <Check size={16} aria-hidden="true" />}
              </button>
            );
          })}
          {(onApple() || !sinkSupported()) && (
            <p className="voice-route-note">
              {onApple()
                ? "Bluetooth, headphones and AirPlay follow your phone's own audio settings. Connect one and choose the earpiece."
                : "This browser cannot pick a device: connect a headset and choose the earpiece."}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
