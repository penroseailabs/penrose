"""The ops lane: deterministic device commands (app/bridge/ops.py), the harness's device calls that send
them (open_app, set_setting... in app/bridge/tools.py), and notifications as events."""

import re

import pytest
from app.bridge import ops as ops_module
from app.bridge import tools
from app.bridge.devices import NOTIFIER
from app.bridge.ops import OpError, Ops, resolve
from app.bridge.opscatalog import CATALOG, is_change
from app.bridge.screen import options, render
from app.device.driver import Driver
from app.store import Store
from phone import FakePhone, element, label, screen_of, snapshot

# --- a device whose adb answers from a script -------------------------------------------------------


class FakeAdb:
    def __init__(self, serial="emulator-5554", replies=None):
        self.serial = serial
        self.replies = replies or {}  # regex on the whole command -> output
        self.commands = []  # every adb call: ("shell", "(cmd) 2>&1") or ("emu", ...)

    async def run(self, *args, timeout=20, binary=False, check=True):
        self.commands.append(args)
        text = " ".join(args)
        for pattern, reply in self.replies.items():
            if re.search(pattern, text):
                return reply
        return ""

    async def shell(self, command, timeout=20):
        return await self.run("shell", command, timeout=timeout)

    def shell_commands(self):
        return [c[1].removeprefix("(").removesuffix(") 2>&1") for c in self.commands if c[0] == "shell"]


class OpsPhone(FakePhone):
    """The tests' phone, with the adb the ops lane needs."""

    def __init__(self, *screens, adb=None):
        super().__init__(*screens)
        self.adb = adb or FakeAdb()
        self.resets = 0

    async def target(self):
        return self.current()["process"]

    async def reset(self):
        self.resets += 1


def lane(*screens, **adb):
    phone = OpsPhone(*screens, adb=FakeAdb(**adb))
    return Ops(phone), phone


# --- the catalogue ------------------------------------------------------------------------------------


def test_every_catalogued_op_has_a_handler_and_the_other_way_round():
    handlers = {name[3:] for name in dir(Ops) if name.startswith("op_")}
    assert handlers == set(CATALOG)


def test_only_ops_that_change_the_device_are_told_to_the_phone_agent():
    assert is_change({"op": "location", "value": "off"})
    assert is_change({"op": "prefs", "action": "set"})
    assert not is_change({"op": "prefs", "action": "get"})
    assert not is_change({"op": "state"})
    assert not is_change({"op": "wait_for", "text": "x"})


async def test_a_toggle_is_one_command_confirmed_by_reading_the_setting_back():
    ops, phone = lane(screen_of("x"), replies={"settings get secure location_mode": "0"})
    result = await ops.run({"op": "location", "value": "off"})
    assert result["ok"] and result["summary"] == "location off"
    assert "cmd location set-location-enabled false" in phone.adb.shell_commands()[0]


async def test_a_toggle_says_so_when_the_setting_does_not_follow(monkeypatch):
    async def no_sleep(_):
        pass

    monkeypatch.setattr(ops_module.asyncio, "sleep", no_sleep)
    ops, _ = lane(screen_of("x"), replies={"settings get global wifi_on": "1"})
    result = await ops.run({"op": "wifi", "value": "off"})
    assert result["ok"] and "requested; setting reads 1" in result["summary"]


async def test_bad_values_are_results_not_exceptions_and_run_nothing():
    ops, phone = lane(screen_of("x"))
    assert not (await ops.run({"op": "wifi", "value": "maybe"}))["ok"]
    assert not (await ops.run({"op": "brightness", "value": 500}))["ok"]
    assert not (await ops.run({"op": "geo", "lat": 99, "lng": 0}))["ok"]
    assert not (await ops.run({"op": "no_such_op"}))["ok"]
    assert phone.adb.commands == []


async def test_launch_starts_the_launcher_activity_and_reports_a_missing_app():
    replies = {
        "pm path com.example.app": "package:/data/app/x/base.apk",
        "resolve-activity": "priority=0 preferredOrder=0\ncom.example.app/.Main",
        "am start": "Starting: Intent { cmp=com.example.app/.Main }",
    }
    ops, phone = lane(screen_of("x"), replies=replies)
    result = await ops.run({"op": "launch", "package": "com.example.app"})
    assert result["ok"] and result["component"] == "com.example.app/.Main"
    # -W: it returns once the app has drawn its first screen.
    assert any(c.startswith("am start -W -n com.example.app/.Main") for c in phone.adb.shell_commands())
    missing, _ = lane(screen_of("x"), replies={"pm path": ""})
    assert (await missing.run({"op": "launch", "package": "com.nope.app"}))["error"] == "com.nope.app is not installed"
    assert not (await missing.run({"op": "launch", "package": "not a package; rm -rf"}))["ok"]


@pytest.mark.parametrize("uri", ["javascript:alert(1)", "file:///data/data/x", "intent://x#Intent;end", "content://sms/inbox", "https://a.b\nam start x"])
async def test_open_refuses_what_is_not_a_plain_link_before_asking_the_phone(uri):
    ops, phone = lane(screen_of("x"))
    assert not (await ops.run({"op": "open", "uri": uri}))["ok"]
    assert phone.adb.commands == []


async def test_open_quotes_the_link_for_the_remote_shell():
    ops, phone = lane(screen_of("x"))
    await ops.run({"op": "open", "uri": "https://example.com/a?b=1&c=$(id)"})
    assert "'https://example.com/a?b=1&c=$(id)'" in phone.adb.shell_commands()[0]


async def test_grant_expands_groups_and_reports_what_the_app_does_not_declare():
    ops, phone = lane(screen_of("x"), replies={"CALENDAR": "Exception: Package has not requested permission"})
    result = await ops.run({"op": "grant", "package": "com.example.app", "permissions": ["location", "calendar"]})
    assert result["done"] == ["ACCESS_FINE_LOCATION", "ACCESS_COARSE_LOCATION"]
    assert result["skipped"] == ["READ_CALENDAR", "WRITE_CALENDAR"]
    assert "pm grant com.example.app android.permission.ACCESS_FINE_LOCATION" in phone.adb.shell_commands()
    refused = await ops.run({"op": "grant", "package": "com.example.app", "permissions": ["all"]})
    assert not refused["ok"]


async def test_keys_are_device_keys_not_navigation_or_typing():
    ops, phone = lane(screen_of("x"))
    assert (await ops.run({"op": "key", "value": "volume_up"}))["ok"]
    for value in ("back", "home", "enter", "tab"):
        assert not (await ops.run({"op": "key", "value": value}))["ok"]
    assert phone.adb.shell_commands() == ["input keyevent 24"]


# --- the emulator console ---------------------------------------------------------------------------


async def test_console_ops_need_a_local_emulator():
    ops, phone = lane(screen_of("x"), serial="emulator:5555")
    result = await ops.run({"op": "sms", "number": "5551234", "text": "hi"})
    assert not result["ok"] and "emulator console is not reachable" in result["error"]
    assert phone.adb.commands == []
    ops, phone = lane(screen_of("x"), serial="emulator-5554", replies={"emu sms": "OK"})
    assert (await ops.run({"op": "sms", "number": "5551234", "text": "Your parcel is here"}))["ok"]
    assert phone.adb.commands == [("emu", "sms", "send", "5551234", "Your parcel is here")]


async def test_a_refused_console_command_comes_back_with_the_reason_and_a_snapshot_load_resets_the_driver():
    ops, phone = lane(screen_of("x"), replies={"snapshot save": "KO: Snapshot save is skipped. Reason: UNSUPPORTED_VK_APP"})
    result = await ops.run({"op": "snapshot", "action": "save", "name": "base"})
    assert not result["ok"] and "swiftshader_indirect" in result["error"]
    ops, phone = lane(screen_of("x"), replies={"snapshot load": "OK"})
    assert (await ops.run({"op": "snapshot", "action": "load", "name": "base"}))["ok"]
    assert phone.resets == 1
    assert not (await ops.run({"op": "snapshot", "action": "load", "name": "../etc"}))["ok"]


# --- state ------------------------------------------------------------------------------------------


def test_state_is_parsed_from_one_read():
    text = "\n".join([
        "wifi=1", "data=1", "airplane=0", "bluetooth=null", "location=3", "night=Night mode: yes", "autorotate=0",
        "user_rotation=1", "brightness=128", "brightness_mode=0", "anim=1.0,1.0,0.0", "zen=0", "locale=en-IN|en-US",
        "tz=America/Chicago", "time=2026-10-03 09:00:00", "battery_level=64", "battery_AC_powered=true",
        "awake=mWakefulness=Awake", "keyguard=isKeyguardShowing=false",
        "focus=  mCurrentFocus=Window{1a u0 com.example.app/com.example.app.Main}",
    ])
    state = Ops.parse_state(text)
    assert state["wifi"] is True and state["airplane"] is False and state["bluetooth"] is None
    assert state["location"] is True and state["dark_mode"] is True and state["rotation"] == "landscape"
    assert state["brightness_percent"] == 50 and state["animations"] is True
    assert state["battery_percent"] == 64 and state["charging"] is True and state["screen_on"] is True
    assert state["keyguard_showing"] is False and state["foreground_package"] == "com.example.app"
    # "Off" leaves the apps' own animations at half speed: still off.
    assert Ops.parse_state(text.replace("anim=1.0,1.0,0.0", "anim=0.0,0.0,0.5"))["animations"] is False


# --- naming an element, waiting for the screen ------------------------------------------------------


def volume_screen():
    def slider(h, text, progress):
        return element(h, None, flags=("adjust",), cls="android.widget.SeekBar", id="seekbar", progress=progress,
                       **{"c": [label(h + 50, text)]})
    return snapshot(slider(1, "Media volume", "5/15"), slider(2, "Ring volume", "3/7"), element(3, "Done"))


def test_a_target_names_one_element_or_says_what_it_found():
    screen = render(volume_screen())
    assert resolve(screen, {"role": "Slider", "text": "ring"}).h == 2
    assert resolve(screen, {"role": "Slider", "index": 0}).h == 1
    with pytest.raises(OpError, match="2 elements match"):
        resolve(screen, {"role": "Slider"})
    with pytest.raises(OpError, match="no element matches"):
        resolve(screen, {"role": "Slider", "text": "alarm"})
    with pytest.raises(OpError, match="outside the 2"):
        resolve(screen, {"role": "Slider", "index": 5})
    with pytest.raises(OpError, match="target must be"):
        resolve(screen, {})


async def test_set_value_finds_the_element_and_sends_the_value_to_its_app():
    ops, phone = lane(volume_screen())

    async def call(cmd, process=None, timeout=10, **args):
        phone.calls.append({"cmd": cmd, "process": process, **args})
        return {"ok": True, "from": 3, "to": 7, "via": "accessibility_set_progress", "at_limit": False}

    phone.call = call
    result = await ops.run({"op": "set_value", "target": {"role": "Slider", "text": "Ring"}, "value": "max"})
    assert result["ok"] and "(3 -> 7)" in result["summary"]
    assert phone.calls == [{"cmd": "set_value", "process": "com.example", "h": 2, "value": "max"}]


async def test_check_is_true_or_false_now_and_wait_for_gives_up_after_its_timeout():
    ops, _ = lane(screen_of("Order placed"))
    assert (await ops.run({"op": "check", "text": "order placed", "foreground": "com.example"}))["met"]
    result = await ops.run({"op": "check", "text": "order placed", "gone": "order placed"})
    assert not result["ok"] and "still on screen" in result["error"]
    started = await ops.run({"op": "wait_for", "text": "never shown", "timeout": 0.5})
    assert not started["ok"] and "timed out" in started["summary"] and "Order placed" in started["screen_start"]
    assert (await ops.run({"op": "check", "element": {"role": "Button", "text": "Continue"}, "absent": {"text": "Cancel"}}))["met"]
    assert not (await ops.run({"op": "check"}))["ok"]


async def test_a_wait_gives_up_once_the_screen_has_settled_without_its_text(monkeypatch):
    monkeypatch.setattr(ops_module, "SETTLED_S", 0.3)
    ops, phone = lane(screen_of("Order placed"))
    started = ops_module.time.monotonic()
    result = await ops.run({"op": "wait_for", "text": "never shown", "timeout": 5})
    assert not result["ok"] and result["settled"] and "settled without it" in result["summary"]
    assert ops_module.time.monotonic() - started < 2
    # A screen that says it is loading has not settled: the wait runs to its timeout.
    loading, _ = lane(screen_of("Loading your order"))
    result = await loading.run({"op": "wait_for", "text": "never shown", "timeout": 0.6})
    assert not result["settled"] and "timed out" in result["summary"]


async def test_a_wait_with_no_condition_ends_when_the_screen_stops_changing(monkeypatch):
    monkeypatch.setattr(ops_module, "SETTLED_S", 0.3)
    ops, phone = lane(lambda done: screen_of(f"Frame {min(phone.reads, 2)}"))  # changes twice, then holds
    result = await ops.run({"op": "wait_for", "timeout": 5})
    assert result["met"] and "settled after" in result["summary"] and phone.reads >= 3
    assert not (await ops.run({"op": "check"}))["ok"]  # a check needs something to check


async def test_wait_for_reads_the_screen_again_when_the_app_changes_not_on_a_timer():
    loading = {**screen_of("Loading"), "bridge_snapshots": {"com.example": {"uptime": 1000}}}
    ops, phone = lane(loading, screen_of("Order placed"))
    waits = []

    async def call(cmd, process=None, timeout=10, **args):
        if cmd == "changed":  # the bridge blocks until the app lays out again; here, the next screen is ready
            waits.append((process, args["since"]))
            phone.done += 1
            return {"ok": True, "changed": True, "waited_ms": 120}
        return {"ok": True, "events": [], "seq": 0}

    phone.call = call
    started = ops_module.time.monotonic()
    result = await ops.run({"op": "wait_for", "text": "order placed", "timeout": 5})
    assert result["met"] and ops_module.time.monotonic() - started < 0.3
    assert waits == [("com.example", 1000)] and phone.reads == 2


async def test_wait_for_on_a_chrome_page_reads_again_every_moment():
    """A page changes inside Chrome's one view without an Android layout, so `changed` would wait out its cap."""
    loading = {**screen_of("Loading"), "bridge_snapshots": {"com.example": {"uptime": 1000}}, "web_page": True}
    ops, phone = lane(lambda done: loading if phone.reads < 2 else screen_of("Order placed"))
    asked = []

    async def call(cmd, process=None, timeout=10, **args):
        asked.append(cmd)
        return {"ok": True, "events": [], "seq": 0}

    phone.call = call
    result = await ops.run({"op": "wait_for", "text": "order placed", "timeout": 5})
    assert result["met"] and result["waited_ms"] < 700 and "changed" not in asked


async def test_wait_for_sees_a_notification_that_arrives_while_it_waits():
    ops, phone = lane({**screen_of("Home"), "notifier": True})
    calls = {"n": 0}

    async def call(cmd, process=None, timeout=10, **args):
        if cmd != "events":
            return {"ok": True}
        if process != NOTIFIER:
            return {"ok": True, "events": [], "seq": 0}
        calls["n"] += 1  # the first look is the baseline; the next brings the notification
        events = [{"type": "notification", "label": "Parcels", "title": "Out for delivery"}] if calls["n"] > 1 else []
        return {"ok": True, "events": events, "seq": calls["n"]}

    phone.call = call
    result = await ops.run({"op": "wait_for", "event": "out for delivery", "timeout": 5})
    assert result["ok"], result


def test_a_long_list_can_jump_to_its_ends():
    rows = [element(10 + i, f"Row {i}", flags=("click",)) for i in range(3)]
    listing = {"h": 5, "cls": "androidx.recyclerview.widget.RecyclerView", "f": ["list", "can_scroll_down", "can_scroll_up"],
               "items": 40, "c": rows}
    menu = options(render(snapshot(listing)), [])
    assert {"scroll_top_1", "scroll_bottom_1"} <= menu.keys()
    assert menu["scroll_bottom_1"][1] == {"cmd": "scroll_to", "n": 1, "pos": "bottom"}
    short = {**listing, "items": 1}
    assert "scroll_top_1" not in options(render(snapshot(short)), [])


async def test_the_device_calls_are_one_op_each_and_open_app_resolves_a_name():
    menu = options(render(snapshot()), [])
    step, _ = tools.resolve({"name": "set_setting", "arguments": {"name": "wifi", "value": "off"}}, menu)
    assert step["kind"] == "op" and step["op"] == {"op": "wifi", "value": "off"}
    _, why = tools.resolve({"name": "set_setting", "arguments": {"name": "volume", "value": "up"}}, menu)
    assert "set_setting.name must be one of" in why

    class Lane:
        def __init__(self):
            self.sent = []

        async def run(self, op):
            self.sent.append(op)
            if op["op"] == "apps":
                return {"op": "apps", "ok": True, "packages": [{"package": "com.instacart.client"}]}
            return {"op": op["op"], "ok": True, "summary": "launched"}

    lane = Lane()
    step, _ = tools.resolve({"name": "open_app", "arguments": {"app": "Instacart"}}, menu)
    assert (await step["run"](lane))["ok"]
    assert lane.sent == [{"op": "apps", "query": "Instacart"}, {"op": "launch", "package": "com.instacart.client"}]


async def test_a_change_a_device_command_made_reaches_the_next_look_once():
    driver = Driver(FakePhone(screen_of("Home")))
    driver.note_operator("location off")
    await driver.observe()
    assert "operator: location off" in driver.take_events()
    await driver.observe()
    assert driver.take_events() == []


class NotifyingPhone(FakePhone):
    """Events per process, and a notification listener that is up."""

    def __init__(self, *screens):
        super().__init__(*screens)
        self.queued = {}
        self.seen = []

    async def snapshot_display(self, visible=None, bridges=None, **kwargs):
        return {**await super().snapshot_display(visible, bridges), "notifier": True}

    async def call(self, cmd, process=None, timeout=10, **args):
        if cmd == "events":
            self.seen.append((process, args.get("since")))
            events = self.queued.pop(process, [])
            return {"ok": True, "events": events, "seq": len(events) + 1}
        return await super().call(cmd, process=process, timeout=timeout, **args)


@pytest.fixture
def leased_gateway(tmp_path, monkeypatch):
    import httpx
    from app import gateway

    lease_store = Store(tmp_path)
    lease_store.lease(["device-1"], "agent")
    monkeypatch.setattr(gateway.core, "store", lease_store)
    monkeypatch.setattr(gateway.core, "slots", {"device-1": "emulator-5554"})
    monkeypatch.setitem(gateway.core.redactions, "device-1", set())
    client = httpx.AsyncClient(base_url="http://gateway", transport=httpx.ASGITransport(app=gateway.app))
    return gateway, client


async def test_the_gateway_runs_ops_only_for_the_lease_holder_and_remembers_a_geo_fix(leased_gateway, monkeypatch):
    gateway, client = leased_gateway
    ran = []

    class Lane:
        async def run_many(self, ops):
            ran.append(ops)
            return [{"op": op["op"], "ok": True, "summary": "done", "lat": op.get("lat"), "lng": op.get("lng"), "accuracy": 10.0} for op in ops]

    monkeypatch.setitem(gateway.core.ops_lane, "device-1", Lane())
    async with client:
        denied = await client.post("/devices/device-1/ops", json={"owner": "other", "ops": [{"op": "wifi", "value": "off"}]})
        assert denied.status_code == 409 and ran == []
        empty = await client.post("/devices/device-1/ops", json={"owner": "agent", "ops": []})
        assert empty.status_code == 422
        ok = await client.post("/devices/device-1/ops", json={"owner": "agent", "ops": [{"op": "geo", "lat": 12.97, "lng": 77.59}]})
        assert ok.status_code == 200 and ok.json()["results"][0]["ok"]
    assert ran == [[{"op": "geo", "lat": 12.97, "lng": 77.59}]]


async def test_the_gateway_hands_the_window_list_it_was_given_to_the_snapshot(leased_gateway, monkeypatch):
    gateway, client = leased_gateway
    seen = []

    class Phone:
        async def snapshot_display(self, visible=None, bridges=None, **kwargs):
            seen.append((visible, bridges))
            return {"windows": []}

    monkeypatch.setitem(gateway.core.phones, "device-1", Phone())
    async with client:
        await client.post("/devices/device-1/bridge", json={"owner": "agent", "method": "snapshot_display", "args": {}})
        await client.post("/devices/device-1/bridge", json={
            "owner": "agent", "method": "snapshot_display", "args": {"visible": [{"process": "p"}], "bridges": ["p"], "junk": 1}})
    assert seen == [(None, None), ([{"process": "p"}], ["p"])]


async def test_notifications_that_arrive_are_events_but_the_shade_at_the_start_is_not():
    phone = NotifyingPhone(screen_of("Home"))
    phone.queued[NOTIFIER] = [{"type": "notification", "app": "x", "label": "Bank", "title": "Salary", "text": "Credited", "t": 1}]
    driver = Driver(phone)
    await driver.observe()
    assert not any("Salary" in e for e in driver.take_events())
    # the first look only fixed the cursor, far in the future, so nothing old came through
    assert (NOTIFIER, 2**62) in phone.seen
    phone.queued[NOTIFIER] = [{"type": "notification", "app": "x", "label": "Bank", "title": "Alert", "text": "Card used", "t": 2}]
    await driver.observe()
    assert "notification from Bank: Alert - Card used" in driver.take_events()


# --- an app's own data: list_data and network ------------------------------------------------------------


def list_screen():
    rows = [element(10 + i, f"Store {i}", flags=("click",)) for i in range(3)]
    return snapshot({"h": 5, "cls": "androidx.recyclerview.widget.RecyclerView", "id": "stores",
                     "f": ["list", "can_scroll_down"], "items": 120, "c": rows})


async def test_list_data_reads_the_numbered_list_from_its_adapter_bounded_and_masked():
    ops, phone = lane(list_screen())

    async def call(cmd, process=None, timeout=10, **args):
        phone.calls.append({"cmd": cmd, "process": process, **args})
        items = [f"Store(name=Store {i}, eta=2{i} min, authToken=abc{i})" for i in range(args["limit"])]
        return {"ok": True, "adapter": "x.StoresAdapter", "source": "getCurrentList()", "total": 120, "items": items}

    phone.call = call
    number = next(n for n, el in render(list_screen()).elements.items() if el.h == 5)
    result = await ops.run({"op": "list_data", "target": {"n": number}, "start": 10, "limit": 5})
    assert result["ok"] and phone.calls == [{"cmd": "list_data", "process": "com.example", "h": 5, "limit": 15}]
    assert "120 rows in the app's data; rows 11-15; 105 more after these (start=15)" in result["summary"]
    assert "11. Store(name=Store 10, eta=210 min, authToken=[redacted])" in result["summary"]
    assert "abc" not in result["summary"]
    # Never more than LIST_ROWS_MAX rows, nor LIST_CHARS characters, whatever is asked.
    phone.calls.clear()
    big = await ops.run({"op": "list_data", "target": {"n": number}, "limit": 10_000})
    assert phone.calls[0]["limit"] == ops_module.LIST_ROWS_MAX and len(big["summary"]) < ops_module.LIST_CHARS + 300
    assert not (await ops.run({"op": "list_data", "target": {"n": 99}}))["ok"]


async def test_list_data_says_when_the_rows_are_only_class_names():
    ops, phone = lane(list_screen())

    async def call(cmd, process=None, timeout=10, **args):
        return {"ok": True, "total": 2, "items": ["com.x.Row@1a2b3c", "com.x.Row@4d5e6f"]}

    phone.call = call
    result = await ops.run({"op": "list_data", "target": {"role": "List"}})
    assert not result["ok"] and "no readable text" in result["error"]


def long_note():
    """A note open in an editor: one field holding far more than a snapshot keeps."""
    return snapshot(element(8, "Recipe: Vegetarian Chili …", flags=("edit", "click"), cls="android.widget.EditText",
                            text_len=4794))


def test_a_text_shown_cut_says_so_and_how_to_read_it_all():
    screen = render(long_note())
    assert "cut: only the start of its 4794 characters is shown, read_text([1]) reads all" in screen.text
    # A plain text too long for a snapshot gets a number too, so it can be read whole.
    plain = render(snapshot(label(9, "Dear Ana, about the lease …", text_len=2500)))
    assert "read_text([1])" in plain.text


async def test_read_text_reads_the_whole_text_in_parts():
    ops, phone = lane(long_note())
    body = "Recipe: Avocado Toast with Egg\n preparationTime: 1 hrs\n" * 200

    async def call(cmd, process=None, timeout=10, **args):
        phone.calls.append({"cmd": cmd, "h": args.get("h"), "start": args.get("start")})
        return {"ok": True, "total": len(body), "start": args["start"], "text": body[args["start"]:args["start"] + args["limit"]]}

    phone.call = call
    first = await ops.run({"op": "read_text", "target": {"n": 1}})
    assert first["ok"] and phone.calls[0] == {"cmd": "text", "h": 8, "start": 0}
    assert f"characters 1-{ops_module.LIST_CHARS} of {len(body)}" in first["summary"]
    assert f"more characters after these (start={ops_module.LIST_CHARS})" in first["summary"]
    rest = await ops.run({"op": "read_text", "target": {"n": 1}, "start": ops_module.LIST_CHARS})
    assert rest["summary"].rstrip().endswith("preparationTime: 1 hrs") and "more characters" not in rest["summary"]


def http(url, body, kind="application/json"):
    return {"method": "GET", "url": url, "code": 200, "ms": 80, "content_type": kind, "body": body}


async def test_network_shows_the_newest_matching_responses_compacted_and_masked():
    ops, phone = lane(screen_of("Results"))
    recorded = [
        http("https://api.example.com/search?q=milk&access_token=SECRET1", '{"items": [{"name": "Horizon Organic Milk", "price": 3.99, "promo": null}], "session": "S2"}'),
        http("https://cdn.example.com/logo.png", None, "image/png"),
        http("https://api.example.com/cart", '{"lines": [], "total": 0}'),
        http("https://api.example.com/search?q=eggs", '{"items": [{"name": "Eggs"}]}'),
    ]

    async def call(cmd, process=None, timeout=10, **args):
        phone.calls.append({"cmd": cmd, "process": process, **args})
        return {"ok": True, "calls": recorded}

    phone.call = call
    result = await ops.run({"op": "network", "url": "search"})
    assert phone.calls == [{"cmd": "network", "process": "com.example", "since": 0}]
    assert result["matched"] == 2 and [c["url"] for c in result["calls"]][0].endswith("q=eggs")  # newest first
    milk = result["calls"][1]
    assert milk["body"] == '{"items":[{"name":"Horizon Organic Milk","price":3.99}],"session":"[redacted]"}'
    assert "SECRET1" not in result["summary"] and "S2" not in result["summary"] and "access_token=[redacted]" in milk["url"]
    found = await ops.run({"op": "network", "contains": "horizon", "limit": 1})
    assert found["matched"] == 1 and "Horizon Organic Milk" in found["summary"]
    none = await ops.run({"op": "network", "url": "orders"})
    assert none["ok"] and none["matched"] == 0 and "https://api.example.com/search" in none["summary"]


async def test_network_bodies_are_cut_around_what_was_asked_for():
    ops, phone = lane(screen_of("Results"))
    body = "{" + ", ".join(f'"k{i}": "{"x" * 40}"' for i in range(200)) + ', "name": "Instacart Pick"}'

    async def call(cmd, process=None, timeout=10, **args):
        return {"ok": True, "calls": [http("https://api.example.com/feed", body[:4000])]}  # cut short by the bridge

    phone.call = call
    result = await ops.run({"op": "network", "chars": 300})
    assert len(result["calls"][0]["body"]) <= 302
    far = body[:4000][3000:3010]
    near = await ops.run({"op": "network", "contains": far, "chars": 300})
    assert far in near["calls"][0]["body"] and near["calls"][0]["body"].startswith("…")


async def test_network_explains_an_app_with_nothing_recorded():
    ops, phone = lane(screen_of("Home"))

    async def call(cmd, process=None, timeout=10, **args):
        if cmd == "hello":
            return {"ok": True, "hooks": {"okhttp": "ClassNotFoundException: okhttp3.OkHttpClient"}}
        return {"ok": True, "calls": []}

    phone.call = call
    result = await ops.run({"op": "network"})
    assert result["ok"] and "no responses recorded" in result["summary"] and "ClassNotFoundException" in result["summary"]


def test_list_data_and_network_are_reads_the_phone_agent_calls_by_element_number():
    assert not is_change({"op": "list_data"}) and not is_change({"op": "network"})
    from app.gateway.scope import FREE_OPS

    assert {"list_data", "network"} <= FREE_OPS  # a lease limited to some apps may still read
    menu = options(render(list_screen()), [])
    step, _ = tools.resolve({"name": "list_data", "arguments": {"element": "[1]", "limit": 20}}, menu)
    assert step["op"] == {"op": "list_data", "target": {"n": 1}, "limit": 20}
    _, why = tools.resolve({"name": "list_data", "arguments": {}}, menu)
    assert "needs element" in why
    step, _ = tools.resolve({"name": "network", "arguments": {"url": "search", "contains": "", "limit": 3}}, menu)
    assert step["op"] == {"op": "network", "url": "search", "limit": 3} and step["label"] == "network(url='search')"
    from app.agents.registry import SUBAGENT

    # Not offered to the agents since the 2026-11-22 tool audit (list_data never worked on a real app, network was
    # never called); the ops and resolve() above stay. read_text is.
    offered = {f["function"]["name"] for f in SUBAGENT.functions}
    assert "read_text" in offered and not {"list_data", "network"} & offered


async def test_what_list_data_and_network_return_is_scrubbed_of_private_values_at_the_gateway(leased_gateway, monkeypatch):
    gateway, client = leased_gateway

    class Lane:
        async def run_many(self, ops):
            return [{"op": "network", "ok": True, "summary": "card 4111 1111 1111 1111 in the body"}]

    monkeypatch.setitem(gateway.core.ops_lane, "device-1", Lane())
    monkeypatch.setitem(gateway.core.redactions, "device-1", {"4111 1111 1111 1111"})
    async with client:
        answer = await client.post("/devices/device-1/ops", json={"owner": "agent", "ops": [{"op": "network"}]})
    assert "4111" not in answer.text


async def test_list_data_takes_a_row_for_a_list_that_has_no_number_of_its_own():
    """Settings' home: the RecyclerView sits in a scroll container, so only its rows are numbered."""
    rows = [element(10 + i, f"Setting {i}", flags=("click",), li=i) for i in range(3)]
    home = snapshot({"h": 2, "cls": "android.widget.ScrollView", "f": ["scroll", "can_scroll_down"], "c": [
        {"h": 5, "cls": "androidx.recyclerview.widget.RecyclerView", "id": "recycler_view", "f": ["list"], "items": 18, "c": rows}]},
        element(30, "Search"))
    ops, phone = lane(home)

    async def call(cmd, process=None, timeout=10, **args):
        phone.calls.append({"cmd": cmd, "h": args.get("h")})
        return {"ok": True, "total": 18, "items": ["Network & internet", "Connected devices"]}

    phone.call = call
    screen = render(home)
    row = next(n for n, el in screen.elements.items() if el.h == 11)
    result = await ops.run({"op": "list_data", "target": {"n": row}})
    assert result["ok"] and phone.calls == [{"cmd": "list_data", "h": 5}] and "2. Connected devices" in result["summary"]
    button = next(n for n, el in screen.elements.items() if el.h == 30)
    assert "not a list, nor a row of one" in (await ops.run({"op": "list_data", "target": {"n": button}}))["error"]


# --- apps by name, an app's shortcuts, and links an installed app declares (2026-11-22) -----------------------

APPS = [
    {"name": "Instagram", "package": "com.instagram.android", "shortcuts": []},
    {"name": "Instagram Lite", "package": "com.instagram.lite", "shortcuts": []},
    {"name": "Maps", "package": "com.google.android.apps.maps", "shortcuts": ["Home", "Work"]},
    {"name": "Play Store", "package": "com.android.vending", "shortcuts": ["My apps"]},
    {"name": "X", "package": "com.twitter.android", "shortcuts": []},
    {"name": "YT Music", "package": "com.google.android.apps.youtube.music", "shortcuts": ["Search"]},
    {"name": "Amazon", "package": "in.amazon.mShop.android.shopping", "shortcuts": ["Search", "Orders", "Cart"]},
]
AMAZON = "in.amazon.mShop.android.shopping"
FOCUS = "mCurrentFocus=Window{{4017523 u0 {package}/{package}.Main}}\n  mFocusedApp=ActivityRecord{{72e165f u0 {package}/.Main t31}}"
STARTING = "mCurrentFocus=null\n  mFocusedApp=ActivityRecord{{72e165f u0 {package}/.Main t31}}"


def home_app(ops, apps=APPS, opened='opened shortcut "Orders" of Amazon', error=None):
    """Answer the ops lane's questions to the phone's home app: its app list, and `open` at a shortcut."""
    asked = []

    async def home(request, timeout=8):
        asked.append(request)
        if request["cmd"] == "apps":
            return {"ok": True, "apps": apps}
        return {"ok": False, "error": error} if error else {"ok": True, "result": opened}

    ops.home = home
    return asked


@pytest.mark.parametrize("query, how, packages", [
    ("Maps", "name", ["com.google.android.apps.maps"]),
    ("instagram", "name", ["com.instagram.android"]),  # the app called exactly that, not Instagram Lite
    ("x", "name", ["com.twitter.android"]),  # a name its package ID does not spell
    ("ytmusic", "name", ["com.google.android.apps.youtube.music"]),
    ("play", "word", ["com.android.vending"]),
    ("insta", "part", ["com.instagram.android", "com.instagram.lite"]),
    ("twitter", "package", ["com.twitter.android"]),
    ("mshop", "package", [AMAZON]),
    ("telegram", None, []),
    ("yt", "word", ["com.google.android.apps.youtube.music"]),
    ("st", None, []),  # two letters are a name or a word of one, never a part: not the st in Store or Instagram
])
def test_an_app_is_found_by_its_name_before_its_package_id(query, how, packages):
    assert ops_module.match_apps([a for a in APPS if a["name"] != "X"], "x") == []  # X is not Excel, nor every ID with an x
    found = ops_module.match_apps(APPS, query)
    best = [app["package"] for kind, app in found if kind == found[0][0]]
    assert best == packages and (found[0][0] if found else None) == how


async def test_apps_lists_names_from_the_home_app_and_says_which_match_is_meant():
    ops, phone = lane(screen_of("x"), replies={"pm list packages -3": "package:com.instagram.android\npackage:com.instagram.lite"})
    asked = home_app(ops)
    result = await ops.run({"op": "apps", "query": "Instagram"})
    assert asked == [{"cmd": "apps"}]
    assert result["best"] == ["com.instagram.android"]  # one app is meant, though two hold the word
    assert [p["package"] for p in result["packages"]] == ["com.instagram.android", "com.instagram.lite"]
    assert result["summary"] == ("2 app(s) match 'instagram': Instagram com.instagram.android (user app); "
                                 "Instagram Lite com.instagram.lite (user app)")
    assert (await ops.run({"op": "apps", "query": "telegram"}))["summary"] == "no installed app matches 'telegram'"


async def test_without_the_home_app_apps_are_found_by_package_id_system_apps_included():
    listing = "priority=0 preferredOrder=0\n  com.android.chrome/com.google.android.apps.chrome.Main\n  com.dd.doordash/.Main\n"
    ops, phone = lane(screen_of("x"), replies={"query-activities": listing, "pm list packages -3": "package:com.dd.doordash"})
    assert await ops.launchable() == [{"name": "", "package": "com.android.chrome", "shortcuts": []},
                                      {"name": "", "package": "com.dd.doordash", "shortcuts": []}]
    assert phone.adb.commands[0][:2] == ("forward", "tcp:0")  # the home app was asked first, and did not answer
    result = await ops.run({"op": "apps", "query": "chrome"})
    assert result["best"] == ["com.android.chrome"] and result["packages"][0]["match"] == "package"


async def test_a_shortcut_opens_from_the_home_screen_and_counts_only_when_the_app_is_in_front(monkeypatch):
    async def no_sleep(_):
        pass

    monkeypatch.setattr(ops_module.asyncio, "sleep", no_sleep)
    replies = {"pm path": "package:/data/app/x/base.apk", "mCurrentFocus": FOCUS.format(package=AMAZON)}
    ops, phone = lane(screen_of("x"), replies=replies)
    asked = home_app(ops)
    result = await ops.run({"op": "launch", "package": AMAZON, "shortcut": "Orders"})
    assert result["ok"] and result["summary"] == f'opened {AMAZON} at its shortcut "Orders"'
    assert asked == [{"cmd": "open", "app": AMAZON, "shortcut": "Orders"}]
    # The home screen came to the front before the home app was asked: Android drops a launcher's start otherwise.
    shell = phone.adb.shell_commands()
    assert shell[1] == "am start -W -a android.intent.action.MAIN -c android.intent.category.HOME"
    assert not any("LAUNCHER" in c for c in shell)  # not the app's first screen as well

    # An app that is still starting (no window of its own yet) is waited for, and said to be starting if it never draws.
    monkeypatch.setattr(ops_module, "SHORTCUT_DRAWN", 0)
    cold, _ = lane(screen_of("x"), replies={**replies, "mCurrentFocus": STARTING.format(package=AMAZON)})
    home_app(cold)
    result = await cold.run({"op": "launch", "package": AMAZON, "shortcut": "Orders"})
    assert result["ok"] and "it is starting and has not shown its screen yet" in result["summary"]

    # The home app says "opened", and another app is still in front: that is a failure, said as one.
    monkeypatch.setattr(ops_module, "SHORTCUT_WAIT", 0)
    stuck, _ = lane(screen_of("x"), replies={**replies, "mCurrentFocus": FOCUS.format(package="app.relay.home")})
    home_app(stuck)
    result = await stuck.run({"op": "launch", "package": AMAZON, "shortcut": "Orders"})
    assert not result["ok"] and "did not open" in result["error"] and "app.relay.home is in front" in result["error"]


async def test_a_shortcut_the_app_does_not_have_or_a_home_app_that_cannot_open_one_is_a_failure(monkeypatch):
    async def no_sleep(_):
        pass

    monkeypatch.setattr(ops_module.asyncio, "sleep", no_sleep)
    replies = {"pm path": "package:/data/app/x/base.apk", "mCurrentFocus": FOCUS.format(package=AMAZON)}
    ops, _ = lane(screen_of("x"), replies=replies)
    home_app(ops, error='Amazon has no shortcut "Returns"; it has: Search | Orders | Cart')
    result = await ops.run({"op": "launch", "package": AMAZON, "shortcut": "Returns"})
    assert not result["ok"] and result["error"] == 'Amazon has no shortcut "Returns"; it has: Search | Orders | Cart'

    old, _ = lane(screen_of("x"), replies=replies)
    home_app(old, opened="opened Amazon (in.amazon.mShop.android.shopping)")  # before 0.5.0: `shortcut` is ignored
    result = await old.run({"op": "launch", "package": AMAZON, "shortcut": "Orders"})
    assert not result["ok"] and "too old to open a shortcut" in result["error"]

    none, phone = lane(screen_of("x"), replies=replies)  # no home app answers at all
    result = await none.run({"op": "launch", "package": AMAZON, "shortcut": "Orders"})
    assert not result["ok"] and "home app did not answer" in result["error"]
    assert not any("LAUNCHER" in c for c in phone.adb.shell_commands())


async def test_a_link_scheme_opens_only_when_an_installed_app_declares_it_for_links_from_outside():
    resolved = "priority=0 preferredOrder=0 match=0x208000\ncom.google.android.apps.maps/com.google.android.maps.MapsActivity"
    ops, phone = lane(screen_of("x"), replies={"resolve-activity": resolved, "am start": "Status: ok"})
    result = await ops.run({"op": "open", "uri": "google.navigation:q=Zilker Park"})
    assert result["ok"] and result["summary"] == "opened google.navigation:q=Zilker Park in com.google.android.apps.maps"
    asked, started = phone.adb.shell_commands()
    browsable = "-a android.intent.action.VIEW -c android.intent.category.BROWSABLE -d 'google.navigation:q=Zilker Park'"
    assert asked == f"cmd package resolve-activity --brief {browsable}"
    assert started == f"am start -W {browsable}"  # started the way it was asked: only what a link may open

    nothing, phone = lane(screen_of("x"), replies={"resolve-activity": "No activity found"})
    result = await nothing.run({"op": "open", "uri": "tg://resolve?domain=telegram"})
    assert not result["ok"] and result["error"].startswith("no installed app opens tg: links")
    assert len(phone.adb.shell_commands()) == 1  # asked, never started

    pinned, phone = lane(screen_of("x"), replies={"resolve-activity": "in.swiggy.android/.activities.DeepLinkActivity"})
    await pinned.run({"op": "open", "uri": "swiggy://explore", "package": "in.swiggy.android"})
    assert all(c.endswith("-d swiggy://explore -p in.swiggy.android") for c in phone.adb.shell_commands())

    # The plain schemes open as before, with no question to the phone first.
    plain, phone = lane(screen_of("x"))
    assert (await plain.run({"op": "open", "uri": "geo:0,0?q=pharmacy"}))["ok"]
    assert phone.adb.shell_commands() == ["am start -W -a android.intent.action.VIEW -d 'geo:0,0?q=pharmacy'"]


@pytest.mark.parametrize("uri", ["android-app://com.example/x", "data:text/html,<b>x</b>", "blob:x", "about:blank",
                                 "view-source:https://a.b", "FILE:///sdcard/x", "no scheme at all", "1x://y", ""])
async def test_some_links_never_open_whatever_declares_them(uri):
    ops, phone = lane(screen_of("x"), replies={"resolve-activity": "com.example/.Opens"})
    assert not (await ops.run({"op": "open", "uri": uri}))["ok"]
    assert phone.adb.commands == []


async def test_open_app_takes_a_name_several_apps_share_and_a_shortcut():
    menu = options(render(snapshot()), [])

    class Lane:
        def __init__(self, found):
            self.sent, self.found = [], found

        async def run(self, op):
            self.sent.append(op)
            return {"op": op["op"], "ok": True, "summary": "done", **(self.found if op["op"] == "apps" else {})}

    two = {"packages": [{"package": "com.instagram.android", "name": "Instagram"}, {"package": "com.instagram.lite", "name": "Instagram Lite"}]}
    # A whole name means one app, though two match the word.
    lane_ = Lane({**two, "best": ["com.instagram.android"]})
    step, _ = tools.resolve({"name": "open_app", "arguments": {"app": "Instagram"}}, menu)
    assert (await step["run"](lane_))["ok"] and lane_.sent[-1] == {"op": "launch", "package": "com.instagram.android"}
    # A part of two names means neither: they are named, with their package IDs, and nothing opens.
    lane_ = Lane({**two, "best": ["com.instagram.android", "com.instagram.lite"]})
    step, _ = tools.resolve({"name": "open_app", "arguments": {"app": "insta"}}, menu)
    result = await step["run"](lane_)
    assert not result["ok"] and "Instagram com.instagram.android, Instagram Lite com.instagram.lite" in result["summary"]
    assert [op["op"] for op in lane_.sent] == ["apps"]
    lane_ = Lane({"packages": [], "best": []})
    step, _ = tools.resolve({"name": "open_app", "arguments": {"app": "Telegram"}}, menu)
    assert "it is not installed" in (await step["run"](lane_))["summary"]
    # A shortcut goes with the launch, by package ID or by name.
    lane_ = Lane({"packages": [{"package": AMAZON, "name": "Amazon"}], "best": [AMAZON]})
    step, _ = tools.resolve({"name": "open_app", "arguments": {"app": "Amazon", "shortcut": " Orders "}}, menu)
    assert step["label"] == "open_app(Amazon, shortcut=Orders)"
    await step["run"](lane_)
    assert lane_.sent[-1] == {"op": "launch", "package": AMAZON, "shortcut": "Orders"}


def test_list_apps_is_not_offered_to_the_agents_and_still_runs_for_the_mcp_server_and_the_cli():
    from app.agents.registry import SUBAGENT, VERIFIER

    for agent in (SUBAGENT, VERIFIER):
        offered = {f["function"]["name"]: f["function"] for f in agent.functions}
        assert "list_apps" not in offered and "list_apps" not in agent.operator
        assert set(offered["open_app"]["parameters"]["properties"]) == {"app", "shortcut"}
        assert "list_apps" not in offered["open_app"]["description"]
    step, _ = tools.resolve({"name": "list_apps", "arguments": {"query": "doordash"}}, options(render(snapshot()), []))
    assert step["op"] == {"op": "apps", "query": "doordash"}
