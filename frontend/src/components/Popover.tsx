import { type ReactNode, useEffect, useRef, useState } from "react";

type Props = {
  /** The button that opens it; gets aria-expanded and toggles on click. */
  trigger: (props: { onClick: () => void; "aria-expanded": boolean; "aria-haspopup": "menu" }) => ReactNode;
  children: (close: () => void) => ReactNode;
  /** Where the menu sits relative to its trigger. */
  place?: "below-end" | "below-start" | "above-start" | "above-end";
  className?: string;
};

const placement: Record<NonNullable<Props["place"]>, React.CSSProperties> = {
  "below-end": { top: "calc(100% + 6px)", right: 0 },
  "below-start": { top: "calc(100% + 6px)", left: 0 },
  "above-start": { bottom: "calc(100% + 6px)", left: 0 },
  "above-end": { bottom: "calc(100% + 6px)", right: 0 },
};

/** A small floating menu: closes on outside click, Escape, or after a choice. */
export default function Popover({ trigger, children, place = "below-end", className = "" }: Props) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    // Pointer events, not mouse events: a touch on the phone app may never produce a mousedown.
    const outside = (event: Event) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => event.key === "Escape" && setOpen(false);
    document.addEventListener("pointerdown", outside, true);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("pointerdown", outside, true);
      document.removeEventListener("keydown", escape);
    };
  }, [open]);
  return (
    <div className="popover" ref={root}>
      {trigger({ onClick: () => setOpen((v) => !v), "aria-expanded": open, "aria-haspopup": "menu" })}
      {open && (
        <div className={`menu ${className}`} role="menu" style={placement[place]}>
          {children(() => setOpen(false))}
        </div>
      )}
    </div>
  );
}
