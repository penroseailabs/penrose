/**
 * The window's layout breakpoints, for code that needs to know which layout is showing. CSS media queries
 * cannot read variables, so app.css and companion.css repeat these numbers: change them together.
 */

/** From this width the phone column sits beside the conversation (app.css: `max-width: 1240px`). */
export const WIDE = 1241;
/** Up to this width there is room for the conversation alone (app.css, companion.css: `max-width: 720px`). */
export const NARROW = 720;

export const wideQuery = () => window.matchMedia(`(min-width: ${WIDE}px)`);
export const narrowQuery = () => window.matchMedia(`(max-width: ${NARROW}px)`);

/**
 * Keeps `--app-h` equal to the part of the screen the page can actually use. The on-screen keyboard shrinks the
 * visual viewport but not always the layout one (iOS, older Android), so the page scrolls out of view behind it;
 * sizing the app to the visual viewport and pinning the scroll keeps the title bar and composer in place.
 */
export function trackViewport() {
  // iOS ignores user-scalable=no: stop pinch (gesture events) and multi-touch moves from zooming the page.
  document.addEventListener("gesturestart", (e) => e.preventDefault());
  document.addEventListener("touchmove", (e) => { if (e.touches.length > 1) e.preventDefault(); }, { passive: false });
  const vv = window.visualViewport;
  if (!vv) return;
  const fit = () => {
    document.documentElement.style.setProperty("--app-h", `${vv.height}px`);
    if (window.scrollY || vv.offsetTop) window.scrollTo(0, 0);
  };
  vv.addEventListener("resize", () => {
    fit();
    // The keyboard opened (or closed): the browser revealed the focused field before the app shrank to fit, so the
    // field can sit under the keyboard. Once the new size has laid out, bring it into view in its own scroll area.
    const field = document.activeElement;
    if (field instanceof HTMLElement && field.matches("input, textarea, [contenteditable='true']")) {
      requestAnimationFrame(() => field.scrollIntoView({ block: "nearest" }));
    }
  });
  vv.addEventListener("scroll", fit);
  fit();
}
