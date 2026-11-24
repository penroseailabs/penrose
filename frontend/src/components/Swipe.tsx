/**
 * A row or card handled like a notification on a phone's lock screen: it follows the finger and shows what the swipe
 * will do behind it; past a third of its width, or flicked, it goes the rest of the way and runs the action. A swipe
 * to the right runs `right`, to the left `left`; a direction without an action does not swipe that way. A tap is
 * the child's own. A drag that starts upward is left to the page, which scrolls.
 */

import { useRef, useState, type ReactNode } from "react";

export type SwipeAction = { label: string; icon: ReactNode; run: () => void };

type Props = {
  className?: string;
  /** Swiped right. */
  right?: SwipeAction;
  /** Swiped left. */
  left?: SwipeAction;
  children: ReactNode;
};

const START_PX = 10; // how far a finger must move sideways before it is a swipe and not a tap or a scroll
const FLICK = 0.5; // px per ms: a quick short swipe counts
const GONE_MS = 180;

export default function Swipe({ className = "", right, left, children }: Props) {
  const box = useRef<HTMLDivElement>(null);
  const drag = useRef<{ id: number; x: number; y: number; t: number; live: boolean } | null>(null);
  const swiped = useRef(false); // a swipe ends in a click on whatever is under the finger: swallowed
  const [dx, setDx] = useState(0);
  const [phase, setPhase] = useState<"rest" | "drag" | "snap" | "gone">("rest");

  const down = (e: React.PointerEvent) => {
    if (phase === "gone" || (e.pointerType === "mouse" && e.button !== 0)) return;
    drag.current = { id: e.pointerId, x: e.clientX, y: e.clientY, t: e.timeStamp, live: false };
    swiped.current = false;
  };
  const move = (e: React.PointerEvent) => {
    const d = drag.current;
    if (!d || d.id !== e.pointerId) return;
    const x = e.clientX - d.x;
    if (!d.live) {
      const y = e.clientY - d.y;
      if (Math.abs(y) > START_PX && Math.abs(y) > Math.abs(x)) return void (drag.current = null); // the page scrolls
      if (Math.abs(x) < START_PX || Math.abs(x) < Math.abs(y)) return;
      if (!(x > 0 ? right : left)) return void (drag.current = null); // nothing to do that way
      d.live = true;
      swiped.current = true;
      box.current?.setPointerCapture(e.pointerId);
      setPhase("drag");
    }
    setDx(x > 0 ? (right ? x : 0) : left ? x : 0);
  };
  const up = (e: React.PointerEvent) => {
    const d = drag.current;
    drag.current = null;
    if (!d || !d.live || d.id !== e.pointerId) return;
    const x = e.clientX - d.x;
    const width = box.current?.offsetWidth || 300;
    const speed = Math.abs(x) / Math.max(1, e.timeStamp - d.t);
    const act = x > 0 ? right : left;
    if (act && (Math.abs(x) > width / 3 || (Math.abs(x) > 40 && speed > FLICK))) {
      const way = x > 0 ? 1 : -1;
      setPhase("gone");
      setDx(way * (width + 40));
      window.setTimeout(act.run, GONE_MS);
    } else {
      setPhase("snap");
      setDx(0);
    }
  };
  const cancel = (e: React.PointerEvent) => {
    if (drag.current?.id !== e.pointerId) return;
    const live = drag.current.live;
    drag.current = null;
    if (live) {
      setPhase("snap");
      setDx(0);
    }
  };

  const reveal = Math.min(1, Math.abs(dx) / 70);
  return (
    <div className={`swipe ${phase}`}>
      {right && (
        <div className="swipe-hint right" style={{ opacity: dx > 0 ? reveal : 0 }} aria-hidden="true">
          {right.icon} {right.label}
        </div>
      )}
      {left && (
        <div className="swipe-hint left" style={{ opacity: dx < 0 ? reveal : 0 }} aria-hidden="true">
          {left.label} {left.icon}
        </div>
      )}
      <div
        ref={box}
        className={`swipe-face ${className}`}
        style={{ transform: dx ? `translateX(${dx}px)` : undefined, opacity: phase === "gone" ? 0 : 1 }}
        onPointerDown={down}
        onPointerMove={move}
        onPointerUp={up}
        onPointerCancel={cancel}
        onClickCapture={(e) => {
          if (swiped.current) {
            swiped.current = false;
            e.stopPropagation();
            e.preventDefault();
          }
        }}
        onTransitionEnd={() => phase === "snap" && setPhase("rest")}
      >
        {children}
      </div>
    </div>
  );
}
