"""Google's results for the orchestrator's `look_up`, read in the phone's own Chrome.

Every query is also searched on Google, with the user's own Chrome (their cookies, consent choices, language and
location): the AI Overview when Google makes one, an answer card (a time, a score), and the top results. It
runs beside the home app's search (home.py) and takes under a second, less than Gmail's second or two.

How, measured on a test phone:
- Each look-up opens a hidden tab in Chrome (DevTools `Target.createTarget` with `hidden`): it is in no tab
  strip, never comes on screen, never becomes the tab the user sees when they open Chrome, and goes when this
  connection closes. A normal tab would not do: Android Chrome brings every new tab to the front, and a tab
  made current while Chrome is in the background cannot be put back over DevTools.
- The tab loads google.com/robots.txt (tiny, and on google.com), then fetches each results page from there and
  parses it without showing it (google_results.js): about 0.2 s to open and 0.5 s for the searches together.
  When Google answers with its JavaScript check instead (now and then, a fetched page cannot pass it), the tab
  loads one real results page, whose script passes it (about 0.75 s), and those queries are fetched again.
  A card Google draws by script (the weather, a score) is only its title in a fetched page: that query's page
  is loaded in the tab and read as shown (about 1 s more, for that query only).
- A sleeping phone is in deep Doze (sleep.py), which cuts apps off the network, and Android's cached-app
  freezer stops Chrome's processes, so DevTools does not answer. Each look-up first unfreezes them and gives
  Chrome a short network pass (`deviceidle tempwhitelist`), without waking the screen. Android freezes them
  again about ten seconds later on its own.
- bridge/chrome.py reads only `page` targets, and a hidden tab is not one, so the phone agent never sees it.
"""

import asyncio
import contextlib
import json
import logging
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote_plus, urlparse

import httpx
import websockets
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import core

router = APIRouter()
log = logging.getLogger("gateway.web")

CHROME = "com.android.chrome"
SOCKET = "chrome_devtools_remote"
TAB_URL = "https://www.google.com/robots.txt"
NEW_TAB = "chrome://newtab/"
READER = (Path(__file__).resolve().parent / "google_results.js").read_text(encoding="utf-8").strip().rstrip(";")
RESULTS = 5  # results kept per query
NETWORK_PASS_MS = 30_000
LOAD_SECONDS = 6  # the first connection out of Doze can take a few seconds
SEARCH_SECONDS = 10
SHOW_SECONDS = 5  # a results page loaded in the hidden tab (Google's JavaScript check, a card drawn by script)
CARD_SECONDS = 2.5  # for a card drawn by script to appear once the results are in
SHOWN_CARDS = 2  # queries whose card is read from the page as shown, one after another

ports: dict[str, int] = {}  # slot -> the local port forwarded to Chrome's DevTools socket
forwarding = defaultdict(asyncio.Lock)  # per slot: Google and Play (play.py) are searched at the same time
opening = defaultdict(asyncio.Lock)  # per slot: one of the two opens Chrome's first tab, the other finds it open

# Chrome's browser up, as each wake leaves it (sleep.wake). Chrome started by a push message (Firebase, after the
# phone restarts) runs without its browser, and then has no DevTools socket: seen 2026-11-04, a look-up's Google
# search failed and its answer took a minute and a half in Chrome on screen instead. Opening Chrome's window
# starts the browser and the socket; Home then puts back the home screen, which sleep leaves in front. One adb
# round trip; when the socket is listening already (nearly always), it only reads /proc/net/unix.
BROWSER_UP = (
    "up() { grep -qE ' 00010000 0001 01 +[0-9]+ @" + SOCKET + "$' /proc/net/unix; }; "
    "if up; then echo up; exit; fi; "
    f"am start -W -n {CHROME}/com.google.android.apps.chrome.Main >/dev/null 2>&1; "
    "n=0; while ! up && [ $n -lt 50 ]; do sleep 0.1; n=$((n+1)); done; "
    "input keyevent KEYCODE_HOME; "
    "if up; then echo started; else echo failed; fi"
)


async def ensure_browser(slot):
    """Chrome's browser running with its DevTools socket, started if it is not: "up", "started" or "failed"."""
    try:
        said = (await core.adb(slot, "shell", BROWSER_UP, timeout=15)).decode(errors="replace").strip()
    except Exception as exc:  # the phone is starting or gone: the look-up reports it
        return f"failed: {exc}"
    if said != "up":
        log.info("Chrome's browser on %s was not running: %s", slot, said)
    return said

# Unfreeze Chrome's browser, its network service and its page processes, let it use the network through Doze,
# and say whether Chrome runs. One adb round trip, about 0.2 s.
PREPARE = (
    f'for p in $(pidof {CHROME}) $(ps -A -o PID,NAME | grep {CHROME}: | awk "{{print \\$1}}"); do '
    "am unfreeze $p >/dev/null 2>&1; done; "
    f"cmd deviceidle tempwhitelist -d {NETWORK_PASS_MS} {CHROME} >/dev/null 2>&1; "
    f"echo pid=$(pidof {CHROME})"
)


class WebUnavailable(Exception):
    pass


class SearchRequest(BaseModel):
    queries: list[str] = Field(min_length=1, max_length=6)


def chrome_running(output):
    """Whether PREPARE found Chrome running."""
    return any(line.startswith("pid=") and line[4:].strip() for line in output.splitlines())


def found_lines(answer):
    """One line per thing Google showed, the overview first:
    `Overview Top French-door fridges keep…`, `Answer 3:26 pm Tuesday…`, `Result "Best Fridges of 2026" CNET · <url> · Oct 6, 2026 — …`."""
    lines = []
    if answer.get("overview"):
        lines.append(f"Overview {answer['overview']}")
    if answer.get("answer"):
        lines.append(f"Answer {answer['answer']}")
    for r in answer.get("results") or []:
        line = f'Result "{r.get("title", "")}" {r.get("source", "")} · {r.get("url", "")}'
        if snippet := r.get("snippet"):
            line += f" · {snippet}"
        lines.append(line)
    return "\n".join(lines)


class Browser:
    """One DevTools connection to Chrome's browser; commands to a tab go through its session."""

    def __init__(self, ws):
        self.ws = ws
        self.ids = 0

    async def call(self, method, params=None, session=None, timeout=5):
        self.ids += 1
        n = self.ids
        message = {"id": n, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        await self.ws.send(json.dumps(message))
        deadline = time.monotonic() + timeout
        while True:  # events in between are skipped
            reply = json.loads(await asyncio.wait_for(self.ws.recv(), max(0.1, deadline - time.monotonic())))
            if reply.get("id") == n:
                if "error" in reply:
                    raise WebUnavailable(f"Chrome refused {method}: {reply['error'].get('message', '')[:120]}")
                return reply.get("result", {})

    async def evaluate(self, session, expression, timeout=5):
        reply = await self.call("Runtime.evaluate", {"expression": expression, "awaitPromise": True,
                                                     "returnByValue": True}, session=session, timeout=timeout)
        if "exceptionDetails" in reply:
            raise WebUnavailable(f"the page script failed: {reply['exceptionDetails'].get('text', '')[:120]}")
        return reply.get("result", {}).get("value")


async def browser_path(slot):
    """The browser's DevTools websocket path, forwarding the socket first or again when Chrome restarted."""
    async with forwarding[slot]:
        for attempt in range(2):
            if slot not in ports:
                out = (await core.adb(slot, "forward", "tcp:0", f"localabstract:{SOCKET}", timeout=8)).decode().strip()
                if not out.isdigit():
                    raise WebUnavailable(f"adb forward returned {out[:80]!r}")
                ports[slot] = int(out)
            try:
                async with httpx.AsyncClient(timeout=3) as client:
                    version = (await client.get(f"http://127.0.0.1:{ports[slot]}/json/version")).json()
                return urlparse(version["webSocketDebuggerUrl"]).path
            except (httpx.HTTPError, ValueError, KeyError):
                port = ports.pop(slot)
                with contextlib.suppress(Exception):
                    await core.command_result("-s", core.slots[slot], "forward", "--remove", f"tcp:{port}", timeout=5)
                if attempt:
                    raise WebUnavailable("Chrome's DevTools socket did not answer") from None


def no_tab(exc):
    """Chrome refused a hidden tab because it has no tab loaded."""
    return "Hidden target" in str(exc)


async def hidden_tab(slot, chrome, url):
    """A hidden tab on `url`, by its target id. Chrome makes one only while it has a tab loaded: with none (the
    user closed them all, or Chrome was started in the background and has loaded none yet) it refuses, "Hidden
    target can be created only when remote debugging is enabled". Then one new-tab page is opened in the
    background (0.2 s on the user's phone, measured 2026-11-20; Chrome stays where it is and nothing comes on
    screen) and the hidden tab is asked for again. The new-tab page stays, so later look-ups find a tab."""
    asked = {"url": url, "background": True, "hidden": True}
    try:
        return (await chrome.call("Target.createTarget", asked))["targetId"]
    except WebUnavailable as exc:
        if not no_tab(exc):
            raise
    async with opening[slot]:
        try:  # the other search may have opened the tab while this one waited
            return (await chrome.call("Target.createTarget", asked))["targetId"]
        except WebUnavailable as exc:
            if not no_tab(exc):
                raise
        log.info("Chrome on %s had no tab loaded: opening a new-tab page so it makes hidden tabs.", slot)
        await chrome.call("Target.createTarget", {"url": NEW_TAB, "background": True})
        return (await chrome.call("Target.createTarget", asked))["targetId"]


async def show(chrome, session, query):
    """Load the query's results page in the hidden tab, scripts and all, until its results are there. That passes
    Google's JavaScript check, which a fetched page never runs (the pass stays in Chrome's cookies), and draws the
    cards Google draws by script (the weather). About 0.75 s."""
    await chrome.call("Page.navigate", {"url": f"https://www.google.com/search?q={quote_plus(query)}"}, session=session)
    deadline = time.monotonic() + SHOW_SECONDS
    while time.monotonic() < deadline:
        with contextlib.suppress(WebUnavailable, TimeoutError):  # between documents nothing answers
            if await chrome.evaluate(session, "!!document.querySelector('#rso')", timeout=1):
                return True
        await asyncio.sleep(0.1)
    return False


async def read_shown(chrome, session, query):
    """The query's answer read from its page as shown, once the card drawn by script is in (up to CARD_SECONDS)."""
    if not await show(chrome, session, query):
        return None
    answer, deadline = None, time.monotonic() + CARD_SECONDS
    while time.monotonic() < deadline:
        with contextlib.suppress(WebUnavailable, TimeoutError):
            [answer] = await chrome.evaluate(session, f"({READER})(null, {RESULTS}, true)", timeout=3) or [None]
            if isinstance(answer, dict) and answer.get("answer"):
                return answer
        await asyncio.sleep(0.2)
    return answer


async def google(slot, queries):
    """[{"query", "found"} or {"query", "error"}] for each query, from Google in a hidden tab of the phone's Chrome."""
    if not chrome_running((await core.adb(slot, "shell", PREPARE, timeout=10)).decode(errors="replace")):
        raise WebUnavailable("Chrome is not running")
    path = await browser_path(slot)
    async with websockets.connect(f"ws://127.0.0.1:{ports[slot]}{path}", max_size=64 * 1024 * 1024,
                                  open_timeout=3, ping_interval=None) as ws:
        chrome = Browser(ws)
        target = await hidden_tab(slot, chrome, TAB_URL)
        try:
            session = (await chrome.call("Target.attachToTarget", {"targetId": target, "flatten": True}))["sessionId"]
            deadline = time.monotonic() + LOAD_SECONDS
            while await chrome.evaluate(session, "location.host+' '+document.readyState") != "www.google.com complete":
                if time.monotonic() > deadline:
                    raise WebUnavailable("google.com did not load")
                await asyncio.sleep(0.05)
            answers = await chrome.evaluate(session, f"({READER})({json.dumps(queries)}, {RESULTS})",
                                            timeout=SEARCH_SECONDS) or []
            checked = [n for n, a in enumerate(answers) if isinstance(a, dict) and a.get("check")]
            if checked:
                await show(chrome, session, queries[checked[0]])
                again = await chrome.evaluate(session, f"({READER})({json.dumps([queries[n] for n in checked])}, {RESULTS})",
                                              timeout=SEARCH_SECONDS) or []
                for n, answer in zip(checked, again):
                    answers[n] = answer
            # A card Google draws by script (the weather) is only its title in a fetched page: read it as shown.
            for n in [n for n, a in enumerate(answers) if isinstance(a, dict) and a.get("drawn")][:SHOWN_CARDS]:
                if (shown := await read_shown(chrome, session, queries[n])) and not shown.get("error"):
                    answers[n] = shown
        finally:
            with contextlib.suppress(Exception):
                await chrome.call("Target.closeTarget", {"targetId": target}, timeout=2)
    out = []
    for query, answer in zip(queries, answers):
        if isinstance(answer, dict) and answer.get("check"):
            out.append({"query": query, "error": "Google's JavaScript check did not pass"})
        elif not isinstance(answer, dict) or answer.get("error"):
            out.append({"query": query, "error": str((answer or {}).get("error") or "no answer")[:200]})
        else:
            out.append({"query": query, "found": found_lines(answer)})
    return out


@router.post("/devices/{slot}/web/search")
async def search(slot: str, body: SearchRequest):
    """Each query searched on Google in the phone's Chrome: {"results": [{"query", "found"} or {"query", "error"}]}.
    503 when Chrome cannot be reached. Read-only: no lease, nothing on screen."""
    core.require_slot(slot)
    queries = [q.strip()[:200] for q in body.queries if q.strip()]
    if not queries:
        raise HTTPException(422, "Give at least one query.")
    try:
        answers = await asyncio.wait_for(google(slot, queries), 35)
    except (WebUnavailable, OSError, TimeoutError, websockets.WebSocketException, httpx.HTTPError) as exc:
        detail = str(exc) if isinstance(exc, WebUnavailable) else f"Chrome did not answer ({type(exc).__name__})"
        raise HTTPException(503, f"Google could not be searched: {detail}.") from None
    return {"results": core.scrub(answers, core.redactions.get(slot, set()))}
