import { ArrowUpRight, ChevronDown } from "lucide-react";
import { createContext, type ReactNode, useCallback, useContext, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

/** What a page says about itself (backend/app/linkpreview.py): the computer reads it, because a window cannot read
 *  other sites. The picture arrives as a small data: URL, so nothing here ever makes this window fetch from a site. */
type Preview = { url: string; host: string; site: string | null; title: string | null; description: string | null; image: string | null };

// One read per address for as long as the window lives; a link with nothing to show is remembered too.
const previews = new Map<string, Promise<Preview | null>>();

function loadPreview(href: string): Promise<Preview | null> {
  let found = previews.get(href);
  if (!found) {
    found = fetch(`/api/link-preview?url=${encodeURIComponent(href)}`)
      .then((r) => (r.ok ? (r.json() as Promise<Preview>) : null))
      .catch(() => null);
    previews.set(href, found);
    // A failure caused by the link being down (not the page having nothing to show) is tried again next time.
    found.then((p) => p || setTimeout(() => previews.delete(href), 5 * 60_000));
  }
  return found;
}

/** `undefined` while it loads, `null` when there is nothing to show. */
function usePreview(href: string, on: boolean) {
  const [found, setFound] = useState<Preview | null | undefined>(undefined);
  useEffect(() => {
    if (!on) return;
    let current = true;
    loadPreview(href).then((p) => current && setFound(p));
    return () => {
      current = false;
    };
  }, [href, on]);
  return found;
}

/** True once the element has been on screen: a long chat's links are read as they are scrolled to, not all at once. */
function useSeen<T extends Element>() {
  const [seen, setSeen] = useState(false);
  const observer = useRef<IntersectionObserver | undefined>(undefined);
  const watch = useCallback((el: T | null) => {
    observer.current?.disconnect();
    if (!el) return;
    observer.current = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) setSeen(true);
    });
    observer.current.observe(el);
  }, []);
  useEffect(() => () => observer.current?.disconnect(), []);
  return [seen, watch] as const;
}

/** Set by a paragraph or list item whose only content is one link: that link is shown as a card. */
export const AloneContext = createContext(false);
/** True while the reply is still being written: a half-typed address is never looked up. */
export const StreamingContext = createContext(false);

const web = (href?: string) => !!href && /^https?:\/\//i.test(href);
const same = (text: string, href: string) => text.replace(/\/$/, "") === href.replace(/\/$/, "");

function Card({ href, label, preview, ...rest }: { href: string; label?: string; preview: Preview } & React.AnchorHTMLAttributes<HTMLAnchorElement>) {
  // The agent's own words for a link lead; the page's title follows. Without any, the page's title leads.
  const title = label || preview.title;
  const sub = label && preview.title && preview.title !== label ? preview.title : preview.description;
  const site = preview.site || preview.host;
  return (
    <a {...rest} className={preview.image ? "lcard" : "lcard plain"} href={href} target="_blank" rel="noopener noreferrer">
      {preview.image ? (
        <span className="lcard-img">
          <img src={preview.image} alt="" />
        </span>
      ) : (
        <span className="lcard-mono" aria-hidden="true">
          {(preview.host[0] || "·").toUpperCase()}
        </span>
      )}
      <span className="lcard-body">
        <span className="lcard-host">
          <span>{site}</span>
          <ArrowUpRight className="lcard-go" size={13} strokeWidth={1.75} aria-hidden="true" />
        </span>
        {title && <span className="lcard-title">{title}</span>}
        {sub && <span className="lcard-desc">{sub}</span>}
      </span>
    </a>
  );
}

/** A link inside a sentence: the chip with a small caret fused to its end, one pill that wraps as one. The card
 *  opens from the caret on a tap, or from a hover over the pill with a mouse. Nothing is looked up until it opens. */
function Chip({ href, label, hidden, children }: { href: string; label?: string; hidden: boolean; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const [at, setAt] = useState<{ left: number; top?: number; bottom?: number } | null>(null);
  const pill = useRef<HTMLSpanElement>(null);
  const timer = useRef<number>(0);
  const found = usePreview(href, open);

  const show = () => {
    window.clearTimeout(timer.current);
    const rects = pill.current?.getClientRects();
    const box = rects && rects[rects.length - 1]; // a pill that wraps: the card hangs from its last line
    if (!box) return;
    const width = Math.min(340, window.innerWidth - 24);
    const left = Math.max(12, Math.min(box.left, window.innerWidth - width - 12));
    // Below the pill, or above it when the card (about 300 px with a picture) would not fit under.
    const above = window.innerHeight - box.bottom < 300 && box.top > window.innerHeight - box.bottom;
    setAt(above ? { left, bottom: window.innerHeight - box.top + 6 } : { left, top: box.bottom + 6 });
    setOpen(true);
  };
  const later = (on: boolean, ms: number) => {
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => (on ? show() : setOpen(false)), ms);
  };
  useEffect(() => {
    if (!open) return;
    const close = () => setOpen(false);
    const outside = (event: Event) => {
      const target = event.target as Element | null;
      if (!target?.closest?.(".lpeek-pop") && !pill.current?.contains(target as Node)) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => event.key === "Escape" && setOpen(false);
    // The card is placed once, so it goes when anything under it moves.
    window.addEventListener("scroll", close, true);
    window.addEventListener("resize", close);
    document.addEventListener("pointerdown", outside, true);
    document.addEventListener("keydown", escape);
    return () => {
      window.removeEventListener("scroll", close, true);
      window.removeEventListener("resize", close);
      document.removeEventListener("pointerdown", outside, true);
      document.removeEventListener("keydown", escape);
    };
  }, [open]);
  useEffect(() => () => window.clearTimeout(timer.current), []);

  const mouse = (event: React.PointerEvent) => event.pointerType === "mouse";
  return (
    <>
      <span
        ref={pill}
        className="lchip"
        onPointerEnter={(e) => !hidden && mouse(e) && later(true, 400)}
        onPointerLeave={(e) => mouse(e) && later(false, 200)}
      >
        {children}
        {"⁠" /* a word joiner: the caret never wraps onto a line of its own */}
        <button
          className="lpeek"
          type="button"
          aria-label="Preview link"
          aria-expanded={open}
          tabIndex={hidden ? -1 : 0}
          style={hidden ? { visibility: "hidden" } : undefined}
          onClick={() => (open ? setOpen(false) : show())}
        >
          <ChevronDown size={14} strokeWidth={2} />
        </button>
      </span>
      {open &&
        at &&
        createPortal(
          <div
            className="lpeek-pop"
            style={at}
            onPointerEnter={(e) => mouse(e) && window.clearTimeout(timer.current)}
            onPointerLeave={(e) => mouse(e) && later(false, 200)}
          >
            {found ? (
              <Card href={href} label={label} preview={found} />
            ) : (
              <span className="lpeek-note">{found === undefined ? "Loading preview…" : "No preview for this link"}</span>
            )}
          </div>,
          document.body,
        )}
    </>
  );
}

/** A link in Relay's words. On its own line or bullet it becomes a card with the page's picture and title; inside a
 *  sentence it stays a chip with a preview caret. */
export function ChatLink({
  href,
  host,
  children,
  ...props
}: { href?: string; host?: string; children?: ReactNode } & React.AnchorHTMLAttributes<HTMLAnchorElement>) {
  const alone = useContext(AloneContext);
  const streaming = useContext(StreamingContext);
  const [seen, watch] = useSeen<HTMLSpanElement>();
  const parts = Array.isArray(children) ? children : [children];
  const plain = parts.every((c) => typeof c === "string");
  const text = plain ? parts.join("") : "";
  const card = alone && plain && web(href);
  const found = usePreview(href || "", card && seen && !streaming);
  const label = text && href && !same(text, href) ? text : undefined;
  const chip = (
    <a {...props} href={href} data-host={host} target="_blank" rel="noopener noreferrer">
      {children}
    </a>
  );
  if (found && href) return <Card href={href} label={label} preview={found} />;
  // Until the card arrives (or when there is none), the chip; its wrapper is what is watched for coming into view.
  if (card) return <span ref={watch}>{chip}</span>;
  // The caret takes its room while the reply is still being written, so nothing moves when it finishes.
  return plain && web(href) ? (
    <Chip href={href!} label={label} hidden={streaming}>
      {chip}
    </Chip>
  ) : (
    chip
  );
}
