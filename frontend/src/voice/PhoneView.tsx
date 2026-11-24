/**
 * The phone's screen on the voice screen: the same live view as the phone panel (lib/useLiveFrames.ts), watched
 * only. It is the stage of the voice screen whenever penrose is using the phone (Voice.tsx), under the pictures,
 * cards and forms that sit on it over a blur. Mounted only then, so the stream is open only then. `leaving` fades it
 * out before the screen unmounts it.
 */

import { useRef } from "react";
import { Smartphone } from "lucide-react";
import { useLiveFrames } from "../lib/useLiveFrames";
import type { Workspace } from "../lib/useWorkspace";

export default function PhoneView({ ws, leaving }: { ws: Workspace; leaving: boolean }) {
  const device = ws.device;
  const connected = !!device?.connected;
  // Height over width: the phone's own, so the frame has its shape before the first picture arrives.
  const ratio = device?.width && device.height ? device.height / device.width : 19.5 / 9;
  return (
    <div className={`voice-phone ${leaving ? "is-leaving" : ""}`} aria-label="The phone's screen" role="img">
      <div className="voice-phone-frame" style={{ "--r": ratio } as React.CSSProperties}>
        {connected && device ? (
          <Live id={device.id} asleep={!!device.asleep} />
        ) : (
          <div className="voice-phone-empty">
            <Smartphone size={22} aria-hidden="true" />
            <span>The phone is starting</span>
          </div>
        )}
      </div>
    </div>
  );
}

function Live({ id, asleep }: { id: string; asleep: boolean }) {
  const image = useRef<HTMLImageElement>(null);
  const live = useLiveFrames(`/api/devices/${id}/stream`, image);
  return (
    <>
      <img ref={image} className={live ? "" : "waiting"} alt="" draggable={false} />
      {asleep && <span className="voice-phone-note">Asleep</span>}
    </>
  );
}
