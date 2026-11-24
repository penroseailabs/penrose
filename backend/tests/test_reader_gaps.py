"""Things on screen the reader used to miss (blind-spot scan of US apps, 2026-11-02): an empty dialog hiding the
screen, the end of a long label, links inside a text, and Android's own "Open with" chooser."""

import asyncio

from app.bridge.devices import NO_BRIDGE, XposedDevice
from app.bridge.screen import clip_middle, options, render
from phone import element, label, snapshot


def with_dialog(*dialog_nodes):
    """Google Photos' backup screen: the app's screen, and a dialog of the same process on top of it."""
    snap = snapshot(element(1, "Let's go", id="onboarding_action_button"), pkg="com.google.android.apps.photos")
    snap["windows"].append({**snap["windows"][0], "kind": "dialog", "title": "dialog", "focused": False,
                            "nodes": list(dialog_nodes)})
    return snap


def test_an_empty_dialog_on_top_hides_nothing_beneath_it():
    screen = render(with_dialog(element(9, None, flags=(), cls="android.widget.FrameLayout")))
    assert [el.label for el in screen.elements.values()] == ["Let's go"]
    assert "behind the dialog" not in screen.text


def test_a_dialog_with_something_in_it_still_blocks_its_app_beneath():
    screen = render(with_dialog(label(9, "Updating the map…")))
    assert screen.elements == {}
    assert "behind the dialog; not reachable" in screen.text


def test_a_long_label_keeps_its_end_where_the_price_is():
    title = ("Handmade ceramic coffee mug with speckled glaze, 12 oz, dishwasher and microwave safe, great gift for "
             "coffee lovers and tea drinkers · Rating: 4.8 out of 5, (62) · $39 (70% off)")
    screen = render(snapshot(element(1, title)))
    shown = screen.elements[1].label
    assert len(shown) <= 160
    assert shown.startswith("Handmade ceramic coffee mug") and shown.endswith("$39 (70% off)")
    assert clip_middle("short", 160) == "short"


def test_a_link_inside_a_text_is_an_element_of_its_own():
    legal = label(5, "By signing up, you agree to the User Notice and Privacy Policy.", id="legal_text",
                  c=[{"h": 6, "cls": "android.text.style.URLSpan", "a11y_role": "link", "text": "User Notice",
                      "href": "https://www.atlassian.com/legal/user-notice", "f": ["click"], "b": [203, 1386, 323, 1414]}])
    screen = render(snapshot(legal, pkg="com.atlassian.android.jira.core"))
    link = next(el for el in screen.elements.values() if el.label == "User Notice")
    assert link.role == "Link"
    assert "href=www.atlassian.com" in link.summary()
    assert f"tap_{link.n}" in options(screen, [])
    assert '"By signing up, you agree to the User Notice and Privacy Policy."' in screen.text


CHOOSER = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?><hierarchy rotation="0">
<node package="android" class="android.widget.FrameLayout" bounds="[0,900][1080,2400]" text="" content-desc="">
<node package="android" class="android.widget.TextView" bounds="[40,940][600,1000]" text="Open with" content-desc="" />
<node package="android" class="android.widget.Button" bounds="[40,2200][500,2300]" text="Just once" content-desc=""
 clickable="true" />
</node></hierarchy>"""


class Shell:
    def __init__(self, xml):
        self.xml, self.calls = xml, []

    async def shell(self, command, timeout=20):
        self.calls.append(command)
        return self.xml


def chooser_window(package):
    return {"kind": "unreadable", "token": "w1", "title": f"{package}/.ResolverActivity", "process": "system_server",
            "package": package, "wm_type": "BASE_APPLICATION", "mode": "fullscreen", "frame": [0, 900, 1080, 2400],
            "focused": True, "nodes": [], "why": NO_BRIDGE}


def device(xml):
    d = XposedDevice.__new__(XposedDevice)
    d.adb, d._unlisted_since = Shell(xml), {}
    return d


def test_androids_own_chooser_is_read_through_accessibility():
    d, window = device(CHOOSER), chooser_window("android")
    asyncio.run(d.read_by_accessibility([window]))
    assert window["kind"] == "activity" and window["read_by"] == "accessibility"
    screen = render({**snapshot(), "pkg": "android", "windows": [window]})
    assert '"Open with"' in screen.text
    assert any(el.label == "Just once" for el in screen.elements.values())


def test_an_apps_screen_without_the_bridge_is_left_for_a_restart_not_read():
    d, window = device(CHOOSER.replace('package="android"', 'package="com.example"')), chooser_window("com.example")
    asyncio.run(d.read_by_accessibility([window]))
    assert window["kind"] == "unreadable" and d.adb.calls == []


def chat(*rows):
    """A conversation's message list, 1080 wide: each row as wide as the list, its words where its bubble is."""
    nodes = []
    for i, (words, x1, x2) in enumerate(rows):
        y = 400 + 150 * i
        nodes.append(element(30 + i, None, flags=("long",), cls="X.MessageRow", id="conversation_row", li=i,
                             b=[0, y, 1080, y + 120],
                             c=[label(60 + 2 * i + k, w, b=[x1 + (x2 - x1) * k // len(words), y + 10,
                                                          x1 + (x2 - x1) * (k + 1) // len(words), y + 60])
                                for k, w in enumerate(words)]))
    return snapshot(element(20, None, flags=("list",), cls="androidx.recyclerview.widget.RecyclerView", id="list",
                            items=len(rows), b=[0, 300, 1080, 2000], c=nodes), pkg="com.whatsapp")


def senders(screen):
    return {el.label: el.sender for el in screen.elements.values() if el.node.get("id") == "conversation_row"}


def test_chat_bubbles_say_who_wrote_them():
    screen = render(chat((["Running late", "10:05 PM"], 40, 600), (["No worries", "10:06 PM"], 560, 1050)))
    assert senders(screen) == {"Running late · 10:05 PM": "them", "No worries · 10:06 PM": "you"}
    assert "from=you" in screen.text and "from=them" in screen.text


def test_a_chat_with_only_their_messages_still_says_so():
    screen = render(chat((["Hello penrose, test here", "10:05 PM"], 50, 700)))
    assert senders(screen) == {"Hello penrose, test here · 10:05 PM": "them"}


def test_a_list_of_short_rows_is_not_a_chat():
    screen = render(chat((["Wi-Fi"], 40, 300), (["Bluetooth"], 40, 340)))
    assert set(senders(screen).values()) == {""}
    assert "from=" not in screen.text


def roots_drawer(list_cls):
    """The system file picker's "Open from" drawer (DocumentsUI RootsList, read on a test phone 2026-11-12): rows
    an icon and a title stretched to the edge, not clickable themselves, a divider with no words between."""
    rows = []
    for i, title in enumerate(["Recent", "Downloads", None, "sdk_gphone64_x86_64"]):
        y = 226 + 98 * i
        kids = [element(100 + i, None, flags=(), cls="android.widget.ImageView", b=[42, y + 14, 84, y + 84])]
        if title:
            kids.append(label(200 + i, title, b=[112, y + 32, 476, y + 66]))
        rows.append(element(50 + i, None, flags=(), cls="com.android.documentsui.sidebar.RootItemView",
                            base="android.widget.LinearLayout", li=i, b=[0, y, 490, y + 98], c=kids))
    return snapshot(element(20, None, flags=("click", "list"), cls="com.android.documentsui.sidebar.RootsList",
                            base=list_cls, id="roots_list", items=4, b=[0, 212, 490, 1516], c=rows),
                    pkg="com.google.android.documentsui")


def test_a_list_view_that_takes_taps_for_its_rows_numbers_each_row():
    screen = render(roots_drawer("android.widget.ListView"))
    rows = {el.label: el for el in screen.elements.values() if el.node.get("li") is not None}
    assert set(rows) == {"Recent", "Downloads", "sdk_gphone64_x86_64"}  # the empty divider is left out
    assert all(el.tappable and el.sender == "" for el in rows.values())  # a menu, not the user's messages
    assert "from=" not in screen.text


def test_a_recycler_views_rows_take_taps_only_when_they_say_so():
    screen = render(roots_drawer("androidx.recyclerview.widget.RecyclerView"))
    assert [el.node.get("id") for el in screen.elements.values()] == ["roots_list"]


def test_the_step_left_open_under_the_next_one_is_not_listed():
    snap = snapshot(element(1, "Next", id="registration_submit"), pkg="com.whatsapp")
    under = {"kind": "unreadable", "title": "com.whatsapp/com.whatsapp.registration.app.EULA", "process": "com.whatsapp",
             "package": "com.whatsapp", "wm_type": "BASE_APPLICATION", "mode": "fullscreen", "frame": [0, 0, 1080, 2400],
             "focused": False, "nodes": [], "why": "the bridge does not list this window"}
    snap["windows"].insert(0, under)
    screen = render(snap)
    assert "unreadable" not in screen.text and "EULA" not in screen.text
    assert [el.label for el in screen.elements.values()] == ["Next"]
    on_top = render({**snap, "windows": [snap["windows"][1], under]})  # a closing step in front: still said
    assert "EULA" in on_top.text


SCREEN_OF_THE_APP = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?><hierarchy rotation="0">
<node package="com.whatsapp" class="android.widget.FrameLayout" bounds="[0,0][1080,2400]" text="" content-desc="">
<node package="com.whatsapp" class="android.widget.Button" bounds="[40,2200][500,2300]" text="Next" content-desc=""
 clickable="true" /></node></hierarchy>"""


def test_a_closing_step_is_not_read_as_a_copy_of_the_screen_that_replaced_it():
    import time
    d = device(SCREEN_OF_THE_APP)
    closing = {**chooser_window("com.whatsapp"), "why": "the bridge does not list this window", "focused": False,
               "frame": [0, 0, 1080, 2400], "title": "com.whatsapp/com.whatsapp.backup.RestoreFromBackupActivity"}
    shown = {"kind": "activity", "package": "com.whatsapp", "process": "com.whatsapp", "wm_type": "BASE_APPLICATION",
             "frame": [0, 0, 1080, 2400], "focused": True, "nodes": [element(1, "Next")]}
    d._unlisted_since = {closing["token"]: time.monotonic() - 10}
    asyncio.run(d.read_by_accessibility([shown, closing]))
    assert closing["kind"] == "unreadable" and not closing.get("read_by")
