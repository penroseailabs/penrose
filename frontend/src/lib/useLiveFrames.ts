import { useEffect, useRef, useState } from "react";

/** The gateway repeats the last frame every 5 s on a still screen, so this much silence means the stream is dead. */
const SILENCE_MS = 12_000;
const HEADER_END = [13, 10, 13, 10]; // \r\n\r\n
const decoder = new TextDecoder();

function indexOf(data: Uint8Array, pattern: number[], from: number, to: number) {
  outer: for (let i = from; i <= to - pattern.length; i++) {
    for (let j = 0; j < pattern.length; j++) if (data[i + j] !== pattern[j]) continue outer;
    return i;
  }
  return -1;
}

/**
 * The phone's live view, read from the gateway's MJPEG stream and shown in `img`.
 *
 * Not `<img src=stream>`: when that stream ends (the services restart, the connection drops) the browser fires
 * no error and the image just keeps its last frame, so the view looks frozen. Reading it here, an ended or
 * silent stream is noticed and reopened. Returns whether a frame is showing.
 *
 * While the window is hidden (minimized, or behind another window on macOS) the stream is closed, so the gateway
 * stops capturing and encoding frames nobody sees; the last frame stays up and the stream reopens on return.
 */
export function useLiveFrames(url: string, img: React.RefObject<HTMLImageElement | null>) {
  const [live, setLive] = useState(false);
  const shown = useRef<string>("");

  useEffect(() => {
    let stopped = false;
    let controller: AbortController | null = null;
    let wake: (() => void) | null = null; // resolves the wait while hidden

    const show = (jpeg: Uint8Array) => {
      const el = img.current;
      if (!el) return;
      const previous = shown.current;
      shown.current = URL.createObjectURL(new Blob([jpeg as BlobPart], { type: "image/jpeg" }));
      el.src = shown.current;
      if (previous) URL.revokeObjectURL(previous);
      setLive(true);
    };

    const read = async () => {
      controller = new AbortController();
      const abort = controller;
      let quiet = setTimeout(() => abort.abort(), SILENCE_MS);
      const response = await fetch(url, { signal: abort.signal, cache: "no-store" });
      if (!response.ok || !response.body) throw new Error(`Stream answered ${response.status}`);
      const reader = response.body.getReader();
      // One growing buffer: bytes [start, end) are unparsed. Chunks are appended, parts consumed from the front.
      let buffer = new Uint8Array(256 * 1024);
      let start = 0;
      let end = 0;
      try {
        for (;;) {
          const { value, done } = await reader.read();
          if (done) return;
          clearTimeout(quiet);
          quiet = setTimeout(() => abort.abort(), SILENCE_MS);
          if (end + value.length > buffer.length) {
            // Compact to the front, growing only when the unparsed bytes need it.
            const pending = end - start;
            const target = pending + value.length > buffer.length ? new Uint8Array((pending + value.length) * 2) : buffer;
            if (target === buffer) buffer.copyWithin(0, start, end);
            else target.set(buffer.subarray(start, end));
            buffer = target;
            start = 0;
            end = pending;
          }
          buffer.set(value, end);
          end += value.length;
          // Parts: --frame\r\nContent-Type: image/jpeg\r\nContent-Length: N\r\n\r\n<N bytes>\r\n
          let latest: Uint8Array | null = null;
          for (;;) {
            const headerEnd = indexOf(buffer, HEADER_END, start, end);
            if (headerEnd < 0) break;
            const header = decoder.decode(buffer.subarray(start, headerEnd));
            const length = Number(/content-length:\s*(\d+)/i.exec(header)?.[1]);
            if (!Number.isFinite(length)) throw new Error("Malformed stream part");
            const body = headerEnd + 4;
            if (end < body + length) break;
            latest = buffer.subarray(body, body + length);
            start = body + length;
          }
          if (latest) show(latest); // only the newest of a burst (the Blob copies it before the buffer moves)
          if (start === end) start = end = 0;
        }
      } finally {
        clearTimeout(quiet);
        abort.abort();
      }
    };

    const onVisibility = () => {
      if (document.hidden) controller?.abort();
      else wake?.();
    };
    document.addEventListener("visibilitychange", onVisibility);

    (async () => {
      let delay = 500;
      while (!stopped) {
        if (document.hidden) {
          await new Promise<void>((r) => (wake = r));
          wake = null;
          delay = 500;
          continue;
        }
        const began = Date.now();
        try {
          await read();
        } catch {
          // ended, refused, went silent, or the window was hidden: reopen below
        }
        if (stopped) break; // the last frame stays up while it reconnects
        if (document.hidden) continue;
        delay = Date.now() - began > 10_000 ? 500 : Math.min(delay * 2, 4000);
        await new Promise((r) => setTimeout(r, delay));
      }
    })();

    return () => {
      stopped = true;
      document.removeEventListener("visibilitychange", onVisibility);
      controller?.abort();
      wake?.();
      if (shown.current) URL.revokeObjectURL(shown.current);
      shown.current = "";
    };
  }, [url, img]);

  return live;
}
