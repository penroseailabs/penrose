import { useEffect, useState } from "react";
import type { StreamdownProps } from "streamdown";

/**
 * Math (KaTeX) for rich text (components/Rich.tsx). It is large and most replies have none, so it loads the first
 * time a text holds a `$$`, then stays. A text without math gets no plugins, so its parse is exactly the plain one.
 */
type Plugins = NonNullable<StreamdownProps["plugins"]>;

let withMath: Plugins | undefined; // one object for every text, so a text's `plugins` keeps its identity
let loading: Promise<void> | undefined;

function loadMath() {
  loading ??= import("./richMath").then(
    ({ math }) => {
      withMath = { math };
    },
    (error) => {
      loading = undefined; // offline, say: the next text with math tries again
      throw error;
    },
  );
  return loading;
}

/** The plugins `text` needs, once loaded: math when it holds a `$$`. The text is rendered again when they arrive. */
export function useRichPlugins(text: string): Plugins | undefined {
  const needsMath = text.includes("$$");
  const [, rerender] = useState(0);
  useEffect(() => {
    if (!needsMath || withMath) return;
    let live = true;
    loadMath()
      .then(() => live && rerender((n) => n + 1))
      .catch(() => {});
    return () => {
      live = false;
    };
  }, [needsMath]);
  return needsMath ? withMath : undefined;
}
