"""The screen driver: looks, events, checked actions, stale handles; and the gateway's bridge."""

import httpx
import pytest
from app import gateway
from app.bridge.devices import DeviceError as BridgeError
from app.bridge.screen import options
from app.bridge.tools import resolve
from app.device.client import Devices
from app.device.driver import Driver
from app.errors import DeviceError
from fastapi import HTTPException
from phone import FakePhone, element, field, label, screen_of, snapshot


async def tap(driver, n=1, look=True):
    """Check a tap on [n] against the driver's current screen (its Interactions) and send it, as a phone agent does."""
    menu = options(driver.screen, [])
    step, why = resolve({"name": "interact", "arguments": {"element": n, "interaction": "tap"}}, menu)
    assert step, why
    description, action = menu[step["keys"][0]]
    return await driver.execute(description, action, target=driver.screen.elements[n], look=look)


async def test_a_step_inside_a_burst_does_not_look_again():
    phone = FakePhone(screen_of("One"), screen_of("Two"))
    driver = Driver(phone)
    await driver.observe()
    reads = phone.reads
    result = await tap(driver, look=False)
    assert result.endswith("(no look: part of a burst)") and phone.reads == reads
    assert "One" in driver.screen.text  # still the screen the burst was decided on


async def test_a_swipe_by_direction_crosses_the_middle_of_the_display():
    phone = FakePhone(screen_of("Feed"))
    driver = Driver(phone)
    await driver.observe()
    assert (await driver.execute("Swipe up", {"gesture": "up"})).startswith("swiped up")
    assert phone.swipes == [(540, 1680, 540, 480, 300)]


async def test_a_look_reads_the_screen_without_judging_whether_the_app_finished_loading():
    class Recording(FakePhone):
        async def call(self, cmd, process=None, timeout=10, **args):
            self.asked = getattr(self, "asked", []) + [cmd]
            return await super().call(cmd, process, timeout, **args)

    phone = Recording(screen_of("Feed"))
    driver = Driver(phone)
    await driver.observe(after_action=True)
    assert "Feed" in driver.screen.text
    assert phone.asked == ["events"]  # no settle polling: the screen and its events, nothing else


async def test_failed_network_calls_stay_out_of_what_the_agent_hears():
    phone = FakePhone(screen_of("Feed"))
    phone.events = [{"type": "http", "method": "POST", "url": "https://x/track", "code": 503, "t": 1},
                    {"type": "toast", "text": "Saved", "t": 2}]
    driver = Driver(phone)
    await driver.observe()
    assert driver.take_events() == ['toast "Saved"']


async def test_a_control_declared_as_a_checkbox_to_accessibility_reads_as_one_and_its_tick_is_reported():
    unticked = snapshot(element(5, "40", cls="com.facebook.react.views.view.ReactViewGroup",
                                flags=("click", "listener", "checkable"), a11y_cls="android.widget.CheckBox"))
    ticked = snapshot(element(5, "40", cls="com.facebook.react.views.view.ReactViewGroup",
                              flags=("click", "listener", "checkable", "checked"), a11y_cls="android.widget.CheckBox"))
    driver = Driver(FakePhone(unticked, ticked))
    await driver.observe()
    assert '[1] Checkbox "40"' in driver.screen.text
    result = await tap(driver)
    assert result.endswith("now checked")


async def test_what_an_action_changed_and_the_toast_it_raised_come_back_in_words():
    class Toasty(FakePhone):
        async def call(self, cmd, process=None, timeout=10, **args):
            if cmd == "click":
                self.events = [{"type": "toast", "text": "Added to cart", "t": 1},
                               {"type": "http", "method": "POST", "url": "https://x/events?a=1", "code": 200, "t": 2}]
            return await super().call(cmd, process, timeout, **args)

    driver = Driver(Toasty(screen_of("Cart"), screen_of("Cart")))
    await driver.observe()
    result = await tap(driver)
    assert result.startswith("tapped [1] Continue") and result.endswith('toast "Added to cart"')
    # Successful network calls stay out of what the agent hears.
    assert driver.take_events() == ['toast "Added to cart"']


def test_of_the_apps_network_calls_the_agent_hears_only_a_write_its_server_refused():
    from app.bridge.screen import collapse, describe_event, events_for_agent

    def heard(*calls):
        events = [{"type": "http", "method": m, "url": url, "code": code, "t": i} for i, (m, url, code) in enumerate(calls)]
        return [describe_event(e) for e in collapse(events_for_agent(events))]

    # What browsing an app looks like: tracking, pages loaded by POST, a picture that is not there.
    assert heard(("POST", "https://touch.shop.com/track", 200), ("POST", "https://api.shop.com/v3/layout/search/shirt", 200),
                 ("GET", "https://cdn.shop.com/icons/star.png", 504), ("GET", "https://m.shop.com/manifest", 403),
                 ("POST", "https://api.shop.com/v1/cart", None), ("POST", "https://api.shop.com/v1/cart", 1022)) == []
    # Trackers, logs and ad services that fail are still nothing for the task.
    assert heard(("POST", "https://touch.shop.com/track", 503), ("POST", "https://logs.adsdk.com/v1/sdklog", 400),
                 ("POST", "https://clickstream.shop.com/v2/eventstore/publish", 500),
                 ("POST", "https://api.shop.com/v1/shield/user-traces", 429)) == []
    # A coupon the server refused, twice: one line, without the query and without the order's number.
    assert heard(("POST", "https://api.shop.com/v2/orders/8812345678/coupon?code=SAVE&token=abc", 422),
                 ("POST", "https://api.shop.com/v2/orders/8812345678/coupon?code=SAVE&token=abc", 422)) == [
        "the app's request POST api.shop.com/v2/orders/…/coupon failed: HTTP 422 (x2)"]
    # A signed-out app fails everywhere at once: three lines and a count, not twenty.
    many = heard(*(("PUT", f"https://api.shop.com/v1/thing{i}", 401) for i in range(8)))
    assert len(many) == 4 and many[-1] == "5 more of the app's requests failed"


async def test_a_redrawn_element_is_found_again_by_its_key():
    class Redrawn(FakePhone):
        async def call(self, cmd, process=None, timeout=10, **args):
            if cmd == "click" and args["h"] == 1:
                self.calls.append({"cmd": cmd, **args})
                return {"ok": False, "error": "stale handle"}
            return await super().call(cmd, process, timeout, **args)

    before = snapshot(element(1, "Buy", id="buy"))
    after_redraw = snapshot(element(7, "Buy", id="buy"))
    phone = Redrawn(lambda done: before if phone.reads <= 1 else after_redraw)
    driver = Driver(phone)
    await driver.observe()
    await tap(driver)
    assert [c["h"] for c in phone.calls] == [1, 7]


async def test_a_touch_nothing_took_is_reported_as_failed_with_no_other_tap():
    class Untaken(FakePhone):
        async def call(self, cmd, process=None, timeout=10, **args):
            if cmd == "click":
                await super().call(cmd, process, timeout, **args)
                return {"ok": False, "via": "touch", "x": 540, "y": 1200, "error": "nothing at that point took the touch"}
            return await super().call(cmd, process, timeout, **args)

    phone = Untaken(screen_of("x"), screen_of("y"))
    driver = Driver(phone)
    await driver.observe()
    result = await tap(driver)
    assert phone.taps == [] and "via touch — FAILED: nothing at that point took the touch" in result


async def test_event_cursors_carry_over_so_old_events_are_not_repeated():
    phone = FakePhone(screen_of("x"))
    phone.events = [{"type": "toast", "text": "Welcome back", "t": 1}]
    driver = Driver(phone)
    await driver.observe()
    assert driver.take_events() == ['toast "Welcome back"']
    await driver.observe()
    assert driver.take_events() == []


async def test_every_action_is_journaled_through_act():
    seen = []

    async def act(kind, description, perform):
        seen.append((kind, description))
        return await perform()

    driver = Driver(FakePhone(screen_of("x"), screen_of("y")), act=act)
    await driver.observe()
    await tap(driver)
    assert seen == [("click", 'Tap [1] Button "Continue"')]


# --- the gateway side -------------------------------------------------------------------------

class GatewayPhone:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.seen = result, error, []

    async def call(self, cmd, process=None, timeout=10, **args):
        self.seen.append((cmd, process, args))
        if self.error:
            raise self.error
        return self.result

    async def snapshot_display(self, **kwargs):
        return self.result


async def test_gateway_bridge_checks_the_lease_and_scrubs_private_values(monkeypatch):
    phone = GatewayPhone({"windows": [{"nodes": [{"text": "Code 482913 sent", "h": 1}]}]})
    monkeypatch.setitem(gateway.core.phones, "device-1", phone)
    monkeypatch.setitem(gateway.core.redactions, "device-1", {"482913"})
    monkeypatch.setattr(gateway.core, "check_owner", lambda slot, owner: None)
    body = gateway.actions.BridgeRequest(owner="o", method="snapshot_display")
    assert (await gateway.actions.bridge("device-1", body))["windows"][0]["nodes"][0]["text"] == "Code [private] sent"
    body = gateway.actions.BridgeRequest(owner="o", method="call", cmd="click", process="com.x", args={"h": 4})
    await gateway.actions.bridge("device-1", body)
    assert phone.seen == [("click", "com.x", {"h": 4})]

    def foreign(slot, owner):
        raise HTTPException(409, "Device is not leased to this caller")

    monkeypatch.setattr(gateway.core, "check_owner", foreign)
    with pytest.raises(HTTPException) as error:
        await gateway.actions.bridge("device-1", body)
    assert error.value.status_code == 409


async def test_gateway_reports_a_missing_bridge_with_its_reason(monkeypatch):
    monkeypatch.setitem(gateway.core.phones, "device-1", GatewayPhone(error=BridgeError("No bridge in com.x")))
    monkeypatch.setattr(gateway.core, "check_owner", lambda slot, owner: None)
    with pytest.raises(HTTPException) as error:
        await gateway.actions.bridge("device-1", gateway.actions.BridgeRequest(owner="o", method="call", cmd="snapshot"))
    assert error.value.status_code == 502 and error.value.detail == "No bridge in com.x"


async def test_devices_bridge_errors_keep_the_gateway_reason():
    devices = Devices()
    await devices.client.aclose()
    devices.client = httpx.AsyncClient(base_url="http://gateway", transport=httpx.MockTransport(
        lambda request: httpx.Response(502, json={"detail": "No bridge in com.x: enable it in the module scope"}),
    ))
    try:
        with pytest.raises(DeviceError, match="No bridge in com.x"):
            await devices.device("device-1", "owner").snapshot_display()
    finally:
        await devices.close()



# --- a look that waits while the app is plainly not done ---------------------------------------------------

def spinner_only_list(pkg="com.example"):
    """A screen whose list holds nothing but a spinner: the results are still coming."""
    return snapshot(element(1, "Search"), element(5, cls="androidx.recyclerview.widget.RecyclerView", flags=("list",),
                                                    c=[element(6, cls="android.widget.ProgressBar", flags=("spinner",))]),
                    pkg=pkg)


def between_screens(pkg="com.example"):
    """The window manager lists the app's window; its bridge does not yet: a screen is opening."""
    s = snapshot(pkg=pkg)
    s["windows"] = [{"kind": "unreadable", "title": f"{pkg}/.Search", "process": pkg, "package": pkg,
                     "wm_type": "BASE_APPLICATION", "mode": "fullscreen", "frame": [0, 0, 1080, 2400],
                     "focused": True, "nodes": [], "why": "the bridge does not list this window"}]
    return s


@pytest.fixture(autouse=True)
def looks_wait_for_loading(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "loading_wait_seconds", 5.0)


class Asking(FakePhone):
    async def call(self, cmd, process=None, timeout=10, **args):
        self.asked = getattr(self, "asked", []) + [cmd]
        return await super().call(cmd, process, timeout, **args)


async def test_a_spinner_standing_for_the_content_is_waited_on_until_the_content_comes():
    phone = Asking(screen_of("Feed"), spinner_only_list(), screen_of("Result one", "Result two"))
    driver = Driver(phone)
    await driver.observe()
    await tap(driver)
    assert "Result one" in driver.screen.text and "(loading spinner)" not in driver.screen.text
    assert "changed" in phone.asked  # waited for the app's next change, not a fixed time
    assert driver.still_loading is None


async def test_an_app_between_two_screens_is_read_again_once_the_screen_is_there():
    phone = Asking(screen_of("Feed"), between_screens(), screen_of("Chat with Ana"))
    driver = Driver(phone)
    await driver.observe()
    await tap(driver)
    assert "Chat with Ana" in driver.screen.text and "unreadable" not in driver.screen.text
    assert driver.unhooked == []


async def test_a_spinner_beside_content_is_not_waited_on():
    screen = snapshot(element(1, "Continue"), element(5, cls="androidx.recyclerview.widget.RecyclerView", flags=("list",), c=[
        label(7, "Hello from Ana"), element(6, cls="android.widget.ProgressBar", flags=("spinner",))]))
    phone = Asking(screen)
    driver = Driver(phone)
    await driver.observe(after_action=True)
    assert phone.asked == ["events"] and driver.waited == 0


async def test_a_spinner_still_there_at_the_cap_is_said_and_the_screen_is_handed_over(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "loading_wait_seconds", 0.05)
    phone = Asking(screen_of("Feed"), spinner_only_list())
    driver = Driver(phone)
    await driver.observe()
    result = await tap(driver)
    assert "(loading spinner)" in driver.screen.text
    assert driver.still_loading == "a spinner stands where the content will be"
    assert "still loading after 0 s (a spinner stands where the content will be)" in result


async def test_a_look_without_an_action_does_not_wait_on_a_spinner():
    phone = Asking(spinner_only_list(), screen_of("Late"))
    driver = Driver(phone)
    await driver.observe()
    assert "(loading spinner)" in driver.screen.text and phone.asked == ["events"]


def test_loading_reads_the_settle_that_ended_with_a_screen_still_opening():
    from app.bridge.screen import loading
    s = screen_of("Feed")
    assert loading(s) is None
    s["settle"] = {"settled": False, "busy": "opening a screen"}
    assert loading(s) == "the app is opening a screen"


async def test_a_spinner_that_outlasted_one_wait_is_not_waited_on_again_while_it_stays(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "loading_wait_seconds", 0.05)

    class Stalling(Asking):
        stalled = True  # the app never moves on by itself

        async def call(self, cmd, process=None, timeout=10, **args):
            if cmd == "changed" and self.stalled:
                self.asked = getattr(self, "asked", []) + [cmd]
                return {"ok": True, "changed": False, "waited_ms": 0}
            return await super().call(cmd, process, timeout, **args)

    phone = Stalling(screen_of("Feed"), spinner_only_list(), spinner_only_list(), screen_of("Done"), spinner_only_list())
    driver = Driver(phone)
    await driver.observe()
    await tap(driver)  # waits, gives up: the spinner is this screen's steady state
    assert driver.still_loading and driver.waited > 0
    asked = len(phone.asked)
    await tap(driver)  # the same screen again: handed over at once, no wait
    assert len(phone.asked) == asked + 2 and driver.still_loading is None and driver.waited == 0  # the tap, its events
    await tap(driver)  # "Done": a screen without the spinner forgets it
    assert driver.gave_up is None
    phone.stalled = False
    await tap(driver)  # a new spinner is waited on again
    assert driver.waited > 0


def bare_chat(pkg="com.example"):
    """A chat that has just opened: its composer is there, its messages are not, and no spinner yet."""
    return snapshot(element(1, "Back"), element(5, cls="androidx.viewpager.widget.ViewPager", flags=("can_scroll_right",), c=[
        field(6, hint="Type a message"), element(7, "Send")]), pkg=pkg, activity=f"{pkg}.ChatsActivity")


async def test_a_screen_that_has_just_opened_with_empty_content_areas_is_waited_on():
    loaded = snapshot(element(1, "Back"), element(5, cls="androidx.viewpager.widget.ViewPager", flags=("can_scroll_right",), c=[
        element(8, cls="androidx.recyclerview.widget.RecyclerView", flags=("list",), items=2, c=[label(9, "Hi Ana", li=0), label(10, "Hi!", li=1)]),
        field(6, hint="Type a message")]), activity="com.example.ChatsActivity")
    phone = Asking(screen_of("Chats"), bare_chat(), loaded)
    driver = Driver(phone)
    await driver.observe()
    await tap(driver)
    assert "Hi Ana" in driver.screen.text and "changed" in phone.asked


async def test_empty_content_areas_are_waited_on_once_then_taken_as_the_screen_s_state(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "loading_wait_seconds", 0.05)
    phone = Asking(bare_chat(), *[bare_chat()] * 40)  # a chat that stays empty: the app never fills it
    driver = Driver(phone)
    await driver.observe()
    await tap(driver)
    assert driver.still_loading == "its content areas are still empty"
    asked = len(phone.asked)
    await tap(driver)  # typing in the composer: the same empty screen is handed over at once
    assert len(phone.asked) == asked + 2 and driver.waited == 0


async def test_an_empty_popup_over_a_screen_does_not_count_as_a_screen_without_words():
    from app.bridge.screen import loading
    s = screen_of("Feed")
    s["windows"].append({"kind": "popup", "title": "PopupWindow", "process": "com.example", "package": "com.example",
                         "wm_type": "APPLICATION_SUB_PANEL", "mode": "fullscreen", "frame": [0, 0, 1, 1], "focused": False, "nodes": []})
    assert loading(s, new_screen=True) is None


async def test_a_screen_that_has_just_opened_with_controls_only_is_waited_on():
    # A chat's header and composer are there before its messages, and its list is not yet a list to the bridge.
    bare = snapshot(element(1, "Back"), element(3, "Launch prep · 9 participants"), element(5, cls="android.widget.FrameLayout", flags=(), c=[
        field(6, hint="Type a message"), element(7, "Send")]), activity="com.example.ChatsActivity")
    loaded = snapshot(element(1, "Back"), element(8, cls="androidx.recyclerview.widget.RecyclerView", flags=("list",), items=2,
                                               c=[label(9, "Hi Ana", li=0)]), activity="com.example.ChatsActivity")
    phone = Asking(screen_of("Chats"), bare, loaded)
    driver = Driver(phone)
    await driver.observe()
    await tap(driver)
    assert "Hi Ana" in driver.screen.text and "changed" in phone.asked


async def test_a_list_that_says_it_has_rows_and_shows_none_is_waited_on_without_a_new_screen():
    bare = snapshot(element(1, "Messages"), element(5, cls="androidx.recyclerview.widget.RecyclerView", flags=("list",), items=26))
    rows = snapshot(element(1, "Messages"), element(5, cls="androidx.recyclerview.widget.RecyclerView", flags=("list",), items=26,
                                                   c=[label(6, "Ana: the report", li=0)]))
    phone = Asking(screen_of("Search"), bare, rows)
    driver = Driver(phone)
    await driver.observe()
    await tap(driver)  # a tab switch inside the same screen
    assert "Ana: the report" in driver.screen.text and "changed" in phone.asked


def with_sheet(screen, *nodes):
    """`screen` with a sheet's window over it (an app's dialog over the same activity)."""
    screen["windows"].append({"kind": "dialog", "title": "", "process": "com.example", "package": "com.example",
                              "wm_type": "APPLICATION", "mode": "fullscreen", "frame": [0, 0, 1080, 2400],
                              "focused": True, "nodes": list(nodes)})
    screen["windows"][0]["focused"] = False
    return screen


async def test_a_sheet_that_opened_with_only_its_close_button_is_waited_on_until_it_fills():
    # A home-services app's booking sheet: its window comes a moment before what it holds. Read at once, it was a bare
    # Close button over the same activity, and the agent closed it as the wrong item (2026-11-07).
    bare = with_sheet(screen_of("Fabric sofa cleaning", "View details"), element(4, desc="Close Button"))
    full = with_sheet(screen_of("Fabric sofa cleaning", "View details"), element(4, desc="Close Button"),
                      label(8, "Select number of sofa seats"), element(9, "3 seats · $129"))
    phone = Asking(screen_of("Fabric sofa cleaning", "View details"), bare, full)
    driver = Driver(phone)
    await driver.observe()
    await tap(driver)
    assert "Select number of sofa seats" in driver.screen.text and "changed" in phone.asked
    assert driver.still_loading is None


def test_a_note_open_in_an_editor_and_an_empty_form_are_not_still_loading():
    # Markor's editor is one field holding the whole note; Broccoli's new-recipe form is a scroll area of empty
    # fields. Both were taken for empty content areas and waited on for the full loading wait (2026-11-15).
    from app.bridge.screen import loading
    scroll = dict(cls="android.widget.ScrollView", flags=("can_scroll_down",))
    editor = snapshot(element(1, "Back"), element(5, **scroll, c=[field(6, "Recipe: Vegetarian Chili\n servings: 2")]))
    form = snapshot(element(1, "Save"), element(5, **scroll, c=[field(6, hint="Title"), field(7, hint="Servings")]))
    assert loading(editor, new_screen=True) is None
    assert loading(form, new_screen=True) is None
    assert loading(bare_chat(), new_screen=True) == "its content areas are still empty"  # one empty composer


async def test_a_sheet_already_open_before_the_action_is_not_a_new_screen():
    from app.bridge.screen import loading, window_key
    s = with_sheet(screen_of("Feed"), element(4, desc="Close Button"))
    assert loading(s) is None  # open before the action: what it shows is its state
    assert loading(s, opened={window_key(s["windows"][1])}) == "its content areas are still empty"


def test_a_list_whose_rows_are_listed_is_not_offered_a_tap():
    # Tapping the list itself lands on its middle row: OpenTracks' popup menu so opened "Delete track?" (2026-11-15).
    from app.bridge.screen import options, render
    menu_list = element(1, cls="android.widget.ListView", flags=("listener", "list"), items=3, c=[
        label(2, "Edit", li=0), label(3, "Delete", li=1), label(4, "Share", li=2)])
    menu = options(render(snapshot(menu_list)), [])
    tapped = {d for d, a in menu.values() if a.get("cmd") == "click"}
    assert not any("ListView" in d or d.startswith("Tap [1] List") for d in tapped)
    assert any('"Delete"' in d for d in tapped) and any('"Edit"' in d for d in tapped)  # the rows themselves
