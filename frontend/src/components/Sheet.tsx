import { type ReactNode, useEffect, useRef } from "react";
import { X } from "lucide-react";

/** A modal panel over a soft scrim. Escape or a click outside closes it. */
export default function Sheet({
  title,
  onClose,
  children,
  className = "",
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  className?: string;
}) {
  const panel = useRef<HTMLElement>(null);
  // The latest onClose, read when it is needed: callers pass a new one each render, and re-running the effect
  // for it would pull focus back to the panel while the user types in it.
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    const escape = (event: KeyboardEvent) => event.key === "Escape" && close.current();
    document.addEventListener("keydown", escape);
    const previous = document.activeElement as HTMLElement | null;
    panel.current?.focus();
    return () => {
      document.removeEventListener("keydown", escape);
      previous?.focus?.();
    };
  }, []);
  return (
    <div className="scrim" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <section ref={panel} tabIndex={-1} role="dialog" aria-modal="true" aria-label={title} className={`sheet surface ${className}`}>
        <header className="sheet-head">
          <h2>{title}</h2>
          <button className="ib" aria-label="Close" onClick={onClose}>
            <X size={15} />
          </button>
        </header>
        {children}
      </section>
    </div>
  );
}
