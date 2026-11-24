"""Frames the page's own script cannot read, as Chrome really lays them out: in the page's process (one
execution context each), in another process (a DevTools session each), and inside one another. The tab here is
the real `chrome.Tab`; only the socket is fake, so the commands, sessions and events are the ones Chrome sees."""

import asyncio
import json
from collections import deque

from app.bridge import chrome
from app.bridge.chrome import ChromePages

VV = {"x": 0, "y": 0, "w": 400, "h": 800, "scale": 1}


def frame_node(i, box, src, origin):
    """A frame as the page script reports one it cannot look into."""
    return {"i": i, "b": box, "cls": "web:frame", "wrole": "frame", "desc": src.split("//")[-1].split("/")[0] or "frame",
            "f": ["blocked_frame"], "src": src, "fo": origin}


def document(epoch, url, nodes, text=""):
    return {"dump": {"epoch": epoch, "url": url, "title": "", "ready": "complete", "vv": VV, "nodes": nodes},
            "text": text}


class Socket:
    """Chrome's end of one tab's DevTools connection. `documents` maps (session, context) to what the page
    script answers there, `trees` a session to its frame tree, `contexts` a session to {frame id: context} and
    `remote` a session to the frames below it that run in another process, as (session, frame id, url, parent
    frame id). The page itself is session None."""

    def __init__(self, documents, trees, contexts=None, remote=None, rects=None):
        self.documents, self.trees, self.contexts, self.remote = documents, trees, contexts or {}, remote or {}
        self.rects = rects or {}  # (session, element id) -> where that frame element is now
        self.out, self.commands = deque(), []

    async def recv(self):
        while not self.out:
            await asyncio.sleep(0.001)
        return self.out.popleft()

    def event(self, method, params, session=None):
        self.out.append(json.dumps({"method": method, "params": params, **({"sessionId": session} if session else {})}))

    async def send(self, text):
        message = json.loads(text)
        method, params, session = message["method"], message["params"], message.get("sessionId")
        self.commands.append((session, method, params))
        result = {}
        if session is not None and all(session != s for frames in self.remote.values() for s, *_ in frames):
            self.out.append(json.dumps({"id": message["id"], "error": {"message": "Session with given id not found."}}))
            return
        if method == "Runtime.enable":
            for frame, context in self.contexts.get(session, {}).items():
                self.event("Runtime.executionContextCreated",
                           {"context": {"id": context, "auxData": {"isDefault": True, "frameId": frame}}}, session)
        elif method == "Target.setAutoAttach":
            for child, frame, url, under in self.remote.get(session, []):
                self.event("Target.attachedToTarget", {"sessionId": child, "targetInfo": {
                    "type": "iframe", "targetId": frame, "url": url, "parentFrameId": under}}, session)
        elif method == "Page.getFrameTree":
            result = {"frameTree": self.trees[session]}
        elif method == "Runtime.evaluate":
            result = {"result": {"value": self.evaluate(params["expression"], session, params.get("contextId"))}}
        self.out.append(json.dumps({"id": message["id"], "result": result}))

    def evaluate(self, expression, session, context):
        if "visibilityState" in expression:
            return "visible"
        if "DOMContentLoaded" in expression:
            return 1
        doc = self.documents.get((session, context))
        if doc is None:
            return {"error": f"no document at {(session, context)}"}
        if ".dump(" in expression:
            return doc["dump"]
        if ".text(" in expression:
            return {"text": doc["text"]}
        if '"prepare"' in expression:
            return {"ok": True, "x": 35, "y": 15, "scrolled": False, "vv": VV}
        if '"rect"' in expression:
            element = int(expression.rsplit(".act(", 1)[1].split(",")[1])
            return {"ok": True, "fo": self.rects[(session, element)]}
        if '"delivered"' in expression:
            return {"ok": True, "delivered": True, "click": True}
        return {"ok": False, "error": "unexpected: " + expression[-80:]}

    def asked(self, method):
        """The sessions `method` was put to, in order."""
        return [session for session, m, _ in self.commands if m == method]


def pages_on(socket):
    pages = ChromePages(adb=None)
    pages.port = 1
    pages.visible = chrome.Tab("t1", socket)
    pages.visible.consent_ran = True
    pages.visible.frame = "F0"
    return pages


def nodes_of(node):
    for n in node.get("c", []):
        yield n
        yield from nodes_of(n)


def by_text(page_node, text):
    return next(n for n in nodes_of(page_node) if n.get("text") == text)


def tree(frame, url, *children):
    return {"frame": {"id": frame, "url": url}, "childFrames": list(children)}


# A shop the user is signed in to, so Chrome keeps its site in a process of its own: the payment sheet it embeds
# is a frame in another process, and the card field inside that sheet (a vault's) is in a third.
SHOP = document("pp", "https://shop.test/pay", [
    {"i": 3, "b": [0, 0, 400, 40], "wrole": "button", "text": "Pay now", "f": ["click"]},
    frame_node(5, [0, 300, 400, 800], "https://gateway.test/checkout?order=1", [0, 300])], "Pay now")
SHEET = document("gg", "https://gateway.test/checkout?order=1&session=9", [
    {"i": 7, "b": [10, 100, 60, 130], "wrole": "button", "text": "Cards", "f": ["click"]},
    frame_node(9, [20, 50, 380, 90], "https://vault.test/number", [20, 50])], "Cards")
FIELD = document("vv", "https://vault.test/number", [
    {"i": 4, "b": [0, 0, 360, 40], "wrole": "textbox", "hint": "Card number", "f": ["click", "edit"]}], "Card number")


def isolated_shop():
    return Socket(
        documents={(None, None): SHOP, ("S1", None): SHEET, ("S2", None): FIELD},
        trees={None: tree("F0", "https://shop.test/pay"),  # Chrome lists no frame of another process here
               "S1": tree("F1", "https://gateway.test/checkout?order=1&session=9"),
               "S2": tree("F2", "https://vault.test/number")},
        remote={None: [("S1", "F1", "https://gateway.test/checkout?order=1&session=9", "F0")],
                "S1": [("S2", "F2", "https://vault.test/number", "F1")]},
        rects={(None, 5): [0, 320], ("S1", 9): [20, 60]})  # the sheet slid up a little since the look


def looked(socket):
    pages = pages_on(socket)
    page_node = {"b": [0, 0, 1080, 2400]}
    asyncio.run(pages.attach(page_node))
    return pages, page_node


def on_screen(x, y):
    f = 1080 / 400
    return [round(x * f), round(2400 - 800 * f + y * f)]


def test_a_frame_in_another_process_is_read_through_its_own_session():
    socket = isolated_shop()
    pages, page_node = looked(socket)
    assert pages.problems == []
    assert not [n for n in nodes_of(page_node) if "blocked_frame" in n.get("f", ())]
    cards = by_text(page_node, "Cards")
    assert cards["h"] == "cdp:gg:7:s1"
    assert cards["b"] == [*on_screen(10, 300 + 100), *on_screen(60, 300 + 130)]
    # Chrome was asked for the frames of the page, then of the sheet, then of the field (which holds none).
    assert socket.asked("Target.setAutoAttach") == [None, "S1"]
    assert pages.frames["s1"] == chrome.Frame("S1", None, "F1", "cdp:pp:5", [0, 300])


def test_a_frame_inside_a_frame_is_read_and_placed_by_both_origins():
    pages, page_node = looked(isolated_shop())
    field = next(n for n in nodes_of(page_node) if n.get("hint") == "Card number")
    assert field["h"] == "cdp:vv:4:s2"
    assert field["b"] == [*on_screen(20, 300 + 50), *on_screen(20 + 360, 300 + 50 + 40)]
    assert pages.frames["s2"] == chrome.Frame("S2", None, "F2", "cdp:gg:9:s1", [20, 50])


def test_a_tap_goes_to_the_page_at_the_point_where_the_inner_frame_is_now():
    socket = isolated_shop()
    pages, _ = looked(socket)
    socket.commands.clear()
    r = asyncio.run(pages.click("cdp:vv:4:s2"))
    assert r == {"ok": True, "via": "chrome_tap", "page_saw_click": True}
    touches = [(session, p) for session, m, p in socket.commands if m == "Input.dispatchTouchEvent"]
    # The touch is the page's to pass on: Chrome hands it to the frame under the point, whatever its process.
    assert touches[0] == (None, {"type": "touchStart", "touchPoints": [{"x": 0 + 20 + 35, "y": 320 + 60 + 15}]})
    prepared = [session for session, m, p in socket.commands if m == "Runtime.evaluate" and '"prepare"' in p["expression"]]
    assert prepared == ["S2"]  # the element is asked where it is inside its own frame


def test_reading_the_whole_page_takes_in_the_text_of_every_frame():
    pages = pages_on(isolated_shop())
    page = asyncio.run(pages.page())
    assert page["text"] == "Pay now\n\n[frame gateway.test]\nCards\n\n[frame vault.test]\nCard number"
    assert {n["h"] for n in nodes_of({"c": page["nodes"]}) if "h" in n} >= {"cdp:pp:3", "cdp:gg:7:s1", "cdp:vv:4:s2"}


def test_a_frame_that_has_gone_leaves_its_handles_stale_and_is_not_asked():
    socket = isolated_shop()
    pages, _ = looked(socket)
    pages.visible.event({"method": "Target.detachedFromTarget", "params": {"sessionId": "S1"}})
    assert pages.visible.remote == {}  # the field went with the sheet that held it
    socket.commands.clear()
    for handle in ("cdp:gg:7:s1", "cdp:vv:4:s2"):
        assert asyncio.run(pages.act(handle, "prepare")) == {"ok": False, "error": "stale handle: the frame has gone"}
    assert socket.commands == []


def test_a_frame_whose_process_went_unannounced_is_stale_too():
    socket = isolated_shop()
    pages, _ = looked(socket)
    socket.remote = {}  # Chrome has dropped the sessions and its word has not been read yet
    assert asyncio.run(pages.act("cdp:gg:7:s1", "prepare")) == {"ok": False, "error": "stale handle: the frame has gone"}


def test_events_of_a_remote_frame_never_pass_for_the_pages_own():
    pages, _ = looked(isolated_shop())
    tab = pages.visible
    moves = tab.moves
    # The sheet goes on to its bank's page: a navigation of that frame, not of the tab.
    tab.event({"method": "Page.frameStartedLoading", "params": {"frameId": "F0"}, "sessionId": "S1"})
    tab.event({"method": "Page.frameNavigated", "params": {"frame": {"id": "F1"}}, "sessionId": "S1"})
    tab.event({"method": "Runtime.executionContextsCleared", "params": {}, "sessionId": "S1"})
    assert tab.pending is None and tab.moves == moves and tab.frame == "F0"
    tab.event({"method": "Target.targetInfoChanged", "params": {"targetInfo": {"targetId": "F1", "url": "https://bank.test/otp"}}})
    assert tab.remote["S1"].url == "https://bank.test/otp"


# The same shop before the user signed in: every frame is in the page's process, one execution context each.
def shared_process_shop():
    nameless = dict(frame_node(9, [20, 50, 380, 90], "", [20, 50]), desc="frame")  # a frame the sheet writes itself
    sheet = document("gg", SHEET["dump"]["url"], [SHEET["dump"]["nodes"][0], nameless], "Cards")
    return Socket(
        documents={(None, None): SHOP, (None, 2): sheet, (None, 3): FIELD},
        trees={None: tree("F0", "https://shop.test/pay",
                          tree("F1", "https://gateway.test/checkout?order=1&session=9", tree("F2", "about:blank")),
                          tree("F3", "https://ads.test/banner"))},
        contexts={None: {"F0": 1, "F1": 2, "F2": 3, "F3": 4}})


def test_frames_of_the_pages_own_process_nest_by_the_frame_tree():
    pages, page_node = looked(shared_process_shop())
    assert pages.problems == []
    assert by_text(page_node, "Cards")["h"] == "cdp:gg:7:2"
    # The frame inside the sheet names no address: it is found among the sheet's own frames (F2), never among
    # the page's others (the sheet itself, the banner).
    field = next(n for n in nodes_of(page_node) if n.get("hint") == "Card number")
    assert field["h"] == "cdp:vv:4:3"
    assert pages.frames["3"] == chrome.Frame(None, 3, "F2", "cdp:gg:9:2", [20, 50])


def test_frames_are_read_only_so_deep():
    # A page that frames itself without end, each frame in the page's process.
    link = document("pp", "https://loop.test/", [frame_node(5, [0, 0, 400, 800], "https://loop.test/", [0, 0])])
    depth = chrome.FRAME_DEPTH + 3
    chain = tree(f"F{depth}", "https://loop.test/")
    for level in range(depth - 1, -1, -1):
        chain = tree(f"F{level}", "https://loop.test/", chain)
    socket = Socket(documents={(None, None): link, **{(None, level): link for level in range(1, depth + 1)}},
                    trees={None: chain}, contexts={None: {f"F{level}": level for level in range(1, depth + 1)}})
    pages, page_node = looked(socket)
    frames = [n for n in nodes_of(page_node) if n.get("wrole") == "frame"]
    assert len(frames) == chrome.FRAME_DEPTH + 1
    assert ["blocked_frame" in n["f"] for n in frames] == [False] * chrome.FRAME_DEPTH + [True]
