"""The gateway's Google search for look_up: a hidden tab in the phone's Chrome, never on screen."""

import contextlib
import json

import pytest

from app.gateway import web


def test_prepare_says_whether_chrome_runs():
    assert web.chrome_running("pid=9211\n") is True
    assert web.chrome_running("pid=\n") is False


def test_found_lines_put_the_overview_and_answer_before_the_results():
    answer = {
        "overview": "Rayleigh scattering makes the sky blue.",
        "answer": None,
        "results": [
            {"title": "Why Is the Sky Blue?", "source": "NASA Space Place", "url": "https://spaceplace.nasa.gov/blue-sky/",
             "snippet": "Blue light is scattered more."},
            {"title": "Sky", "source": "Wikipedia", "url": "https://en.wikipedia.org/wiki/Sky", "snippet": ""},
        ],
    }
    assert web.found_lines(answer) == (
        "Overview Rayleigh scattering makes the sky blue.\n"
        'Result "Why Is the Sky Blue?" NASA Space Place · https://spaceplace.nasa.gov/blue-sky/ · Blue light is scattered more.\n'
        'Result "Sky" Wikipedia · https://en.wikipedia.org/wiki/Sky'
    )
    assert web.found_lines({"overview": None, "answer": "3:26 pm Tuesday", "results": []}) == "Answer 3:26 pm Tuesday"


class FakeChrome:
    """Chrome's browser DevTools socket: a hidden tab that loads google.com on the second look."""

    def __init__(self, answers, fail_reader=False, after_check=None):
        self.answers, self.fail_reader, self.after_check = answers, fail_reader, after_check
        self.navigated = []
        self.sent, self.replies, self.looks = [], [], 0

    async def send(self, text):
        m = json.loads(text)
        self.sent.append(m)
        method, result = m["method"], {}
        if method == "Target.createTarget":
            result = {"targetId": "T1"}
        elif method == "Target.attachToTarget":
            result = {"sessionId": "S1"}
        elif method == "Runtime.evaluate" and m["params"]["expression"].startswith("location.host"):
            self.looks += 1
            result = {"result": {"value": "www.google.com complete" if self.looks > 1 else " complete"}}
        elif method == "Page.navigate":
            self.navigated.append(m["params"]["url"])
        elif method == "Runtime.evaluate" and "#rso" in m["params"]["expression"] and "=>" not in m["params"]["expression"]:
            result = {"result": {"value": True}}
        elif method == "Runtime.evaluate" and m["params"]["expression"].endswith(", true)"):  # the page as shown
            value = [{"overview": None, "answer": "Now 21° Cloudy · 60% rain at 2 pm", "results": [], "drawn": False}]
            result = {"result": {"value": value}}
        elif method == "Runtime.evaluate":
            value = self.after_check if self.navigated and self.after_check is not None else self.answers
            result = ({"exceptionDetails": {"text": "Uncaught TypeError"}} if self.fail_reader
                      else {"result": {"value": value}})
        self.replies.append(json.dumps({"id": m["id"], "result": result}))

    async def recv(self):
        return self.replies.pop(0)

    def methods(self):
        return [m["method"] for m in self.sent]


@pytest.fixture
def chrome(monkeypatch):
    state = {"prepare": "pid=9211\n", "chrome": None, "commands": []}

    async def adb(slot, *args, timeout=25, include_stderr=False):
        state["commands"].append(args)
        return state["prepare"].encode()

    async def browser_path(slot):
        web.ports[slot] = 1
        return "/devtools/browser/x"

    @contextlib.asynccontextmanager
    async def connect(url, **_):
        yield state["chrome"]

    monkeypatch.setattr(web.core, "adb", adb)
    monkeypatch.setattr(web, "browser_path", browser_path)
    monkeypatch.setattr(web.websockets, "connect", connect)
    return state


async def test_each_look_up_opens_a_hidden_tab_and_closes_it(chrome):
    chrome["chrome"] = fake = FakeChrome([
        {"overview": "Rayleigh scattering.", "answer": None, "results": []},
        {"error": "Google asked to prove this is not a robot"},
    ])
    answers = await web.google("device-1", ["sky blue", "fridge"])
    assert answers == [{"query": "sky blue", "found": "Overview Rayleigh scattering."},
                       {"query": "fridge", "error": "Google asked to prove this is not a robot"}]
    create = next(m for m in fake.sent if m["method"] == "Target.createTarget")
    assert create["params"]["hidden"] is True and create["params"]["background"] is True
    assert fake.methods()[-1] == "Target.closeTarget"
    # Chrome is unfrozen and let through Doze before DevTools is asked anything
    assert "am unfreeze" in chrome["commands"][0][1] and "tempwhitelist" in chrome["commands"][0][1]


async def test_google_s_javascript_check_is_passed_by_loading_one_results_page(chrome):
    chrome["chrome"] = fake = FakeChrome(
        [{"overview": None, "answer": "3:26 pm", "results": []}, {"check": True}, {"check": True}],
        after_check=[{"overview": "Clogged filter.", "answer": None, "results": []}, {"overview": "Rayleigh.", "answer": None, "results": []}])
    answers = await web.google("device-1", ["time in new york", "dishwasher not draining", "sky blue"])
    assert fake.navigated == ["https://www.google.com/search?q=dishwasher+not+draining"]  # once, for the first
    assert answers == [{"query": "time in new york", "found": "Answer 3:26 pm"},
                       {"query": "dishwasher not draining", "found": "Overview Clogged filter."},
                       {"query": "sky blue", "found": "Overview Rayleigh."}]
    reruns = [m for m in fake.sent if m["method"] == "Runtime.evaluate" and "=>" in m["params"]["expression"]]
    assert '["dishwasher not draining", "sky blue"]' in reruns[-1]["params"]["expression"]  # only the checked ones again


async def test_the_hidden_tab_is_closed_when_the_reader_fails(chrome):
    chrome["chrome"] = fake = FakeChrome([], fail_reader=True)
    with pytest.raises(web.WebUnavailable, match="page script failed"):
        await web.google("device-1", ["sky blue"])
    assert fake.methods()[-1] == "Target.closeTarget"


async def test_without_chrome_running_nothing_is_searched(chrome):
    chrome["prepare"] = "pid=\n"
    with pytest.raises(web.WebUnavailable, match="Chrome is not running"):
        await web.google("device-1", ["sky blue"])


async def test_a_card_google_draws_by_script_is_read_from_the_page_as_shown(chrome):
    chrome["chrome"] = fake = FakeChrome([
        {"overview": None, "answer": None, "results": [], "drawn": True},
        {"overview": "Rayleigh.", "answer": None, "results": [], "drawn": False},
    ])
    answers = await web.google("device-1", ["will it rain tomorrow", "sky blue"])
    assert fake.navigated == ["https://www.google.com/search?q=will+it+rain+tomorrow"]  # only the drawn card's query
    assert answers == [{"query": "will it rain tomorrow", "found": "Answer Now 21° Cloudy · 60% rain at 2 pm"},
                       {"query": "sky blue", "found": "Overview Rayleigh."}]


async def test_a_chrome_with_no_tab_loaded_gets_a_new_tab_page_and_is_asked_again(chrome):
    """Chrome refuses a hidden tab while it has no tab loaded: one plain tab, in the background, and it makes them."""
    class NoTab(FakeChrome):
        plain = 0

        async def send(self, text):
            m = json.loads(text)
            if m["method"] == "Target.createTarget" and not m["params"].get("hidden"):
                self.plain += 1
            if m["method"] == "Target.createTarget" and m["params"].get("hidden") and not self.plain:
                self.sent.append(m)
                self.replies.append(json.dumps({"id": m["id"], "error": {
                    "code": -32000, "message": "Hidden target can be created only when remote debugging is enabled"}}))
                return
            await super().send(text)

    chrome["chrome"] = fake = NoTab([{"overview": "Rayleigh scattering.", "answer": None, "results": []}])
    assert await web.google("device-1", ["sky blue"]) == [{"query": "sky blue", "found": "Overview Rayleigh scattering."}]
    made = [m["params"] for m in fake.sent if m["method"] == "Target.createTarget"]
    assert [p.get("hidden", False) for p in made] == [True, True, False, True]
    assert made[2] == {"url": web.NEW_TAB, "background": True} and fake.plain == 1


async def test_another_refusal_of_the_hidden_tab_is_reported_as_it_is(chrome):
    class Refuses(FakeChrome):
        async def send(self, text):
            m = json.loads(text)
            self.sent.append(m)
            self.replies.append(json.dumps({"id": m["id"], "error": {"code": -32000, "message": "Not allowed"}}))

    chrome["chrome"] = fake = Refuses([])
    with pytest.raises(web.WebUnavailable, match="Not allowed"):
        await web.google("device-1", ["sky blue"])
    assert len(fake.sent) == 1
