/**
 * What the voice screen shows while Relay speaks: the pictures of the news or reply it is telling (a product, an
 * order confirmation). No frame around them: the pictures sit on the screen itself, in an area of their own that
 * the words above never resize. Several are an album: they scroll sideways, the next one peeking in, the one in
 * the middle full size and its neighbours a little smaller and quieter. A tap shows one full screen; pulling the
 * pictures down (or a tap on the handle above them) puts them away. Everything shown is also in the chat.
 */

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { usePicture } from "../components/Rich";
import type { Picture } from "./desk";

export type Shown = { pictures: Picture[]; key: number };

const AWAY = 90; // pixels pulled down that put the pictures away

export default function ShownCard({ shown, onClose }: { shown: Shown; onClose: () => void }) {
  const { pictures } = shown;
  const many = pictures.length > 1;
  const [at, setAt] = useState(0);
  const [big, setBig] = useState<Picture | null>(null);
  const [pull, setPull] = useState(0);
  const [leaving, setLeaving] = useState(false);
  const strip = useRef<HTMLDivElement>(null);
  const drag = useRef<{ x: number; y: number; down: boolean | null } | null>(null);
  const frame = useRef(0);

  // The album's depth: each picture scaled and faded by how far it sits from the middle.
  const place = useCallback(() => {
    frame.current = 0;
    const el = strip.current;
    if (!el) return;
    const mid = el.scrollLeft + el.clientWidth / 2;
    let nearest = 0;
    let best = Infinity;
    Array.from(el.children).forEach((child, i) => {
      const slide = child as HTMLElement;
      const centre = slide.offsetLeft + slide.offsetWidth / 2;
      const off = Math.min(1, Math.abs(centre - mid) / slide.offsetWidth);
      if (Math.abs(centre - mid) < best) {
        best = Math.abs(centre - mid);
        nearest = i;
      }
      slide.style.setProperty("--off", off.toFixed(3));
    });
    setAt(nearest);
  }, []);
  const onScroll = () => {
    if (!frame.current) frame.current = requestAnimationFrame(place);
  };
  useLayoutEffect(() => {
    strip.current?.scrollTo({ left: 0 });
    place();
  }, [shown.key, place]);
  useEffect(() => () => cancelAnimationFrame(frame.current), []);

  const away = () => {
    setLeaving(true);
    window.setTimeout(onClose, 220);
  };

  // Pulling down puts the pictures away; a sideways move is the album's own scroll.
  const down = (e: React.PointerEvent) => {
    drag.current = { x: e.clientX, y: e.clientY, down: null };
  };
  const move = (e: React.PointerEvent) => {
    const d = drag.current;
    if (!d) return;
    const dx = e.clientX - d.x;
    const dy = e.clientY - d.y;
    if (d.down === null && Math.hypot(dx, dy) > 8) {
      d.down = dy > Math.abs(dx);
      // A pull follows the finger wherever it goes, so letting go below the pictures still ends it. Taken only
      // once it is a pull: taken earlier, a tap would land on the album instead of the picture.
      if (d.down) (e.currentTarget as Element).setPointerCapture?.(e.pointerId);
    }
    if (d.down) setPull(Math.max(0, dy));
  };
  const up = () => {
    const pulled = drag.current?.down && pull > AWAY;
    drag.current = null;
    if (pulled) away();
    else setPull(0);
  };

  const style = leaving
    ? { transform: "translateY(60px)", opacity: 0 }
    : pull
      ? { transform: `translateY(${pull * 0.6}px) scale(${1 - Math.min(pull, 240) / 2400})`, opacity: 1 - Math.min(pull, 240) / 340, transition: "none" }
      : undefined;

  return (
    <>
      <div
        className={`voice-shown ${many ? "is-album" : ""}`}
        role="group"
        aria-label={many ? `${pictures.length} pictures` : "A picture"}
        style={style}
        onPointerDown={down}
        onPointerMove={move}
        onPointerUp={up}
        onPointerCancel={up}
      >
        <button className="voice-shown-handle" onClick={away} aria-label="Put the pictures away" title="Pull down to put away" />
        <div className="voice-shown-strip" ref={strip} onScroll={onScroll}>
          {pictures.map((p) => (
            <Slide key={p.url} picture={p} onOpen={() => setBig(p)} />
          ))}
        </div>
        {many && (
          <div className="voice-shown-dots" aria-hidden="true">
            {pictures.map((p, i) => (
              <span key={p.url} className={i === at ? "on" : ""} />
            ))}
          </div>
        )}
      </div>
      {big && <FullPicture picture={big} onClose={() => setBig(null)} />}
    </>
  );
}

function Slide({ picture, onOpen }: { picture: Picture; onOpen: () => void }) {
  const { url, failed, size } = usePicture(picture.url);
  const shape = size ? ({ "--r": (size.h / size.w).toFixed(4) } as React.CSSProperties) : undefined;
  return (
    <figure className="voice-shown-slide">
      <div className="voice-shown-frame">
        {url ? (
          <button className="voice-shown-img" onClick={onOpen} aria-label={`Show ${picture.caption || "the picture"} full screen`}>
            <img src={url} alt={picture.caption} draggable={false} />
          </button>
        ) : failed ? (
          <span className="voice-shown-missing">The picture could not be loaded. It is in the chat.</span>
        ) : (
          <span className="voice-shown-wait" style={shape} aria-hidden="true" />
        )}
      </div>
      {picture.caption && <figcaption>{picture.caption}</figcaption>}
    </figure>
  );
}

function FullPicture({ picture, onClose }: { picture: Picture; onClose: () => void }) {
  const { url } = usePicture(picture.url);
  useEffect(() => {
    const key = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, [onClose]);
  return (
    <div className="voice-full" role="dialog" aria-label={picture.caption || "Picture"} onClick={onClose}>
      {url && <img src={url} alt={picture.caption} />}
      {picture.caption && <p>{picture.caption}</p>}
    </div>
  );
}
