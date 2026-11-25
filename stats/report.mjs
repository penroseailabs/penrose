// Prints the usage count (src/index.ts, GET /report). Days are UTC; "active" means the copy ran that day.
//
//   npm run report
//
// The report token comes from PENROSE_STATS_TOKEN or the git-ignored file .report-token here; it is the worker's
// REPORT_TOKEN secret (set with `npx wrangler secret put REPORT_TOKEN`). The address is the one in
// desktop/package.json (relayStatsUrl), with /report in place of /ping.

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const read = (file) => (fs.existsSync(file) ? fs.readFileSync(file, "utf8").trim() : "");
const token = process.env.PENROSE_STATS_TOKEN || read(path.join(here, ".report-token"));
const pingUrl = JSON.parse(read(path.join(here, "..", "desktop", "package.json"))).relayStatsUrl;
if (!token) throw new Error("No report token: set PENROSE_STATS_TOKEN or write it to stats/.report-token.");
if (!pingUrl) throw new Error("No relayStatsUrl in desktop/package.json.");

const response = await fetch(pingUrl.replace(/\/ping$/, "/report"), { headers: { Authorization: `Bearer ${token}` } });
if (!response.ok) throw new Error(`The report failed: ${response.status} (a wrong token reads as 404).`);
const r = await response.json();

console.log(`penrose usage, ${r.today} (UTC)\n`);
console.log(`Installs ever:              ${r.installs.total}`);
console.log(`New installs, last 7 days:  ${r.installs.last7}`);
console.log(`New installs, last 30 days: ${r.installs.last30}`);
console.log(`Active today:               ${r.active.today}`);
console.log(`Active, last 7 days:        ${r.active.last7}`);
console.log(`Active, last 30 days:       ${r.active.last30}`);
console.log("\nActive in the last 30 days, by OS:");
for (const row of r.byOs30) console.log(`  ${row.os.padEnd(8)} ${row.n}`);
console.log("\nActive in the last 7 days, by version:");
for (const row of r.byVersion7) console.log(`  ${row.version.padEnd(16)} ${row.n}`);
console.log("\nDaily active, last 30 days:");
for (const row of r.daily) console.log(`  ${row.day} ${row.n}`);
