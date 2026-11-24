import React from "react";
import ReactDOM from "react-dom/client";
import "@fontsource-variable/inter";
// The wordmark: Fraunces, soft cut (weight + softness axes), only where the product name stands on its own.
import "@fontsource-variable/fraunces/soft.css";
import App from "./App";
import ErrorBoundary from "./components/ErrorBoundary";
import { startTiledBackground } from "./lib/tiledBackground";
import { applyTheme } from "./lib/theme";
import "./styles/tokens.css";
import "./styles/base.css";
import "./styles/app.css";

applyTheme(); // before the first paint, so the window never flashes the wrong theme
startTiledBackground();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </React.StrictMode>,
);
