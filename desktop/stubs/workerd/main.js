// Stands in for the `workerd` package in the installed app. wrangler loads it as it starts, and the real one
// refuses to load without its program (about 100 MB), which only running a Worker locally needs. penrose only signs
// in and deploys (src/companion.js), so the installer carries this instead (package.json, extraResources).
Object.defineProperty(exports, "__esModule", { value: true });
exports.default = ""; // the program's path: there is none
exports.compatibilityDate = "2026-09-01";
exports.version = "0.0.0";
