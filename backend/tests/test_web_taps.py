"""What a tap on a Chrome page set off, as Chrome reports it (bridge/chrome.py): the tab asking for another page
and that page arriving, and the requests the page sent in answer. Before this, a tap whose next page was slow
came back FAILED with an empty page, and a button whose request was slow looked untouched."""
import asyncio
import time

from app.bridge import chrome
from app.bridge.chrome import ChromePages, short_url, site
from app.bridge.screen import loading, performed, web_wait
from test_chrome_tabs import FakeTab

PAGE = {"epoch": "aa", "url": "https://shop.test/product", "title": "Product", "ready": "complete", "count": 1,
        "vv": {"x": 0, "y": 0, "w": 400, "h": 800, "scale": 1}, "frames": {"blocked": []},
        "nodes": [{"i": 3, "b": [0, 0, 400, 40], "wrole": "button", "text": "Add to cart", "f": ["click"]}]}
ADD = "cdp:aa:3"


def say(tab, method, **params):
    tab.event({"method": method, "params": params})


class PageTab(FakeTab):
    """One page with an Add to cart button. `on_touch` is what Chrome reports once the finger lifts; `delivered`
    is the page's answer to "did you get the touch" (an exception: it does not answer)."""

    def __init__(self, on_touch=None, delivered=None, silent=False):
        super().__init__("t1")
        self.frame = "F"
        self.on_touch, self.silent = on_touch, silent
        self.delivered = delivered if delivered is not None else {"ok": True, "delivered": True, "click": True, "at": 1_000_000.0}
        self.sent, self.asked = [], []

    async def send(self, method, params=None, timeout=None, stop=None, session=None):
        self.sent.append(method)
        if method == "Input.dispatchTouchEvent" and params["type"] == "touchEnd" and self.on_touch:
            self.on_touch(self)
        return {}

    async def evaluate(self, expression, timeout=None, await_promise=False, context=None, stop=None, devtools=False, session=None):
        if self.silent:
            raise TimeoutError
        if "visibilityState" in expression:
            return "visible"
        if "DOMContentLoaded" in expression or expression == "1":
            return 1
        if ".dump(" in expression:
            assert devtools and "getEventListeners" in expression  # the page script is handed Chrome's listener list
            return PAGE
        self.asked.append(expression.rsplit(",", 2)[-2].strip('"'))
        if '"prepare"' in expression:
            return {"ok": True, "x": 200, "y": 20, "scrolled": False, "vv": {"x": 0, "y": 0, "w": 400, "h": 800}}
        if '"delivered"' in expression:
            if isinstance(self.delivered, Exception):
                raise self.delivered
            return self.delivered
        if '"click"' in expression:
            return {"ok": True, "via": "web_js_click"}
        return {"ok": False, "error": "unexpected"}


def pages_with(tab):
    pages = ChromePages(adb=None)
    pages.port = 1
    pages.visible = tab
    pages.seen[tab.target_id] = time.monotonic()
    return pages


def look(pages):
    page_node = {"b": [0, 0, 1080, 2400]}
    asyncio.run(pages.attach(page_node))
    return page_node


def snapshot(page_node):
    return {"web_page": True, "windows": [{"nodes": [page_node]}]}


# --- the tab asking for another page ---------------------------------------------------------------------

def test_a_page_asked_for_is_awaited_until_it_arrives():
    tab = FakeTab("t")
    tab.frame = "F"
    say(tab, "Page.frameRequestedNavigation", frameId="F", reason="formSubmissionPost", url="https://shop.test/cart", disposition="currentTab")
    assert tab.pending["url"] == "https://shop.test/cart" and not tab.pending["started"] and tab.moves == 1
    say(tab, "Page.frameStartedLoading", frameId="F")
    assert tab.pending["started"] and tab.moves == 1
    say(tab, "Page.frameNavigated", frame={"id": "F", "url": "https://shop.test/cart"})
    assert tab.pending is None and tab.moves == 2


def test_only_the_tabs_own_next_document_is_awaited():
    tab = FakeTab("t")
    tab.frame = "F"
    say(tab, "Page.frameRequestedNavigation", frameId="ad-frame", url="https://ads.test/x", disposition="currentTab")
    say(tab, "Page.frameStartedLoading", frameId="ad-frame")
    say(tab, "Page.frameRequestedNavigation", frameId="F", url="https://shop.test/other", disposition="newTab")
    say(tab, "Page.frameStartedNavigating", frameId="F", url="https://shop.test/product#reviews", navigationType="sameDocument")
    assert tab.pending is None and tab.moves == 0


def test_a_load_that_brings_no_document_ends_the_wait():
    tab = FakeTab("t")
    tab.frame = "F"
    say(tab, "Page.frameStartedLoading", frameId="F")  # the address bar or Back: no URL said
    assert tab.pending == {"url": "", "since": tab.pending["since"], "started": True}
    say(tab, "Page.frameStoppedLoading", frameId="F")  # a download
    assert tab.pending is None


def test_a_link_another_app_takes_is_not_awaited_for_long():
    tab = FakeTab("t")
    tab.frame = "F"
    say(tab, "Page.frameRequestedNavigation", frameId="F", url="tel:+15550100", disposition="currentTab")
    assert asyncio.run(tab.arrived(0)) is False
    tab.pending["since"] -= chrome.UNSTARTED + 1
    assert asyncio.run(tab.arrived(0)) is True and tab.pending is None


# --- the tap ----------------------------------------------------------------------------------------------

def ask_for_cart(tab):
    say(tab, "Page.frameRequestedNavigation", frameId="F", reason="formSubmissionPost",
        url="https://shop.test/cart?added=1", disposition="currentTab")


def test_a_tap_that_asks_for_another_page_has_landed_and_the_old_page_is_left_alone():
    tab = PageTab(on_touch=ask_for_cart)
    r = asyncio.run(pages_with(tab).click(ADD))
    assert r == {"ok": True, "via": "chrome_tap", "navigated": True, "opening": "shop.test/cart"}
    assert tab.asked == ["prepare"]  # never asked whether it got the touch, never clicked by script


def test_a_page_that_leaves_while_it_is_asked_about_the_touch_has_taken_it():
    tab = PageTab()
    late = chrome.Moved("left")

    async def evaluate(expression, *args, **kw):
        if '"delivered"' in expression:
            ask_for_cart(tab)  # Chrome says so while the question waits
            raise late
        return await PageTab.evaluate(tab, expression, *args, **kw)

    tab.evaluate = evaluate
    r = asyncio.run(pages_with(tab).click(ADD))
    assert r["ok"] and r["opening"] == "shop.test/cart"


def test_a_page_silent_after_the_touch_is_not_tapped_again_by_script():
    tab = PageTab(delivered=TimeoutError())
    pages = pages_with(tab)
    r = asyncio.run(pages.click(ADD))
    assert r["ok"] is False and "may or may not have happened" in r["error"]
    assert "click" not in tab.asked
    assert pages.problems == [("web_tap_unanswered", "the page did not answer")]


def test_a_touch_the_page_never_saw_is_still_made_by_script():
    tab = PageTab(delivered={"ok": True, "delivered": False, "click": False, "at": 0})
    r = asyncio.run(pages_with(tab).click(ADD))
    assert r["ok"] and r["via"].startswith("web_js_click") and tab.asked[-1] == "click"


def test_a_tap_is_worded_with_the_page_it_asked_for():
    target = type("T", (), {"node": {}, "label": "Add to cart", "role": "Button", "n": 9, "list_pos": ""})()
    said = performed({"cmd": "click"}, target, {"ok": True, "via": "chrome_tap", "navigated": True, "opening": "shop.test/cart"})
    assert said == "tapped [9] Add to cart via chrome_tap, which asked for shop.test/cart"


# --- the look after it ------------------------------------------------------------------------------------

def test_a_tab_waiting_for_its_next_page_reads_as_loading_not_as_an_empty_or_failed_page():
    tab = PageTab()
    pages = pages_with(tab)
    look(pages)  # the page as it was
    ask_for_cart(tab)
    say(tab, "Page.frameStartedLoading", frameId="F")
    tab.silent = True  # the old page answers nothing while the next one is on its way
    page_node = look(pages)
    assert page_node["url"] == "https://shop.test/product" and page_node["title"] == "Product"
    assert page_node["awaiting"]["url"] == "shop.test/cart" and "loading" in page_node["f"] and "c" not in page_node
    assert pages.problems == []
    assert loading(snapshot(page_node)) == "the next page (shop.test/cart) has not arrived yet"
    tab.silent = False
    say(tab, "Page.frameNavigated", frame={"id": "F", "url": "https://shop.test/cart?added=1"})
    page_node = look(pages)
    assert "awaiting" not in page_node and page_node["c"] and loading(snapshot(page_node)) is None


def test_an_old_page_that_still_answers_is_read_and_says_what_is_on_its_way():
    tab = PageTab()
    pages = pages_with(tab)
    ask_for_cart(tab)
    say(tab, "Page.frameStartedLoading", frameId="F")
    page_node = look(pages)
    assert page_node["c"] and page_node["awaiting"]["url"] == "shop.test/cart"


def test_the_whole_page_is_not_read_while_the_next_one_is_awaited():
    tab = PageTab(silent=True)
    ask_for_cart(tab)
    say(tab, "Page.frameStartedLoading", frameId="F")
    r = asyncio.run(pages_with(tab).page())
    assert r == {"ok": False, "error": "the next page (shop.test/cart) has not arrived yet: wait for it, then read it"}


# --- the requests a tap set off ---------------------------------------------------------------------------

def request(tab, rid, url, at, method="POST", kind="Fetch"):
    say(tab, "Network.requestWillBeSent", requestId=rid, type=kind, wallTime=at, request={"method": method, "url": url})


def test_a_request_the_tap_sent_is_waited_on_until_the_site_answers():
    tab = PageTab()
    pages = pages_with(tab)
    look(pages)
    assert asyncio.run(pages.click(ADD)) == {"ok": True, "via": "chrome_tap", "page_saw_click": True}
    assert tab.network and "Network.enable" in tab.sent and tab.tap["at"] == 1000.0
    request(tab, "r1", "https://shop.test/cart/add", 1000.05)
    request(tab, "r2", "https://metrics.example/v1", 1000.06)  # another site: analytics
    request(tab, "r3", "https://shop.test/api/collect", 1001.2)  # too late to be the tap's
    request(tab, "r4", "https://shop.test/pixel.gif", 1000.05, method="GET", kind="Image")  # not a script's request
    page_node = look(pages)
    assert page_node["waiting"] == ["POST /cart/add"] and page_node["c"]
    assert loading(snapshot(page_node)) == "the site has not answered POST /cart/add yet"
    say(tab, "Network.responseReceived", requestId="r1")
    tab.tap["since"] -= 5  # the page has had its time to send more
    page_node = look(pages)
    assert "waiting" not in page_node and loading(snapshot(page_node)) is None
    assert tab.tap is None and not tab.network and tab.sent[-1] == "Network.disable"


def test_requests_are_not_attributed_before_the_page_says_when_the_tap_reached_it():
    tab = PageTab()
    asyncio.run(tab.follow_tap())
    request(tab, "r1", "https://shop.test/cart/add", 1000.05)
    tab.url = "https://shop.test/product"
    assert tab.waiting() == []
    tab.tap["at"] = 1000.0
    assert tab.waiting() == [{"method": "POST", "url": "https://shop.test/cart/add"}]
    tab.tap["since"] -= chrome.REQUEST_WATCH + 1  # unanswered for that long: the page's own business
    assert tab.waiting() == []


def test_site_and_short_url():
    assert site("https://checkout.shop.test/pay?x=1") == site("https://www.shop.test/") == "shop.test"
    assert site("https://shop.co.uk/a") == "shop.co.uk" != site("https://other.co.uk/a")
    assert short_url("https://shop.test/cart/add?id=1#x") == "shop.test/cart/add"
    assert short_url("https://shop.test/cart/add?id=1", host="https://shop.test/product") == "/cart/add"
    assert short_url("https://api.shop.test/cart", host="https://shop.test/product") == "api.shop.test/cart"


def test_web_wait_words():
    assert web_wait({"awaiting": {"url": "", "seconds": 3}}) == "the next page has not arrived yet"
    assert web_wait({"waiting": ["POST /a", "GET /b"]}) == "the site has not answered POST /a, GET /b yet"
    assert web_wait({"url": "https://shop.test"}) == ""
