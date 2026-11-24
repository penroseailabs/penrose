"""The device driver: `XposedDevice` reaches the relay-bridge module inside the foreground app of
an emulator through adb and speaks its JSON commands (see android/bridge/src/app/relay/bridge/Server.java)."""

import asyncio
import contextlib
import json
import re
import time
import xml.etree.ElementTree as ET

from . import adbwire
from .chrome import CHROME, ChromePages, is_page_view
from .config import settings
from .opscatalog import NOTIFIER  # noqa: F401


class DeviceError(RuntimeError):
    pass


class Adb:
    def __init__(self, path=None, serial=None):
        self.path = path or settings.adb_path
        self.serial = serial or settings.adb_serial

    async def run(self, *args, timeout=20, binary=False, check=True):
        prefix = [self.path] + (["-s", self.serial] if self.serial else [])
        try:
            # Without starting the adb program, when it is a request adb's server takes directly (adbwire.py).
            out, err, code = await adbwire.run(prefix[1:] + list(args), timeout=timeout)
        except adbwire.Unavailable:
            pass
        except TimeoutError:
            raise DeviceError(f"adb {' '.join(args[:2])} timed out") from None
        else:
            if code and check:
                raise DeviceError(f"adb {' '.join(args[:2])} failed: {err.decode(errors='replace').strip()[:300]}")
            return out if binary else out.decode(errors="replace")
        try:
            proc = await asyncio.create_subprocess_exec(
                *prefix, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            raise DeviceError(f"adb not found at {self.path}; set ADB_PATH") from None
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()  # reaped, not left as a zombie
            raise DeviceError(f"adb {' '.join(args[:2])} timed out") from None
        if proc.returncode and check:
            raise DeviceError(f"adb {' '.join(args[:2])} failed: {err.decode(errors='replace').strip()[:300]}")
        return out if binary else out.decode(errors="replace")

    async def shell(self, command, timeout=20):
        return await self.run("shell", command, timeout=timeout)


class StaleReply(ConnectionError):
    """The reply belongs to an earlier, abandoned request: this one was sent and may have been done."""


class Connection:
    """One forwarded socket to one process's bridge; requests are serialized."""

    def __init__(self, reader, writer, port):
        self.reader, self.writer, self.port = reader, writer, port
        self.lock = asyncio.Lock()
        self.next_id = 0

    async def call(self, payload, timeout):
        async with self.lock:
            self.next_id += 1
            self.writer.write((json.dumps({**payload, "id": self.next_id}) + "\n").encode())
            await self.writer.drain()
            line = await asyncio.wait_for(self.reader.readline(), timeout)
            if not line:
                raise ConnectionError("bridge closed the connection")
            response = json.loads(line)
            # A reply to an earlier, abandoned request (timeout, oversized line) means the
            # stream is out of step: the caller drops this connection and reconnects.
            if response.get("id") != self.next_id:
                raise StaleReply(f"reply id {response.get('id')} does not match request {self.next_id}")
            return response

    def close(self):
        with contextlib.suppress(Exception):
            self.writer.close()


KEYS = {
    "back": 4, "home": 3, "enter": 66, "recents": 187,
    # Only the operator (the subagent's ops lane) presses these; the phone agent's menu offers the four above.
    "power": 26, "wake": 224, "sleep": 223, "menu": 82, "tab": 61, "delete": 67, "escape": 111, "search": 84,
    "volume_up": 24, "volume_down": 25, "mute": 164, "up": 19, "down": 20, "left": 21, "right": 22, "center": 23,
    "page_up": 92, "page_down": 93, "move_home": 122, "move_end": 123, "camera": 27,
}

MAX_REPLY_BYTES = 32 * 1024 * 1024
# Commands that only read: safe to send again when the first try timed out. Anything else (a tap, typed text,
# a scroll) may already have happened in the app, and is resent only when the bridge was gone before it read
# the request (the connection closed or broke), never after a timeout.
READ_ONLY = {"hello", "diag", "snapshot", "settle", "changed", "events", "network", "web_page", "inspect", "prefs_list",
             "prefs_get", "list_data", "text"}
# Window types that are device chrome rather than content.
SYSTEM_CHROME = {"STATUS_BAR", "STATUS_BAR_ADDITIONAL", "NAVIGATION_BAR", "NAVIGATION_BAR_PANEL", "WALLPAPER",
                 "SCREENSHOT", "SECURE_SYSTEM_OVERLAY", "ACCESSIBILITY_OVERLAY", "DOCK_DIVIDER", "POINTER"}
NO_BRIDGE, UNLISTED = "no bridge in this process", "the bridge does not list this window"
NOT_RESPONDING = "the app is not responding"
# The window manager's title of Android's "<app> isn't responding" dialog (drawn by system_server, package android),
# followed by the process it is about.
ANR_TITLE = "Application Not Responding: "
# Windows no bridge reads are read the way uiautomator reads any screen, through accessibility: Android's own
# dialogs (its autofill "Use your saved password?" dialog is drawn by system_server) and screens a bridge does
# not list (some of Google's sign-in screens). A window its own bridge does not list is usually a screen still
# opening, so it is read only once it has stayed this long; the dump costs about 2 s.
ACCESSIBILITY_DUMP = "/data/local/tmp/relay-accessibility.xml"
# Packages the bridge is never loaded in, whose screens are Android's own: an activity of theirs (the "Open with"
# chooser is one of the system's) cannot be restarted to load the bridge, so it is read through accessibility too.
SYSTEM_PACKAGES = {"android", "com.android.systemui", "com.android.phone", "com.android.stk"}
UNLISTED_GRACE = 2.5
LIST_CLASSES = ("ListView", "RecyclerView", "GridView")


def accessibility_nodes(element, row=None):
    """One element of uiautomator's dump as a bridge node (as Dump.describeVirtual writes one). Its handle is
    the point at its centre ("ua:x,y"): such an element takes a tap on the screen, nothing else."""
    m = re.fullmatch(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", element.get("bounds", ""))
    b = [int(v) for v in m.groups()] if m else [0, 0, 0, 0]
    cls = element.get("class") or "virtual"
    node = {"h": f"ua:{(b[0] + b[2]) // 2},{(b[1] + b[3]) // 2}", "cls": cls, "b": b, "virtual": True}
    if rid := element.get("resource-id"):
        node["id"] = rid.split(":id/")[-1]
    password = element.get("password") == "true"
    if text := element.get("text"):
        node["text"] = "[redacted]" if password else text
    for attr, key in (("hint", "hint"), ("content-desc", "desc")):
        if value := element.get(attr):
            node[key] = value
    flags = [flag for attr, flag in (("clickable", "click"), ("long-clickable", "long"), ("checkable", "checkable"),
                                     ("checked", "checked"), ("selected", "selected"), ("focused", "focused"),
                                     ("password", "password")) if element.get(attr) == "true"]
    if element.get("enabled") == "false":
        flags.append("disabled")
    if row is not None:
        node["li"] = row
        if "click" not in flags:
            flags.append("click")  # a list's row takes its tap through the list, which accessibility does not mark
    children = list(element)
    if cls.rsplit(".", 1)[-1] in LIST_CLASSES:
        flags.append("list")
        if element.get("scrollable") != "true":
            node["items"] = len(children)
        node["c"] = [accessibility_nodes(c, i) for i, c in enumerate(children)]
    else:
        node["c"] = [accessibility_nodes(c) for c in children]
    if flags:
        node["f"] = flags
    return node


def accessibility_roots(xml):
    """uiautomator's dump of every window on screen: one root per window, as (package, bounds, node)."""
    start, end = xml.find("<hierarchy"), xml.rfind("</hierarchy>")
    if start < 0 or end < 0:
        return []
    try:
        hierarchy = ET.fromstring(xml[start:end + len("</hierarchy>")])
    except ET.ParseError:
        return []
    return [(root.get("package") or "", node["b"], node) for root in hierarchy if (node := accessibility_nodes(root))]


def overlap(a, b):
    return max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))


def fit(a, b):
    """How well two rectangles coincide: their overlap over the area they cover together (1 = the same)."""
    common = overlap(a, b)
    area = lambda r: max(0, r[2] - r[0]) * max(0, r[3] - r[1])
    return common / (area(a) + area(b) - common) if common else 0


class XposedDevice:
    def __init__(self, serial=None):
        self.adb = Adb(serial=serial)
        self.connections: dict[str, Connection] = {}
        self._target = None  # (process, monotonic time resolved)
        self._pid_names = {}  # pid -> process name, checked against the window's package before reuse
        self._unlisted_since = {}  # window token -> when its bridge was first seen not listing it
        self.chrome = ChromePages(self.adb)

    async def close(self):
        for process in list(self.connections):
            await self.drop(process)
        await self.chrome.close()

    async def drop(self, process):
        """Close the socket to `process`'s bridge and remove its adb forward: each connect makes a new one, which
        the adb server would otherwise keep listening for as long as it runs."""
        conn = self.connections.pop(process, None)
        if conn:
            conn.close()
            with contextlib.suppress(DeviceError, OSError):
                await self.adb.run("forward", "--remove", f"tcp:{conn.port}", timeout=5, check=False)

    async def reset(self):
        """Forget every cached socket, pid and target: the guest was replaced (a snapshot was loaded)."""
        await self.close()
        self._target = None
        self._pid_names.clear()

    async def foreground(self):
        """The package that owns the focused window (a dialog of another app counts)."""
        dump = await self.adb.shell("dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'")
        for pattern in (r"mCurrentFocus=Window\{\S+ u\d+ ([\w.]+)/", r"mFocusedApp=\S*ActivityRecord\{\S+ u\d+ ([\w.]+)/"):
            if m := re.search(pattern, dump):
                return m.group(1)
        return None

    async def bridges(self):
        """Processes with a live bridge socket, from the kernel's unix socket table."""
        table = await self.adb.shell("grep -F @relaybridge. /proc/net/unix")
        return self.parse_bridges(table)

    @staticmethod
    def parse_bridges(table):
        return sorted(set(re.findall(r"@relaybridge\.(\S+)", table)))

    WINDOWS = "dumpsys window windows; dumpsys window displays | grep mCurrentFocus"
    SOCKETS_MARK = "--relay-sockets--"

    async def display_and_bridges(self):
        """display() and bridges() from one adb call: each adb spawn costs 50-150 ms on Windows."""
        dump = await self.adb.shell(f"{self.WINDOWS}; echo {self.SOCKETS_MARK}; grep -F @relaybridge. /proc/net/unix")
        windows_part, _, sockets = dump.partition(self.SOCKETS_MARK)
        return await self.display(windows_part), self.parse_bridges(sockets)

    async def connect(self, process):
        if process in self.connections:
            return self.connections[process]
        port = (await self.adb.run("forward", "tcp:0", f"localabstract:relaybridge.{process}")).strip()
        if not port.isdigit():
            raise DeviceError(f"adb forward returned {port!r}")
        # One JSON line per reply: a Compose screen's snapshot is easily a few hundred KB, far
        # above asyncio's 64 KB default line limit.
        reader, writer = await asyncio.open_connection("127.0.0.1", int(port), limit=MAX_REPLY_BYTES)
        self.connections[process] = Connection(reader, writer, int(port))
        return self.connections[process]

    async def display(self, dump=None):
        """Everything visible on the main display, top window first, from the window manager:
        title, owning package and process, type, windowing mode (fullscreen, pinned = PIP,
        multi-window = split screen), frame and focus. System bars are left out. `dump`: the window
        manager's output when the caller already has it (display_and_bridges)."""
        dump = await self.adb.shell(self.WINDOWS) if dump is None else dump
        focus = None
        for m in re.finditer(r"mCurrentFocus=Window\{(\w+) ", dump):
            focus = m.group(1)
        windows = []
        for block in re.split(r"\n(?=  Window #\d+ Window\{)", dump):
            head = re.match(r"\s*Window #\d+ Window\{(\w+) u\d+ ([^\n]*)\}:", block)
            if not head or "isVisible=true" not in block or "mDisplayId=0" not in block:
                continue
            kind = re.search(r" ty=(\w+)", block)
            kind = kind.group(1) if kind else "?"
            if kind in SYSTEM_CHROME:
                continue
            pid = re.search(r"mSession=Session\{\w+ (\d+):", block)
            frame = re.search(r"Frames: [^\n]*?frame=\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", block)
            mode = re.search(r"mWindowingMode=([\w-]+)", block)
            package = re.search(r" package=([\w.]+)", block)
            windows.append({
                "token": head.group(1), "title": head.group(2), "type": kind,
                "package": package.group(1) if package else "", "pid": int(pid.group(1)) if pid else None,
                "frame": [int(v) for v in frame.groups()] if frame else None,
                "mode": mode.group(1) if mode else "", "focused": head.group(1) == focus,
            })
        # A pid names the same process until it dies, so /proc is read only for pids not seen
        # before, or whose remembered name no longer fits the window's package (the pid was reused).
        unknown = sorted({w["pid"] for w in windows if w["pid"]
                          and not self._pid_names.get(w["pid"], "").startswith(w["package"] or "\0")})
        if unknown:
            names = await self.adb.shell("; ".join(f"echo {p}=$(cat /proc/{p}/cmdline | tr '\\0' ' ')" for p in unknown))
            for pid, name in re.findall(r"^(\d+)=(\S*)", names, re.M):
                self._pid_names[int(pid)] = name
        for w in windows:
            w["process"] = self._pid_names.get(w["pid"], "") or w["package"]
        return windows

    async def settle(self, settle):
        """After an action, before the screen is read: wait until the app it went to has stopped changing (its
        bridge's `settle`: no layout for `quiet` ms and no screen opening, see Settle.java), at most `max` ms.
        `min` ms are waited first, as a plain pause: actions the app cannot see coming (keys, swipes, launches
        from adb) keep the fixed wait. Without a readable `process` it is the plain pause of `max` ms. Never
        longer than `max` in all. Returns the bridge's answer and the ms left of the cap."""
        start = time.monotonic()
        cap = settle.get("max", 500) / 1000
        minimum = min(settle.get("min", 0) / 1000, cap)
        answer = None
        if minimum:
            await asyncio.sleep(minimum)
        process = settle.get("process")
        left = cap - (time.monotonic() - start)
        if process and left > 0:
            try:
                answer = await self.call("settle", process=process, timeout=cap + 3, quiet=settle.get("quiet", 150),
                                         max=int(left * 1000), from_call=not minimum)
            except DeviceError:
                answer = None
        if answer is None:
            await asyncio.sleep(max(0.0, cap - (time.monotonic() - start)))
        return answer, max(0, int((cap - (time.monotonic() - start)) * 1000))

    async def snapshot_display(self, visible=None, bridges=None, settle=None, events_since=None):
        """One snapshot of the whole display: each visible window read from the bridge of the
        process that draws it, in z-order (bottom first, like a bridge snapshot). Windows no
        bridge can read (System UI, keyboard, unscoped apps) are kept as unreadable layers so
        the phone agent knows something is there. A caller that has just read the window list and the
        bridge sockets passes them in; each costs an adb round trip.

        `settle` ({quiet, max, min, process}, after an action): the screen is read once it stopped changing
        (settle()); every app's snapshot then also waits, within what is left of the cap, for its own last
        layout to be `quiet` ms old, so an app that has just come up is not read mid-layout. `events_since`
        ({process: seq}, unknown processes from 0): each app's events since then come back in "events"
        ({process: {seq, events}}), saving a round trip per app."""
        settled, left = (await self.settle(settle)) if settle else (None, 0)
        if visible is None or bridges is None:
            read_visible, read_bridges = await self.display_and_bridges()
            visible = read_visible if visible is None else visible
            bridges = read_bridges if bridges is None else bridges
        bridges = set(bridges)
        # An app Android has put up its "isn't responding" dialog for cannot answer its bridge either: asking would
        # only cost the call's whole timeout on every read (twice, with the retry), and the dialog in front, which is
        # what there is to act on, is read without it (an app that hung made every look take 20 s and more).
        hung = {w["title"].split(": ", 1)[1] for w in visible
                if w.get("package") == "android" and w.get("title", "").startswith(ANR_TITLE)}
        by_process = {}
        for w in visible:
            if w.get("process") in bridges and w.get("process") not in hung:
                by_process.setdefault(w["process"], []).append(w)

        async def snap(process):
            args = {}
            if settle and left > 0:
                args["settle"] = {"quiet": settle.get("quiet", 150), "max": left, "from_call": False}
            if events_since is not None:
                args["events_since"] = int(events_since.get(process, 0))
            try:
                return await self.call("snapshot", process=process, timeout=10 + left / 1000, **args)
            except DeviceError as exc:
                return {"error": str(exc), "windows": []}

        answers = await asyncio.gather(*(snap(p) for p in by_process))
        snapshots = dict(zip(by_process, answers))
        composite = []
        used = set()
        for w in reversed(visible):  # bottom to top
            snap = snapshots.get(w.get("process"))
            match = None
            if snap:
                # The window manager names a window by its LayoutParams title, as the bridge does.
                candidates = [i for i, mw in enumerate(snap.get("windows", []))
                              if (w["process"], i) not in used and (mw.get("title") == w["title"] or (
                                  mw.get("kind") == "activity" and w["type"] == "BASE_APPLICATION"
                                  and snap.get("activity") and w["title"].endswith(snap["activity"])))]
                if candidates:
                    match = candidates[0]
                    used.add((w["process"], match))
            if match is not None:
                composite.append({**snap["windows"][match], "process": w["process"], "package": w["package"],
                                  "wm_type": w["type"], "mode": w["mode"], "frame": w["frame"], "focused": w["focused"]})
            else:
                composite.append({"kind": "unreadable", "title": w["title"], "process": w.get("process"),
                                  "package": w["package"], "wm_type": w["type"], "mode": w["mode"],
                                  "frame": w["frame"], "focused": w["focused"], "nodes": [], "token": w["token"],
                                  "why": NOT_RESPONDING if w.get("process") in hung
                                  else NO_BRIDGE if w.get("process") not in bridges else UNLISTED})
        await self.read_by_accessibility(composite)
        focused = next((w for w in visible if w["focused"]), visible[0] if visible else {})
        main = snapshots.get(focused.get("process"), {})
        chrome_problems = await self.attach_chrome_page(composite)
        web_page = chrome_problems is not None
        return {
            "pkg": focused.get("package"), "process": focused.get("process"), "activity": main.get("activity"),
            "fragments": main.get("fragments", []), "screen": main.get("screen"),
            "ime_visible": any(w["type"] == "INPUT_METHOD" for w in visible),
            "truncated": any(s.get("truncated") for s in snapshots.values()),
            "windows": composite, "processes": sorted(snapshots), "display": visible,
            "bridge_snapshots": snapshots, "chrome_problems": chrome_problems or [],
            "web_page": web_page,  # a Chrome page is on screen, read over DevTools
            "notifier": NOTIFIER in bridges,
            "settle": settled,
            # When each app's snapshot was taken (its bridge's uptime): `changed` since then is the next change.
            "uptimes": {p: s.get("uptime") for p, s in snapshots.items() if s.get("uptime") is not None},
            "events": {p: {"seq": s.get("events_seq"), "events": s.get("events", [])}
                       for p, s in snapshots.items() if "events" in s} if events_since is not None else None,
        }

    async def read_by_accessibility(self, windows):
        """Fill in the unreadable windows that accessibility can read (ACCESSIBILITY_DUMP), in place: Android's own
        dialogs and overlays, and screens their bridge has not listed for UNLISTED_GRACE seconds. An app's screen
        with no bridge at all stays unreadable: the app is restarted to load it (device.driver `unhooked`). The
        keyboard's keys are the user's and are never read."""
        now = time.monotonic()
        self._unlisted_since = {w["token"]: self._unlisted_since.get(w["token"], now)
                                for w in windows if w.get("why") == UNLISTED}
        wanted = [w for w in windows if w.get("kind") == "unreadable" and w.get("frame") and w["wm_type"] != "INPUT_METHOD"
                  and (w["why"] == NO_BRIDGE and (w["wm_type"] != "BASE_APPLICATION" or w.get("package") in SYSTEM_PACKAGES)
                       or w["why"] == UNLISTED and now - self._unlisted_since[w["token"]] >= UNLISTED_GRACE)]
        if not wanted:
            return
        try:
            xml = await self.adb.shell(f"uiautomator dump {ACCESSIBILITY_DUMP} >/dev/null 2>&1; cat {ACCESSIBILITY_DUMP};"
                                       f" rm -f {ACCESSIBILITY_DUMP}", timeout=15)
        except DeviceError:
            return
        roots = accessibility_roots(xml)
        # A root of a screen its bridge has read is that screen, not a window beside it: a closing step of an app
        # would otherwise be read as a second copy of the step that replaced it.
        for w in reversed(windows):
            if w.get("kind") in ("activity", "dialog") and not w.get("read_by") and w.get("frame"):
                fits = [(fit(bounds, w["frame"]), i) for i, (package, bounds, _) in enumerate(roots)
                        if package == w.get("package")]
                if (best := max(fits, default=(0, -1)))[0] > 0.8:
                    roots.pop(best[1])
        for w in reversed(wanted):  # top first: the window in front gets the best-fitting root
            fits = [(overlap(bounds, w["frame"]), i) for i, (package, bounds, _) in enumerate(roots)
                    if package == w["package"]]
            area, i = max(fits, default=(0, -1))
            if area:
                _, _, node = roots.pop(i)
                w.update(kind="activity" if w["wm_type"] == "BASE_APPLICATION" else "dialog", nodes=[node],
                         read_by="accessibility")

    async def attach_chrome_page(self, windows):
        """Chrome's own page view is opaque to its bridge: read the visible tab over DevTools
        (chrome.py) and hang its DOM under that view. Returns problems for the diagnostics, None without a page."""
        def find(nodes):
            for node in nodes:
                if is_page_view(node):
                    return node
                if found := find(node.get("c", [])):
                    return found
            return None

        for w in reversed(windows):  # the topmost Chrome window with a page in it
            if w.get("process") == CHROME and w.get("kind") != "unreadable" and (node := find(w.get("nodes", []))):
                self.chrome.problems = []
                await self.chrome.attach(node)
                return self.chrome.problems
        return None

    async def focused_process(self):
        """(package, process) owning the focused window. Apps like GMS draw their screens in a
        secondary process (com.google.android.gms.ui), so the package name is not enough: the
        window's session pid names the process."""
        dump = await self.adb.shell("dumpsys window windows; dumpsys window displays | grep mCurrentFocus")
        focus = None
        for m in re.finditer(r"mCurrentFocus=Window\{(\w+) u\d+ ([\w.]+)", dump):
            focus = m  # the last non-null one is the default display's
        if not focus:
            package = await self.foreground()
            return package, package
        token, package = focus.groups()
        block = re.search(r"Window\{" + token + r" [^\n]*\}:\n(?:[^\n]*\n){0,4}?[^\n]*mSession=Session\{\w+ (\d+):", dump)
        if block:
            name = (await self.adb.shell(f"cat /proc/{block.group(1)}/cmdline")).split("\0")[0].strip()
            if name:
                return package, name
        return package, package

    async def target(self):
        """The bridge process to talk to. Cached briefly: a wait polls several times a second."""
        now = time.monotonic()
        if self._target and now - self._target[1] < 1.0:
            return self._target[0]
        package, process = await self.focused_process()
        if not package:
            raise DeviceError("No focused app window (screen off or locked?).")
        if process not in await self.bridges():
            raise DeviceError(
                f"No bridge in {package} (process {process}): enable it in the LSPosed/Vector module scope, then "
                "force-stop and reopen the app. System UI (notification shade, status bar) is not scoped."
            )
        self._target = (process, now)
        return process

    async def call(self, cmd, process=None, timeout=10, **args):
        if str(args.get("h", "")).startswith("cdp:"):  # an element of a Chrome page (chrome.py)
            return {**await self.chrome.handle(cmd, args["h"], args), "_process": CHROME}
        if str(args.get("h", "")).startswith("ua:"):  # an element read through accessibility: a touch at its centre
            x, y = (int(v) for v in args["h"][3:].split(","))
            if cmd not in ("click", "long_click", "focus"):
                return {"ok": False, "error": f"{cmd} is not supported here: this window is read through accessibility, "
                                              "its elements take taps only", "_process": process}
            await self.adb.shell(f"input swipe {x} {y} {x} {y} 700" if cmd == "long_click" else f"input tap {x} {y}")
            return {"ok": True, "via": "input tap", "x": x, "y": y, "_process": process}
        process = process or await self.target()
        response = await self._call(cmd, process, timeout, **args)
        if cmd in ("click", "long_click") and response.get("tap_at"):
            # A keyboard chip drawn by another process (Inline.java): the bridge names the point, the host
            # touches the screen there, which is the one way its content takes a tap.
            x, y = (int(v) for v in response["tap_at"])
            await self.adb.shell(f"input swipe {x} {y} {x} {y} 700" if cmd == "long_click" else f"input tap {x} {y}")
            response = {**response, "via": "input tap", "x": x, "y": y}
        return response

    async def _call(self, cmd, process, timeout=10, **args):
        for attempt in range(2):
            conn = await self.connect(process)
            try:
                response = await conn.call({"cmd": cmd, **args}, timeout)
                response["_process"] = process
                return response
            except (ConnectionError, OSError, ValueError, TimeoutError) as exc:  # ValueError: JSON or line limit
                await self.drop(process)
                unsent = isinstance(exc, (ConnectionError, OSError)) and not isinstance(exc, (TimeoutError, StaleReply))
                if attempt or not (cmd in READ_ONLY or unsent):
                    raise DeviceError(f"bridge {process}: {type(exc).__name__} {exc}") from None
        raise AssertionError("unreachable")

    async def status(self):
        state = {"serial": self.adb.serial or "(default)"}
        try:
            state["devices"] = (await self.adb.run("devices")).strip().splitlines()[1:]
            state["foreground"] = await self.foreground()
            state["bridges"] = await self.bridges()
            if state["foreground"] in state["bridges"]:
                state["hello"] = await self.call("hello", process=state["foreground"])
        except DeviceError as exc:
            state["error"] = str(exc)
        return state

    async def tap(self, x, y):
        await self.adb.shell(f"input tap {int(x)} {int(y)}")

    async def swipe(self, direction):
        """A finger swipe across the middle of the screen: the gesture for what no on-screen element
        offers (the launcher's app drawer, the notification shade). `direction` is where the finger moves."""
        if not getattr(self, "size", None):
            out = await self.adb.shell("wm size")
            match = re.search(r"(\d+)x(\d+)", out.splitlines()[-1] if out.strip() else "")
            self.size = (int(match.group(1)), int(match.group(2))) if match else (1080, 2400)
        w, h = self.size
        x1, y1, x2, y2 = {
            "up": (w // 2, int(h * .7), w // 2, int(h * .2)), "down": (w // 2, int(h * .2), w // 2, int(h * .7)),
            "left": (int(w * .85), h // 2, int(w * .15), h // 2), "right": (int(w * .15), h // 2, int(w * .85), h // 2),
        }[direction]
        await self.adb.shell(f"input swipe {x1} {y1} {x2} {y2} 300")

    async def key(self, name):
        await self.adb.shell(f"input keyevent {KEYS[name]}")

    async def screenshot(self):
        return await self.adb.run("exec-out", "screencap", "-p", binary=True, timeout=15)
