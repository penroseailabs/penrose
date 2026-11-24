// Runs wrangler with Electron's own Node (ELECTRON_RUN_AS_NODE), as the main module, which is how wrangler
// decides to run at all. yargs sees Electron in process.versions and, unless process.defaultApp is set, drops
// one argument fewer than it would for Node, so it is set. RELAY_WRANGLER_CLI is wrangler-dist/cli.js.
process.defaultApp = true;
process.argv[1] = process.env.RELAY_WRANGLER_CLI;
require("node:module").runMain();
