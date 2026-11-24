import { fileURLToPath } from "node:url";
import type { OutputChunk } from "rollup";
import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import { VitePWA } from "vite-plugin-pwa";

declare const process: { env: Record<string, string | undefined>; platform: string };

/** The companion build (`vite build --mode companion`): the same window, started from src/companion/main.tsx,
 * as an installable web app the coordinator serves (coordinator/), with its own service worker
 * (src/companion/sw.ts: the install, and the notification a push becomes). Output: dist-companion. */
function companionEntry(): Plugin {
  return {
    name: "relay-companion-entry",
    transformIndexHtml: { order: "pre", handler: (html) => html.replace("/src/main.tsx", "/src/companion/main.tsx") },
  };
}

/** Math and diagrams in replies (src/lib/richMath.ts, src/lib/richDiagrams.ts) load on demand, with several MB of
 * KaTeX and Mermaid behind them. The companion app leaves out of its install every chunk that only they reach, and
 * caches each the first time a reply needs it (VitePWA below). Worked out from the bundle's import graph, so it
 * holds whatever chunks Mermaid splits itself into. */
const RICH = /\/src\/lib\/rich(Math|Diagrams)\.ts$/;
const onDemand = new Set<string>();
function onDemandChunks(): Plugin {
  return {
    name: "relay-on-demand-chunks",
    generateBundle(_options, bundle) {
      const chunks = new Map(
        Object.values(bundle)
          .filter((file): file is OutputChunk => file.type === "chunk")
          .map((chunk) => [chunk.fileName, chunk]),
      );
      // Everything a chunk loads, now or on demand; `stop` ends the walk at the rich modules.
      const reach = (roots: OutputChunk[], stop: (chunk: OutputChunk) => boolean) => {
        const seen = new Set<string>();
        const visit = (name: string) => {
          const chunk = chunks.get(name);
          if (!chunk || seen.has(name) || stop(chunk)) return;
          seen.add(name);
          [...chunk.imports, ...chunk.dynamicImports].forEach(visit);
        };
        roots.forEach((chunk) => visit(chunk.fileName));
        return seen;
      };
      // A rich module may share its chunk with what it pulls in (Mermaid's diagrams import from it), so a chunk is
      // rich when it holds one, not only when it is one's entry.
      const isRich = (chunk: OutputChunk) => chunk.moduleIds.some((id) => RICH.test(id.replaceAll("\\", "/")));
      const all = [...chunks.values()];
      const entries = all.filter((chunk) => chunk.isEntry);
      const app = reach(entries, isRich);
      for (const name of reach(all.filter(isRich), () => false)) if (!app.has(name)) onDemand.add(name);
    },
  };
}

/** What the builds write inside this folder. The dev server must not watch it: every companion build empties and
 * refills dist-companion, which reloaded the dev window each time and set off the fault deletedFolderGuard is for. */
const BUILD_OUTPUT = ["**/dist", "**/dist/**", "**/dist-companion", "**/dist-companion/**"];

/** On Windows, Node's watch on a folder that is deleted is never told so: from then on it reports that the folder
 * itself was renamed, over and over (130 000 times a second measured, Node 22.12 and 24.11), and never stops. That
 * holds a processor core for as long as the dev server runs, and on Node 22.12 each report also leaks about 120
 * bytes: a dev server left running reached 152 GB on 2026-11-09, used up this computer's virtual memory and
 * froze the phone. A report naming the watched folder itself is that fault and nothing else, so its watch is
 * closed at the first one; the folder is watched again if it is still there or comes back. */
function deletedFolderGuard(): Plugin {
  // One spelling of a folder's path: the fault's report has the \\?\ prefix, the watcher's own path may not.
  const own = (folder: string) =>
    folder.replace(/^\\\\\?\\/, "").replace(/\//g, "\\").replace(/\\+$/, "").toLowerCase();
  return {
    name: "relay-deleted-folder-guard",
    apply: "serve",
    configureServer(server) {
      if (process.platform !== "win32") return;
      const closing = new Set<string>();
      server.watcher.on("raw", (_event: string, file: unknown, details: { watchedPath?: string } | undefined) => {
        const folder = details?.watchedPath;
        if (!folder || typeof file !== "string" || closing.has(folder) || own(file) !== own(folder)) return;
        closing.add(folder);
        server.watcher.unwatch(folder);
        server.config.logger.info(`closed the watch on a deleted folder: ${folder}`, { timestamp: true });
        setTimeout(() => {
          closing.delete(folder);
          server.watcher.add(folder);
        }, 500);
      });
    },
  };
}

export default defineConfig(({ mode }) => ({
  plugins:
    mode === "companion"
      ? [
          react(),
          deletedFolderGuard(),
          companionEntry(),
          onDemandChunks(),
          VitePWA({
            strategies: "injectManifest",
            srcDir: "src/companion",
            filename: "sw.ts",
            registerType: "prompt",
            includeAssets: ["favicon.png", "companion/icon-192.png", "companion/icon-512.png", "companion/icon-maskable-192.png", "companion/icon-maskable-512.png"],
            manifest: {
              name: "penrose",
              short_name: "penrose",
              description: "penrose on your phone",
              start_url: "/",
              display: "standalone",
              background_color: "#0f0f10",
              theme_color: "#0f0f10",
              // "any" is the rounded mark with a margin; "maskable" is the mark small on the splash colour, so the
              // launcher's crop and Android's splash screen show a modest mark rather than a wall of tiles.
              icons: [
                { src: "/companion/icon-192.png", sizes: "192x192", type: "image/png", purpose: "any" },
                { src: "/companion/icon-512.png", sizes: "512x512", type: "image/png", purpose: "any" },
                { src: "/companion/icon-maskable-192.png", sizes: "192x192", type: "image/png", purpose: "maskable" },
                { src: "/companion/icon-maskable-512.png", sizes: "512x512", type: "image/png", purpose: "maskable" },
              ],
            },
            injectManifest: {
              // Math and diagram code stays out of the install and is kept once fetched (file names are hashed;
              // src/companion/sw.ts caches them on demand).
              manifestTransforms: [
                async (entries) => ({ manifest: entries.filter((entry) => !onDemand.has(entry.url)), warnings: [] }),
              ],
            },
          }),
        ]
      : [react(), deletedFolderGuard()],
  define: { "import.meta.env.VITE_COMPANION": JSON.stringify(mode === "companion" ? "1" : "") },
  // Raw HTML in a reply is never rendered (src/lib/no-raw-html.ts): rehype-raw and its HTML parser stay out.
  resolve: { alias: { "rehype-raw": fileURLToPath(new URL("./src/lib/no-raw-html.ts", import.meta.url)) } },
  build: { outDir: mode === "companion" ? "dist-companion" : "dist" },
  server: {
    host: "127.0.0.1",
    port: 8000,
    strictPort: true,
    watch: { ignored: BUILD_OUTPUT },
    // 127.0.0.1, not localhost: Node 18+ resolves localhost to ::1, where the API is not listening.
    proxy: {
      "/api": {
        target: `http://127.0.0.1:${process.env.RELAY_API_PORT || 8001}`,
        changeOrigin: true,
        // The API restarts whenever backend code changes. Answer 503 "restarting" (which the UI retries)
        // instead of Vite's bare 500. Runs before Vite's own handler, which then skips its reply.
        configure: (proxy) => {
          proxy.on("error", (_err, _req, res) => {
            if (!("writeHead" in res) || res.headersSent || res.writableEnded) return;
            res.writeHead(503, { "Content-Type": "application/json", "Retry-After": "1", "X-Relay-Restarting": "1" });
            res.end(JSON.stringify({ detail: "penrose is restarting. Try again in a moment.", restarting: true }));
          });
          // A streaming answer (the live view, run events) cut off by a restart would otherwise leave the
          // browser's side open and silent, so the stream looks frozen. Cut it off there too.
          proxy.on("proxyRes", (proxyRes, _req, res) => {
            proxyRes.on("close", () => {
              if (!proxyRes.complete && !res.writableEnded) res.destroy();
            });
          });
        },
      },
    },
  },
}));
