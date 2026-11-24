import { useEffect, useState } from "react";

export type ThemeChoice = "system" | "light" | "dark";
const KEY = "relay.theme";
const CHANGED = "relay-theme-changed";
const dark = () => window.matchMedia("(prefers-color-scheme: dark)");

function read(): ThemeChoice {
  try {
    const saved = localStorage.getItem(KEY);
    if (saved === "light" || saved === "dark") return saved;
  } catch {}
  return "system";
}

/** Set <html data-theme> from the saved choice, before the first paint. */
export function applyTheme(choice: ThemeChoice = read()) {
  const resolved: "light" | "dark" = choice === "system" ? (dark().matches ? "dark" : "light") : choice;
  document.documentElement.dataset.theme = resolved;
  // The desktop app draws its title bar buttons to match.
  window.relay?.setTheme?.(choice);
  // A phone's status bar takes the window's colour (the companion app's manifest can only name one).
  let bar = document.querySelector<HTMLMetaElement>('meta[name="theme-color"]');
  if (!bar) {
    bar = document.createElement("meta");
    bar.name = "theme-color";
    document.head.appendChild(bar);
  }
  bar.content = getComputedStyle(document.documentElement).getPropertyValue("--canvas").trim() || (resolved === "dark" ? "#1e1f23" : "#e7e8eb");
  return resolved;
}

/** The saved choice, and the theme it resolves to ("system" follows the OS). Both are state, so the UI updates on the click itself. */
export function useTheme() {
  const [choice, setChoice] = useState<ThemeChoice>(read);
  const [resolved, setResolved] = useState<"light" | "dark">(() => (document.documentElement.dataset.theme === "dark" ? "dark" : "light"));
  // Another screen (the voice one) may change the theme: follow it.
  useEffect(() => {
    const sync = () => setChoice(read());
    window.addEventListener(CHANGED, sync);
    return () => window.removeEventListener(CHANGED, sync);
  }, []);
  useEffect(() => {
    setResolved(applyTheme(choice));
    try {
      if (choice === "system") localStorage.removeItem(KEY);
      else localStorage.setItem(KEY, choice);
    } catch {}
    window.dispatchEvent(new Event(CHANGED));
    if (choice !== "system") return;
    const media = dark();
    const follow = () => setResolved(applyTheme("system"));
    media.addEventListener("change", follow);
    return () => media.removeEventListener("change", follow);
  }, [choice]);
  return [choice, setChoice, resolved] as const;
}
