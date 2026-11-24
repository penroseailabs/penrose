import asyncio

from app.bridge import chrome
from app.bridge.chrome import MAX_TABS, ChromePages


class FakeTab(chrome.Tab):
    """A tab with no socket: what Chrome would send is fed to `event`, what is asked of it is answered here."""

    def __init__(self, target_id):
        super().__init__(target_id, ws=None)
        self.consent_ran = True  # the cookie-banner script is tested on its own below

    async def listen(self, seconds=0.0, until=None):
        return bool(until()) if until is not None else False

    async def close(self):
        pass


class FakeResponse:
    pass


def trimmed(monkeypatch, ids, visible, seen=None):
    closed = []

    class Client:
        def __init__(self, **_):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def get(self, url):
            closed.append(url.rsplit("/", 1)[1])
            return FakeResponse()

    monkeypatch.setattr(chrome.httpx, "AsyncClient", Client)
    pages = ChromePages(adb=None)
    pages.port = 1
    pages.visible = FakeTab(visible)
    pages.seen = dict(seen or {})
    asyncio.run(pages.trim([{"id": i} for i in ids]))
    return closed


def test_keeps_five_newest_by_list_order(monkeypatch):
    ids = [str(n) for n in range(1, 21)]
    assert trimmed(monkeypatch, ids, visible="20") == [str(n) for n in range(1, 16)]


def test_nothing_closed_up_to_the_limit(monkeypatch):
    assert trimmed(monkeypatch, ["1", "2", "3", "4", "5"][:MAX_TABS], visible="5") == []


def test_visible_tab_and_recently_seen_tabs_survive(monkeypatch):
    ids = [str(n) for n in range(1, 9)]
    closed = trimmed(monkeypatch, ids, visible="1", seen={"2": 50.0, "3": 40.0, "4": 30.0})
    assert "1" not in closed and not {"2", "3", "4"} & set(closed)
    assert sorted(closed) == ["5", "6", "7"]


# --- cross-origin frames ------------------------------------------------------------------------

TOP = {"epoch": "aa", "url": "http://shop.test/pay", "title": "Pay", "ready": "complete",
       "vv": {"x": 0, "y": 0, "w": 400, "h": 800, "scale": 1}, "frames": {"blocked": ["gateway.test"]},
       "nodes": [{"i": 3, "b": [0, 0, 400, 40], "wrole": "button", "text": "Pay now", "f": ["click"]},
                 {"i": 5, "b": [0, 300, 400, 800], "cls": "web:frame", "wrole": "frame", "desc": "gateway.test",
                  "f": ["blocked_frame"], "src": "https://gateway.test/checkout?x=1", "fo": [0, 300]}]}
FRAME = {"epoch": "bb", "url": "https://gateway.test/checkout?x=1", "ready": "complete",
         "vv": {"x": 0, "y": 0, "w": 400, "h": 500, "scale": 1}, "frames": {"blocked": []},
         "nodes": [{"i": 7, "b": [10, 100, 60, 130], "wrole": "button", "text": "Card", "f": ["click"]}]}
TREE = {"frameTree": {"frame": {"id": "F0", "url": "http://shop.test/pay"},
                      "childFrames": [{"frame": {"id": "F1", "url": "https://gateway.test/checkout?x=1&session=9"}}]}}


class FrameTab(FakeTab):
    """A tab with one cross-origin frame: context 1 is the page, context 2 the frame."""

    def __init__(self):
        super().__init__("t1")
        self.contexts = {}
        self.sent, self.evaluated = [], []
        self.frame_said = "nothing yet"

    async def watch_frames(self, session=None):
        self.contexts = {"F0": 1, "F1": 2}

    async def send(self, method, params=None, timeout=None, stop=None, session=None):
        self.sent.append((method, params))
        return TREE if method == "Page.getFrameTree" else {}

    async def evaluate(self, expression, timeout=None, await_promise=False, context=None, **_):
        self.evaluated.append((expression[-60:], context))
        if "visibilityState" in expression:
            return "visible"
        if "DOMContentLoaded" in expression:
            return 1
        if ".dump(" in expression:
            return FRAME if context == 2 else TOP
        if ".text(" in expression:
            return {"text": "Card chosen"} if context == 2 else {"text": "Pay now"}
        if '"prepare"' in expression:
            return {"ok": True, "x": 35, "y": 115, "scrolled": False, "vv": {"x": 0, "y": 0, "w": 400, "h": 500}}
        if '"rect"' in expression:
            return {"ok": True, "fo": [0, 320]}  # the sheet slid up a little since the look
        if '"delivered"' in expression:
            return {"ok": True, "delivered": True, "click": True}
        return {"ok": False, "error": "unexpected: " + expression[-80:]}


def frame_pages():
    pages = ChromePages(adb=None)
    pages.port = 1
    pages.visible = FrameTab()
    return pages


def nodes_of(node):
    for n in node.get("c", []):
        yield n
        yield from nodes_of(n)


def test_a_cross_origin_frame_is_read_in_its_own_context_and_placed_by_its_origin():
    pages = frame_pages()
    page_node = {"b": [0, 0, 1080, 2400]}
    asyncio.run(pages.attach(page_node))
    frame = next(n for n in nodes_of(page_node) if n.get("wrole") == "frame")
    assert "blocked_frame" not in frame["f"] and frame["frame"] == "https://gateway.test/checkout?x=1"
    assert "h" not in frame and "fo" not in frame and "src" not in frame
    card = frame["c"][0]
    assert card["h"] == "cdp:bb:7:2"
    f = 1080 / 400
    top = 2400 - 800 * f
    assert card["b"] == [round(10 * f), round(top + (300 + 100) * f), round(60 * f), round(top + (300 + 130) * f)]
    assert pages.problems == [] and pages.frames == {"2": chrome.Frame(None, 2, "F1", "cdp:aa:5", [0, 300])}
    assert ("Page.getFrameTree", None) in pages.visible.sent


def test_a_tap_in_a_frame_lands_where_the_frame_is_now():
    pages = frame_pages()
    page_node = {"b": [0, 0, 1080, 2400]}
    asyncio.run(pages.attach(page_node))
    touches = []

    async def send(method, params=None, timeout=None):
        if method == "Input.dispatchTouchEvent":
            touches.append(params)
        return {}

    pages.visible.send = send
    r = asyncio.run(pages.click("cdp:bb:7:2"))
    assert r == {"ok": True, "via": "chrome_tap", "page_saw_click": True}
    assert touches[0]["touchPoints"] == [{"x": 35, "y": 320 + 115}]
    assert any('"prepare"' in e and c == 2 for e, c in pages.visible.evaluated)  # prepared inside the frame


def test_read_page_appends_the_frame_text_and_a_frame_in_another_process_stays_blocked():
    pages = frame_pages()
    page = asyncio.run(pages.page())
    assert page["text"] == "Pay now\n\n[frame gateway.test]\nCard chosen"
    assert any(n.get("h") == "cdp:bb:7:2" for n in nodes_of({"c": page["nodes"]}))
    lonely = frame_pages()
    lonely.visible.watch_frames = lambda session=None: asyncio.sleep(0)  # Chrome lists no context for the frame
    page_node = {"b": [0, 0, 1080, 2400]}
    asyncio.run(lonely.attach(page_node))
    frame = next(n for n in nodes_of(page_node) if n.get("wrole") == "frame")
    assert "blocked_frame" in frame["f"] and "c" not in frame
    assert lonely.problems == [("web_blocked_frame", "gateway.test")]


class EvalTab:
    def __init__(self, fail=False):
        self.consent_ran, self.fail, self.calls = False, fail, 0

    async def evaluate(self, expression, timeout=None, **_):
        self.calls += 1
        if self.fail:
            raise TimeoutError("slow page")


def test_cookie_script_runs_once_in_the_open_page():
    pages = ChromePages(adb=None)
    pages.visible = tab = EvalTab()
    asyncio.run(pages.consent_now())
    asyncio.run(pages.consent_now())
    assert tab.calls == 1


def test_cookie_script_failure_never_fails_the_look():
    pages = ChromePages(adb=None)
    pages.visible = EvalTab(fail=True)
    asyncio.run(pages.consent_now())
    assert pages.problems == [("cookie_script_failed", "slow page")]
