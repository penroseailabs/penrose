import { memo, useEffect, useState } from "react";
import {
  type AnimateOptions,
  type Components,
  defaultRehypePlugins,
  type LinkSafetyConfig,
  Streamdown,
  type StreamdownProps,
} from "streamdown";
import "streamdown/styles.css";
import { useRichPlugins } from "../lib/richPlugins";
import Diagram from "./Diagram";
import { AloneContext, ChatLink, StreamingContext } from "./LinkCard";

/** A run's picture (its image URL), loaded with fetch rather than by the browser, so it also arrives over the
 *  companion's tunnel, which carries it as base64 JSON. Any other source is not loaded: a page the agent read must
 *  never make the user's browser fetch from somewhere else. Also the voice screen's (voice/Shown.tsx). The URL may
 *  end in the picture's size (`#540x1200`, runtime/pictures.py), so its place can take its shape before it loads. */
export function usePicture(src?: string) {
  const [path, w, h] = (() => {
    const [p, frag] = (src || "").split("#");
    const m = /^(\d+)x(\d+)$/.exec(frag || "");
    return [p, m ? +m[1] : 0, m ? +m[2] : 0] as const;
  })();
  const [url, setUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  const own = path.startsWith("/api/");
  useEffect(() => {
    if (!own) return;
    let cancelled = false;
    let object: string | null = null;
    setUrl(null);
    setFailed(false);
    (async () => {
      try {
        const r = await fetch(path);
        if (!r.ok) throw new Error(String(r.status));
        const type = r.headers.get("content-type") || "";
        if (type.startsWith("image/")) {
          object = URL.createObjectURL(await r.blob());
          if (!cancelled) setUrl(object);
        } else {
          const j = (await r.json()) as { image?: string; content_type?: string };
          if (!j.image) throw new Error("no image");
          if (!cancelled) setUrl(`data:${j.content_type || "image/jpeg"};base64,${j.image}`);
        }
      } catch {
        if (!cancelled) setFailed(true);
      }
    })();
    return () => {
      cancelled = true;
      if (object) URL.revokeObjectURL(object);
    };
  }, [path, own]);
  return { url, failed: !own || failed, size: w && h ? { w, h } : null };
}

/** A picture Relay kept from the phone while it worked, shown with its caption. A click shows it at full size. */
function Picture({ src, alt }: { src?: string; alt?: string }) {
  const { url, failed, size } = usePicture(src);
  const [big, setBig] = useState(false);
  // While it loads, its place has the picture's own shape at the size it will show (at most 240 high, as .pic img),
  // so nothing jumps when it arrives: the box takes the width (against the paragraph), the placeholder the shape.
  const wide = size ? Math.min(size.w, Math.round((240 * size.w) / size.h)) : 0;
  const box = size && !url ? { width: `min(100%, ${wide}px)` } : undefined;
  const holder = size ? { width: "100%", height: "auto", aspectRatio: `${size.w} / ${size.h}` } : undefined;
  if (failed) return alt ? <span className="pic-missing">{alt}</span> : null;
  return (
    <span className={"pic" + (url ? "" : " loading") + (big ? " big" : "")} style={box}>
      {url ? (
        <img src={url} alt={alt || ""} onClick={() => setBig((b) => !b)} />
      ) : (
        <span className="pic-wait" style={holder} aria-hidden="true" />
      )}
      {alt && <span className="pic-cap">{alt}</span>}
    </span>
  );
}

/** A code fence tagged mermaid, drawn as a diagram (components/Diagram.tsx). Inline code never has a language. */
const isDiagram = (className: unknown) =>
  (Array.isArray(className) ? className.join(" ") : String(className ?? "")).split(" ").includes("language-mermaid");

// The site a link goes to, shown beside its text as a quiet tag (CSS reads `data-host`); empty when the text already
// names the site, so a bare address is not said twice.
const linkHost = (href: unknown, children: unknown): string | undefined => {
  try {
    const host = new URL(String(href)).hostname.replace(/^www\./, "");
    const text = (Array.isArray(children) ? children : [children]).filter((c) => typeof c === "string").join("");
    return host && !text.toLowerCase().includes(host) ? host : undefined;
  } catch {
    return undefined;
  }
};

// A paragraph or list item whose only content is one link (a bare address, or a line or bullet that is just the
// link): that link is shown as a card (LinkCard.tsx). A nested list under a bullet does not count as content.
const aloneLink = (node?: { children?: { type: string; value?: string; tagName?: string }[] }) => {
  const content = (node?.children ?? []).filter(
    (c) => !(c.type === "text" && !c.value?.trim()) && !(c.type === "element" && (c.tagName === "ul" || c.tagName === "ol")),
  );
  return content.length === 1 && content[0].type === "element" && content[0].tagName === "a";
};

// Streamdown draws its elements with Tailwind classes and adds its own controls (a link prompt, table and code
// toolbars, image wrappers). Relay styles plain markup itself (app.css), so every element Streamdown would draw is
// named here as the plain tag, and the few Relay draws differently get their own components. Made once: Streamdown
// and each of its blocks re-render only when this object changes.
const COMPONENTS: Components = {
  p: ({ node, children, ...props }) => (
    <p {...props}>
      <AloneContext.Provider value={aloneLink(node)}>{children}</AloneContext.Provider>
    </p>
  ),
  strong: "strong",
  blockquote: "blockquote",
  h1: "h1",
  h2: "h2",
  h3: "h3",
  h4: "h4",
  h5: "h5",
  h6: "h6",
  hr: "hr",
  ol: "ol",
  ul: "ul",
  li: ({ node, children, ...props }) => (
    <li {...props}>
      <AloneContext.Provider value={aloneLink(node)}>{children}</AloneContext.Provider>
    </li>
  ),
  pre: ({ node, children, ...props }) => {
    const code = node?.children[0];
    return code?.type === "element" && isDiagram(code.properties.className) ? (
      children
    ) : (
      <pre {...props}>{children}</pre>
    );
  },
  code: ({ node: _node, className, children, ...props }) =>
    isDiagram(className) ? (
      <Diagram source={String(children).replace(/\n$/, "")} />
    ) : (
      <code className={className} {...props}>
        {children}
      </code>
    ),
  section: "section",
  sub: "sub",
  sup: "sup",
  thead: "thead",
  tbody: "tbody",
  tr: "tr",
  th: "th",
  td: "td",
  a: ({ node: _node, href, children, ...props }) => (
    <ChatLink {...props} href={href} host={linkHost(href, children)}>
      {children}
    </ChatLink>
  ),
  img: ({ node: _node, src, alt }) => <Picture src={typeof src === "string" ? src : undefined} alt={alt} />,
  table: ({ node: _node, ...props }) => (
    <div className="table-wrap">
      <table {...props} />
    </div>
  ),
  input: ({ node: _node, ...props }) => <input {...props} disabled />,
};

// Raw HTML in the text shows as text, never as markup: Relay's words can carry what a page on the phone said.
// `rehypePlugins` replaces Streamdown's own list, so this is that list without rehype-raw.
const REHYPE = [defaultRehypePlugins.sanitize, defaultRehypePlugins.harden];
// Links already open outside the app (the system browser), so there is no confirmation step in between.
const LINKS: LinkSafetyConfig = { enabled: false };
// A link still being written shows as its text until its address is complete, never as a link to nowhere.
const REMEND: StreamdownProps["remend"] = { linkMode: "text-only" };
// New words fade in as they stream, so text arriving in bursts reads as a steady flow.
const ANIMATED: AnimateOptions = { animation: "fadeIn", duration: 160, sep: "word" };

/** Relay's words as rich text: GitHub-flavoured markdown (tables, task lists, strikethrough, bare links), math
 *  between `$$`, mermaid diagrams, and the pictures it kept from the phone. Raw HTML is not rendered; links open
 *  outside the app.
 *
 *  While `streaming`, the text is the reply so far: it is split into blocks and only the block still being written
 *  is parsed again on each update, markdown cut off mid-way (an open `**`, a half-written link) is closed for
 *  display, new words fade in, and a caret follows the last word. A finished text is rendered exactly as written. */
function Rich({ children, streaming = false }: { children: string; streaming?: boolean }) {
  const plugins = useRichPlugins(children);
  return (
    <StreamingContext.Provider value={streaming}>
    <Streamdown
      className={streaming ? "md streaming" : "md"}
      mode="streaming"
      isAnimating={streaming}
      parseIncompleteMarkdown={streaming}
      remend={REMEND}
      animated={ANIMATED}
      components={COMPONENTS}
      rehypePlugins={REHYPE}
      plugins={plugins}
      linkSafety={LINKS}
      controls={false}
    >
      {children}
    </Streamdown>
    </StreamingContext.Provider>
  );
}

/** Rendered again only when the text or its streaming changes. */
export default memo(Rich);
