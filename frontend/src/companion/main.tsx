/// <reference types="vite-plugin-pwa/client" />
import React from "react";
import ReactDOM from "react-dom/client";
import "@fontsource-variable/inter";
// The wordmark: Fraunces, soft cut (weight + softness axes), only where the product name stands on its own.
import "@fontsource-variable/fraunces/soft.css";
import { registerSW } from "virtual:pwa-register";
import ErrorBoundary from "../components/ErrorBoundary";
import { trackViewport } from "../lib/layout";
import { startTiledBackground } from "../lib/tiledBackground";
import { applyTheme } from "../lib/theme";
import "../styles/tokens.css";
import "../styles/base.css";
import "../styles/app.css";
import "./companion.css";
import Companion from "./Companion";
import { showUpdateBanner } from "./updateBanner";

// A page whose stylesheet did not arrive (kept by the service worker from one deploy, asking for a file an earlier or
// later deploy had and the server no longer does): drop the worker's stored files and load once more, which the
// worker then answers from the server. Only the file caches go: the pairing (IndexedDB) and the push subscription
// (the worker's registration) stay. Once a session, so a page that cannot be styled is shown rather than reloaded.
if (!getComputedStyle(document.documentElement).getPropertyValue("--canvas").trim() && !sessionStorage.getItem("restyled")) {
  sessionStorage.setItem("restyled", "1");
  void caches
    .keys()
    .then((names) => Promise.all(names.map((name) => caches.delete(name))))
    .finally(() => location.reload());
}

applyTheme();
startTiledBackground();
trackViewport();
// A new version waits in the background and a dismissible bar offers it (updateBanner.ts); Update installs it and
// reloads. Also look for one whenever the app comes back to the front.
const updateSW = registerSW({
  immediate: true,
  onNeedRefresh: () => showUpdateBanner(() => void updateSW(true)),
  onRegisteredSW: (_url, registration) => {
    if (!registration) return;
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") registration.update().catch(() => {});
    });
  },
});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ErrorBoundary>
      <Companion />
    </ErrorBoundary>
  </React.StrictMode>,
);
