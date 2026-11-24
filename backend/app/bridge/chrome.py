"""Pages shown by Chrome itself (tabs and Custom Tabs). Chrome draws them in its own compositor
view, not an android.webkit.WebView, so the bridge inside Chrome sees one opaque "Web View" box
and Web.java cannot reach the page. Chrome's DevTools socket (@chrome_devtools_remote, open while
adb is enabled) can: this runs the bridge's own page script (web.js) in the visible tab with
Runtime.evaluate and grafts its answer under that box, in the same node format Web.java produces
for WebViews, so the phone agent reads and acts on Chrome pages exactly like WebView pages.

Handles of Chrome DOM elements are strings ("cdp:<epoch>:<id>"; "cdp:root" is the page itself),
so XposedDevice.call routes their commands here instead of to the bridge. A tap is a trusted touch
dispatched through DevTools (Input.dispatchTouchEvent); if the page saw no input event at all, a
script click is used instead, the same rule as Web.java.

A cross-origin frame (a payment gateway's checkout sheet, an embedded map) is blank to the page's own
script, but DevTools runs the same script inside the frame's own execution context (Runtime.enable lists
one per frame of the tab), so its nodes hang under the frame's box as if the page had drawn them, with
handles "cdp:<epoch>:<id>:<frame>". A frame Chrome runs in another process is not among those contexts, and
Chrome does that more often than it seems: on Android it isolates every site the user has typed a password
into, among others, so on a shop the user is signed in to, the payment gateway's sheet is such a frame.
DevTools reaches it as a session of the tab's own connection (Target.setAutoAttach, `Tab.remote`), with its
own frame tree and contexts, and the same script runs there. Frames inside frames (a card field inside a
checkout sheet) are read the same way, each from the document that holds it, a few levels deep. A touch
needs none of this: sent to the page, Chrome passes it to whichever frame is under the point.

What a tap sets off, the page itself cannot tell: a link or a form that asks the server for the next page leaves
the old page on screen, answering no more questions, until that page arrives, and a button that sends a request
(add to cart) looks the same before and after until the answer comes. Chrome tells both. Its Page events say
when the tab has asked for another document and when that document arrives (`Tab.pending`): until then the tab is
read as "the next page has not arrived yet", never as a failed tap or an empty page. Its Network events, switched
on for a moment around each tap, say which requests the page sent in answer and whether they were answered
(`Tab.waiting`). Both reach the phone agent as the screen still loading (screen.loading), which waits a little
and then says what is outstanding, so the same tap is not sent twice.

What can be tapped is asked of Chrome too: the page script takes Chrome's own list of an element's listeners
(getEventListeners, there only for DevTools) and counts an element the page listens to for clicks as a control,
whatever its tag (web.js heardControl). A WebView read by Web.java has no such list and goes by markup.

Cookie banners: every tab this attaches to gets autoconsent.js (DuckDuckGo autoconsent, set to reject)
registered to run at the start of each new document, so on the pages that tab loads next the site's
consent popup is answered before it shows. The page already open when the tab is first attached gets
it once at the next look. The registration lasts as long as this connection to the tab.

Chrome reads its command line (--disable-fre) and opens the socket only when set up for it:
provision/adbui.py chrome_debug_setup does that at boot.
"""

import asyncio
import contextlib
import itertools
import json
import time
from pathlib import Path

import httpx
import websockets

WEB_JS = Path(__file__).resolve().parent / "web.js"
SCRIPT_VERSION = "9"  # Web.SCRIPT_VERSION: both install window.__relay under this version
CONSENT_JS = Path(__file__).resolve().parent / "autoconsent.js"  # a build of @duckduckgo/autoconsent, see NOTICE
REJECT_COOKIE_BANNERS = True
FRAME_DEPTH = 4  # frames inside frames that are read: a frame of the page, a frame inside that one, and so on
FRAMES_KEPT = 48  # frames whose handles still work: those of the last looks
CHROME = "com.android.chrome"
SOCKET = "chrome_devtools_remote"
EVAL_MS = 2500
LOAD_WAIT = 1.5  # seconds a look waits for a page that is still loading its document
ARRIVE_WAIT = 1.0  # seconds a look waits for a page the tab has asked for, before reading it as not yet arrived
REQUEST_WINDOW = 0.5  # seconds after a tap reached the page in which a request the page sends counts as the tap's
REQUEST_WATCH = 20.0  # seconds a tap's requests are followed; one unanswered for longer is the page's own business
PENDING_TRUST = 15.0  # seconds a tab waiting for its next page is taken to be still in front before the others are asked
UNSTARTED = 2.0  # seconds after which a page the tab asked for and never started loading is no longer awaited
REQUEST_KINDS = {"XHR", "Fetch"}  # what a script sends; a document is a navigation, a picture or a ping is neither
# The page script's dump, with Chrome's list of listeners handed in (there only when DevTools asks for it).
DUMP = "dump({{scope:{scope!r},listeners:typeof getEventListeners==='function'?getEventListeners:null}})"
MAX_TABS = 5  # tabs kept open; older ones are closed whenever Chrome is on screen (nothing else closes them)
# Bridge commands that act on one DOM element (Server.WEB_COMMANDS).
WEB_COMMANDS = {"click", "long_click", "set_text", "select", "scroll", "ime_action", "focus", "web_page"}


class ChromeError(RuntimeError):
    pass


class Moved(ChromeError):
    """The tab asked for another page while a question to the old one was waiting for its answer."""


def is_page_view(node):
    """Chrome's content view: the box the page is drawn into (class names are obfuscated; its
    accessibility description is not)."""
    return node.get("desc") == "Web View" and not node.get("c")


def site(url):
    """The site a URL belongs to, roughly its registrable domain: the last two labels of its host, three when the
    last two are a country's second level (co.uk, com.au). A store and its api. or checkout. host are one site;
    the analytics services a page also posts to are others."""
    host = url.split("//", 1)[-1].split("/", 1)[0].split("@")[-1].split(":")[0].lower()
    labels = host.split(".")
    if len(labels) >= 3 and len(labels[-1]) == 2 and len(labels[-2]) <= 3:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def short_url(url, limit=70, host=None):
    """A URL as the agent is told it: host and path, no scheme, no query; the path alone when it is on the same
    host as the page at `host`."""
    text = url.split("//", 1)[-1].split("?", 1)[0].split("#", 1)[0]
    if host and "/" in text and text.split("/", 1)[0] == host.split("//", 1)[-1].split("/", 1)[0]:
        text = "/" + text.split("/", 1)[1]
    return text if len(text) <= limit else text[:limit - 1] + "…"


class RemoteFrame:
    """A frame of the tab that Chrome runs in another process: DevTools attaches to it as a session of the
    tab's connection. `parent` is the session of the document that holds it (None: the tab's own page) and
    `under` that document's frame id, when Chrome says; `n` numbers it for handles."""

    def __init__(self, session, frame, url, parent, under, n):
        self.session, self.frame, self.url, self.parent, self.under, self.n = session, frame, url, parent, under, n
        self.contexts: dict[str, int] = {}  # frame id -> execution context, for the frames of its own process


class Tab:
    """One DevTools connection to one page target."""

    def __init__(self, target_id, ws):
        self.target_id, self.ws = target_id, ws
        self.ids = itertools.count(1)
        self.lock = asyncio.Lock()
        self.contexts: dict[str, int] = {}  # frame id -> its document's main execution context, once frames are watched
        self.remote: dict[str, RemoteFrame] = {}  # session id -> a frame in another process, once frames are watched
        self.remotes = itertools.count(1)
        self.watched = set()  # the sessions (None: the page itself) whose frames Chrome reports
        self.consent_ran = False  # the cookie-banner script has run in the document that was open at attach
        self.frame = None  # the id of the tab's main frame, once Chrome has named it
        # {"url", "since", "started"}: the tab has asked for another document that has not arrived yet. `started`
        # once Chrome is loading it: a link another app takes (tel:, a store link) is asked for and never loaded.
        self.pending = None
        self.moves = 0  # times the main frame asked for, or got, another document: a tap compares before and after
        self.url = self.title = ""  # the page as it was last read
        self.network = False  # Network events are on (only while a tap's requests are followed)
        self.tap = None  # the last tap: {"at": device clock (s) when the page got it, or None, "since", "requests"}

    async def send(self, method, params=None, timeout=EVAL_MS / 1000, stop=None, session=None):
        """`stop` (a callable) ends the wait early with Moved once an event made it true: a page that has started
        to leave will not answer. `session` puts the command to a frame in another process (`Tab.remote`)."""
        async with self.lock:
            msg_id = next(self.ids)
            message = {"id": msg_id, "method": method, "params": params or {}}
            if session is not None:
                message["sessionId"] = session
            await self.ws.send(json.dumps(message))
            deadline = time.monotonic() + timeout
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError(f"{method}: the page did not answer within {int(timeout * 1000)} ms")
                reply = json.loads(await asyncio.wait_for(self.ws.recv(), left))
                if reply.get("id") == msg_id:
                    if "error" in reply:
                        raise ChromeError(f"{method}: {reply['error'].get('message')}")
                    return reply.get("result", {})
                self.event(reply)  # other events and late replies are skipped
                if stop is not None and stop():
                    raise Moved(f"{method}: the tab went on to another page")

    async def listen(self, seconds=0.0, until=None):
        """Take in the events Chrome has sent since the last command (they wait on the socket until something
        reads it), then go on listening for up to `seconds` or until `until()` is true. Returns `until()`."""
        async with self.lock:
            deadline = time.monotonic() + seconds
            while until is None or not until():
                left = deadline - time.monotonic()
                try:
                    # Even with no time left, what has already arrived is read: 10 ms is enough to hand it over.
                    reply = json.loads(await asyncio.wait_for(self.ws.recv(), max(left, 0.01)))
                except TimeoutError:
                    if left <= 0:
                        break
                    continue
                self.event(reply)
        return bool(until()) if until is not None else False

    def event(self, reply):
        """What Chrome reports unasked: the frames it runs in another process coming and going (Target), which
        execution context belongs to which frame (Runtime), the main frame asking for and getting another
        document (Page), and the requests a tap set off (Network). An event of a frame in another process
        carries that frame's session."""
        method, params, session = reply.get("method"), reply.get("params") or {}, reply.get("sessionId")
        if method == "Target.attachedToTarget":
            info = params.get("targetInfo") or {}
            if info.get("type") == "iframe" and params.get("sessionId"):
                self.remote[params["sessionId"]] = RemoteFrame(
                    params["sessionId"], info.get("targetId"), info.get("url") or "", session,
                    info.get("parentFrameId"), next(self.remotes))
        elif method == "Target.detachedFromTarget":
            self.detached(params.get("sessionId"))
        elif method == "Target.targetInfoChanged":
            info = params.get("targetInfo") or {}
            for remote in self.remote.values():
                if remote.frame == info.get("targetId") and info.get("url"):
                    remote.url = info["url"]
        elif method in ("Runtime.executionContextCreated", "Runtime.executionContextDestroyed",
                        "Runtime.executionContextsCleared"):
            contexts = self.contexts_of(session)
            if contexts is None:  # of a frame that has gone since
                return
            if method == "Runtime.executionContextCreated":
                context = params.get("context") or {}
                aux = context.get("auxData") or {}
                if aux.get("isDefault") and aux.get("frameId"):
                    contexts[aux["frameId"]] = context["id"]
            elif method == "Runtime.executionContextDestroyed":
                for frame in [f for f, c in contexts.items() if c == params.get("executionContextId")]:
                    del contexts[frame]
            else:
                contexts.clear()
        elif session is not None:
            return  # the rest is about the tab's own page
        elif method in ("Page.frameRequestedNavigation", "Page.frameStartedNavigating", "Page.frameStartedLoading"):
            # A link, a form or a script asked for another document (requested: Chrome says so at once, long
            # before the server answers), or the tab started loading one for the address bar or Back (started).
            if not self.main(params.get("frameId")) or params.get("disposition", "currentTab") != "currentTab":
                return
            if params.get("navigationType") in ("sameDocument", "historySameDocument"):
                return
            if self.pending is None:
                self.pending = {"url": "", "since": time.monotonic(), "started": False}
                self.moves += 1
            if params.get("url") and not self.pending["url"]:
                self.pending["url"] = params["url"]
            if method != "Page.frameRequestedNavigation":
                self.pending["started"] = True
        elif method == "Page.frameNavigated":
            frame = params.get("frame") or {}
            if not frame.get("parentId"):  # the document arrived
                self.frame = frame.get("id") or self.frame
                self.pending = None
                self.moves += 1
        elif method == "Page.frameStoppedLoading":
            if self.main(params.get("frameId")):  # also how a load ends that brought no document (a download, a 204)
                self.pending = None
        elif method == "Network.requestWillBeSent":
            if self.tap is not None and params.get("type") in REQUEST_KINDS and len(self.tap["requests"]) < 50:
                request = params.get("request") or {}
                self.tap["requests"][params.get("requestId")] = {
                    "method": request.get("method") or "GET", "url": request.get("url") or "",
                    "at": params.get("wallTime") or 0, "answered": False}
        elif method in ("Network.responseReceived", "Network.loadingFinished", "Network.loadingFailed"):
            if self.tap is not None and (request := self.tap["requests"].get(params.get("requestId"))):
                request["answered"] = True

    def contexts_of(self, session):
        """Frame id -> execution context for the frames of one process: the page's own (`session` None) or a
        remote frame's. None when that frame is no longer attached."""
        if session is None:
            return self.contexts
        remote = self.remote.get(session)
        return remote.contexts if remote is not None else None

    def detached(self, session):
        """A frame in another process went away (closed, or gone on to another site), and with it the frames
        it held."""
        if self.remote.pop(session, None) is None:
            return
        self.watched.discard(session)
        for inner in [r.session for r in self.remote.values() if r.parent == session]:
            self.detached(inner)

    def main(self, frame_id):
        """Is `frame_id` the tab's main frame? Unknown until Chrome names it: then an event is taken as the main
        frame's only when it says so itself."""
        return frame_id is not None and frame_id == self.frame

    async def enable_page(self):
        """Once per connection: Chrome reports the tab's navigations, and runs new-document scripts."""
        await self.send("Page.enable")
        with contextlib.suppress(ChromeError, TimeoutError, KeyError):
            self.frame = (await self.send("Page.getFrameTree"))["frameTree"]["frame"]["id"]

    async def watch_frames(self, session=None):
        """Once for the page, and once for each frame in another process that is looked into (`session`): Chrome
        then reports every context of its frames, and attaches to each frame under it that runs in yet
        another process. The existing ones are reported at once: their events come before the answer."""
        if session in self.watched:
            return
        await self.send("Runtime.enable", session=session)
        self.watched.add(session)
        attach = {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True}
        try:
            await self.send("Target.setAutoAttach", {**attach, "filter": [{"type": "iframe"}]}, session=session)
        except ChromeError:  # a Chrome that knows no filter: its workers attach too, and are not listened to
            with contextlib.suppress(ChromeError):
                await self.send("Target.setAutoAttach", attach, session=session)

    async def arrived(self, seconds):
        """Listen until the document the tab asked for has arrived, at most `seconds`. True when none is awaited
        (any more: one Chrome was asked for and did not start loading within UNSTARTED seconds went elsewhere)."""
        await self.listen(seconds, until=lambda: self.pending is None)
        if self.pending is not None and not self.pending["started"] and time.monotonic() - self.pending["since"] > UNSTARTED:
            self.pending = None
        return self.pending is None

    async def answers(self):
        """Does the page answer at all? One whose tab is waiting for the next document usually does not."""
        try:
            await self.evaluate("1", timeout=0.3)
            return True
        except (ChromeError, TimeoutError):
            return False

    async def follow_tap(self):
        """Before a tap: from here on, the requests the page sends are the tap's to answer for."""
        await self.listen()
        self.tap = {"at": None, "since": time.monotonic(), "requests": {}}
        if not self.network:
            await self.send("Network.enable")
            self.network = True

    def waiting(self):
        """The requests the page sent in answer to the last tap, to its own site, that have no answer yet:
        [{"method", "url"}]. None before the page has said when the tap reached it (which requests are the
        tap's is not known), and none once the tap is no longer followed."""
        tap = self.tap
        if tap is None or tap["at"] is None or time.monotonic() - tap["since"] > REQUEST_WATCH:
            return []
        home = site(self.url) if self.url else None
        return [{"method": r["method"], "url": r["url"]} for r in tap["requests"].values()
                if not r["answered"] and -0.1 <= r["at"] - tap["at"] <= REQUEST_WINDOW
                and (home is None or site(r["url"]) == home)]

    async def unfollow(self):
        """Stop following the last tap once the page has had its time to send requests and none is outstanding
        (or it has been too long): every request of a page is not worth hearing about otherwise."""
        tap = self.tap
        if tap is not None:
            age = time.monotonic() - tap["since"]
            if age <= REQUEST_WATCH and (age < REQUEST_WINDOW + 0.5 or self.waiting()):
                return
        self.tap = None
        if self.network:
            self.network = False
            with contextlib.suppress(ChromeError, TimeoutError, websockets.WebSocketException):
                await self.send("Network.disable")

    async def evaluate(self, expression, timeout=EVAL_MS / 1000, await_promise=False, context=None, stop=None,
                       devtools=False, session=None):
        """`expression` in the page, or in a frame: the one whose execution context is `context`, among the
        frames of the page's process or, with `session`, of that remote frame's (there, no `context` is the
        remote frame's own document). With `devtools`, the names Chrome keeps for its own console
        (getEventListeners) are there for the expression to use."""
        params = {"expression": expression, "returnByValue": True, "awaitPromise": await_promise}
        if context is not None:
            params["contextId"] = context
        if devtools:
            params["includeCommandLineAPI"] = True
        result = await self.send("Runtime.evaluate", params, timeout, stop=stop, session=session)
        if "exceptionDetails" in result:
            details = result["exceptionDetails"]
            raise ChromeError("js: " + str(details.get("exception", {}).get("description") or details.get("text"))[:300])
        return result.get("result", {}).get("value")

    async def close(self):
        with contextlib.suppress(Exception):
            await self.ws.close()


class Frame:
    """A frame read from the inside: how its document is reached (`session` of its process, None for the
    page's own; `context` in that process, None for a remote frame's own document), its frame `id`, and where
    it sits: its element's handle in the document that holds it (`owner`) and its content box's origin there,
    in that document's CSS px (`origin`, as of the last look)."""

    def __init__(self, session, context, frame_id, owner, origin):
        self.session, self.context, self.id, self.owner, self.origin = session, context, frame_id, owner, origin

    def __eq__(self, other):
        return isinstance(other, Frame) and vars(self) == vars(other)

    def __repr__(self):
        return f"Frame({self.session!r}, {self.context!r}, {self.id!r}, {self.owner!r}, {self.origin!r})"


class ChromePages:
    def __init__(self, adb):
        self.adb = adb
        self.port = None
        self.tabs: dict[str, Tab] = {}
        self.visible: Tab | None = None
        self.box = None  # the content view's screen box [l, t, r, b] from the last snapshot
        self.vv = None  # the page's visual viewport from the last dump
        self.problems = []  # (kind, detail) since the last snapshot, for the host's diagnostics
        self.seen: dict[str, float] = {}  # tab id -> when it was last the visible one
        self.frames: dict[str, Frame] = {}  # the frames read at the last looks, by the key their handles end in
        self._source = None
        self._consent = None

    def consent_source(self):
        if self._consent is None:
            self._consent = CONSENT_JS.read_text(encoding="utf-8") if REJECT_COOKIE_BANNERS and CONSENT_JS.exists() else ""
        return self._consent

    async def consent_now(self):
        """The cookie-banner script in the visible tab's current document, once per connection: documents loaded
        later run it on their own. Never fails a look."""
        tab = self.visible
        if tab is None or tab.consent_ran or not self.consent_source():
            return
        tab.consent_ran = True
        try:
            await tab.evaluate(self.consent_source() + ";0")
        except (ChromeError, TimeoutError, websockets.WebSocketException) as exc:
            self.problems.append(("cookie_script_failed", str(exc)))

    def script(self, call):
        """The same expression Web.script builds: the page's API (installed on first use), then `call`."""
        if self._source is None:
            self._source = WEB_JS.read_text(encoding="utf-8").rstrip().rstrip(";")
        return f"(window.__relay&&window.__relay.v==='{SCRIPT_VERSION}'?window.__relay:{self._source}).{call}"

    async def close(self):
        for tab in self.tabs.values():
            await tab.close()
        self.tabs.clear()
        self.frames.clear()
        self.visible = None

    # --- finding the visible tab ------------------------------------------------------------

    async def targets(self):
        for attempt in range(2):
            if self.port is None:
                out = (await self.adb.run("forward", "tcp:0", f"localabstract:{SOCKET}")).strip()
                if not out.isdigit():
                    raise ChromeError(f"adb forward returned {out!r}")
                self.port = int(out)
            try:
                async with httpx.AsyncClient(timeout=3) as client:
                    response = await client.get(f"http://127.0.0.1:{self.port}/json/list")
                return [t for t in response.json() if t.get("type") == "page"]
            except (httpx.HTTPError, ValueError) as exc:
                # Chrome restarted (new socket) or the forward went away: forward again once, without the old one.
                with contextlib.suppress(Exception):
                    await self.adb.run("forward", "--remove", f"tcp:{self.port}", timeout=5, check=False)
                self.port = None
                await self.close()
                if attempt:
                    raise ChromeError(f"Chrome's DevTools socket did not answer ({type(exc).__name__}); "
                                      "is Chrome set up for debugging (provision/adbui.py chrome_debug_setup)?") from None
        return []

    async def tab(self, target):
        tab = self.tabs.get(target["id"])
        if tab is None:
            url = f"ws://127.0.0.1:{self.port}/devtools/page/{target['id']}"
            try:
                ws = await websockets.connect(url, max_size=64 * 1024 * 1024, open_timeout=3, ping_interval=None)
            except (OSError, websockets.WebSocketException, TimeoutError) as exc:
                raise ChromeError(f"could not attach to tab {target.get('url', '')[:80]}: {exc}") from None
            tab = self.tabs[target["id"]] = Tab(target["id"], ws)
            try:  # the tab's navigations are reported, and new-document scripts run, only with the Page domain on
                await tab.enable_page()
                if self.consent_source():
                    await tab.send("Page.addScriptToEvaluateOnNewDocument", {"source": self.consent_source()})
            except (ChromeError, TimeoutError, websockets.WebSocketException) as exc:
                self.problems.append(("cookie_script_failed", str(exc)))
        return tab

    async def find_visible(self):
        """The tab on screen. DevTools lists every tab; only the one in front reports a visible document. The tab
        that was in front at the last look is asked first: still in front (the usual case), that is one round trip
        instead of the tab list and a question to each tab. A tab waiting for a page it has asked for may answer
        nothing (`Tab.pending`): it is not asked, and it stays the one in front unless another tab says it is."""
        was = self.visible
        if was is not None:
            with contextlib.suppress(ChromeError, TimeoutError, websockets.WebSocketException):
                if await self.in_front(was):
                    self.seen[was.target_id] = time.monotonic()
                    return was
                # It cannot say, and asking every other tab instead costs seconds (a tab in the background is
                # slow to answer): it was in front a moment ago, so it is taken to be still, and the others are
                # asked only now and then.
                if was.pending is not None and time.monotonic() - self.seen.get(was.target_id, 0) < PENDING_TRUST:
                    return was
        targets = await self.targets()
        live = {t["id"] for t in targets}
        for gone in [i for i in self.tabs if i not in live]:
            await self.tabs.pop(gone).close()
        for target in targets:
            try:
                tab = await self.tab(target)
                if await self.in_front(tab):
                    self.visible = tab
                    self.seen[target["id"]] = time.monotonic()
                    await self.trim(targets)
                    return tab
            except (ChromeError, TimeoutError, websockets.WebSocketException):
                with contextlib.suppress(KeyError):
                    await self.tabs.pop(target["id"]).close()
        if was is not None and was.pending is not None and self.tabs.get(was.target_id) is was:
            self.seen[was.target_id] = time.monotonic()
            return was
        self.visible = None
        return None

    async def in_front(self, tab):
        """Does `tab` say its page is the visible one? None when it cannot say: it is waiting for a page it asked
        for (the question is not put, or is dropped the moment Chrome says the tab is leaving)."""
        if tab.pending is not None:
            await tab.listen()  # the page may have arrived since
        if tab.pending is not None:
            return None
        try:
            return await tab.evaluate("document.visibilityState", timeout=1.5,
                                      stop=lambda: tab.pending is not None) == "visible"
        except Moved:
            return None

    async def trim(self, targets):
        """Close all but the MAX_TABS most recent tabs, the visible one always among them. DevTools lists
        tabs oldest first; one this process has seen in front ranks by when, so recency wins over position."""
        if len(targets) <= MAX_TABS:
            return
        visible = self.visible.target_id if self.visible else None
        ranked = sorted(enumerate(targets), key=lambda it: (it[1]["id"] == visible, self.seen.get(it[1]["id"], 0), it[0]))
        for _, target in ranked[:len(targets) - MAX_TABS]:
            try:
                async with httpx.AsyncClient(timeout=3) as client:
                    await client.get(f"http://127.0.0.1:{self.port}/json/close/{target['id']}")
            except httpx.HTTPError:
                continue
            self.seen.pop(target["id"], None)
            if tab := self.tabs.pop(target["id"], None):
                await tab.close()

    async def loaded(self, seconds=LOAD_WAIT):
        """A page still being parsed is read once its document is in (its own DOMContentLoaded), at most `seconds`:
        read sooner, it would show half a page. One that takes longer is read as it is, marked loading."""
        wait = (f"new Promise(r=>{{if(document.readyState!=='loading')return r(1);"
                f"document.addEventListener('DOMContentLoaded',()=>r(1),{{once:true}});setTimeout(()=>r(0),{int(seconds * 1000)})}})")
        with contextlib.suppress(ChromeError, TimeoutError, websockets.WebSocketException):
            await self.visible.evaluate(wait, timeout=seconds + 1, await_promise=True)

    async def call(self, expression_call, timeout=EVAL_MS / 1000, context=None, stop=None, devtools=False,
                   session=None):
        if self.visible is None:
            raise ChromeError("no Chrome tab is visible")
        try:
            return await self.visible.evaluate(self.script(expression_call), timeout, context=context, stop=stop,
                                               devtools=devtools, session=session)
        except websockets.WebSocketException:
            await self.tabs.pop(self.visible.target_id, self.visible).close()
            self.visible = None
            raise ChromeError("the tab closed") from None

    # --- the snapshot -----------------------------------------------------------------------

    async def attach(self, page_node):
        """Fill the visible tab's DOM into Chrome's content-view node, like Web.attach for a WebView.
        A failure leaves the node opaque and says why."""
        self.box = page_node.get("b")
        page_node["webview"] = True
        page_node["chrome"] = True
        page_node["h"] = "cdp:root"
        try:
            if await self.find_visible() is None:
                self.problems.append(("web_dump_failed", "no Chrome tab reports a visible page"))
                return
            tab = self.visible
            if tab.pending is not None and not await tab.arrived(ARRIVE_WAIT) and not await tab.answers():
                if self.awaited(page_node, tab):
                    return
            await self.consent_now()
            await self.loaded()
            d = await self.call(DUMP.format(scope="viewport"), devtools=True)
        except (ChromeError, TimeoutError, websockets.WebSocketException) as exc:
            tab = self.visible
            if tab is not None and self.awaited(page_node, tab):  # it asked for another page while being read
                return
            self.problems.append(("web_dump_failed", str(exc)))
            return
        if not isinstance(d, dict) or "error" in d:
            self.problems.append(("web_dump_failed", (d or {}).get("error", "the page returned nothing") if isinstance(d, dict) else "the page returned nothing"))
            return
        self.vv = d["vv"]
        to_screen = self.mapper(d)
        page_node["c"] = [self.convert(n, to_screen, d["epoch"]) for n in d.get("nodes", [])]
        page_node["url"] = d.get("url", "")
        if d.get("title"):
            page_node["title"] = d["title"][:300]
        tab.url, tab.title = page_node["url"], page_node.get("title", "")
        if d.get("ready") == "loading":
            page_node.setdefault("f", []).append("loading")
        page_node["page"] = {"scroll": d.get("scroll"), "ready": d.get("ready"), "nodes": d.get("count"),
                             "ms": d.get("ms"), "vv": d["vv"], "stats": d.get("stats"), "via": "devtools"}
        if d.get("heard"):
            page_node["page"]["heard"] = d["heard"]
        if tab.pending is not None:  # the old page still answers while the one the tab asked for is on its way
            self.awaited(page_node, tab, old_page=False)
        # What the last tap asked the site for and has no answer to yet: the page shows nothing of it until then.
        if tab.tap is not None or tab.network:
            with contextlib.suppress(ChromeError, TimeoutError, websockets.WebSocketException):
                await tab.listen()
                if waiting := tab.waiting():
                    page_node["waiting"] = [f"{r['method']} {short_url(r['url'], host=tab.url)}" for r in waiting[:3]]
                await tab.unfollow()
        for frame in await self.graft_frames(page_node["c"], to_screen, "viewport"):
            if frame is not None:
                self.problems.append(("web_blocked_frame", frame))
        if d.get("truncated"):
            self.problems.append(("web_truncated", d.get("url", "")))

    def awaited(self, page_node, tab, old_page=True):
        """The tab has asked for another page that has not arrived. The node says where the tab is going; with
        `old_page`, the page still drawn answers nothing, so the node carries its address and no content.
        False, and the node untouched, when the page has arrived after all: it comes while the caller waits (for
        the old page to answer, for a read to fail), and then there is a page to read, not one to wait for."""
        pending = tab.pending
        if pending is None:
            return False
        if old_page and tab.url:
            page_node["url"] = tab.url
        if old_page and tab.title:
            page_node["title"] = tab.title[:300]
        if "loading" not in page_node.setdefault("f", []):
            page_node["f"].append("loading")
        page_node["awaiting"] = {"url": short_url(pending["url"]) if pending["url"] else "",
                                 "seconds": round(time.monotonic() - pending["since"], 1)}
        return True

    # --- cross-origin frames ----------------------------------------------------------------

    async def frames_in(self, session=None, frame_id=None):
        """The frames under one document: the one read through `session` (None: the tab's own connection), in
        its frame `frame_id` (None: the top one there). First those of the same process, in document order,
        each with its execution context (None while it has none); then those Chrome runs in another process,
        each a session of its own. As [{"id", "url", "session", "context", "remote"}]."""
        tab = self.visible
        await tab.watch_frames(session)
        top = (await tab.send("Page.getFrameTree", session=session)).get("frameTree") or {}
        if frame_id is not None:
            top = next((t for t in subtrees(top) if t["frame"]["id"] == frame_id), {})
        below = list(subframes(top))
        ids = {f["id"] for f in below} | {(top.get("frame") or {}).get("id")}
        contexts = tab.contexts_of(session) or {}
        found = [{"id": f["id"], "url": f.get("url") or "", "session": session, "context": contexts.get(f["id"]),
                  "remote": False} for f in below]
        found += [{"id": r.frame, "url": r.url, "session": r.session, "context": None, "remote": True}
                  for r in tab.remote.values() if r.parent == session and (r.under is None or r.under in ids)]
        return found

    def frame_key(self, frame):
        """What a handle inside `frame` (one of frames_in) ends in: its context for a frame of the page's own
        process ("7"), "s<n>" for the n-th remote frame, "s<n>.<context>" for a frame of that one's process."""
        remote = self.visible.remote.get(frame["session"]) if frame["session"] is not None else None
        parts = ([f"s{remote.n}"] if remote is not None else []) + ([str(frame["context"])] if frame["context"] is not None else [])
        return ".".join(parts)

    async def graft_frames(self, nodes, to_screen, scope, texts=None, session=None, frame_id=None, depth=1):
        """Fill every blocked frame among `nodes` (converted; of the document read through `session` and
        `frame_id`, see frames_in) with the frame's own nodes, read by the script run inside the frame and
        placed by the frame's origin, and so on for the frames blocked inside those. Returns, per blocked
        frame, None when it was filled or its host when it stays blocked (a frame Chrome does not list, or
        one with no document yet). With `texts`, each frame's reading text (scope 'page') is appended to it
        as (host, text)."""
        blocked = [n for n in walk(nodes) if "blocked_frame" in n.get("f", ())]
        if not blocked:
            return []
        listed = []
        try:
            listed = await self.frames_in(session, frame_id)
        except (ChromeError, TimeoutError, websockets.WebSocketException) as exc:
            self.problems.append(("web_frame_tree_failed", str(exc)))
        left, used = [], set()
        for n in blocked:
            handle, src, origin = n.pop("h", None), n.pop("src", ""), n.pop("fo", None)
            host = (src.split("//", 1)[-1].split("/", 1)[0]) if src else n.get("desc") or "frame"
            frame = match_frame(src, listed, used)
            if frame is None or origin is None or handle is None or (frame["context"] is None and not frame["remote"]):
                left.append(host)
                continue
            used.add(frame["id"])
            where = {"context": frame["context"], "session": frame["session"]}
            try:
                d = await self.call(DUMP.format(scope=scope), devtools=True, **where)
                text = (await self.call("text(20000)", **where)) if texts is not None else None
            except (ChromeError, TimeoutError) as exc:
                self.problems.append(("web_frame_failed", f"{host}: {exc}"))
                left.append(host)
                continue
            if not isinstance(d, dict) or "error" in d:
                left.append(host)
                continue
            key = self.frame_key(frame)

            def inner(x, y, o=origin):  # the frame's CSS px, from the origin of its box in the document holding it
                return to_screen(o[0] + x, o[1] + y)

            n["c"] = [self.convert(c, inner, d["epoch"], key) for c in d.get("nodes", [])]
            n["f"] = [f for f in n["f"] if f != "blocked_frame"]
            n["frame"] = src
            self.frames.pop(key, None)  # the newest last: the oldest are the ones forgotten
            self.frames[key] = Frame(frame["session"], frame["context"], frame["id"], handle, origin)
            for old in list(self.frames)[:-FRAMES_KEPT]:
                del self.frames[old]
            if texts is not None and isinstance(text, dict) and text.get("text"):
                texts.append((host, text["text"]))
            left.append(None)
            if depth < FRAME_DEPTH:
                left += await self.graft_frames(n["c"], inner, scope, texts, frame["session"],
                                                None if frame["remote"] else frame["id"], depth + 1)
        return left

    def mapper(self, d):
        """CSS px (layout viewport) to screen px. The page fills the content view's width; its top
        sits below Chrome's toolbar, which overlaps the view's top edge, so the page is anchored at
        the view's bottom (the toolbar never covers it)."""
        left, top, right, bottom = self.box or [0, 0, 1080, 2400]
        vv = d["vv"]
        f = (right - left) / vv["w"] if vv.get("w") else 1
        page_top = bottom - vv["h"] * f

        def to_screen(x, y):
            return round(left + (x - vv["x"]) * f), round(page_top + (y - vv["y"]) * f)
        return to_screen

    def convert(self, j, to_screen, epoch, frame=None):
        """`frame`: the key of the frame the node was read in (frame_key), none for the page's own."""
        n = {k: v for k, v in j.items() if k not in ("i", "b", "c")}
        b = j["b"]
        n["b"] = [*to_screen(b[0], b[1]), *to_screen(b[2], b[3])]
        n["dom"] = n["virtual"] = True
        if "i" in j:
            n["h"] = f"cdp:{epoch}:{j['i']}" + (f":{frame}" if frame is not None else "")
        if "c" in j:
            n["c"] = [self.convert(c, to_screen, epoch, frame) for c in j["c"]]
        return n

    # --- acting -----------------------------------------------------------------------------

    def frame_of(self, h):
        """The frame `h` was read in (None: the page's own document), and whether it can still be asked: not
        once it is forgotten, or once Chrome has said its process is gone."""
        key = h.split(":")[3:]
        if not key:
            return None, True
        frame = self.frames.get(key[0])
        tab = self.visible
        return frame, frame is not None and (frame.session is None or tab is None or frame.session in tab.remote)

    async def act(self, h, action, args=None, stop=None):
        _, epoch, web_id, *_ = h.split(":")
        frame, there = self.frame_of(h)
        if not there:
            return {"ok": False, "error": "stale handle: the frame has gone"}
        call = f"act({json.dumps(epoch)},{int(web_id)},{json.dumps(action)},{json.dumps(args or {})})"
        try:
            r = await self.call(call, context=frame.context if frame else None, stop=stop,
                                session=frame.session if frame else None)
        except (ChromeError, TimeoutError) as exc:
            error = str(exc) or "the page did not answer"
            # The frame navigated or went away (its context, or its whole process): its handles are stale.
            if "Cannot find context" in error or "Session with given id not found" in error:
                error = "stale handle: the frame has gone"
            return {"ok": False, "error": error}
        return r if isinstance(r, dict) else {"ok": False, "error": "the page returned no answer (it navigated away?)"}

    async def frame_origin(self, h):
        """Where the frame holding `h` sits now, in the page's CSS px: each frame on the way up is asked of its
        element in the document that holds it, or taken where the last look saw it when that one no longer
        answers."""
        x = y = 0
        for _level in range(FRAME_DEPTH):
            frame = self.frame_of(h)[0]
            if frame is None:
                break
            r = await self.act(frame.owner, "rect")
            origin = r["fo"] if r.get("ok") and r.get("fo") else frame.origin
            x, y, h = x + origin[0], y + origin[1], frame.owner
        return (x, y)

    async def handle(self, cmd, h, args):
        if self.visible is None:
            await self.find_visible()
        if h == "cdp:root":
            if cmd == "scroll":
                r = await self.call(f"scrollPage({json.dumps(args.get('dir', 'down'))})")
                return r if isinstance(r, dict) else {"ok": False, "error": "the page returned no answer"}
            if cmd == "web_page":
                return await self.page()
            return {"ok": False, "error": f"{cmd} is not supported on the page itself"}
        if cmd in ("click", "long_click"):
            return await self.click(h, long_press=cmd == "long_click")
        if cmd in ("set_text", "select"):
            return await self.act(h, cmd, {"text": args.get("text", "")})
        if cmd == "scroll":
            return await self.act(h, "scroll", {"dir": args.get("dir", "down")})
        if cmd == "ime_action":
            return await self.act(h, "submit")
        if cmd == "focus":
            return await self.act(h, "focus")
        if cmd == "web_page":
            return await self.page()
        if cmd == "capture":
            return await self.capture(h)
        return {"ok": False, "error": f"{cmd} is not supported on web elements"}

    async def capture(self, h):
        """A picture of one element, from Chrome itself: the page says where the element is now ("box", document
        CSS px) and DevTools renders exactly that clip, also when it is below the fold, so the bounds and the
        pixels are from the same instant. Answers {"jpeg": base64, "w", "h"} like the bridge's capture."""
        if h.split(":")[3:]:  # an element of a frame read apart: DevTools clips in the main frame's coordinates only
            return {"ok": False, "error": "capture is not supported inside a frame"}
        box = await self.act(h, "box")
        if not box.get("ok"):
            return box
        if box["w"] < 1 or box["h"] < 1:
            return {"ok": False, "error": "the element has no size"}
        if box.get("src", "").startswith(("http://", "https://")):
            # The original, from the browser's own cache: no request, no CORS, full size. A miss falls through.
            try:
                await self.visible.send("Page.enable")  # getResourceContent answers only with the domain on
                tree = (await self.visible.send("Page.getFrameTree")).get("frameTree") or {}
                got = await self.visible.send("Page.getResourceContent", {"frameId": tree["frame"]["id"], "url": box["src"]}, timeout=8)
                if got.get("base64Encoded") and got.get("content"):
                    return {"ok": True, "jpeg": got["content"], "w": box.get("nw") or 0, "h": box.get("nh") or 0, "source": "cache"}
            except (ChromeError, TimeoutError, KeyError):
                pass
        try:
            shot = await self.visible.send("Page.captureScreenshot", {
                "format": "jpeg", "quality": 88, "captureBeyondViewport": True,
                "clip": {"x": box["x"], "y": box["y"], "width": box["w"], "height": box["h"], "scale": box.get("dpr") or 1},
            }, timeout=8)
        except (ChromeError, TimeoutError) as exc:
            return {"ok": False, "error": str(exc)}
        scale = box.get("dpr") or 1
        return {"ok": True, "jpeg": shot.get("data", ""), "w": round(box["w"] * scale), "h": round(box["h"] * scale)}

    async def click(self, h, long_press=False):
        """A trusted touch where the element is what a finger would hit (see Web.click). What the touch set off
        is read from Chrome, not from the page: a tab that asks for another page after it has taken the tap, and
        the old page says no more (`opening`); a page that sends requests is followed until they are answered."""
        p = await self.act(h, "prepare")
        if not p.get("ok"):
            return p
        tab = self.visible
        via = "chrome_long_press" if long_press else "chrome_tap"
        with contextlib.suppress(ChromeError, TimeoutError, websockets.WebSocketException):
            await tab.follow_tap()
        before = tab.moves

        def moved():
            return tab.moves != before

        def opening():
            return {"ok": True, "via": via, "navigated": True,
                    "opening": short_url(tab.pending["url"]) if tab.pending and tab.pending["url"] else ""}
        vv = p["vv"] if len(h.split(":")) < 4 else (self.vv or {"x": 0, "y": 0})  # a frame's point is offset into the page
        ox, oy = await self.frame_origin(h)
        x, y = ox + p["x"] - vv["x"], oy + p["y"] - vv["y"]  # DevTools wants CSS px in the visual viewport
        if p.get("scrolled"):
            await asyncio.sleep(0.18)
        point = [{"x": x, "y": y}]
        try:
            await self.visible.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": point})
            await asyncio.sleep(0.7 if long_press else 0.04)
            await self.visible.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        except (ChromeError, TimeoutError, websockets.WebSocketException) as exc:
            return await self.js_click(h, f"the touch could not be sent: {exc}")
        await asyncio.sleep(0.1 if long_press else 0.14)
        with contextlib.suppress(ChromeError, TimeoutError, websockets.WebSocketException):
            await tab.listen()
        if moved():
            return opening()
        d = await self.act(h, "delivered", stop=moved)
        if moved():
            return opening()
        if d.get("delivered"):
            if tab.tap is not None and d.get("at"):
                tab.tap["at"] = d["at"] / 1000
            return {"ok": True, "via": via, "page_saw_click": d.get("click")}
        if not d.get("ok") and "stale" in str(d.get("error")):
            # The tap navigated: the old page (and its handles) are gone, which means it landed.
            return {"ok": True, "via": via, "navigated": True}
        if not d.get("ok"):
            # The page took the touch and has not answered since (busy with what the tap started, or leaving
            # without Chrome having said so yet). Tapping it again by script could do the thing twice.
            self.problems.append(("web_tap_unanswered", str(d.get("error"))))
            return {"ok": False, "error": "the page has not answered since the tap, so it may or may not have "
                                          "happened: look at the screen before tapping again"}
        self.problems.append(("web_tap_dropped", "a DevTools touch produced no input event in the page"))
        if long_press:
            return {"ok": False, "error": "the long press did not reach the page"}
        return await self.js_click(h, "the touch did not reach the page")

    async def js_click(self, h, why):
        r = await self.act(h, "click")
        if r.get("ok"):
            r["via"] = f"web_js_click ({why})"
        return r

    async def page(self):
        """The whole page (Web.page): its text and every readable element, off-screen ones flagged."""
        tab = self.visible
        if tab is not None:
            try:
                if (tab.pending is not None and not await tab.arrived(ARRIVE_WAIT) and not await tab.answers()
                        and (pending := tab.pending) is not None):  # it can arrive while the old page is asked
                    return {"ok": False, "error": "the next page" + (f" ({short_url(pending['url'])})" if pending["url"] else "")
                                                  + " has not arrived yet: wait for it, then read it"}
            except websockets.WebSocketException:
                pass  # the tab closed: the read below says so
        try:
            d = await self.call(DUMP.format(scope="page"), timeout=4, devtools=True)
            text = await self.call("text(20000)")
        except (ChromeError, TimeoutError) as exc:
            return {"ok": False, "error": str(exc) or "the page did not answer"}
        if not isinstance(d, dict) or "error" in d:
            return {"ok": False, "error": "the page could not be read"}
        to_screen = self.mapper(d)
        nodes = [self.convert(n, to_screen, d["epoch"]) for n in d.get("nodes", [])]
        frame_texts = []
        await self.graft_frames(nodes, to_screen, "page", texts=frame_texts)
        reading = (text or {}).get("text", "") if isinstance(text, dict) else ""
        for host, frame_text in frame_texts:
            reading += f"\n\n[frame {host}]\n{frame_text}"
        return {"ok": True, "url": d.get("url"), "title": d.get("title"), "scroll": d.get("scroll"),
                "truncated": d.get("truncated"), "nodes": nodes, "text": reading}


def walk(nodes):
    for n in nodes:
        yield n
        yield from walk(n.get("c", []))


def subtrees(tree):
    """A frame tree and every tree under it, in document order."""
    if tree.get("frame"):
        yield tree
    for child in tree.get("childFrames") or []:
        yield from subtrees(child)


def subframes(tree):
    """The frames below the top one, in tree (document) order."""
    for child in tree.get("childFrames") or []:
        yield child["frame"]
        yield from subframes(child)


def match_frame(src, frames, used):
    """The listed frame a blocked frame element loads: by its URL, then by its host (a redirect changed the
    path), then the next unmatched one in document order."""
    free = [f for f in frames if f["id"] not in used]
    host = src.split("//", 1)[-1].split("/", 1)[0] if src else None
    for pick in (lambda f: src and f.get("url") == src,
                 lambda f: host and f.get("url", "").split("//", 1)[-1].split("/", 1)[0] == host,
                 lambda f: True):
        found = next((f for f in free if pick(f)), None)
        if found:
            return found
    return None
