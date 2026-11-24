"""The gateway's Google Play search for look_up (`apps: true`): Play's apps for a query, read in a hidden tab of
the phone's Chrome and marked with the ones the user has on this phone or ever installed."""

import contextlib
import json

import pytest
from app.gateway import play, web
from fastapi import HTTPException

MAY_2024 = 1715300000000  # 10 May 2024, in ms

DOORDASH = {"package": "com.dd.doordash", "title": "DoorDash - Food Delivery", "developer": "DoorDash", "rating": "4.8",
            "summary": "Restaurants and groceries delivered."}
INSTACART = {"package": "com.instacart.client", "title": "Instacart: Get Food Delivery", "developer": "Instacart",
             "rating": "4.2", "summary": ""}


def test_an_app_s_standing_says_what_the_user_has_to_do_with_it():
    on_phone, owned = {"com.dd.doordash"}, {"com.dd.doordash": MAY_2024, "com.instacart.client": MAY_2024, "com.old": None}
    assert play.standing("com.dd.doordash", on_phone, owned) == "on this phone"
    assert play.standing("com.instacart.client", on_phone, owned) == "in the user's Play library since May 2024, not on this phone"
    assert play.standing("com.old", on_phone, owned) == "in the user's Play library, not on this phone"  # no date kept
    assert play.standing("com.ubercab.eats", on_phone, owned) == "never installed by the user"
    # Without the library, only what the phone itself says; without either, nothing is claimed.
    assert play.standing("com.instacart.client", on_phone, None) == "not on this phone"
    assert play.standing("com.instacart.client", None, None) == ""


def test_found_lines_keep_play_s_first_results_and_every_app_the_user_has():
    apps = [{"package": f"com.other{n}", "title": f"Other {n}", "developer": "Someone", "rating": "4.0", "summary": ""}
            for n in range(8)]
    apps[0] = DOORDASH
    apps.append(INSTACART)  # ranked ninth, but the user has had it
    lines = play.found_lines("food delivery", {"apps": apps}, {"com.dd.doordash"}, {"com.instacart.client": MAY_2024}).splitlines()
    assert lines[0] == ('Play app "DoorDash - Food Delivery" DoorDash · com.dd.doordash · 4.8★ · on this phone · '
                        "Restaurants and groceries delivered.")
    assert lines[1] == 'Play app "Other 1" Someone · com.other1 · 4.0★ · never installed by the user'
    assert len(lines) == play.KEPT + 2
    assert lines[-2] == ('Play app "Instacart: Get Food Delivery" Instacart · com.instacart.client · 4.2★ · '
                         "in the user's Play library since May 2024, not on this phone")
    assert lines[-1] == 'Play "food delivery" search Google Play · 9 apps · 2 the user has or had'
    assert play.found_lines("nothing", {"apps": []}, set(), {}) == ""


def test_a_library_that_cannot_be_read_is_said_and_claims_nothing():
    lines = play.found_lines("food delivery", {"apps": [INSTACART]}, {"com.whatsapp"}, None, "su: not found").splitlines()
    assert lines == ['Play app "Instacart: Get Food Delivery" Instacart · com.instacart.client · 4.2★ · not on this phone',
                     "Play \"food delivery\" search Google Play · 1 app · the user's Play library could not be read (su: not found)"]


def test_a_package_id_s_line_ends_with_what_the_app_says_it_does():
    answer = {"app": {**DOORDASH, "about": "Order meals and groceries from local restaurants."}}
    assert play.found_lines("com.dd.doordash", answer, set(), {"com.dd.doordash": MAY_2024}) == (
        'Play app "DoorDash - Food Delivery" DoorDash · com.dd.doordash · 4.8★ · '
        "in the user's Play library since May 2024, not on this phone · Order meals and groceries from local restaurants.")


async def test_the_library_is_every_app_the_account_installed_and_says_why_when_it_cannot_be_read(monkeypatch):
    asked = []

    async def query(slot, body):
        asked.append((slot, body.package, body.file, body.queries["apps"][0]))
        return {"ok": True, "results": {"apps": {"columns": ["doc_id", "since"], "rows": [["com.dd.doordash", MAY_2024], ["com.old", None]]}}}

    monkeypatch.setattr(play.app_data, "query", query)
    assert await play.library("device-1") == ({"com.dd.doordash": MAY_2024, "com.old": None}, None)
    assert asked[0][:3] == ("device-1", "com.android.vending", r"library\.db") and "doc_type = 1" in asked[0][3]

    async def no_root(slot, body):
        raise HTTPException(503, "Could not copy the app's data from the phone: permission denied")

    async def empty(slot, body):
        return {"ok": True, "results": {"apps": {"columns": [], "rows": []}}}

    monkeypatch.setattr(play.app_data, "query", no_root)
    assert await play.library("device-1") == (None, "Could not copy the app's data from the phone: permission denied")
    monkeypatch.setattr(play.app_data, "query", empty)  # no Google account yet: nothing is known, not "never installed"
    assert await play.library("device-1") == (None, "the Play Store has no library yet")


class FakeChrome:
    """Chrome's browser DevTools socket: a hidden tab on play.google.com; `bounce` answers the first read of each
    query with Play's sign-in bounce, until a results page was loaded in the tab."""

    def __init__(self, answers, bounce=()):
        self.answers, self.bounce = answers, set(bounce)
        self.sent, self.replies, self.navigated = [], [], []

    async def send(self, text):
        m = json.loads(text)
        self.sent.append(m)
        method, result = m["method"], {}
        expression = m.get("params", {}).get("expression", "")
        if method == "Target.createTarget":
            result = {"targetId": "T1"}
        elif method == "Target.attachToTarget":
            result = {"sessionId": "S1"}
        elif method == "Page.navigate":
            self.navigated.append(m["params"]["url"])
        elif method == "Runtime.evaluate" and expression.startswith("location.host"):
            page = "/store/search" if self.navigated else "/robots.txt"
            result = {"result": {"value": f"play.google.com{page} complete"}}
        elif method == "Runtime.evaluate":
            queries = json.loads(expression[expression.rindex(")(") + 2:expression.rindex(", ")])
            value = [{"bounce": True} if q in self.bounce and not self.navigated else self.answers.get(q) for q in queries]
            result = {"result": {"value": value}}
        self.replies.append(json.dumps({"id": m["id"], "result": result}))

    async def recv(self):
        return self.replies.pop(0)

    def methods(self):
        return [m["method"] for m in self.sent]


@pytest.fixture
def chrome(monkeypatch):
    state = {"prepare": "pid=9211\n", "packages": "package:com.dd.doordash\npackage:com.android.chrome\n",
             "chrome": None, "commands": [], "library": ({"com.instacart.client": MAY_2024}, None)}

    async def adb(slot, *args, timeout=25, include_stderr=False):
        state["commands"].append(args)
        return (state["packages"] if args[1] == "pm list packages" else state["prepare"]).encode()

    async def browser_path(slot):
        web.ports[slot] = 1
        return "/devtools/browser/x"

    @contextlib.asynccontextmanager
    async def connect(url, **_):
        yield state["chrome"]

    async def library(slot):
        return state["library"]

    monkeypatch.setattr(play.core, "adb", adb)
    monkeypatch.setattr(web, "browser_path", browser_path)
    monkeypatch.setattr(play.websockets, "connect", connect)
    monkeypatch.setattr(play, "library", library)
    return state


async def test_play_is_read_in_a_hidden_tab_and_its_apps_marked_with_the_user_s(chrome):
    chrome["chrome"] = fake = FakeChrome({"food delivery": {"apps": [DOORDASH, INSTACART]},
                                          "com.nope.app": {"error": "Play has no app com.nope.app"}})
    answers = await play.apps("device-1", ["food delivery", "com.nope.app"])
    assert answers == [
        {"query": "food delivery", "found": (
            'Play app "DoorDash - Food Delivery" DoorDash · com.dd.doordash · 4.8★ · on this phone · Restaurants and groceries delivered.\n'
            'Play app "Instacart: Get Food Delivery" Instacart · com.instacart.client · 4.2★ · '
            "in the user's Play library since May 2024, not on this phone\n"
            'Play "food delivery" search Google Play · 2 apps · 2 the user has or had')},
        {"query": "com.nope.app", "error": "Play has no app com.nope.app"},
    ]
    create = next(m for m in fake.sent if m["method"] == "Target.createTarget")
    assert create["params"] == {"url": "https://play.google.com/robots.txt", "background": True, "hidden": True}
    assert fake.methods()[-1] == "Target.closeTarget" and not fake.navigated  # nothing loaded, only fetched
    assert "am unfreeze" in chrome["commands"][0][1] and "tempwhitelist" in chrome["commands"][0][1]


async def test_play_s_sign_in_bounce_is_passed_by_loading_one_results_page(chrome):
    chrome["chrome"] = fake = FakeChrome({"food delivery": {"apps": [DOORDASH]}, "groceries": {"apps": [INSTACART]}},
                                         bounce=["food delivery", "groceries"])
    answers = await play.apps("device-1", ["food delivery", "groceries"])
    assert fake.navigated == ["https://play.google.com/store/search?c=apps&q=food+delivery"]  # once, for the first
    assert [a["found"].splitlines()[0].split(" · ")[1] for a in answers] == ["com.dd.doordash", "com.instacart.client"]
    assert fake.methods()[-1] == "Target.closeTarget"


async def test_a_bounce_that_does_not_pass_is_an_error_for_that_query_only(chrome, monkeypatch):
    chrome["chrome"] = FakeChrome({"groceries": {"apps": [INSTACART]}}, bounce=["food delivery"])

    async def never(chrome, session, seconds, path):
        return path == "/robots.txt"  # the results page never finishes loading

    monkeypatch.setattr(play, "loaded", never)
    answers = await play.apps("device-1", ["food delivery", "groceries"])
    assert answers[0] == {"query": "food delivery", "error": "Google Play's sign-in check did not pass"}
    assert answers[1]["found"].startswith('Play app "Instacart')


async def test_without_chrome_running_play_is_not_searched(chrome):
    chrome["prepare"] = "pid=\n"
    with pytest.raises(web.WebUnavailable, match="Chrome is not running"):
        await play.apps("device-1", ["food delivery"])
