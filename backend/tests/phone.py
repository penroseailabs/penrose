"""A phone for tests: bridge snapshots and the device calls the screen driver makes (the interface of
device.driver.BridgeDevice and bridge.devices.XposedDevice)."""

import copy


def element(h, text=None, flags=("click",), cls="android.widget.Button", **extra):
    """One view as the bridge reports it: handle, class, flags, its own text, children in `c`."""
    node = {"h": h, "cls": cls, "f": list(flags), **extra}
    if text is not None:
        node["text"] = text
    return node


def label(h, text, **extra):
    return element(h, text, flags=(), cls="android.widget.TextView", **extra)


def field(h, text="", hint=None, flags=("edit", "click"), **extra):
    return element(h, text or None, flags=flags, cls="android.widget.EditText",
                   **({"hint": hint} if hint else {}), **extra)


def snapshot(*nodes, pkg="com.example", activity="com.example.MainActivity", kind="activity", title=None):
    """A display snapshot with one focused window of `pkg`, as XposedDevice.snapshot_display returns it."""
    window = {
        "kind": kind, "title": title or activity, "process": pkg, "package": pkg,
        "wm_type": "BASE_APPLICATION", "mode": "fullscreen", "frame": [0, 0, 1080, 2400],
        "focused": True, "nodes": list(nodes),
    }
    return {
        "pkg": pkg, "process": pkg, "activity": activity, "fragments": [], "screen": None,
        "ime_visible": False, "truncated": False, "windows": [window], "processes": [pkg],
        "display": [], "bridge_snapshots": {pkg: {}}, "chrome_problems": [], "uptimes": {pkg: 1000},
    }


def screen_of(*labels, **kwargs):
    """A screen that shows `labels` as text and one Continue button."""
    return snapshot(*[label(i + 100, t) for i, t in enumerate(labels)], element(1, "Continue"), **kwargs)


class FakePhone:
    """Shows `screens` in turn: each action inside an app or system key moves to the next.
    `screens` may also be a function of the number of actions done so far."""

    def __init__(self, *screens):
        self.screens = screens[0] if len(screens) == 1 and callable(screens[0]) else list(screens)
        self.done = 0
        self.calls = []  # bridge commands that act: {"cmd": ..., "h": ..., ...}
        self.keys, self.taps, self.swipes, self.typed = [], [], [], []
        self.events = []  # events the next "events" call returns
        self.seq = 0
        self.reads = 0  # snapshots taken
        self.error = None  # raised by every call when set
        self.pages = {}  # web page handle -> what web_page returns for it (read_page)

    def current(self):
        if callable(self.screens):
            return self.screens(self.done)
        return self.screens[min(self.done, len(self.screens) - 1)]

    def check(self):
        if self.error:
            raise self.error

    async def display(self):
        self.check()
        return [{"process": self.current()["process"], "focused": True}]

    async def bridges(self):
        self.check()
        return [self.current()["process"]]

    async def snapshot_display(self, visible=None, bridges=None, **kwargs):
        self.check()
        self.reads += 1
        return copy.deepcopy(self.current())

    async def call(self, cmd, process=None, timeout=10, **args):
        self.check()
        if cmd == "events":
            events, self.events = self.events, []
            self.seq += len(events)
            return {"ok": True, "events": events, "seq": self.seq}
        if cmd == "settle":  # waiting for the screen is not an action
            return {"ok": True, "settled": True, "waited_ms": 0}
        if cmd == "changed":  # the app moved on by itself: the next screen is there to read
            self.changes = getattr(self, "changes", 0) + 1
            self.done += 1
            return {"ok": True, "changed": True, "waited_ms": 0}
        if cmd == "web_page":  # reading a whole page changes nothing on screen
            self.calls.append({"cmd": cmd, **args})
            return self.pages.get(args.get("h"), {"ok": False, "error": "no such page"})
        if cmd == "capture":  # a picture of an element, drawn by the app: nothing changes on screen
            self.calls.append({"cmd": cmd, **args})
            return getattr(self, "captures", {}).get(args.get("h"), {"ok": False, "error": "cannot draw it"})
        self.calls.append({"cmd": cmd, **args})
        self.done += 1
        return {"ok": True, "via": "touch"}

    async def key(self, name):
        self.check()
        self.keys.append(name)
        self.done += 1

    async def tap(self, x, y):
        self.taps.append((x, y))

    async def swipe(self, x, y, x2, y2, duration_ms=350):
        self.swipes.append((x, y, x2, y2, duration_ms))

    async def type_text(self, text):
        self.typed.append(text)

    async def ops(self, ops):
        """Ops-lane results: every op succeeds and is recorded in `op_log`."""
        self.check()
        self.op_log = getattr(self, "op_log", []) + list(ops)
        return [{"op": op["op"], "ok": True, "summary": f"{op['op']} done"} for op in ops]

    async def screenshot(self):
        return b"\xff\xd8fake-jpeg"
