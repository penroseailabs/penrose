"""A phone agent's session: the state it reads, checked calls, incidents, notes, guards, screenshots."""

import re

import pytest
from app.agents.registry import HANDOVER, SUBAGENT, VERIFIER
from app.bridge.tools import MAX_BURST
from app.config import settings
from app.errors import DeviceError, NeedUser, PhoneSetupError
from app.runtime.phone import PhoneAgent, clock, describe_call
from app.runtime.runner import Runner
from app.tools.device import AdbCommand, InstallApp, finance_package
from app.tools.subagent import Report, Unfinished
from app.tools.verifier import Verdict
from fakes import (
    FakeDevices,
    Script,
    checkpoint,
    click,
    handover,
    log,
    make_state,
    note,
    phone,
    report,
    step_spec,
    verdict,
)
from phone import FakePhone, element, field, label, screen_of, snapshot

JPEG = b"\xff\xd8fake-jpeg\xff\xd9"
SHOT = {"tool": "request_screenshot"}


def turn_no(text):
    return int(re.search(r"^Turn: (\d+)\.", text, re.M).group(1))


def section(text, name):
    """One labelled part of the state text ("Screen", "Notices"...), without its label; None when absent."""
    for part in text.split("\n\n"):
        if part.startswith(f"{name}:"):
            return part[len(name) + 1:].strip()
    return None


def last_result(text):
    return re.search(r"^Last result: (.*)$", text, re.M).group(1)


def incident(text):
    return re.search(r"^Incident \((\d+) in a row\): (.*)$", text, re.M)


def number(text, word):
    """The element number the screen text gives the first line with `word`."""
    return int(re.search(rf"\[(\d+)\][^\n]*{word}", section(text, "Screen")).group(1))


class Session:
    """One phone-agent session on a fake phone, with `decide(text)` as the model."""

    def __init__(self, store, decide, phone=None, devices=None, max_turns=None, agent=SUBAGENT, handover=None,
                 summarize=None):
        self.devices = devices or FakeDevices(phone or FakePhone(screen_of("First"), screen_of("Finished")), jpeg=JPEG)
        self.state = make_state(store, steps=[step_spec("a")], slot="device-1")
        self.step = self.state["plan"][0]
        self.agent = agent
        self.model = Script(**{agent.name: lambda text, n: decide(text)},
                            **({"Checkpoint": lambda text, n: summarize(text)} if summarize else {}))
        self.runner = Runner(store, model=self.model, devices=self.devices)
        self.max_turns = max_turns or settings.subagent_max_turns
        self.handover = handover

    async def brief(self, session):
        return {"step": {"id": "a", "goal": "Do a"}}

    def session(self, fresh=True):
        final = (lambda reason: Report(status="failed", summary=reason)) if self.agent is SUBAGENT else (
            lambda reason: Verdict(passed=False, reason=reason))
        return PhoneAgent(self.runner, self.state, self.step, self.agent, brief=self.brief, max_turns=self.max_turns,
                          fresh=fresh, give_up=final, handover=self.handover)

    async def run(self, fresh=True):
        return await self.session(fresh).run()

    @property
    def calls(self):
        return self.model.of(self.agent.name)

    @property
    def mem(self):
        return self.step["sessions"][self.agent.prompt]

    @property
    def phone(self):
        return self.devices.phone


async def run(store, decide, **kw):
    s = Session(store, decide, **kw)
    return await s.run(), s


def then_report(first, *later):
    """A model that does `first` on turn 1, each of `later` on the turns after, then reports."""
    turns = [first, *later]
    return lambda text: turns[turn_no(text) - 1] if turn_no(text) <= len(turns) else phone(report())


# --- the loop and what it reads ----------------------------------------------------------------------

async def test_it_taps_by_number_looks_again_and_reports(store):
    def decide(text):
        if turn_no(text) == 1:
            assert "First" in section(text, "Screen") and last_result(text) == "(no action yet)"
            return phone(click(1))
        assert last_result(text).startswith("tapped [1] Continue") and "Finished" in section(text, "Screen")
        assert section(text, "Recent actions, oldest first").startswith("1. T+00:00 tapped [1] Continue")
        return phone(report(summary="Reached the Finished screen"))

    end, s = await run(store, decide)
    assert isinstance(end, Report) and end.summary == "Reached the Finished screen"
    assert s.phone.calls == [{"cmd": "click", "h": 1}]
    steps = [t for t in s.step["turns"] if t.get("step")]
    assert [x["result"]["status"] for x in steps] == ["completed", "completed"]
    assert steps[0]["goal"] == "interact(element=1, interaction=tap)"


async def test_the_brief_comes_first_and_the_screen_and_its_interactions_last(store):
    end, s = await run(store, lambda text: phone(report()))
    text = s.calls[0]["context"]
    assert text.startswith("step:\nid: a\ngoal: Do a")
    parts = [p.split(":")[0] for p in text.split("\n\n")]
    assert parts[-2:] == ["Screen", "Interactions"]
    assert section(text, "Interactions") == "[1] tap"


async def test_a_needs_input_report_needs_a_question(store):
    end, s = await run(store, then_report(phone(report("needs_input", "Two match."))))
    assert "NOT ACCEPTED: report needs_input needs question" in last_result(s.calls[1]["context"])


LOG = "Your log, oldest first (time @ where you were | what you wrote)"


async def test_the_log_is_kept_stamped_and_old_actions_leave_the_recent_list(store, monkeypatch):
    monkeypatch.setattr(settings, "phone_log_actions", 2)

    def decide(text):
        n = turn_no(text)
        if n == 1:
            assert section(text, LOG) == "(nothing yet)"
            return phone(log("Cart total $62"), click(1))
        if n == 4:
            lines = section(text, LOG).splitlines()
            # Where it was is said once, then only when it changes; a turn without a line gets the runtime's.
            assert lines[0] == "- T+00:00 @ com.example MainActivity | Cart total $62"
            assert lines[1].startswith("- T+00:00 | (runtime) tapped [1] Continue") and len(lines) == 3
            recent = section(text, "Recent actions, oldest first (1 earlier, summed up in your log)").splitlines()
            assert [line.split(".")[0] for line in recent] == ["2", "3"]
            assert section(text, "Screens so far, oldest first") is None  # the log says where it was
            return phone(report())
        return phone(click(1))

    end, s = await run(store, decide, phone=FakePhone(lambda done: screen_of(f"Screen {done}")))
    assert s.mem["log"][0].endswith(" | Cart total $62") and s.mem["notes"] == []
    assert clock(125) == "T+02:05"


async def test_the_verifier_keeps_notes_the_trail_and_many_actions(store, monkeypatch):
    monkeypatch.setattr(settings, "phone_recent_actions", 2)

    def decide(text):
        n = turn_no(text)
        if n == 1:
            return phone(note("Cart total $62"), click(1))
        if n == 4:
            assert section(text, "Your notes") == "- Cart total $62" and section(text, LOG) is None
            recent = section(text, "Recent actions, oldest first (1 earlier not shown)").splitlines()
            assert [line.split(".")[0] for line in recent] == ["2", "3"]
            return phone(verdict())
        return phone(click(1))

    end, s = await run(store, decide, phone=FakePhone(lambda done: screen_of(f"Screen {done}")), agent=VERIFIER)
    assert end.passed and s.mem["notes"] == ["Cart total $62"]


PAGE_TEXT = "Page text (the whole page, also what is below the screen; shown every turn while you stay on this page, so never scroll it to read it)"


def web_page(h, url, title="Results", ready="complete"):
    return element(h, cls="android.webkit.WebView", webview=True, title=title, url=url, page={"nodes": 40, "ready": ready})


def page_shown(text):
    return section(text, PAGE_TEXT)


async def test_read_page_joins_a_turn_and_the_page_stays_shown_while_on_it(store):
    """The agent is already on the page (the runtime read nothing: the fake phone showed it from the start), asks
    for it with read_page in the turn of a tap, and sees the text that turn and the turns after, as long as
    the screen stays on the page."""
    screen = snapshot(web_page(5, "https://shop.example/s?q=tea"), element(1, "Continue"), pkg="com.android.chrome")
    tree = [{"dom": True, "wrole": "link", "text": "Mint candy $4", "c": [
        {"dom": True, "text": "4.5", "desc": "Rated 4.5 out of 5 stars"}]}]

    def decide(text):
        n = turn_no(text)
        if n == 1:
            assert page_shown(text) is None
            return phone(log("Results page open"), click(2), {"tool": "read_page"})
        if n == 2:
            assert last_result(text).startswith("tapped [2] Continue")
            assert "read_page -> read the whole page" in last_result(text)
            assert page_shown(text).splitlines() == ["Results https://shop.example/s?q=tea", "Mint candy $4",
                                                     "  Rated 4.5 out of 5 stars"]
            return phone(log("Mint $4, 4.5 stars"), click(2))
        assert page_shown(text).startswith("Results https://shop.example/s?q=tea")  # still on the page: still shown
        return phone(report())

    phone_ = FakePhone(lambda done: screen)
    phone_.pages = {5: {"ok": True, "title": "Results", "url": "https://shop.example/s?q=tea", "nodes": tree}}
    end, s = await run(store, decide, phone=phone_)
    assert end.status == "completed"
    assert s.phone.calls.count({"cmd": "web_page", "h": 5}) == 1  # read once, after the tap, through the page's handle


async def test_a_page_that_opens_is_read_for_you_and_forgotten_when_you_leave_it(store):
    """A tap lands on a web page: the runtime reads it whole that turn. The text stays while the screen stays on
    that page (no second read) and is replaced when another page opens."""
    pages = {
        5: {"ok": True, "title": "Results", "url": "https://shop.example/s?q=tea", "nodes": [{"dom": True, "text": "Mint candy $4"}]},
        6: {"ok": True, "title": "Mint candy", "url": "https://shop.example/p/mint", "nodes": [{"dom": True, "text": "Mint candy, 100 g, $4"}]},
    }

    def screens(done):
        if done == 0:
            return screen_of("Home")
        if done < 3:
            return snapshot(web_page(5, "https://shop.example/s?q=tea"), element(1, "Continue"), pkg="com.android.chrome")
        return snapshot(web_page(6, "https://shop.example/p/mint", title="Mint candy"), element(1, "Continue"), pkg="com.android.chrome")

    def decide(text):
        n = turn_no(text)
        if n == 1:
            return phone(click(1))
        if n == 2:
            assert "read_page (by the runtime: a new page opened) -> read the whole page (13 characters)" in last_result(text)
            assert page_shown(text).splitlines() == ["Results https://shop.example/s?q=tea", "Mint candy $4"]
            return phone(click(1))
        if n == 3:
            assert "read_page" not in last_result(text)  # the same page: not read again
            assert page_shown(text).splitlines() == ["Results https://shop.example/s?q=tea", "Mint candy $4"]
            return phone(click(1))
        if n == 4:
            assert "read_page (by the runtime: a new page opened)" in last_result(text)
            assert page_shown(text).splitlines() == ["Mint candy https://shop.example/p/mint", "Mint candy, 100 g, $4"]
            return phone({"tool": "press_key", "key": "back"})
        assert page_shown(text) is None  # back on the launcher: the page is gone
        return phone(report())

    phone_ = FakePhone(screens)
    phone_.pages = pages
    phone_.screens = lambda done: screens(done) if done < 4 else screen_of("Home")
    end, s = await run(store, decide, phone=phone_)
    assert end.status == "completed"
    assert [c for c in s.phone.calls if c["cmd"] == "web_page"] == [{"cmd": "web_page", "h": 5}, {"cmd": "web_page", "h": 6}]


async def test_a_page_still_loading_is_not_read_for_you_until_it_has_loaded(store):
    def screens(done):
        if done == 0:
            return screen_of("Home")
        ready = "loading" if done == 1 else "complete"
        return snapshot(web_page(5, "https://shop.example/s", ready=ready), element(1, "Continue"), pkg="com.android.chrome")

    def decide(text):
        n = turn_no(text)
        if n == 1:
            return phone(click(1))
        if n == 2:
            assert "read_page" not in last_result(text) and page_shown(text) is None
            return phone(click(1))
        if n == 3:
            assert "read_page (by the runtime: a new page opened)" in last_result(text)
            return phone(report())

    phone_ = FakePhone(screens)
    phone_.pages = {5: {"ok": True, "nodes": [{"dom": True, "text": "Mint candy $4"}]}}
    end, s = await run(store, decide, phone=phone_)
    assert end.status == "completed"
    assert s.phone.calls.count({"cmd": "web_page", "h": 5}) == 1


async def test_read_page_alone_on_a_page_already_shown_costs_a_turn_and_a_notice(store):
    screen = snapshot(web_page(5, "https://shop.example/s"), element(1, "Continue"), pkg="com.android.chrome")

    def decide(text):
        n = turn_no(text)
        if n == 1:
            return phone(click(1), {"tool": "read_page"})
        if n == 2:
            assert page_shown(text) and section(text, "Notices") is None
            return phone({"tool": "read_page"})
        assert "That turn only read the page again" in section(text, "Notices")
        return phone(report())

    phone_ = FakePhone(lambda done: screen)
    phone_.pages = {5: {"ok": True, "nodes": [{"dom": True, "text": "Mint candy $4"}]}}
    end, s = await run(store, decide, phone=phone_)
    assert end.status == "completed"


async def test_read_page_on_a_page_still_loading_says_it_may_be_incomplete(store):
    page = element(5, cls="android.webkit.WebView", webview=True, title="Results", url="https://shop.example/s",
                   page={"nodes": 3, "ready": "loading"})
    phone_ = FakePhone(lambda done: snapshot(page, pkg="com.android.chrome"))
    phone_.pages = {5: {"ok": True, "nodes": [{"dom": True, "text": "Mint candy $4"}]}}
    end, s = await run(store, then_report(phone({"tool": "read_page"})), phone=phone_)
    assert "it was still loading, so it may be incomplete" in last_result(s.calls[1]["context"])


async def test_read_page_without_a_web_page_says_so(store):
    end, s = await run(store, then_report(phone({"tool": "read_page"})))
    assert "read_page -> NOT RUN: no web page is on screen" in last_result(s.calls[1]["context"])


async def test_a_note_from_an_agent_that_keeps_a_log_is_kept_as_a_log_line(store):
    def decide(text):
        if turn_no(text) == 1:
            return phone(note("Cart total $62"), click(1))
        assert "note -> kept as your log (you have no note tool)" in last_result(text)
        assert "Cart total $62" in section(text, "Your log, oldest first (time @ where you were | what you wrote)")
        return phone(report())

    end, s = await run(store, decide)
    assert end.status == "completed" and s.mem["notes"] == []


async def test_an_unknown_tool_is_refused_with_the_tools_it_can_use(store):
    end, s = await run(store, then_report(phone({"tool": "teleport"})))
    assert "unknown tool 'teleport'" in last_result(s.calls[1]["context"]) and "interact" in last_result(s.calls[1]["context"])


# --- the check before each action, and incidents -----------------------------------------------------

async def test_a_stale_number_is_refused_and_stays_an_incident_until_an_action_works(store):
    end, s = await run(store, then_report(phone(click(9)), phone(click(1))))
    assert s.phone.calls == [{"cmd": "click", "h": 1}]  # the refused tap never reached the phone
    assert incident(s.calls[0]["context"]) is None
    assert incident(s.calls[1]["context"]).group(1) == "1"
    assert "element [9] has no interactions on this screen" in last_result(s.calls[1]["context"])
    assert incident(s.calls[2]["context"]) is None  # the tap that worked closed it


async def test_three_refused_turns_in_a_row_end_the_session(store):
    end, s = await run(store, lambda text: phone(click(8 + turn_no(text))))
    assert incident(s.calls[2]["context"]).group(1) == "2"
    assert end.status == "failed" and end.summary.startswith("The runtime ended the session: 3 turns in a row had no call that could run")
    assert len(s.calls) == 3


async def test_a_covered_control_offers_nothing_and_is_refused(store):
    screen = snapshot(element(1, "Pay", flags=("click", "covered")), element(2, "Close"))
    end, s = await run(store, then_report(phone(click(1))), phone=FakePhone(screen))
    assert "[1]" not in section(s.calls[0]["context"], "Interactions")
    assert "has no interactions on this screen" in last_result(s.calls[1]["context"]) and s.phone.calls == []


async def test_a_password_the_user_gave_can_be_typed(store):
    screen = snapshot(field(1, flags=("edit", "click", "password")))
    typing = {"tool": "interact", "element": 1, "interaction": "type", "value": "hunter2"}
    end, s = await run(store, then_report(phone(typing)), phone=FakePhone(screen))
    assert [c["cmd"] for c in s.phone.calls] == ["set_text"] and s.phone.calls[0]["text"] == "hunter2"


async def test_type_and_submit_types_then_presses_the_keyboard_key(store):
    screen = snapshot(field(2, hint="Search"), element(1, "Go"))

    def decide(text):
        if turn_no(text) > 1:
            return phone(report())
        return phone({"tool": "type_and_submit", "element": number(text, "Field"), "text": "pasta"})

    end, s = await run(store, decide, phone=FakePhone(screen))
    assert [c["cmd"] for c in s.phone.calls] == ["set_text", "ime_action"] and s.phone.calls[0]["text"] == "pasta"


async def test_a_burst_acts_on_the_screen_the_turn_was_decided_on_and_looks_once(store):
    first = snapshot(element(1, "Menu"), element(2, "Settings"))
    later = snapshot(element(3, "Other"))  # the screen after the first tap has other numbers
    end, s = await run(store, then_report(phone(click(1), click(2))), phone=FakePhone(first, later))
    assert [c["h"] for c in s.phone.calls] == [1, 2]
    recent = section(s.calls[1]["context"], "Recent actions, oldest first").splitlines()
    assert recent[0].endswith("(no look: part of a burst)") and "Other" in section(s.calls[1]["context"], "Screen")


async def test_a_burst_runs_at_most_the_limit(store):
    end, s = await run(store, then_report(phone(*[click(1)] * (MAX_BURST + 1))),
                       phone=FakePhone(lambda done: screen_of(f"Screen {done}")))
    assert len(s.phone.calls) == MAX_BURST
    result = last_result(s.calls[1]["context"])
    assert f"only the first {MAX_BURST} screen actions of a turn are run; NOT RUN, send these next turn: " in result
    assert "Continue" in result.split("NOT RUN")[1]  # the dropped call is named


async def test_a_wait_may_close_a_turn_of_screen_calls(store):
    wait = {"tool": "wait_for", "text": "Finished"}
    end, s = await run(store, then_report(phone(click(1), wait, click(1))))
    assert [c["h"] for c in s.phone.calls] == [1]  # the tap, then the wait; the call after it is not run
    assert s.devices.op_log == [{"op": "wait_for", "text": "Finished", "timeout": 5.0}]
    result = last_result(s.calls[1]["context"])
    assert "wait_for(text='Finished') -> wait_for done" in result and "the calls after wait_for were not run" in result


async def test_a_device_call_runs_alone_and_a_report_cannot_share_its_turn(store):
    adb = {"tool": "adb_command", "command": "adb shell pm list packages"}
    end, s = await run(store, then_report(phone(click(1), adb), phone(click(1), report())),
                       phone=FakePhone(lambda done: screen_of(f"Screen {done}")))
    assert "adb_command must be the only call of its turn, so it was not run" in last_result(s.calls[1]["context"])
    assert "report must be the only call of its turn; nothing was run" in last_result(s.calls[2]["context"])
    assert len(s.phone.calls) == 1 and s.devices.actions == []


async def test_a_verifier_only_looks(store):
    screen = snapshot(field(2, "8 servings", hint="Servings"), element(1, "Go"))
    typing = {"tool": "interact", "element": 1, "interaction": "type", "value": "x"}
    long_press = {"tool": "interact", "element": 2, "interaction": "long_press"}
    turns = {1: phone(typing), 2: phone(long_press)}
    end, s = await run(store, lambda text: turns.get(turn_no(text)) or phone({"tool": "verdict", "passed": True, "reason": "ok"}),
                       phone=FakePhone(screen), agent=VERIFIER)
    assert "a check types and submits only in a search box" in last_result(s.calls[1]["context"])
    assert "you only look: interact takes tap, scroll_up" in last_result(s.calls[2]["context"]) and s.phone.calls == []
    assert isinstance(end, Verdict) and end.passed


async def test_a_verifier_may_search(store):
    # Checking that a recipe was added among 43 needs the list's search box (2026-11-15).
    screen = snapshot(field(2, hint="Search recipes", id="search_src_text"))
    typing = {"tool": "interact", "element": 1, "interaction": "type", "value": "Avocado Toast with Egg"}
    end, s = await run(store, lambda text: phone(typing) if turn_no(text) == 1 else phone({"tool": "verdict", "passed": True, "reason": "ok"}),
                       phone=FakePhone(screen), agent=VERIFIER)
    assert s.phone.calls and s.phone.calls[0]["cmd"] == "set_text"


async def test_a_field_filled_by_a_picker_is_tapped_not_typed_into(store):
    # OpenTracks' date filter showed the typed date while the app kept its own (2026-11-15).
    screen = snapshot(field(13, "Sunday, October 15, 2023", hint="From", flags=("edit", "click", "picker")))
    typing = {"tool": "interact", "element": 1, "interaction": "type", "value": "10/09/2023"}
    end, s = await run(store, then_report(phone(typing)), phone=FakePhone(screen))
    assert "is filled by the picker a tap on it opens" in last_result(s.calls[1]["context"]) and s.phone.calls == []
    assert "{picker}" in section(s.calls[0]["context"], "Screen")


async def test_a_verifier_cannot_tap_a_control_that_changes_something(store):
    # A verifier that doubted a deletion deleted the second copy itself (2026-11-14).
    screen = snapshot(label(5, "Tutoring Services $100.16"), element(1, "Delete", id="btn_delete"), element(2, "Back"))
    tap = lambda n: {"tool": "interact", "element": n, "interaction": "tap"}
    turns = {1: phone(tap(1)), 2: phone(tap(2))}
    end, s = await run(store, lambda text: turns.get(turn_no(text)) or phone({"tool": "verdict", "passed": True, "reason": "ok"}),
                       phone=FakePhone(screen), agent=VERIFIER)
    assert 'a check only looks, and "Delete" would change something' in last_result(s.calls[1]["context"])
    assert s.phone.calls == [{"cmd": "click", "h": 2}]  # Back is fine; Delete never reached the phone


# --- no coordinates; an app without the bridge ----------------------------------------------------

async def test_there_are_no_coordinates_to_tap(store):
    assert not {"tap_at", "tap"} & {f["function"]["name"] for f in SUBAGENT.functions}
    end, s = await run(store, then_report(phone({"tool": "tap_at", "points": [[500, 250]], "description": "field"})))
    assert "unknown tool 'tap_at'" in last_result(s.calls[1]["context"]) and s.phone.taps == []


def unhooked(pkg="com.target.ui"):
    """`pkg` on screen, drawn by a process the bridge is not loaded in."""
    snap = snapshot(pkg=pkg)
    snap["windows"] = [{**snap["windows"][0], "kind": "unreadable", "why": "no bridge in this process", "nodes": []}]
    snap.update(process="no-bridge", processes=[], bridge_snapshots={})
    return snap


class Restarting(FakeDevices):
    """Its app comes up with the bridge after `hooks_after` launches (never when None)."""

    def __init__(self, hooks_after):
        self.launches, self.hooks_after = 0, hooks_after
        hooked = snapshot(field(6, hint="Mobile Number"), element(8, "CONTINUE"), pkg="com.target.ui")
        super().__init__(FakePhone(lambda done: hooked if hooks_after and self.launches >= hooks_after else unhooked()))

    async def ops(self, slot, owner, ops):
        self.launches += sum(op["op"] == "launch" for op in ops)
        return await super().ops(slot, owner, ops)


@pytest.fixture
def quick_rehook(monkeypatch):
    import app.runtime.phone as module
    monkeypatch.setattr(module, "REHOOK_WAIT", 0)


async def test_an_app_running_without_the_bridge_is_restarted_and_read(store, quick_rehook):
    end, s = await run(store, lambda text: phone(report()), devices=Restarting(hooks_after=1))
    first = s.calls[0]["context"]
    assert s.devices.op_log == [{"op": "stop", "package": "com.target.ui"}, {"op": "launch", "package": "com.target.ui"}]
    assert s.devices.rescoped == ["com.target.ui"]  # put in the bridge's scope before the restart
    assert "com.target.ui was running without the bridge" in section(first, "Notices")
    assert "can be read now" in section(first, "Notices")
    assert 'Field "Mobile Number"' in section(first, "Screen") or "Mobile Number" in section(first, "Screen")
    assert s.calls[0]["images"] == []


async def test_an_app_is_restarted_once_then_the_agent_is_told_it_cannot_be_read(store, quick_rehook):
    end, s = await run(store, then_report(phone({"tool": "press_key", "key": "back"})), devices=Restarting(hooks_after=None))
    assert [op["op"] for op in s.devices.op_log] == ["stop", "launch"]
    assert "the bridge still did not load" in section(s.calls[0]["context"], "Notices")
    assert "still runs without the bridge" in section(s.calls[1]["context"], "Notices")
    assert s.calls[0]["images"] == [] and s.calls[1]["images"] == []
    assert s.devices.rescoped == ["com.target.ui"]  # once, like the restart


async def test_an_app_the_scope_pass_cannot_add_says_so(store, quick_rehook):
    devices = Restarting(hooks_after=None)
    devices.scoped = False
    end, s = await run(store, then_report(phone({"tool": "press_key", "key": "back"})), devices=devices)
    assert "not in the bridge's scope" in section(s.calls[0]["context"], "Notices")


async def test_a_phone_with_no_bridge_anywhere_ends_the_run(store, quick_rehook):
    devices = Restarting(hooks_after=None)

    async def no_bridges():
        return []

    devices.phone.bridges = no_bridges
    with pytest.raises(PhoneSetupError, match="penrose bridge is not running"):
        await run(store, then_report(phone({"tool": "press_key", "key": "back"})), devices=devices)


# --- scrolling, swiping, keys ---------------------------------------------------------------------

async def test_a_list_scrolls_by_its_number(store):
    screen = snapshot(element(7, flags=("list", "can_scroll_down"), cls="androidx.recyclerview.widget.RecyclerView"))
    scroll = {"tool": "interact", "element": 1, "interaction": "scroll_down"}
    end, s = await run(store, then_report(phone(scroll)), phone=FakePhone(screen))
    assert s.phone.calls == [{"cmd": "scroll", "h": 7, "dir": "down"}]


async def test_a_swipe_crosses_the_whole_screen(store):
    end, s = await run(store, then_report(phone({"tool": "swipe", "direction": "up"})))
    assert s.phone.swipes == [(540, 1680, 540, 480, 300)]


async def test_system_keys_and_a_keyboard_that_is_not_shown(store):
    end, s = await run(store, then_report(phone({"tool": "press_key", "key": "back"}), phone({"tool": "press_key", "key": "hide_keyboard"})))
    assert s.phone.keys == ["back"]
    assert last_result(s.calls[1]["context"]).startswith("pressed Android Back")
    assert "the keyboard is not shown" in last_result(s.calls[2]["context"])


# --- device calls -------------------------------------------------------------------------------

async def test_device_calls_go_through_the_ops_lane_and_say_what_they_did(store):
    end, s = await run(store, then_report(
        phone({"tool": "open_app", "app": "com.instacart.client"}),
        phone({"tool": "set_setting", "name": "wifi", "value": "off"}),
    ))
    assert s.devices.op_log == [{"op": "launch", "package": "com.instacart.client"}, {"op": "wifi", "value": "off"}]
    assert last_result(s.calls[1]["context"]) == "open_app(com.instacart.client) -> launch done"
    assert last_result(s.calls[2]["context"]) == "set_setting(wifi=off) -> wifi done"
    assert [t["action"] for t in s.step["turns"] if t.get("action")] == ["open_app", "set_setting"]


async def test_a_failed_device_call_is_an_incident(store):
    class Refusing(FakeDevices):
        async def ops(self, slot, owner, ops):
            return [{"op": ops[0]["op"], "ok": False, "summary": "the setting did not change"}]

    end, s = await run(store, then_report(phone({"tool": "set_setting", "name": "location", "value": "on"})), devices=Refusing())
    assert last_result(s.calls[1]["context"]) == "set_setting(location=on) -> FAILED: the setting did not change"
    assert incident(s.calls[1]["context"]).group(1) == "1"


async def test_adb_results_and_refusals_come_back_as_results(store):
    end, s = await run(store, then_report(
        phone({"tool": "adb_command", "command": "adb shell pm list packages"}),
        phone({"tool": "adb_command", "command": "adb shell input tap 5 5"}),
        phone({"tool": "adb_command", "command": "adb shell uiautomator dump"}),
    ))
    assert s.devices.actions == [{"kind": "adb_command", "command": "adb shell pm list packages"}]
    assert last_result(s.calls[1]["context"]) == "adb_command -> ok: ran adb shell pm list packages"
    assert last_result(s.calls[2]["context"]).startswith("adb_command -> NOT RUN: on-screen input")
    assert incident(s.calls[2]["context"]) is not None
    assert last_result(s.calls[3]["context"]).startswith("adb_command -> NOT RUN: Screen shows the screen")
    assert [t["command"] for t in s.step["turns"] if t.get("action")][0] == "adb shell pm list packages"


async def test_install_app_reports_what_happened(store):
    end, s = await run(store, then_report(phone({"tool": "install_app", "package": "com.dd.doordash"})))
    assert last_result(s.calls[1]["context"]) == "install_app -> installed: com.dd.doordash installed"


async def test_a_gateway_that_does_not_answer_marks_the_command_uncertain(store):
    class Silent(FakeDevices):
        async def action(self, slot, owner, action):
            if action["kind"] == "adb_command":
                raise DeviceError("The gateway did not answer.")
            return await super().action(slot, owner, action)

    end, s = await run(store, then_report(phone({"tool": "adb_command", "command": "adb shell am start -d https://x"})), devices=Silent())
    assert last_result(s.calls[1]["context"]).startswith("adb_command -> FAILED: The gateway did not answer.")
    assert [t["status"] for t in s.step["turns"] if t.get("action")] == ["uncertain"]


# --- guards -------------------------------------------------------------------------------------

async def test_a_session_that_goes_in_circles_is_warned_then_ended(store):
    end, s = await run(store, lambda text: phone(click(1)), phone=FakePhone(lambda done: screen_of("Same")))
    assert len(s.calls) == 4
    notices = [section(c["context"], "Notices") or "" for c in s.calls]
    assert notices[0] == notices[1] == ""
    assert "same actions on this same screen 2 times. Each time: (1) tapped [1] Continue" in notices[2]
    assert "(2) tapped [1] Continue" in notices[2] and "2 more times" in notices[2]
    assert end.status == "failed"
    assert end.summary.startswith("The runtime ended the session: the same actions were done on the same screen 4 times.")


async def test_the_same_command_with_the_same_answer_is_warned_then_ended(store):
    # One `cat` of a long file ran twenty times, cut the same way each time (2026-11-15).
    end, s = await run(store, lambda text: phone({"tool": "adb_command", "command": "adb shell cat /sdcard/notes.txt"}))
    assert len(s.calls) == 4
    notices = [section(c["context"], "Notices") or "" for c in s.calls]
    assert "same actions in a row and got the same answer 2 times" in notices[2]
    assert end.status == "failed"
    assert end.summary.startswith("The runtime ended the session: the same actions were done in a row and got the same answer 4 times.")


async def test_the_same_command_again_between_other_actions_is_no_loop(store):
    # Closing and reopening an app that stopped answering is done again and again, with other actions between.
    stop = phone({"tool": "adb_command", "command": "adb shell am force-stop com.example"})
    turns = [stop, phone(click(1))] * 5
    end, s = await run(store, then_report(*turns), phone=FakePhone(lambda done: screen_of(f"Screen {done}")))
    assert end.status == "completed"
    assert not any("got the same answer" in (section(c["context"], "Notices") or "") for c in s.calls)


def handing_over(text):
    """Taps Continue until the runtime asks for the handover, then hands over."""
    if section(text, "Turn").startswith("handover"):
        return phone(handover("Fri 23: AMC Barton Creek 7:50 PM", "control_missing", facts={"fri": "7:50 PM"}))
    return phone(log("Fri 23: AMC Barton Creek 7:50 PM"), click(1))


async def test_a_session_the_runtime_ends_hands_over_in_the_agents_own_words(store):
    s = Session(store, handing_over, phone=FakePhone(lambda done: screen_of("Same")), handover=HANDOVER)
    end = await s.run()
    last = s.calls[-1]["context"]
    assert len(s.calls) == 5 and section(last, "Interactions") is None
    assert "This is your handover turn" in section(last, "Notices") and "4 times" in section(last, "Notices")
    assert isinstance(end, Unfinished) and end.status == "unfinished" and end.facts == {"fri": "7:50 PM"}
    assert end.summary.startswith("The runtime ended the session: the same actions were done on the same screen "
                                  "4 times. Found so far: Fri 23: AMC Barton Creek 7:50 PM")
    assert end.handover["cause"] == "control_missing" and end.handover["next"] == "open the item's own page"
    assert s.step["turns"][-1]["kind"] == "handover" and s.phone.calls == [{"cmd": "click", "h": 1}] * 4


async def test_out_of_turns_also_hands_over(store):
    def decide(text):
        if section(text, "Turn").startswith("handover"):
            return phone(handover("Read two of three cinemas"))
        return phone(click(1))

    s = Session(store, decide, phone=FakePhone(lambda done: screen_of(f"Screen {done}")), max_turns=2, handover=HANDOVER)
    end = await s.run()
    assert end.status == "unfinished" and end.summary.startswith("The session used all its 2 turns. Found so far: Read two")


async def test_without_a_valid_handover_what_the_session_kept_goes_back(store):
    s = Session(store, lambda text: phone(log("Fri 23: 7:50 PM"), click(1)),
                phone=FakePhone(lambda done: screen_of("Same")), handover=HANDOVER)
    end = await s.run()
    assert len(s.calls) == 5  # the handover turn was offered
    assert end.status == "failed" and "Its log: T+00:00 @ com.example MainActivity | Fri 23: 7:50 PM" in end.summary
    assert s.step["turns"][-1]["result"]["status"] == "failed"


async def test_each_turn_reads_the_screens_so_far_and_the_end_of_its_last_thinking(store):
    sat = snapshot(element(1, "Sat", flags=("click", "selected")), element(2, "Sun", flags=("click",)), element(3, "Next"))
    sun = snapshot(element(1, "Sat", flags=("click",)), element(2, "Sun", flags=("click", "selected")), element(3, "Next"))
    detail = snapshot(element(1, "Book"), activity="com.example.DetailActivity")

    def decide(text):
        n = turn_no(text)
        if n == 1:
            assert section(text, "Your thinking last turn (its end)") is None
            return phone(click(3), thought="Saturday is shown. " + "x" * 800 + " Next shows the other day.")
        if n == 2:
            assert last_result(text).endswith("selected now: Sun (was Sat)")
            thinking = section(text, "Your thinking last turn (its end)")
            assert thinking.startswith("…") and thinking.endswith("Next shows the other day.") and len(thinking) == 701
            trail = section(text, "Screens so far, oldest first").splitlines()
            assert [line.split(" ", 2)[2] for line in trail] == [
                "com.example MainActivity · selected: Sat", "com.example MainActivity · selected: Sun"]
            return phone(click(3))
        assert section(text, "Your thinking last turn (its end)") is None  # that turn gave none
        assert section(text, "Screens so far, oldest first").splitlines()[-1].endswith("com.example DetailActivity")
        return phone(verdict())

    # The verifier keeps notes, so it reads the trail; the subagent's log says where it was instead.
    end, s = await run(store, decide, phone=FakePhone(sat, sun, detail), agent=VERIFIER)
    assert end.passed and len(s.calls) == 3


async def test_a_log_line_says_where_it_was_written_when_that_changed(store):
    sat = snapshot(element(1, "Sat", flags=("click", "selected")), element(2, "Sun", flags=("click",)), element(3, "Next"))
    sun = snapshot(element(1, "Sat", flags=("click",)), element(2, "Sun", flags=("click", "selected")), element(3, "Next"))
    end, s = await run(store, then_report(phone(log("Sat: 7:50 PM"), click(3)), phone(log("Sun: none"), click(3)),
                                          phone(log("Sun again"), click(3))), phone=FakePhone(sat, sun, sun, sun))
    assert [line.split(" | ", 1)[0] for line in s.mem["log"]] == [
        "T+00:00 @ com.example MainActivity · selected: Sat", "T+00:00 @ com.example MainActivity · selected: Sun", "T+00:00"]


async def test_the_last_turn_only_accepts_a_final_tool(store):
    end, s = await run(store, lambda text: phone(log("got to Screen 1"), click(1)),
                       phone=FakePhone(lambda done: screen_of(f"Screen {done}")), max_turns=2)
    assert "This is your final turn" in section(s.calls[1]["context"], "Notices")
    assert end.status == "failed" and end.summary.startswith("The session used all its 2 turns. Its log: T+00:00 @ ") and len(s.calls) == 2
    assert " | got to Screen 1" in end.summary and "tapped [1]" in end.summary  # says where it got to


async def test_an_unreadable_screen_is_shown_as_unavailable_not_an_error(store):
    class Unreadable(FakePhone):
        async def snapshot_display(self, visible=None, bridges=None, **kwargs):
            raise DeviceError("The app's bridge is not running.")

    end, s = await run(store, lambda text: phone(report("failed", "cannot see")), phone=Unreadable(screen_of("x")))
    assert section(s.calls[0]["context"], "Screen").startswith("unavailable: ")


# --- asking privately ---------------------------------------------------------------------------

async def test_a_code_is_asked_for_privately_and_the_session_continues_where_it_was(store):
    s = Session(store, lambda text: phone({"tool": "ask_for_password_or_code", "question": "The code?"}))
    with pytest.raises(NeedUser) as asked:
        await s.run()
    assert asked.value.sensitive and asked.value.question == "The code?"
    assert s.mem["open"] is True

    def resumed(text):
        assert last_result(text) == "ask_for_password_or_code -> asked the user: The code?" and turn_no(text) == 2
        return phone(report())

    s.model.handlers["Subagent"] = lambda text, n: resumed(text)
    await s.run(fresh=False)
    assert s.mem["turns"] == 2 and s.mem["open"] is False


# --- screenshots --------------------------------------------------------------------------------

async def test_a_screenshot_comes_only_with_the_turn_after_one_is_asked_for(store):
    def decide(text):
        n = turn_no(text)
        if n == 1:
            return phone(click(1), SHOT)  # act, then see the result
        if n == 2:
            assert section(text, "Screenshot").startswith("attached, the screen now (you asked for it).")
            assert last_result(text).endswith("request_screenshot -> see Screenshot (taken after this turn's actions)")
            return phone(click(1))
        if n == 3:
            assert section(text, "Screenshot") is None
            return phone(SHOT)  # a turn that only asks, with no picture in hand
        return phone(report())

    phone_ = FakePhone(lambda done: screen_of(f"Screen {done}"))
    end, s = await run(store, decide, devices=FakeDevices(phone_, jpeg=JPEG))
    assert [len(c["images"]) for c in s.calls] == [0, 1, 0, 1]
    assert len(phone_.calls) == 2  # the screenshot request is not sent to the phone


async def test_asking_again_with_a_picture_in_hand_and_nothing_run_is_refused(store):
    def decide(text):
        n = turn_no(text)
        if n == 1:
            return phone(SHOT)
        if n == 2:
            return phone(SHOT)  # the picture of the screen now is attached: asking again shows the same
        if n == 3:
            assert last_result(text).startswith("(refused request_screenshot")
            assert "nothing has run since it was taken" in last_result(text)
            assert section(text, "Screenshot") is None
            return phone(click(1))
        return phone(report())

    end, s = await run(store, decide, devices=FakeDevices(jpeg=JPEG))
    assert [len(c["images"]) for c in s.calls] == [0, 1, 0, 0]


async def test_a_screen_without_elements_still_comes_with_no_picture_unasked(store):
    phone_ = FakePhone(snapshot(label(5, "Map")), screen_of("After"))
    end, s = await run(store, then_report(phone({"tool": "press_key", "key": "back"})), devices=FakeDevices(phone_, jpeg=JPEG))
    assert s.calls[0]["images"] == [] and section(s.calls[0]["context"], "Screenshot") is None


async def test_a_screen_showing_a_private_value_is_never_photographed(store):
    phone_ = FakePhone(snapshot(label(5, "[private]"), element(1, "Continue")), screen_of("After"))

    def decide(text):
        n = turn_no(text)
        if n == 1:
            return phone(SHOT)
        if n == 2:
            assert section(text, "Screenshot") == "not taken: a privately entered value is on screen."
            return phone(click(1), SHOT)
        return phone(report())

    end, s = await run(store, decide, devices=FakeDevices(phone_, jpeg=JPEG))
    assert [len(c["images"]) for c in s.calls] == [0, 0, 1]


async def test_a_private_value_is_blacked_out_of_the_picture_when_its_field_is_known(store):
    import io

    from PIL import Image

    white = io.BytesIO()
    Image.new("RGB", (540, 1200), (255, 255, 255)).save(white, "JPEG")
    code_field = field(5, "[private]", b=[100, 1000, 980, 1100])
    phone_ = FakePhone(snapshot(label(4, "Enter the code", b=[100, 900, 980, 980]), code_field, element(1, "Continue", b=[100, 1300, 980, 1400])),
                       screen_of("After"))

    def decide(text):
        n = turn_no(text)
        if n == 1:
            return phone(SHOT)
        if n == 2:
            return phone(click(1))
        return phone(report())

    end, s = await run(store, decide, devices=FakeDevices(phone_, jpeg=white.getvalue()))
    assert [len(c["images"]) for c in s.calls] == [0, 1, 0]
    shown = Image.open(io.BytesIO(s.calls[1]["images"][0])).convert("RGB")
    assert shown.size == (540, 1200)
    # the picture is scaled by its width over the widest bound (540 / 980): the code field (y 1000-1100) lands on y 551-606
    assert shown.getpixel((270, 580)) == (0, 0, 0)
    assert shown.getpixel((270, 500)) == (255, 255, 255) and shown.getpixel((270, 650)) == (255, 255, 255)  # its neighbours


async def test_vision_can_be_switched_off(store, monkeypatch):
    monkeypatch.setattr(settings, "subagent_vision", False)
    end, s = await run(store, then_report(phone(SHOT)), devices=FakeDevices(jpeg=JPEG))
    assert [c["images"] for c in s.calls] == [[], []]


# --- budgets: turns, time, and checkpoints that find nothing new ----------------------------------------

async def test_a_time_budget_ends_the_session_with_a_handover(store):
    def decide(text):
        assert section(text, "Turn").startswith("handover")
        return phone(handover("Read one of three pharmacies: Boots, open until 22:00"))

    s = Session(store, decide, handover=HANDOVER)
    agent = s.session()
    agent.max_seconds = 300
    agent.mem["started"] -= 400  # the budget ran out while the last turn was being decided
    end = await agent.run()
    assert isinstance(end, Unfinished) and end.handover["why_stopped"] == "The session used its time budget of 05:00 minutes."
    assert end.summary.startswith("The session used its time budget of 05:00 minutes. Found so far: Read one of three")
    assert len(s.calls) == 1  # no turn was offered after the budget


async def test_the_agent_reads_its_budget_and_is_warned_near_the_end(store):
    def decide(text):
        assert section(text, "Turn") == "1. Clock: T+08:20 of 10:00."  # the clock is the budget; no turn ceiling
        assert re.search(r"Your time budget ends in about (99|100) seconds", section(text, "Notices"))
        return phone(report())

    s = Session(store, decide)
    agent = s.session()
    agent.max_seconds = 600
    agent.mem["started"] -= 500
    assert (await agent.run()).status == "completed"


async def test_the_wait_for_the_users_code_does_not_count_against_the_budget(store):
    s = Session(store, lambda text: phone({"tool": "ask_for_password_or_code", "question": "The code?"}))
    agent = s.session()
    agent.max_seconds = 60
    with pytest.raises(NeedUser):
        await agent.run()
    s.mem["parked_at"] -= 3600  # the user took an hour to answer

    def resumed(text):
        assert section(text, "Turn").startswith("2. Clock: T+00:0")
        return phone(report())

    s.model.handlers["Subagent"] = lambda text, n: resumed(text)
    agent = s.session(fresh=False)
    agent.max_seconds = 60
    assert (await agent.run()).status == "completed"


async def test_two_checkpoints_that_find_the_same_things_end_the_session(store, monkeypatch):
    monkeypatch.setattr(settings, "phone_checkpoint_turns", 2)

    def decide(text):
        if section(text, "Turn").startswith("handover"):
            return phone(handover("Boots Chorlton open until 22:00", "on_track"))
        return phone(log("Boots Chorlton open until 22:00"), click(1))

    same = lambda text: phone(checkpoint(found=("Boots Chorlton open until 22:00",), plan="[now] find a second one"))  # noqa: E731
    s = Session(store, decide, phone=FakePhone(lambda done: screen_of(f"Screen {done}")), handover=HANDOVER,
                summarize=same)
    end = await s.run()
    assert len(s.model.of("Checkpoint")) == 2 and len(s.calls) == 5  # four turns, then the handover
    assert end.status == "unfinished"
    assert end.summary.startswith("The runtime ended the session: your last 2 checkpoints found the same things, so the 2 turns since turn 2 brought nothing new.")
    assert s.mem["stalls"] == 1


async def test_a_checkpoint_that_finds_something_new_resets_the_stall_count(store, monkeypatch):
    monkeypatch.setattr(settings, "phone_checkpoint_turns", 2)
    found = []

    def summarize(text):
        found.append(f"place {len(found) + 1}")
        return phone(checkpoint(found=tuple(found)))

    s = Session(store, then_report(*[phone(click(1))] * 6), phone=FakePhone(lambda done: screen_of(f"Screen {done}")),
                handover=HANDOVER, summarize=summarize)
    end = await s.run()
    assert end.status == "completed" and len(s.model.of("Checkpoint")) == 3 and s.mem["stalls"] == 0


async def test_a_report_with_too_many_issues_is_cut_to_fit_instead_of_costing_a_turn(store):
    end, s = await run(store, lambda text: phone(report(summary="x" * 1000, issues=["a", "b", "c", "d", "e", "f", "g"])))
    assert end.status == "completed" and len(end.summary) == 1000 and len(end.issues) == 5 and len(s.calls) == 1


# --- web pages that are walls, or empty ------------------------------------------------------------------

def web_screen(title, url, nodes):
    page = element(5, cls="android.webkit.WebView", webview=True, title=title, url=url, page={"nodes": nodes, "ready": "complete"})
    return snapshot(page, element(1, "Continue"), pkg="com.android.chrome")


async def test_a_block_page_is_told_once_and_an_empty_page_gets_a_picture(store):
    def decide(text):
        n = turn_no(text)
        notices = section(text, "Notices") or ""
        if n == 1:
            assert notices.startswith('- The page at www.opentable.com is blocking this browser ("Access Denied")')
            assert "proxy, mirror" in notices and "no readable text" not in notices
            return phone(click(1))
        if n == 2:
            assert "blocking" not in notices  # once per host
            assert "The page at www.opentable.com has come back with no readable text twice in a row" in notices
            assert section(text, "Screenshot").startswith("attached")
            return phone(click(2))
        assert "blocking" not in notices and "no readable text" not in notices and section(text, "Screenshot") is None
        return phone(report("failed", "OpenTable blocks this browser."))

    blocked = web_screen("Access Denied", "https://www.opentable.com/r/bbq", 0)
    end, s = await run(store, decide, phone=FakePhone(lambda done: blocked))
    assert end.status == "failed"
    assert s.calls[1]["images"] == [JPEG] and s.calls[0]["images"] == [] and s.calls[2]["images"] == []
    assert any('runtime: www.opentable.com is blocking this browser ("Access Denied")' in a for a in s.mem["actions"])


async def test_a_page_with_text_is_neither(store):
    def decide(text):
        assert section(text, "Notices") is None
        return phone(report())

    end, s = await run(store, decide, phone=FakePhone(web_screen("BBQ in Austin", "https://maps.google.com/", 240)))
    assert end.status == "completed"


# --- saved sign-ins: account names from Google Password Manager above the screen -------------------------

async def test_saved_signins_for_the_page_sit_above_the_screen_and_are_read_once_per_site(store):
    signin = web_screen("Amazon Sign In", "https://www.amazon.com/ap/signin", 120)
    devices = FakeDevices(FakePhone(lambda done: signin), jpeg=JPEG)
    devices.saved["https://www.amazon.com/ap/signin"] = {
        "site": "amazon.com", "passkeys": ["a@b.com", "c@d.com"], "passwords": ["a@b.com"]}

    def decide(text):
        assert section(text, "Saved sign-ins") == (
            "amazon.com (Google Password Manager): passkeys for a@b.com, c@d.com; passwords for a@b.com")
        assert text.index("Saved sign-ins:") < text.index("Screen:")
        return phone(click(1)) if turn_no(text) == 1 else phone(report())

    end, s = await run(store, decide, devices=devices)
    assert end.status == "completed" and len(s.calls) == 2
    assert devices.signin_lookups == ["https://www.amazon.com/ap/signin"]  # once for www.amazon.com


async def test_an_app_screen_is_looked_up_by_package_and_nothing_saved_adds_no_line(store):
    devices = FakeDevices(FakePhone(screen_of("Home")), jpeg=JPEG)

    def decide(text):
        assert section(text, "Saved sign-ins") is None
        return phone(report())

    end, s = await run(store, decide, devices=devices)
    assert end.status == "completed" and devices.signin_lookups and not devices.signin_lookups[0].startswith("http")


def test_a_long_list_of_names_is_cut_with_a_count():
    from app.runtime.phone import signins_line

    names = [f"user{i}@x.com" for i in range(8)]
    assert signins_line({"site": "x.com", "passkeys": [], "passwords": names}) == (
        "x.com (Google Password Manager): passwords for user0@x.com, user1@x.com, user2@x.com, user3@x.com, "
        "user4@x.com and 3 more")
    assert signins_line({"site": "x.com", "passkeys": [], "passwords": []}) == ""


# --- adb and installs: the phone is the user's; no banks ----------------------------------------------------

@pytest.mark.parametrize("command,refusal", [
    ("adb emu geo fix -97.74 30.27", "never move it"),
    ("adb -s emulator-5554 emu geo fix 1 2", "never move it"),
    ("adb shell settings put secure location_mode 3", "never move it"),
    ("adb shell cmd location providers add-test-provider gps", "phone_location"),
    ("adb shell appops set com.google.android.apps.maps android:mock_location allow", "never move it"),
    ("adb shell su -c setprop persist.sys.timezone Europe/Berlin", "never move it"),
    ("adb shell pm uninstall com.example", "never move it"),
    ("adb shell pm grant com.example android.permission.ACCESS_FINE_LOCATION", "never move it"),
    ("adb shell reboot", "never move it"),
    ("adb shell dumpsys location | grep last", "phone_location"),
    ("adb shell which curl wget", "never fetched from the shell"),
    ("adb shell curl -s https://r.jina.ai/https://www.opentable.com", "never fetched from the shell"),
])
async def test_adb_refuses_what_moves_the_phone_reads_its_fix_or_fetches_pages(store, command, refusal):
    s = Session(store, lambda text: phone(report()))
    line = await AdbCommand(command=command).run(s.session())
    assert line.startswith("NOT RUN:") and refusal in line
    assert s.devices.actions == []


@pytest.mark.parametrize("command", [
    "adb shell pm list packages -3", "adb shell dumpsys package com.example | grep versionName", "adb shell getprop ro.build.version.release",
    "adb shell am force-stop com.example", "adb shell settings get secure location_mode", "adb shell pm path com.example",
])
async def test_adb_still_runs_read_only_and_app_commands(store, command):
    end, s = await run(store, then_report(phone({"tool": "adb_command", "command": command})))
    assert s.devices.actions == [{"kind": "adb_command", "command": command}]


def test_finance_packages_are_recognised_by_their_parts():
    rows = [
    ("co.uk.getmondo", False), ("com.monzo.android", True), ("com.revolut.revolut", True), ("com.chase.sig.android", True),
    ("com.barclays.android.barclaysmobilebanking", True), ("com.squareup.cash", True), ("com.transferwise.android", True),
    ("com.ebay.kleinanzeigen", False), ("com.opentable", False), ("com.google.android.apps.maps", False), ("br.com.nubank", True),
    ("com.google.android.apps.walletnfcrel", True), ("net.one97.paytm", True), ("com.zerodha.kite3", True),
]
    for package, finance in rows:
        assert finance_package(package) is finance


async def test_install_app_refuses_bank_and_wallet_apps(store):
    s = Session(store, lambda text: phone(report()))
    line = await InstallApp(package="co.uk.monzo.app").run(s.session())
    assert line.startswith("NOT RUN: co.uk.monzo.app looks like a bank, wallet, payment or trading app")
    assert s.devices.actions == []


def test_calls_are_described_in_plain_words():
    rows = [
    ("wait_for", {"text": "mayadesai", "timeout": 5}, "Waiting for “mayadesai” to show up"),
    ("wait_for", {"activity": "Checkout"}, "Waiting for the screen to settle"),
    ("wait_for", {}, "Waiting for the screen to settle"),
    ("open_link", {"uri": "https://www.google.com/search?q=site%3Ainstagram.com+mayadesai&num=20", "package": "com.android.chrome"},
     "Searching Google for “site:instagram.com mayadesai”"),
    ("open_link", {"uri": "https://www.example.com/a/b/"}, "Opening a link: example.com/a/b"),
    ("set_setting", {"name": "wifi", "value": "off"}, "Turning Wi-Fi off"),
    ("press_key", {"key": "back"}, "Going back"),
    ("open_app", {"app": "Instacart"}, "Opening Instacart"),
    ("some_new_tool", {}, "Some new tool"),
]
    for name, args, said in rows:
        assert describe_call({"name": name, "arguments": args}) == said


def test_an_element_is_named_by_its_menu_entry():
    menu = {"tap_4": ('Tap [4] "Search" (Button)', {}), "scroll_down_9": ("Scroll down in [9] Results", {})}
    assert describe_call({"name": "interact", "arguments": {"element": 4, "interaction": "tap"}}, menu) == 'Tapping "Search" (Button)'
    assert describe_call({"name": "interact", "arguments": {"element": 9, "interaction": "scroll_down"}}, menu) == "Scrolling down in Results"
    assert describe_call({"name": "interact", "arguments": {"element": 7, "interaction": "tap"}}) == "Tapping item 7"


# --- pictures for the user ----------------------------------------------------------------------------

def clip(element, caption, picture=True):
    return {"tool": "clip", "element": element, "picture": picture, "caption": caption}


async def test_clip_keeps_the_picture_the_app_draws_and_falls_back_to_the_screenshot(store):
    import base64
    import io

    from PIL import Image

    def jpeg(colour, size=(120, 90)):
        out = io.BytesIO()
        Image.new("RGB", size, colour).save(out, "JPEG")
        return out.getvalue()

    card = element(1, "Pegasus Trail", b=[0, 100, 720, 500], c=[
        {"h": 9, "cls": "android.widget.ImageView", "b": [10, 110, 410, 490]},
    ])
    proof = element(2, "Order placed", b=[0, 600, 720, 900])
    phone_ = FakePhone(snapshot(card, proof), screen_of("Finished"))
    phone_.captures = {9: {"ok": True, "jpeg": base64.b64encode(jpeg((200, 30, 30))).decode(), "w": 120, "h": 90}}

    def decide(text):
        n = turn_no(text)
        if n == 1:
            return phone(clip(1, "Pegasus Trail, $129"), clip(2, "Your order", picture=False), click(1))
        assert "Pictures kept for the user" in text and 'image:1 "Pegasus Trail, $129" (picture, 120×90)' in text
        assert "drawn by the app's drawing of it" in last_result(text) and "drawn by screenshot" in last_result(text)
        return phone(report())

    shot = jpeg((30, 30, 200), size=(720, 1600))
    end, s = await run(store, decide, devices=FakeDevices(phone_, jpeg=shot))
    assert isinstance(end, Report)
    kept = s.mem["clips"]
    assert [(c["id"], c["kind"], c["element"]) for c in kept] == [(1, "picture", 1), (2, "card", 2)]
    assert [c["cmd"] for c in s.phone.calls if c["cmd"] == "capture"] == ["capture", "capture"]  # [2] could not be drawn
    rows = store.rows("SELECT id, kind, caption, w, h FROM images ORDER BY id")
    assert [(r["kind"], r["caption"], r["w"], r["h"]) for r in rows] == [("clip", "Pegasus Trail, $129", 120, 90), ("clip", "Your order", 720, 300)]


async def test_clip_keeps_an_image_of_the_page_read_page_read_from_the_page_itself(store):
    import base64
    import io

    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (640, 400), (200, 30, 30)).save(out, "JPEG")
    page = element(5, cls="android.webkit.WebView", webview=True, title="Lake Louise", url="https://travel.example/lake-louise",
                   page={"nodes": 40, "ready": "complete"})
    screen = snapshot(page, element(1, "Continue"), pkg="com.android.chrome")
    tree = [{"dom": True, "wrole": "heading", "text": "Lake Louise", "c": [
        {"dom": True, "cls": "web:img", "wrole": "img", "b": [0, 2000, 640, 2400], "desc": "The lake", "h": "cdp:1:7"},
        {"dom": True, "text": "Open from March to May."}]}]

    def decide(text):
        n = turn_no(text)
        if n == 1:
            return phone(click(2), {"tool": "read_page"})
        if n == 2:
            assert '(image 1 "The lake")' in text
            return phone({"tool": "clip", "page_image": 1, "caption": "The lake at sunrise"},
                         {"tool": "clip", "page_image": 2, "caption": "nothing"}, click(2))
        assert 'image:1 "The lake at sunrise" (picture, 640×400)' in text
        assert "image 1 of the page, 640×400, drawn by the page's own image file" in last_result(text)
        assert "no (image 2)" in last_result(text)
        return phone(report())

    phone_ = FakePhone(lambda done: screen)
    phone_.pages = {5: {"ok": True, "title": "Lake Louise", "url": "https://travel.example/lake-louise", "nodes": tree}}
    phone_.captures = {"cdp:1:7": {"ok": True, "jpeg": base64.b64encode(out.getvalue()).decode(), "source": "cache"}}
    end, s = await run(store, decide, phone=phone_)
    assert isinstance(end, Report)
    assert {"cmd": "capture", "h": "cdp:1:7", "process": screen["windows"][0].get("process")} in s.phone.calls or any(
        c["cmd"] == "capture" and c["h"] == "cdp:1:7" for c in s.phone.calls)
    assert [(c["id"], c["kind"], c["element"]) for c in s.mem["clips"]] == [(1, "picture", 0)]


async def test_a_drag_draws_inside_the_elements_box(store):
    # A canvas got the same two screen-wide lines whatever was asked (2026-11-15): strokes go where they are asked.
    canvas = element(4, cls="android.view.View", flags=("click",), b=[100, 200, 500, 600], desc="Drawing canvas")
    stroke = {"tool": "drag", "element": 1, "path": [[0, 0.5], [1, 0.5], [0.5, 1]]}
    end, s = await run(store, then_report(phone(stroke)), phone=FakePhone(snapshot(canvas)))
    assert s.phone.swipes == [(100, 400, 500, 400, 400), (500, 400, 300, 600, 400)]
    assert "dragged 2 strokes in [1]" in last_result(s.calls[1]["context"])
    bad = {"tool": "drag", "element": 1, "path": [[0, 2]]}
    end, s = await run(store, then_report(phone(bad)), phone=FakePhone(snapshot(canvas)))
    assert "drag.path must be 2 to 8 points" in last_result(s.calls[1]["context"]) and s.phone.swipes == []


async def test_a_verifier_cannot_submit_a_message(store):
    # Its keyboard action key in a chat composer would send the draft.
    screen = snapshot(field(2, "See you at 6", hint="Message"))
    submit = {"tool": "interact", "element": 1, "interaction": "submit"}
    end, s = await run(store, lambda text: phone(submit) if turn_no(text) == 1 else phone({"tool": "verdict", "passed": True, "reason": "ok"}),
                       phone=FakePhone(screen), agent=VERIFIER)
    assert "a check types and submits only in a search box" in last_result(s.calls[1]["context"]) and s.phone.calls == []


async def test_appending_through_a_bridge_that_cannot_says_it_failed(store):
    # An older bridge or a page answers insert_text "unknown cmd" (or without via=insert): never a quiet replace.
    class Old(FakePhone):
        async def call(self, cmd, process=None, timeout=10, **args):
            if cmd == "insert_text":
                return {"ok": False, "error": "unknown cmd insert_text"}
            return await super().call(cmd, process, timeout, **args)

    adding = {"tool": "interact", "element": 1, "interaction": "append", "value": "\nMilk"}
    end, s = await run(store, then_report(phone(adding)), phone=Old(snapshot(field(2, "Eggs"))))
    assert "FAILED" in last_result(s.calls[1]["context"])


async def test_the_same_command_between_notes_is_still_a_loop(store):
    # 129 identical `ls` calls, each with a screenshot or a note between, never tripped the guard (2026-11-15).
    ls = phone({"tool": "adb_command", "command": "adb shell ls -l /sdcard/Download/clip.mp4"})
    noted = phone(log("still watching"))
    end, s = await run(store, then_report(*[ls, noted] * 6))
    assert end.status == "failed" and "in a row and got the same answer 4 times" in end.summary
