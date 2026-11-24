"""Saved sign-ins offered as chips in the keyboard (Gboard, read by the bridge's Inline hook): the agent sees
each chip as a numbered element in a keyboard window, the keyboard's keys stay out of its way, the chip event
says what came, and a tap on a chip is a host touch at the point the bridge names (the chip's content is
another process's surface)."""

import asyncio

from app.bridge.devices import XposedDevice
from app.bridge.screen import event_line, options, render
from app.device import driver as driver_module
from app.device.driver import Driver
from phone import FakePhone, element, field, snapshot


def chip(h, b, **inline):
    node = element(h, flags=("click", "listener", "long", "opaque"), cls="android.widget.inline.InlineContentView", b=b)
    node["inline"] = {"source": "autofill", "pinned": False, "hints": [], **inline}
    return node


def key(h, text, b):
    return element(h, text, flags=("click",), cls="com.google.android.apps.inputmethod.libs.framework.keyboard.widget.SoftKeyView", b=b)


def keyboard(*chips, with_keys=True):
    """Gboard's window as the display snapshot carries it: the strip with its chips, rows of keys below."""
    strip = element(10, flags=(), cls="android.widget.LinearLayout", b=[0, 940, 720, 1040], c=list(chips))
    keys = element(20, flags=(), cls="android.widget.FrameLayout", b=[0, 1040, 720, 1500],
                   c=[key(21, "q", [0, 1130, 70, 1210]), key(22, "w", [70, 1130, 140, 1210])]) if with_keys else None
    return {
        "kind": "keyboard", "title": "InputMethod", "process": "com.google.android.inputmethod.latin",
        "package": "com.google.android.inputmethod.latin", "wm_type": "INPUT_METHOD", "mode": "fullscreen",
        "frame": [0, 920, 720, 1600], "focused": False, "nodes": [n for n in (strip, keys) if n],
    }


def login(*windows):
    s = snapshot(element(1, "Log in"), pkg="com.instagram.android", activity="com.instagram.modal.ModalActivity")
    s["windows"].extend(windows)
    s["ime_visible"] = True
    return s


def test_chips_are_elements_and_keys_are_not():
    screen = render(login(keyboard(chip(149, [240, 952, 480, 1029], kind="suggestion", index=1, of=1),
                                   chip(157, [649, 952, 709, 1029], kind="action", pinned=True))))
    chips = [el for el in screen.elements.values() if el.node.get("inline")]
    assert [el.role for el in chips] == ["Saved sign-in", "Password manager"]
    assert chips[0].label == "saved sign-in"
    assert chips[1].label.startswith("all saved sign-ins")
    assert "keyboard: saved sign-ins offered by Google Password Manager" in screen.text
    assert '"q"' not in screen.text and "SoftKeyView" not in screen.text
    # The app's own elements are not renamed after the keyboard's package: it is one app, with a keyboard.
    assert "com.instagram.android activity" not in screen.text
    menu = options(screen, [])
    assert f"tap_{chips[0].n}" in menu and menu[f"tap_{chips[0].n}"][1] == {"cmd": "click", "n": chips[0].n}


def test_several_accounts_are_numbered_in_googles_order():
    screen = render(login(keyboard(chip(1, [100, 952, 300, 1029], kind="suggestion", index=1, of=2),
                                   chip(2, [300, 952, 500, 1029], kind="suggestion", index=2, of=2))))
    labels = [el.label for el in screen.elements.values() if el.node.get("inline")]
    assert labels == ["saved sign-in 1 of 2", "saved sign-in 2 of 2"]


def test_accounts_google_confirms_first_are_sign_ins_not_the_key():
    """Android types an account Google must confirm before filling (a password saved for a related site) as an
    action, like the pinned key: the pin tells them apart (seen with Amazon.in, 2026-10-30)."""
    screen = render(login(keyboard(chip(1, [0, 952, 240, 1029], kind="action", type="autofill:action"),
                                   chip(2, [240, 952, 480, 1029], kind="action", type="autofill:action"),
                                   chip(3, [649, 952, 709, 1029], kind="action", type="autofill:action", pinned=True))))
    chips = [el for el in screen.elements.values() if el.node.get("inline")]
    assert [el.role for el in chips] == ["Saved sign-in", "Saved sign-in", "Password manager"]
    assert chips[0].label == "saved sign-in 1 of 2 (Google confirms it before filling)"
    assert chips[1].label.startswith("saved sign-in 2 of 2")
    assert event_line({"type": "inline_suggestions", "suggestions": 0, "actions": 3}).startswith(
        "the keyboard offers 3 chips from Google Password Manager")


def test_a_keyboard_without_chips_is_not_shown():
    screen = render(login(keyboard()))
    assert "keyboard: saved" not in screen.text and '"q"' not in screen.text
    assert "keyboard: shown" in screen.text


def test_chip_event_line():
    assert event_line({"type": "inline_suggestions", "suggestions": 1, "actions": 1}) == (
        "the keyboard offers 1 saved sign-in chip from Google Password Manager and its key for the full list: "
        "numbered elements in the keyboard window")
    assert event_line({"type": "inline_suggestions", "suggestions": 2, "actions": 0}).startswith(
        "the keyboard offers 2 saved sign-in chips")


class TapDevice(XposedDevice):
    """A device whose bridge answers a click with the point to tap, and whose adb records the shell."""

    def __init__(self):
        super().__init__(serial="test")
        self.shells = []
        self.adb.shell = self.shell  # type: ignore[method-assign]

    async def shell(self, command, timeout=20):
        self.shells.append(command)
        return ""

    async def _call(self, cmd, process, timeout=10, **args):
        if cmd in ("click", "long_click"):
            return {"ok": True, "via": "host tap", "tap_at": [360, 990], "_process": process}
        return {"ok": True, "_process": process}


def test_a_chip_tap_is_a_host_touch():
    device = TapDevice()
    answer = asyncio.run(device.call("click", process="com.google.android.inputmethod.latin", h=149))
    assert device.shells == ["input tap 360 990"]
    assert answer["via"] == "input tap" and (answer["x"], answer["y"]) == (360, 990)
    device.shells.clear()
    asyncio.run(device.call("long_click", process="com.google.android.inputmethod.latin", h=149))
    assert device.shells == ["input swipe 360 990 360 990 700"]


class Offering(FakePhone):
    """A sign-in page whose password field the tap focuses; Google's chips come in the keyboard only from the
    `chips_at`-th look (the first look is before the tap, the second right after it)."""

    def __init__(self, chips_at):
        super().__init__()
        self.chips_at = chips_at

    def current(self):
        focused = self.done > 0
        flags = ("edit", "click", "password") + (("focused",) if focused else ())
        s = snapshot(field(5, hint="Password", flags=flags), element(6, "Log in"), pkg="com.shop", activity="com.shop.Login")
        if focused and self.reads >= self.chips_at:
            s["windows"].append(keyboard(chip(1, [0, 952, 420, 1029], kind="suggestion", index=1, of=1),
                                         chip(2, [649, 952, 709, 1029], kind="action", pinned=True)))
        s["ime_visible"] = focused
        return s


async def tap_password(phone, offer):
    driver = Driver(phone)
    await driver.observe()
    driver.signin_offer = offer
    target = next(el for el in driver.screen.elements.values() if el.role == "Field")
    await driver.execute("Tap the password field", {"cmd": "click", "n": target.n}, target=target)
    return driver


async def test_a_tap_on_a_field_waits_for_googles_offer_when_it_has_a_saved_sign_in():
    """Seen 2026-10-30: the chips came about a second after the tap, the look 0.3 s after it showed a bare field,
    and the agent asked the user for a password Google was offering."""
    driver = await tap_password(Offering(chips_at=4), offer=True)
    assert any(el.role == "Saved sign-in" for el in driver.screen.elements.values())


async def test_without_a_saved_sign_in_the_look_does_not_wait():
    phone = Offering(chips_at=4)
    await tap_password(phone, offer=False)
    assert phone.reads == 2  # the look before the tap and the one after it


async def test_an_offer_that_never_comes_is_waited_on_only_so_long(monkeypatch):
    monkeypatch.setattr(driver_module, "OFFER_WAIT", 0.05)  # the pause between looks is instant in tests
    phone = Offering(chips_at=10**9)
    driver = await tap_password(phone, offer=True)
    assert phone.reads > 2 and not any(el.node.get("inline") for el in driver.screen.elements.values())


class Dialog(Offering):
    """Android's own fill dialog is already over the page in the first look after the tap."""

    def current(self):
        s = super().current()
        if self.done > 0:
            s["windows"].append({"kind": "dialog", "title": "", "process": "system_server", "package": "android",
                                 "wm_type": "APPLICATION_OVERLAY", "mode": "fullscreen", "frame": [0, 900, 720, 1600],
                                 "focused": True, "nodes": [element(9, "No thanks")], "read_by": "accessibility"})
        return s


async def test_an_offer_already_there_after_the_tap_is_not_waited_for():
    phone = Dialog(chips_at=10**9)
    await tap_password(phone, offer=True)
    assert phone.reads == 2


class Searching(Offering):
    """The tap focuses the app's search box, on a site Google has a saved sign-in for."""

    def current(self):
        s = super().current()
        s["windows"][0]["nodes"][0] = field(5, hint="Search Amazon.in", flags=("edit", "click") + (("focused",) if self.done else ()))
        return s


async def test_a_search_box_is_not_a_sign_in_field_and_is_not_waited_on():
    phone = Searching(chips_at=10**9)
    await tap_password(phone, offer=True)
    assert phone.reads == 2


def test_sign_in_fields_are_known_by_their_cues():
    from app.device.driver import signin_field
    screen = render(snapshot(field(1, hint="Enter mobile number or email", id="ap_email_login"), field(2, hint="Username"),
                             field(3, hint="Search", id="nav-search"), field(4, hint="Street address"),
                             field(5, flags=("edit", "click", "password"))))
    assert [signin_field(el) for el in screen.elements.values() if el.role == "Field"] == [True, True, False, False, True]
