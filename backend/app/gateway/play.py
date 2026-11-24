"""Google Play's apps for the orchestrator's `look_up` (`apps: true`): which app does this, and has the user got it.

Each query is searched on Google Play's own website, in a hidden tab of the phone's Chrome (as web.py does for
Google: nothing comes on screen, and the Play Store app is never opened). Every app found is then marked with what
the phone knows about the user:

- on this phone: `pm list packages`.
- in the user's Play library: every app their Google account ever installed, on any of their devices (this
  phone included: an app penrose installed here and the user later removed is in it too), with the month it was
  first installed. The Play Store keeps that list on the phone (library.db, `ownership`); it is read as root
  through app_data.py (a copy, taken again only when the Play Store rewrote it). It says the user has had the
  app, not which of their devices has it today: Play shows that only inside the Play Store app.

A query that is a package ID ("com.dd.doordash") reads that app's own page instead: what it says it does.

Measured on a test phone: 1.3 s for a look-up's queries together (0.4 s to open the tab, 0.7 s
for the searches), 2.5 s the first time, when the library is copied. What Play's pages need:
- The tab loads play.google.com/robots.txt (tiny) and fetches each page from there. Play sends a browser it has not
  seen lately through accounts.google.com and back, which a fetch cannot follow ("Failed to fetch"): the tab then
  loads one real results page, which passes it (Chrome keeps the pass in its cookies), and those queries are
  fetched again.
- An app's page is only ever fetched: loaded in a tab, it redirects to the Play Store app.
- Play's pages parse HTML only through a Trusted Types policy (play_results.js makes one).
"""

import asyncio
import contextlib
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus

import httpx
import websockets
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import app_data, core, web

router = APIRouter()
log = logging.getLogger("gateway.play")

STORE = "com.android.vending"
TAB_URL = "https://play.google.com/robots.txt"
SEARCH_URL = "https://play.google.com/store/search?c=apps&q="
READER = (Path(__file__).resolve().parent / "play_results.js").read_text(encoding="utf-8").strip().rstrip(";")
LISTED = 30  # apps read per query (Play shows about twenty)
KEPT = 5  # of Play's results kept per query, in its order; the apps the user has are kept wherever they rank
LOAD_SECONDS = 6
SEARCH_SECONDS = 12
PASS_SECONDS = 8  # for a results page to load through Google's sign-in and back
# Every app the account ever installed: doc_type 1 is an app, purchase_time when it was first installed (the same
# app's licence rows carry 0).
LIBRARY = "SELECT doc_id, MIN(NULLIF(purchase_time, 0)) FROM ownership WHERE doc_type = 1 GROUP BY doc_id"


class SearchRequest(BaseModel):
    queries: list[str] = Field(min_length=1, max_length=6)


async def library(slot):
    """({package: when it was first installed, in ms, or None}, None) for every app the phone's Google account ever
    installed; (None, why) when the Play Store's list cannot be read (no root, no account signed in yet)."""
    try:
        answer = await app_data.query(slot, app_data.Query(package=STORE, file=r"library\.db", queries={"apps": [LIBRARY, []]}))
    except HTTPException as exc:
        return None, str(exc.detail)[:160]
    except (OSError, TimeoutError) as exc:
        return None, type(exc).__name__
    if not answer.get("ok"):
        return None, str(answer.get("error") or "not found")[:160]
    apps = answer["results"]["apps"]
    if apps.get("error") or not apps.get("rows"):
        return None, str(apps.get("error") or "the Play Store has no library yet")[:160]
    return {package: since for package, since in apps["rows"]}, None


async def installed(slot):
    """The packages on this phone, or None when the phone did not say."""
    try:
        out = (await core.adb(slot, "shell", "pm list packages", timeout=10)).decode(errors="replace")
    except (HTTPException, OSError, TimeoutError):
        return None
    return {line.removeprefix("package:").strip() for line in out.splitlines() if line.startswith("package:")} or None


def theirs(package, on_phone, owned):
    """Whether the user has this app on the phone or has had it on any of their devices."""
    return package in (on_phone or ()) or package in (owned or ())


def standing(package, on_phone, owned):
    """What the user has to do with an app: "on this phone", "in the user's Play library since May 2024, not on
    this phone", "never installed by the user"; less when the phone or the library could not be read."""
    if package in (on_phone or ()):
        return "on this phone"
    if owned is None:
        return "not on this phone" if on_phone is not None else ""
    if package in owned:
        since = owned[package]
        when = f" since {datetime.fromtimestamp(since / 1000, timezone.utc):%b %Y}" if since else ""
        return f"in the user's Play library{when}, not on this phone"
    return "never installed by the user"


def app_line(app, on_phone, owned):
    """`Play app "DoorDash - Food Delivery" DoorDash · com.dd.doordash · 4.8★ · on this phone · Get your…`"""
    line = f'Play app "{app.get("title", "")}"'
    if app.get("developer"):
        line += f" {app['developer']}"
    line += f" · {app.get('package', '')}"
    if app.get("rating"):
        line += f" · {app['rating']}★"
    if where := standing(app.get("package", ""), on_phone, owned):
        line += f" · {where}"
    if text := app.get("about") or app.get("summary"):
        line += f" · {text}"
    return line


def found_lines(query, answer, on_phone, owned, unread=None):
    """One line per app, in Play's order: its first KEPT results and every app the user has or had, then the
    summary row. For a package ID, that app's line with what its page says it does."""
    if "app" in answer:
        return app_line(answer["app"], on_phone, owned)
    apps = answer.get("apps") or []
    if not apps:
        return ""
    lines = [app_line(app, on_phone, owned) for n, app in enumerate(apps)
             if n < KEPT or theirs(app.get("package", ""), on_phone, owned)]
    summary = f'Play "{query}" search Google Play · {len(apps)} app{"" if len(apps) == 1 else "s"}'
    if owned is None:
        summary += f" · the user's Play library could not be read ({unread or 'no answer'})"
    else:
        summary += f" · {sum(theirs(a.get('package', ''), on_phone, owned) for a in apps)} the user has or had"
    return "\n".join([*lines, summary])


async def loaded(chrome, session, seconds, path):
    """Wait for the tab to show a play.google.com page under `path`, fully loaded."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        with contextlib.suppress(web.WebUnavailable, TimeoutError):  # between documents nothing answers
            state = await chrome.evaluate(session, "location.host+location.pathname+' '+document.readyState", timeout=1)
            if state and state.startswith(f"play.google.com{path}") and state.endswith(" complete"):
                return True
        await asyncio.sleep(0.05)
    return False


async def read(slot, queries):
    """What play_results.js returns for each query, from a hidden tab of the phone's Chrome."""
    if not web.chrome_running((await core.adb(slot, "shell", web.PREPARE, timeout=10)).decode(errors="replace")):
        raise web.WebUnavailable("Chrome is not running")
    path = await web.browser_path(slot)
    async with websockets.connect(f"ws://127.0.0.1:{web.ports[slot]}{path}", max_size=64 * 1024 * 1024,
                                  open_timeout=3, ping_interval=None) as ws:
        chrome = web.Browser(ws)
        target = await web.hidden_tab(slot, chrome, TAB_URL)
        try:
            session = (await chrome.call("Target.attachToTarget", {"targetId": target, "flatten": True}))["sessionId"]
            if not await loaded(chrome, session, LOAD_SECONDS, "/robots.txt"):
                raise web.WebUnavailable("play.google.com did not load")
            answers = list(await chrome.evaluate(session, f"({READER})({json.dumps(queries)}, {LISTED})",
                                                 timeout=SEARCH_SECONDS) or [])
            bounced = [n for n, a in enumerate(answers) if isinstance(a, dict) and a.get("bounce")]
            if bounced:
                log.info("Play sent %s through its sign-in: loading one results page", slot)
                await chrome.call("Page.navigate", {"url": SEARCH_URL + quote_plus(queries[bounced[0]])}, session=session)
                if await loaded(chrome, session, PASS_SECONDS, "/store/search"):
                    again = await chrome.evaluate(session, f"({READER})({json.dumps([queries[n] for n in bounced])}, {LISTED})",
                                                  timeout=SEARCH_SECONDS) or []
                    for n, answer in zip(bounced, again):
                        answers[n] = answer
        finally:
            with contextlib.suppress(Exception):
                await chrome.call("Target.closeTarget", {"targetId": target}, timeout=2)
    return answers + [None] * (len(queries) - len(answers))


async def apps(slot, queries):
    """[{"query", "found"} or {"query", "error"}] for each query: Play's apps for it, marked with the ones the
    user has. Play, the library and the phone's own apps are read at the same time."""
    answers, (owned, unread), on_phone = await asyncio.gather(read(slot, queries), library(slot), installed(slot))
    out = []
    for query, answer in zip(queries, answers):
        if isinstance(answer, dict) and answer.get("bounce"):
            out.append({"query": query, "error": "Google Play's sign-in check did not pass"})
        elif not isinstance(answer, dict) or answer.get("error"):
            out.append({"query": query, "error": str((answer or {}).get("error") or "no answer")[:200]})
        else:
            out.append({"query": query, "found": found_lines(query, answer, on_phone, owned, unread)})
    return out


@router.post("/devices/{slot}/play/search")
async def search(slot: str, body: SearchRequest):
    """Each query searched on Google Play in the phone's Chrome, its apps marked with the ones the user has:
    {"results": [{"query", "found"} or {"query", "error"}]}. 503 when Chrome cannot be reached. Read-only: no
    lease, nothing on screen, nothing installed."""
    core.require_slot(slot)
    queries = [q.strip()[:200] for q in body.queries if q.strip()]
    if not queries:
        raise HTTPException(422, "Give at least one query.")
    try:
        answers = await asyncio.wait_for(apps(slot, queries), 35)
    except (web.WebUnavailable, OSError, TimeoutError, websockets.WebSocketException, httpx.HTTPError) as exc:
        detail = str(exc) if isinstance(exc, web.WebUnavailable) else f"Chrome did not answer ({type(exc).__name__})"
        raise HTTPException(503, f"Google Play could not be searched: {detail}.") from None
    return {"results": core.scrub(answers, core.redactions.get(slot, set()))}
