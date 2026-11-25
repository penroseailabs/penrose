/**
 * The usage count: a Cloudflare Worker that counts how many copies of penrose are in use, and nothing else.
 *
 * An installed copy (desktop/src/main.js, "usage count") sends at most one ping a day:
 *
 *   POST /ping   {"id": "<random UUID made on first start>", "version": "2026.1124.2248", "os": "win32"}
 *
 * The ID is random and stays in that copy's config; it says nothing about the person or the computer. This worker
 * keeps the ID, the version, the OS and the UTC day. It does not keep the IP address, the country, headers or
 * anything else the request carries. A copy whose user turned the count off in Settings sends nothing.
 *
 * Storage is one SQLite Durable Object (Counter). `installs` holds each ID once, with the day it was first seen
 * (new and total installs); `pings` holds one row per ID per day it was used (daily, weekly, monthly active) and is
 * trimmed to 400 days by the daily cron.
 *
 *   GET /report  with `Authorization: Bearer <REPORT_TOKEN>` (a worker secret): the numbers as JSON, which
 *                `npm run report` prints (report.mjs). Without the token: 404.
 */

import { DurableObject } from "cloudflare:workers";

export interface Env {
  COUNTER: DurableObjectNamespace<Counter>;
  REPORT_TOKEN?: string;
}

const ID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const VERSION = /^\d{1,4}(\.\d{1,6}){1,3}$/;
const OS = new Set(["win32", "darwin", "linux"]);
const MAX_BODY = 512;
const KEEP_DAYS = 400;
const DAY_MS = 86_400_000;

/** The UTC day `daysAgo` days before now, as YYYY-MM-DD. */
const day = (daysAgo = 0) => new Date(Date.now() - daysAgo * DAY_MS).toISOString().slice(0, 10);

const counter = (env: Env) => env.COUNTER.get(env.COUNTER.idFromName("all"));

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/ping") {
      if (request.method !== "POST") return new Response("Method not allowed", { status: 405, headers: { Allow: "POST" } });
      const text = await request.text();
      if (text.length > MAX_BODY) return new Response("Too large", { status: 413 });
      let ping: { id?: unknown; version?: unknown; os?: unknown };
      try {
        ping = JSON.parse(text);
      } catch {
        return new Response("Bad request", { status: 400 });
      }
      const { id, version, os } = ping ?? {};
      if (typeof id !== "string" || !ID.test(id) || typeof version !== "string" || !VERSION.test(version) ||
          typeof os !== "string" || !OS.has(os)) {
        return new Response("Bad request", { status: 400 });
      }
      await counter(env).ping(id, version, os);
      return new Response(null, { status: 204 });
    }
    if (url.pathname === "/report" && request.method === "GET" && (await authorized(request, env))) {
      return Response.json(await counter(env).report(), { headers: { "Cache-Control": "no-store" } });
    }
    return new Response("Not found", { status: 404 });
  },

  async scheduled(_controller: ScheduledController, env: Env): Promise<void> {
    await counter(env).trim();
  },
};

/** The report token, compared in constant time. No token set: nobody may read the report. */
async function authorized(request: Request, env: Env): Promise<boolean> {
  const expected = env.REPORT_TOKEN;
  const given = request.headers.get("Authorization")?.replace(/^Bearer /, "") ?? "";
  if (!expected || !given) return false;
  const hash = async (s: string) => new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s)));
  const [a, b] = await Promise.all([hash(expected), hash(given)]);
  return crypto.subtle.timingSafeEqual(a, b);
}

export class Counter extends DurableObject<Env> {
  private sql: SqlStorage;

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    this.sql = ctx.storage.sql;
    this.sql.exec(`
      CREATE TABLE IF NOT EXISTS installs (id TEXT PRIMARY KEY, first_day TEXT NOT NULL, os TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS pings (
        day TEXT NOT NULL, id TEXT NOT NULL, version TEXT NOT NULL, os TEXT NOT NULL, PRIMARY KEY (day, id)
      );`);
  }

  ping(id: string, version: string, os: string): void {
    const today = day();
    this.sql.exec("INSERT OR IGNORE INTO installs (id, first_day, os) VALUES (?, ?, ?)", id, today, os);
    this.sql.exec("INSERT OR IGNORE INTO pings (day, id, version, os) VALUES (?, ?, ?, ?)", today, id, version, os);
  }

  trim(): void {
    this.sql.exec("DELETE FROM pings WHERE day < ?", day(KEEP_DAYS));
  }

  report() {
    const count = (query: string, ...args: string[]) => Number(this.sql.exec(query, ...args).one().n);
    const rows = (query: string, ...args: string[]) => this.sql.exec(query, ...args).toArray();
    // "Last N days" includes today: from the day N-1 days ago.
    const since = (n: number) => day(n - 1);
    return {
      today: day(),
      installs: {
        total: count("SELECT COUNT(*) AS n FROM installs"),
        last7: count("SELECT COUNT(*) AS n FROM installs WHERE first_day >= ?", since(7)),
        last30: count("SELECT COUNT(*) AS n FROM installs WHERE first_day >= ?", since(30)),
      },
      active: {
        today: count("SELECT COUNT(*) AS n FROM pings WHERE day = ?", day()),
        last7: count("SELECT COUNT(DISTINCT id) AS n FROM pings WHERE day >= ?", since(7)),
        last30: count("SELECT COUNT(DISTINCT id) AS n FROM pings WHERE day >= ?", since(30)),
      },
      byOs30: rows("SELECT os, COUNT(DISTINCT id) AS n FROM pings WHERE day >= ? GROUP BY os ORDER BY n DESC", since(30)),
      byVersion7: rows(
        "SELECT version, COUNT(DISTINCT id) AS n FROM pings WHERE day >= ? GROUP BY version ORDER BY version DESC LIMIT 10",
        since(7),
      ),
      daily: rows("SELECT day, COUNT(*) AS n FROM pings WHERE day >= ? GROUP BY day ORDER BY day", since(30)),
    };
  }
}
