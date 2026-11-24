"""The ops lane: deterministic device commands the supervising agent (or a person) runs beside
the phone agent, with no model in the loop. Where the phone agent needs a turn per screen ("open the shade, find the
Location tile, tap it"), an op is one call: `{"op": "location", "value": "off"}`.

Three backends sit behind the one vocabulary, chosen per op:
  - plain adb shell (`settings`, `svc`, `cmd`, `pm`, `am`, `wm`): no root needed;
  - the emulator console (`adb emu ...`: SMS, calls, battery, snapshots), local emulators only;
  - the relay-bridge module: notifications (the listener service), app preferences, setting a
    control to a value, and reading the screen to wait for or check a condition.

Nothing here taps or types for the phone agent: on-screen input stays the phone agent's. Nothing runs as root, wipes app
data, or reads credentials (preference values under credential-looking keys are masked by the
module before they get here).

`Ops.run(op)` always returns a dict: {"op", "ok", "summary", ...}. Failures are results, not
exceptions, so a caller can show them to a model as they are.
"""

import asyncio
import json
import re
import shlex
import time

from .devices import KEYS, DeviceError
from .opscatalog import CATALOG, NOTIFIER, catalog_text, is_change  # noqa: F401
from .screen import render

PACKAGE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+")
NUMBER = re.compile(r"\+?\d{3,15}")
URI_SCHEMES = ("http", "https", "market", "geo", "mailto", "tel", "sms", "smsto")
# Any other scheme opens only when an installed app says a link from outside may open it: an intent filter for
# VIEW with the category BROWSABLE, Android's own mark for what a link on a web page may start. So an app's deep
# links work (google.navigation:, an app's own scheme) with no list of apps here, and a scheme nothing declares
# is refused. These never open, whatever declares them: they carry a file, another app's data, code or a whole
# intent instead of an address.
URI_REFUSED = ("intent", "android-app", "file", "content", "javascript", "data", "blob", "about", "view-source", "vbscript")
URI_SCHEME = re.compile(r"[a-z][a-z0-9+.\-]{0,63}")
BROWSABLE = "-a android.intent.action.VIEW -c android.intent.category.BROWSABLE"
# The penrose home app (android/home): the phone's launcher, which knows every app's name and its shortcuts, and
# is the only app Android lets start one.
HOME_SOCKET = "relayhome"
HOME_INTENT = "-a android.intent.action.MAIN -c android.intent.category.HOME"
HOME_REPLY_BYTES = 1024 * 1024
SHORTCUT_WAIT = 4.0  # seconds a shortcut's app has to start coming to the front
SHORTCUT_DRAWN = 30.0  # seconds its first screen then has to show, as a plain launch gets (Amazon: 10 to 20 s, 2026-11-22)
SHORTCUTS_SHOWN = 10
# The keys an op may press: the device's own, never navigation (the phone agent's) or typing.
WAIT_MAX = 5  # seconds a wait_for may hold
CHANGE_WAIT = 1.0  # seconds a wait_for blocks on the app in front before reading the screen again regardless
SETTLED_S = 1.5  # seconds the screen stays the same (nothing loading) before a wait concludes the app has stopped
DEVICE_KEYS = ("power", "wake", "sleep", "volume_up", "volume_down", "mute")
ON = {"on", "true", "yes", "1", "enable", "enabled"}
OFF = {"off", "false", "no", "0", "disable", "disabled"}
ANIMATOR_OFF = "0.5"  # the apps' own animation speed when animations are "off" (op_animations)
# Reading an app's own data (list_data, network): bounded, so that one call cannot flood the agent's context.
LIST_ROWS, LIST_ROWS_MAX = 50, 200
LIST_ROW_CHARS, LIST_CHARS = 400, 6000
NETWORK_CALLS, NETWORK_CALLS_MAX = 5, 10
BODY_CHARS, BODY_CHARS_MAX = 1500, 4000  # the bridge keeps the first 4000 characters of a body
NETWORK_CHARS, URL_CHARS = 9000, 200
REDACTED = "[redacted]"
# Names of values that sign someone in: masked in what an app's data shows, as the module masks its preferences.
CREDENTIAL = r"passw|passcode|pwd|secret|token|auth(?!ors?\b)|cookie|session|api_?key|credential|signature|cvv|cvc|otp\b"
CREDENTIAL_KEY = re.compile(CREDENTIAL, re.I)
CREDENTIAL_PAIR = re.compile(rf'(["\']?)([\w.-]*(?:{CREDENTIAL})[\w.-]*)\1(\s*[=:]\s*)("(?:[^"\\]|\\.)*"|[^\s,;&)}}\]]+)', re.I)
BARE_OBJECT = re.compile(r"[\w.$]+@[0-9a-f]{4,}")  # what toString() gives for an object that does not describe itself

PERMISSIONS = {
    "location": ["ACCESS_FINE_LOCATION", "ACCESS_COARSE_LOCATION"],
    "camera": ["CAMERA"], "microphone": ["RECORD_AUDIO"],
    "contacts": ["READ_CONTACTS", "WRITE_CONTACTS"], "calendar": ["READ_CALENDAR", "WRITE_CALENDAR"],
    "notifications": ["POST_NOTIFICATIONS"], "phone": ["READ_PHONE_STATE", "CALL_PHONE"],
    "sms": ["READ_SMS", "SEND_SMS", "RECEIVE_SMS"], "media": ["READ_MEDIA_IMAGES", "READ_MEDIA_VIDEO", "READ_MEDIA_AUDIO"],
    "storage": ["READ_EXTERNAL_STORAGE", "WRITE_EXTERNAL_STORAGE"], "nearby_devices": ["BLUETOOTH_CONNECT", "BLUETOOTH_SCAN"],
}

class OpError(Exception):
    """A request that cannot be carried out: the message goes back to the caller as the result."""


class Ambiguous(OpError):
    """A target that names several elements: it is there, but not one of them is meant yet."""


def want(value, allowed=None):
    """on/off words to a bool, or one of `allowed` words."""
    word = str(value).strip().lower()
    if allowed is not None:
        if word not in allowed:
            raise OpError(f"value must be one of {', '.join(allowed)}")
        return word
    if word in ON:
        return True
    if word in OFF:
        return False
    raise OpError("value must be on or off")


def package_of(op, key="package"):
    package = str(op.get(key) or "").strip()
    if not PACKAGE.fullmatch(package):
        raise OpError(f"{key} must be an Android package ID like com.android.chrome")
    return package


def describe_element(el):
    return f"[{el.n}] {el.summary()}"[:160]


def plain(text):
    """Words as they are compared: lower case, punctuation as spaces ("Wi‑Fi" is "wi fi")."""
    return " ".join(re.sub(r"[\W_]+", " ", str(text or "").lower()).split())


def match_apps(apps, query):
    """The apps a name or a part of a package ID means, best first, as (how, app): "name" when it is the app's
    whole name (Maps, X, Instagram and not Instagram Lite), "word" when the name holds it as words (Play in Play
    Store), "part" when it is a part of the name, "package" when only the package ID holds it (twitter in
    com.twitter.android). A part needs three letters: "X" is the app called X or no app, never the x in Excel.
    `apps`: [{"name", "package"}]."""
    words = plain(query)
    squashed = words.replace(" ", "")
    if not squashed:
        return []
    raw = "".join(str(query).lower().split())
    parts = len(squashed) >= 3
    found = []
    for app in apps:
        name, package = plain(app.get("name")), str(app.get("package") or "").lower()
        if name and squashed == name.replace(" ", ""):
            how = 0
        elif name and f" {words} " in f" {name} ":
            how = 1
        elif parts and name and squashed in name.replace(" ", ""):
            how = 2
        elif parts and (raw in package or squashed in package.replace(".", "").replace("_", "")):
            how = 3
        else:
            continue
        found.append((how, app))
    found.sort(key=lambda hit: (hit[0], plain(hit[1].get("name")), hit[1].get("package")))
    return [(("name", "word", "part", "package")[how], app) for how, app in found]


def app_line(app):
    """An app as agents read it: its name, then its package ID ("Maps com.google.android.apps.maps")."""
    return f"{app['name']} {app['package']}" if app.get("name") else app["package"]


def masked(text):
    """Text from an app with credential-looking values (token=..., "password": "...") replaced."""
    def put(m):
        quote, key, sep, value = m.groups()
        return f"{quote}{key}{quote}{sep}" + (f'"{REDACTED}"' if value.startswith('"') else REDACTED)
    return CREDENTIAL_PAIR.sub(put, text)


def lean(value):
    """Parsed JSON without its empty values, credential-looking keys masked."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if item in (None, "", [], {}):
                continue
            if CREDENTIAL_KEY.search(str(key)) and not isinstance(item, (dict, list)):
                out[key] = REDACTED
            elif (item := lean(item)) not in (None, "", [], {}):
                out[key] = item
        return out
    if isinstance(value, list):
        return [item for item in map(lean, value) if item not in (None, "", [], {})]
    return value


def compact_body(body):
    """A response body in as few characters as it takes: JSON written again without spaces or empty fields, other
    text with its whitespace collapsed (a body cut short by the bridge is not JSON any more). Credentials masked."""
    text = str(body or "")
    try:
        return json.dumps(lean(json.loads(text)), ensure_ascii=False, separators=(",", ":"))
    except (ValueError, RecursionError):
        return " ".join(masked(text).split())


def excerpt(text, chars, needle=""):
    """At most `chars` of `text`: from a little before the first `needle` when there is one, else from the start."""
    if len(text) <= chars:
        return text
    at = text.lower().find(needle.lower()) if needle else -1
    start = max(0, min(at - chars // 3, len(text) - chars)) if at > 0 else 0
    return ("…" if start else "") + text[start:start + chars] + ("…" if start + chars < len(text) else "")


def list_around(snapshot, el):
    """The list `el` is, or is a row of (its node): its nearest list ancestor in the window it came from. A list
    inside a scroll container is not numbered itself (Settings' home), so the agent names one of its rows."""
    def find(node, lists):
        lists = [*lists, node] if "list" in node.get("f", ()) or "items" in node else lists
        if node.get("h") == el.h:
            return lists[-1] if lists else None
        for child in node.get("c") or ():
            if (found := find(child, lists)) is not None:
                return found
        return None

    for window in snapshot.get("windows") or ():
        if el.process in (None, window.get("process")):
            for root in window.get("nodes") or ():
                if (found := find(root, [])) is not None:
                    return found
    return None


def bounded(op, key, default, low, high):
    """A whole-number parameter, held to low..high."""
    value = op.get(key)
    if value in (None, ""):
        return default
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        raise OpError(f"{key} must be a whole number from {low} to {high}") from None


def resolve(screen, spec):
    """The one element of `screen` a target names. Several matches without an index, or none, are errors
    that list what was found, so the caller can be more specific."""
    if not isinstance(spec, dict) or not spec:
        raise OpError("target must be an object like " + '{"role": "Slider", "text": "Font size"}')
    if spec.get("n") is not None:  # an element's number on the screen just read
        try:
            el = screen.elements.get(int(spec["n"]))
        except (TypeError, ValueError):
            raise OpError("n must be an element's number") from None
        if el is None:
            raise OpError(f"there is no element [{spec['n']}] on the screen now: look again")
        return el
    role = str(spec.get("role") or "").lower()
    text = str(spec.get("text") or "").lower()
    ident = str(spec.get("id") or "")
    found = []
    for el in screen.elements.values():
        if role and el.role.lower() != role:
            continue
        if ident and el.node.get("id") != ident:
            continue
        if text and text not in " ".join(filter(None, (el.label, el.ctx, el.node.get("id") or "", el.node.get("hint") or ""))).lower():
            continue
        found.append(el)
    if "index" in spec:
        index = int(spec["index"])
        if not 0 <= index < len(found):
            raise OpError(f"index {index} is outside the {len(found)} matching element(s)")
        return found[index]
    if len(found) == 1:
        return found[0]
    if not found:
        raise OpError("no element matches " + str({k: v for k, v in spec.items() if v}))
    raise Ambiguous(f"{len(found)} elements match; add id or text, or an index: " + " | ".join(describe_element(e) for e in found[:6]))


class Ops:
    def __init__(self, device):
        self.device = device
        self.adb = device.adb

    # ---- plumbing ------------------------------------------------------------------------------

    async def run(self, op):
        name = str(op.get("op") or "")
        handler = getattr(self, "op_" + name, None) if name in CATALOG else None
        if handler is None:
            return {"op": name, "ok": False, "summary": f"unknown op {name!r}", "error": f"unknown op {name!r}; known: {', '.join(CATALOG)}"}
        started = time.monotonic()
        try:
            result = await handler(op)
        except OpError as exc:
            result = {"ok": False, "error": str(exc), "summary": f"{name} not done: {exc}"}
        except (DeviceError, ConnectionError, OSError, asyncio.TimeoutError) as exc:
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "summary": f"{name} failed: {exc}"}
        result.setdefault("ok", True)
        result["op"] = name
        result["ms"] = int((time.monotonic() - started) * 1000)
        return result

    async def run_many(self, ops):
        """Ops one after another, stopping at the first failure (later ones usually depend on it)."""
        results = []
        for op in ops:
            results.append(await self.run(op))
            if not results[-1]["ok"]:
                break
        return results

    async def sh(self, command, timeout=20):
        """An adb shell command; its output and error text, trimmed. A nonzero exit is not an
        error here (`pm path` of a missing package exits 1): callers read the text."""
        try:
            return (await self.adb.run("shell", f"({command}) 2>&1", timeout=timeout, check=False)).strip()
        except DeviceError as exc:
            raise OpError(str(exc)) from None

    async def emu(self, *args, timeout=30):
        serial = self.adb.serial or ""
        if not re.fullmatch(r"emulator-\d+", serial):
            raise OpError(f"the emulator console is not reachable through adb serial {serial or '(default)'!r}: "
                          "this op needs a local emulator (emulator-NNNN)")
        try:
            out = (await self.adb.run("emu", *args, timeout=timeout)).strip()
        except DeviceError as exc:
            raise OpError(str(exc)) from None
        lines = out.splitlines()
        if lines and lines[-1].startswith("KO"):
            raise OpError("emulator console: " + lines[-1])
        return out

    async def read_setting(self, namespace, key):
        return await self.sh(f"settings get {namespace} {key}")

    async def confirmed(self, read, expected, tries=4, delay=0.5):
        """Poll `read()` until it returns one of `expected`; the last reading either way."""
        value = None
        for _ in range(tries):
            value = await read()
            if value in expected:
                return True, value
            await asyncio.sleep(delay)
        return False, value

    async def screen(self):
        """(Screen, snapshot) of the display now, reading the window list and sockets once."""
        windows = await self.device.display()
        bridges = await self.device.bridges()
        snapshot = await self.device.snapshot_display(windows, bridges)
        return render(snapshot), snapshot

    async def process_for(self, op):
        """The bridge process an op addresses: the named package's, else the foreground app's."""
        if op.get("package"):
            package = package_of(op)
            if package not in await self.device.bridges():
                raise OpError(f"{package} has no running bridge: open it first (launch), and it must be in the module's scope")
            return package
        return await self.device.target()

    # ---- device state ------------------------------------------------------------------------------

    STATE_SCRIPT = "; ".join([
        'echo "wifi=$(settings get global wifi_on)"',
        'echo "data=$(settings get global mobile_data)"',
        'echo "airplane=$(settings get global airplane_mode_on)"',
        'echo "bluetooth=$(settings get global bluetooth_on)"',
        'echo "location=$(settings get secure location_mode)"',
        'echo "night=$(cmd uimode night 2>&1)"',
        'echo "autorotate=$(settings get system accelerometer_rotation)"',
        'echo "user_rotation=$(settings get system user_rotation)"',
        'echo "brightness=$(settings get system screen_brightness)"',
        'echo "brightness_mode=$(settings get system screen_brightness_mode)"',
        'echo "anim=$(settings get global window_animation_scale),$(settings get global transition_animation_scale),$(settings get global animator_duration_scale)"',
        'echo "zen=$(settings get global zen_mode)"',
        'echo "locale=$(getprop persist.sys.locale)|$(getprop ro.product.locale)"',
        'echo "tz=$(getprop persist.sys.timezone)"',
        'echo "time=$(date \'+%Y-%m-%d %H:%M:%S\')"',
        "dumpsys battery | grep -E '^  (level|AC powered|status):' | sed 's/^  /battery_/;s/: /=/;s/ /_/g'",
        'echo "awake=$(dumpsys power | grep -m1 mWakefulness=)"',
        'echo "keyguard=$(dumpsys window | grep -m1 -E \'isKeyguardShowing|mKeyguardShowing\')"',
        'echo "focus=$(dumpsys window | grep -m1 mCurrentFocus)"',
    ])

    @staticmethod
    def parse_state(text):
        raw = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)

        def onoff(key):
            value = raw.get(key, "").strip()
            return None if value in ("", "null") else value == "1"

        anim = raw.get("anim", "").split(",")
        focus = re.search(r"([\w.]+)/([\w.$]+)", raw.get("focus", ""))
        locale = [x for x in raw.get("locale", "").split("|") if x]
        battery_level = raw.get("battery_level", "").strip()
        state = {
            "wifi": onoff("wifi"), "mobile_data": onoff("data"), "airplane": onoff("airplane"), "bluetooth": onoff("bluetooth"),
            "location": None if raw.get("location", "").strip() in ("", "null") else raw["location"].strip() != "0",
            "dark_mode": "yes" in raw.get("night", "").lower() if raw.get("night") else None,
            "rotation": "auto" if raw.get("autorotate", "").strip() == "1" else
                        {"0": "portrait", "1": "landscape", "2": "portrait (upside down)", "3": "landscape (reverse)"}.get(raw.get("user_rotation", "").strip(), "?"),
            "brightness_percent": round(int(raw["brightness"]) * 100 / 255) if raw.get("brightness", "").strip().isdigit() else None,
            "adaptive_brightness": onoff("brightness_mode"),
            # The screen-to-screen scales say it: "off" leaves the apps' own animations at ANIMATOR_OFF.
            "animations": None if not anim or anim == [""] else any(a.strip() not in ("0", "0.0") for a in anim[:2]),
            "do_not_disturb": raw.get("zen", "").strip() not in ("", "0", "null"),
            "battery_percent": int(battery_level) if battery_level.isdigit() else None,
            "charging": "true" in raw.get("battery_AC_powered", "").lower() or None,
            "locale": locale[0] if locale else None,
            "timezone": raw.get("tz", "").strip() or None,
            "time": raw.get("time", "").strip() or None,
            "screen_on": "Awake" in raw.get("awake", "") if raw.get("awake") else None,
            "keyguard_showing": "true" in raw.get("keyguard", "").lower() if raw.get("keyguard") else None,
            "foreground_package": focus.group(1) if focus else None,
            "foreground_activity": focus.group(2) if focus else None,
        }
        return state

    async def op_state(self, op):
        state = self.parse_state(await self.sh(self.STATE_SCRIPT, timeout=30))
        shown = ", ".join(f"{k}={v}" for k, v in state.items() if v is not None)
        return {"summary": "state: " + shown[:300], "state": state}

    # ---- toggles ---------------------------------------------------------------------------------------

    async def op_wifi(self, op):
        on = want(op.get("value"))
        await self.sh(f"svc wifi {'enable' if on else 'disable'}")
        done, now = await self.confirmed(lambda: self.read_setting("global", "wifi_on"), {"1"} if on else {"0"})
        return {"summary": f"wifi {'on' if on else 'off'}" + ("" if done else f" (requested; setting reads {now})")}

    async def op_mobile_data(self, op):
        on = want(op.get("value"))
        await self.sh(f"svc data {'enable' if on else 'disable'}")
        done, now = await self.confirmed(lambda: self.read_setting("global", "mobile_data"), {"1"} if on else {"0"})
        return {"summary": f"mobile data {'on' if on else 'off'}" + ("" if done else f" (requested; setting reads {now})")}

    async def op_airplane(self, op):
        on = want(op.get("value"))
        out = await self.sh(f"cmd connectivity airplane-mode {'enable' if on else 'disable'}")
        if "rror" in out or "xception" in out:
            raise OpError("airplane mode could not be changed: " + out[:200])
        done, now = await self.confirmed(lambda: self.read_setting("global", "airplane_mode_on"), {"1"} if on else {"0"})
        return {"summary": f"airplane mode {'on' if on else 'off'}" + ("" if done else f" (requested; setting reads {now})")}

    async def op_bluetooth(self, op):
        on = want(op.get("value"))
        out = await self.sh(f"cmd bluetooth_manager {'enable' if on else 'disable'}")
        if "rror" in out or "xception" in out or "nknown" in out:
            out = await self.sh(f"svc bluetooth {'enable' if on else 'disable'}")
        done, now = await self.confirmed(lambda: self.read_setting("global", "bluetooth_on"), {"1"} if on else {"0"}, tries=6)
        return {"summary": f"bluetooth {'on' if on else 'off'}" + ("" if done else f" (requested; setting reads {now}; {out[:80]})")}

    async def op_location(self, op):
        on = want(op.get("value"))
        out = await self.sh(f"cmd location set-location-enabled {'true' if on else 'false'}")
        if "rror" in out or "xception" in out or "nknown" in out:
            await self.sh(f"settings put secure location_mode {3 if on else 0}")
        done, now = await self.confirmed(lambda: self.read_setting("secure", "location_mode"), {"0"} if not on else {"1", "2", "3", "4"})
        return {"summary": f"location {'on' if on else 'off'}" + ("" if done else f" (requested; setting reads {now})")}

    async def op_dark_mode(self, op):
        word = want(op.get("value"), ("on", "off", "auto", "true", "false", "yes", "no"))
        night = {"on": "yes", "true": "yes", "yes": "yes", "off": "no", "false": "no", "no": "no", "auto": "auto"}[word]
        out = await self.sh(f"cmd uimode night {night}")
        return {"summary": f"dark mode {word}", "detail": out[:120]}

    async def op_rotation(self, op):
        word = want(op.get("value"), ("auto", "portrait", "landscape"))
        if word == "auto":
            await self.sh("wm user-rotation free")
        else:
            await self.sh(f"wm user-rotation lock {0 if word == 'portrait' else 1}")
        return {"summary": f"rotation {word}"}

    async def op_brightness(self, op):
        try:
            percent = int(float(op.get("value")))
        except (TypeError, ValueError):
            raise OpError("value must be a number from 0 to 100") from None
        if not 0 <= percent <= 100:
            raise OpError("value must be a number from 0 to 100")
        await self.sh(f"settings put system screen_brightness_mode 0; settings put system screen_brightness {round(percent * 255 / 100)}")
        return {"summary": f"brightness {percent}%"}

    async def op_animations(self, op):
        # Off: screen-to-screen animations stop, and the apps' own run at half speed, never zero. At zero an app
        # that waits for its animation to end can stick halfway: a home-services app's sheet stayed under its dimmed
        # backdrop, which hid the whole sheet from the screen reading (2026-11-07).
        on = want(op.get("value"))
        scale, animator = ("1", "1") if on else ("0", ANIMATOR_OFF)
        await self.sh(f"settings put global window_animation_scale {scale}; settings put global transition_animation_scale {scale}; "
                      f"settings put global animator_duration_scale {animator}")
        return {"summary": f"animations {'on' if on else 'off'} (apply to apps as they next start animating)"}

    async def op_dnd(self, op):
        on = want(op.get("value"))
        out = await self.sh(f"cmd notification set_dnd {'priority' if on else 'off'}")
        return {"summary": f"do not disturb {'on' if on else 'off'}", "detail": out[:120]}

    async def op_keep_awake(self, op):
        on = want(op.get("value"))
        await self.sh(f"svc power stayon {'true' if on else 'false'}")
        return {"summary": f"screen stays on while charging: {'yes' if on else 'no'}"}

    # ---- location, apps, keys ----------------------------------------------------------------------

    async def op_geo(self, op):
        try:
            lat, lng = float(op.get("lat")), float(op.get("lng"))
        except (TypeError, ValueError):
            raise OpError("lat and lng must be numbers") from None
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise OpError("lat must be within -90..90 and lng within -180..180")
        accuracy = float(op.get("accuracy") or 10)
        # The shell may not mock locations until it holds this app-op.
        await self.sh("appops set com.android.shell android:mock_location allow")
        for provider in ("gps", "network"):
            for command in (f"cmd location providers add-test-provider {provider}",
                            f"cmd location providers set-test-provider-enabled {provider} true",
                            f"cmd location providers set-test-provider-location {provider} --location {lat:.7f},{lng:.7f} --accuracy {accuracy:.1f}"):
                out = await self.sh(command)
                if "Exception" in out and "already" not in out:
                    raise OpError(f"Android rejected the location update: {out[:200]}")
        return {"summary": f"device location set to {lat:.5f},{lng:.5f}", "lat": lat, "lng": lng, "accuracy": accuracy}

    async def home(self, request, timeout=8):
        """One request to the penrose home app on its socket, and its answer; None when it does not answer (not
        installed, not running, the phone's launcher is another)."""
        try:
            port = (await self.adb.run("forward", "tcp:0", f"localabstract:{HOME_SOCKET}", timeout=8)).strip()
        except DeviceError:
            return None
        if not port.isdigit():
            return None
        writer = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", int(port), limit=HOME_REPLY_BYTES), 5)
            writer.write((json.dumps({**request, "id": 1}) + "\n").encode())
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout)
            reply = json.loads(line) if line else None
            return reply if isinstance(reply, dict) else None
        except (OSError, asyncio.TimeoutError, ValueError):
            return None
        finally:
            if writer is not None:
                writer.close()
            # Only this forward: the adb server keeps each one listening for as long as it runs.
            try:
                await self.adb.run("forward", "--remove", f"tcp:{port}", timeout=5, check=False)
            except DeviceError:
                pass

    async def launchable(self):
        """Every app with a launcher icon, Google's and the system's too: [{"name", "package", "shortcuts"}] by
        name. Names and shortcuts (an app's fixed ways in: Search, Orders, New timer) come from the home app;
        without it (an older one, another launcher) the package manager gives the package IDs alone."""
        reply = await self.home({"cmd": "apps"})
        listed = reply.get("apps") if reply and reply.get("ok") else None
        if isinstance(listed, list):
            apps = [{"name": str(a.get("name") or "").strip()[:60], "package": a["package"],
                     "shortcuts": [str(s)[:40] for s in (a.get("shortcuts") or [])[:SHORTCUTS_SHOWN] if s]}
                    for a in listed if isinstance(a, dict) and PACKAGE.fullmatch(str(a.get("package") or ""))]
            if apps:
                return apps
        out = await self.sh("cmd package query-activities --brief -a android.intent.action.MAIN -c android.intent.category.LAUNCHER")
        packages = sorted({m.group(1) for m in re.finditer(r"^\s*([\w.]+)/\S+\s*$", out, re.M)})
        return [{"name": "", "package": p, "shortcuts": []} for p in packages]

    async def front(self):
        """(the package whose window has the focus, the package of the app in front): the first is None while no
        window has it, which is the seconds an app that is starting has not drawn its first screen."""
        dump = await self.sh("dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'")
        window = re.search(r"mCurrentFocus=Window\{\S+ u\d+ ([\w.]+)/", dump)
        app = re.search(r"mFocusedApp=\S*ActivityRecord\{\S+ u\d+ ([\w.]+)/", dump)
        return window and window.group(1), app and app.group(1)

    async def launch_shortcut(self, package, shortcut):
        """Open an app at one of its shortcuts (Orders, Cart, New timer): what a long press on its icon offers.
        Only the launcher may start one, and Android lets it only while its own screen shows: asked with another
        app in front, the home app answered "opened" and nothing came up (measured 2026-11-22, "Background
        activity launch blocked"). So the home screen comes to the front first, and the outcome is what is in
        front afterwards, never the home app's word."""
        await self.sh(f"am start -W {HOME_INTENT}", timeout=20)
        request = {"cmd": "open", "app": package, "shortcut": shortcut}
        reply = await self.home(request)
        for _ in range(4):  # the home screen is still coming up
            if reply is None or reply.get("ok") or "not in front" not in str(reply.get("error")):
                break
            await asyncio.sleep(0.4)
            reply = await self.home(request)
        if reply is None:
            raise OpError("the phone's home app did not answer, so no shortcut can be opened: open the app and go there through its screens")
        if not reply.get("ok"):
            raise OpError(str(reply.get("error") or "the home app could not open it")[:300])
        said = str(reply.get("result") or "")
        # Two waits, as `am start -W` is one for a plain launch: the app comes to the front (or the start was
        # dropped, and nothing will), then its first screen shows, so that the look after this reads the app and
        # not the home screen still showing behind a cold start.
        started = time.monotonic()
        window, app = await self.front()
        while window != package and time.monotonic() - started < (SHORTCUT_DRAWN if app == package else SHORTCUT_WAIT):
            await asyncio.sleep(0.3)
            window, app = await self.front()
        if package not in (window, app):
            raise OpError(f"the shortcut {shortcut!r} did not open {package}: {window or app or 'no app'} is in front. "
                          "Open the app and go there through its screens")
        if not said.startswith("opened shortcut"):  # a home app from before 0.5.0 opens the app and ignores `shortcut`
            raise OpError(f"{package} is open at its first screen: the phone's home app is too old to open a shortcut. "
                          "Go there through its screens")
        label = said.removeprefix("opened shortcut ").split(" of ")[0]
        if window != package:
            return {"summary": f"opening {package} at its shortcut {label}: it is starting and has not shown its screen "
                               "yet. wait_for it", "shortcut": shortcut}
        return {"summary": f"opened {package} at its shortcut {label}", "shortcut": shortcut}

    async def op_launch(self, op):
        package = package_of(op)
        if not (await self.sh(f"pm path {package}")).startswith("package:"):
            raise OpError(f"{package} is not installed")
        if shortcut := " ".join(str(op.get("shortcut") or "").split())[:80]:
            return await self.launch_shortcut(package, shortcut)
        found = await self.sh(f"cmd package resolve-activity --brief -c android.intent.category.LAUNCHER {package}")
        component = next((line.strip() for line in reversed(found.splitlines()) if "/" in line and " " not in line.strip()), "")
        if component:
            # -W: returns once the app has drawn its first screen, so the look after it settles from there.
            out = await self.sh(f"am start -W -n {shlex.quote(component)} -a android.intent.action.MAIN "
                                "-c android.intent.category.LAUNCHER", timeout=30)
        else:
            out = await self.sh(f"monkey -p {package} -c android.intent.category.LAUNCHER 1")
        if re.search(r"Error|Exception|No activities found|aborted", out):
            raise OpError(f"{package} did not start: {out.splitlines()[-1][:200]}")
        return {"summary": f"opened {package}", "component": component or None}

    # Android's package manager has no app labels over adb: the names come from the home app (launchable), and a
    # query is matched against them and against the package IDs (match_apps).
    async def op_apps(self, op):
        words = " ".join(str(op.get("query") or "").lower().split())
        if not words:
            raise OpError("query: an app's name or part of its package ID, such as maps or amazon")
        hits = match_apps(await self.launchable(), words)
        user_apps = set(re.findall(r"package:(\S+)", await self.sh("pm list packages -3")))
        found = hits[:12]
        listed = "; ".join(app_line(app) + (" (user app)" if app["package"] in user_apps else "") for _, app in found)
        return {"summary": f"{len(found)} app(s) match {words!r}: {listed}" if found else f"no installed app matches {words!r}",
                "packages": [{"package": app["package"], "name": app["name"], "user": app["package"] in user_apps, "match": how,
                              "shortcuts": app["shortcuts"]} for how, app in found],
                # What the words mean when they mean one app: the matches of the best kind (a whole name before a
                # part of one, a name before a package ID).
                "best": [app["package"] for how, app in found if how == found[0][0]]}

    async def op_open(self, op):
        uri = str(op.get("uri") or "").strip()
        scheme = uri.split(":", 1)[0].lower() if ":" in uri else ""
        if len(uri) > 2000 or any(c in uri for c in "\n\r\0"):
            raise OpError("uri must be one line of at most 2000 characters")
        if not URI_SCHEME.fullmatch(scheme) or scheme in URI_REFUSED:
            raise OpError(f"uri must be a link: {', '.join(s + ':' for s in URI_SCHEMES)} or an installed app's own "
                          "link scheme" + (f"; {scheme}: links are never opened" if scheme in URI_REFUSED else ""))
        target = f" -p {package_of(op)}" if op.get("package") else ""
        if scheme in URI_SCHEMES:
            how, handler = "-a android.intent.action.VIEW", ""
        else:
            how = BROWSABLE
            found = await self.sh(f"cmd package resolve-activity --brief {how} -d {shlex.quote(uri)}{target}")
            handler = next((line.strip().split("/")[0] for line in reversed(found.splitlines())
                            if "/" in line and " " not in line.strip()), "")
            if not handler:
                raise OpError(f"no installed app opens {scheme}: links" + (f" in {op['package']}" if target else "") +
                              ": the app that would is not installed, or it takes no such link. Open the app and go "
                              "there through its screens, or use its https link")
        out = await self.sh(f"am start -W {how} -d {shlex.quote(uri)}{target}", timeout=30)
        if re.search(r"Error|Exception|No activities found|aborted", out):
            raise OpError(f"nothing opened {uri[:80]}: {out.splitlines()[-1][:200]}")
        # "android" is the system's "Open with" list: several apps take the link, and the screen asks which.
        where = f" in {handler}" if handler and handler != "android" else ""
        return {"summary": f"opened {uri[:100]}{where}"}

    async def op_stop(self, op):
        package = package_of(op)
        # A force-stopped app is "stopped" until it is opened, and Android delivers it no push meanwhile: its
        # processes go, its notifications must not.
        await self.sh(f"am force-stop {package}; cmd package unstop {package} >/dev/null 2>&1")
        return {"summary": f"stopped {package}"}

    async def permissions(self, op, verb):
        package = package_of(op)
        wanted = op.get("permissions")
        if isinstance(wanted, str):
            wanted = [wanted]
        if not wanted or not isinstance(wanted, list):
            raise OpError("permissions is a list such as [\"location\", \"camera\"]")
        names = []
        for item in wanted:
            item = str(item).strip()
            if item.lower() in PERMISSIONS:
                names += ["android.permission." + p for p in PERMISSIONS[item.lower()]]
            elif re.fullmatch(r"[A-Za-z][\w.]*\.[A-Z][A-Z0-9_]+", item):
                names.append(item)
            else:
                raise OpError(f"unknown permission {item!r}; use a group ({', '.join(PERMISSIONS)}) or a full name")
        done, skipped = [], []
        for name in dict.fromkeys(names):
            out = await self.sh(f"pm {verb} {package} {name}")
            (skipped if out else done).append(name.rsplit(".", 1)[-1])
        return {"summary": f"{verb} on {package}: {', '.join(done) or 'nothing'}" + (f" (not applicable: {', '.join(skipped)})" if skipped else ""),
                "done": done, "skipped": skipped}

    async def op_grant(self, op):
        return await self.permissions(op, "grant")

    async def op_revoke(self, op):
        return await self.permissions(op, "revoke")

    async def op_shade(self, op):
        word = want(op.get("value"), ("notifications", "settings", "collapse"))
        await self.sh({"notifications": "cmd statusbar expand-notifications", "settings": "cmd statusbar expand-settings",
                       "collapse": "cmd statusbar collapse"}[word])
        if word == "collapse":
            # A collapse sent while the shade is still opening is dropped: look, and send it again.
            for _ in range(2):
                await asyncio.sleep(0.6)
                if "NotificationShade" not in await self.sh("dumpsys window | grep -m1 mCurrentFocus"):
                    break
                await self.sh("cmd statusbar collapse")
        return {"summary": f"shade {word}"}

    async def op_key(self, op):
        word = want(op.get("value"), DEVICE_KEYS)
        await self.sh(f"input keyevent {KEYS[word]}")
        return {"summary": f"pressed {word}"}

    async def op_unlock(self, op):
        await self.sh(f"input keyevent {KEYS['wake']}; wm dismiss-keyguard")
        return {"summary": "woke the screen and dismissed the keyguard"}

    async def op_hide_keyboard(self, op):
        # Whichever window shows the keyboard is not necessarily the focused one: ask every visible app.
        bridges = set(await self.device.bridges())
        processes = list(dict.fromkeys(w.get("process") for w in await self.device.display() if w.get("process") in bridges))
        if not processes:
            raise OpError("no visible window has a readable bridge")
        answers = await asyncio.gather(*(self.device.call("hide_keyboard", process=p) for p in processes), return_exceptions=True)
        shown = any(isinstance(a, dict) and a.get("was_visible") for a in answers)
        return {"summary": "keyboard hidden" if shown else "keyboard was not showing", "was_visible": shown}

    # ---- emulator console ---------------------------------------------------------------------------

    async def op_sms(self, op):
        number, text = str(op.get("number") or ""), str(op.get("text") or "")
        if not NUMBER.fullmatch(number):
            raise OpError("number must be digits, optionally with a leading +")
        if not text or len(text) > 160 or not text.isascii() or any(ord(c) < 32 for c in text):
            raise OpError("text must be 1..160 printable ASCII characters")
        await self.emu("sms", "send", number, text)
        return {"summary": f"SMS from {number} delivered to the emulator"}

    async def op_call(self, op):
        number = str(op.get("number") or "")
        if not NUMBER.fullmatch(number):
            raise OpError("number must be digits, optionally with a leading +")
        await self.emu("gsm", "call", number)
        return {"summary": f"incoming call from {number}"}

    async def op_call_end(self, op):
        number = str(op.get("number") or "")
        if not NUMBER.fullmatch(number):
            raise OpError("number must be digits, optionally with a leading +")
        await self.emu("gsm", "cancel", number)
        return {"summary": f"call from {number} ended"}

    async def op_battery(self, op):
        try:
            level = int(op.get("level"))
        except (TypeError, ValueError):
            raise OpError("level must be a number from 0 to 100") from None
        if not 0 <= level <= 100:
            raise OpError("level must be a number from 0 to 100")
        await self.emu("power", "capacity", str(level))
        if op.get("charging") is not None:
            await self.emu("power", "ac", "on" if want(op["charging"]) else "off")
        return {"summary": f"battery {level}%" + ("" if op.get("charging") is None else (", charging" if want(op["charging"]) else ", not charging"))}

    async def op_snapshot(self, op):
        action = want(op.get("action"), ("save", "load", "delete", "list"))
        if action == "list":
            return {"summary": "snapshots listed", "output": (await self.emu("avd", "snapshot", "list"))[:1500]}
        name = str(op.get("name") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name):
            raise OpError("name must be 1..32 letters, digits, - or _")
        try:
            out = await self.emu("avd", "snapshot", action, name, timeout=180)
        except OpError as exc:
            if "UNSUPPORTED" in str(exc):
                raise OpError(f"{exc}. This emulator was started with a GPU mode that cannot snapshot; "
                              "restart it with -gpu swiftshader_indirect (or angle) to use snapshots") from None
            raise
        if action == "load":  # the guest was replaced: every cached socket, pid and target is now wrong
            await self.device.reset()
        return {"summary": f"snapshot {name} {action}d", "output": out[-300:]}

    # ---- notifications -----------------------------------------------------------------------------------

    async def listed(self, op):
        response = await self.device.call("notifications", process=NOTIFIER, ongoing=bool(op.get("ongoing")))
        if not response.get("ok"):
            raise OpError(response.get("error") or "notifications are not available")
        return response["notifications"]

    async def find_notification(self, op):
        words = str(op.get("match") or "").lower().strip()
        if not words:
            raise OpError("match: words from the notification's app, title or text")
        hits = [n for n in await self.listed({"ongoing": True})
                if words in " ".join(str(n.get(k) or "") for k in ("label", "app", "title", "text", "big_text")).lower()]
        if len(hits) != 1:
            names = " | ".join(f"{n.get('label')}: {n.get('title') or n.get('text') or '(no title)'}" for n in hits[:5])
            raise OpError("no notification matches" if not hits else f"{len(hits)} notifications match; be more specific: {names}")
        return hits[0]

    async def op_notifications(self, op):
        items = await self.listed(op)
        brief = [{k: n.get(k) for k in ("label", "title", "text", "actions", "tappable", "clearable") if n.get(k) not in (None, [], "")} for n in items]
        return {"summary": f"{len(items)} notification(s)" + "".join(f"; {n.get('label')}: {n.get('title') or ''} {n.get('text') or ''}"[:90] for n in items[:3]),
                "notifications": brief}

    async def op_notification_tap(self, op):
        note = await self.find_notification(op)
        action = op.get("action")
        response = await self.device.call("notification_click", process=NOTIFIER, key=note["key"], **({"action": int(action)} if action is not None else {}))
        if not response.get("ok"):
            raise OpError(response.get("error") or "the notification could not be tapped")
        what = f"action {action}" if action is not None else "it"
        return {"summary": f"tapped {what} of {note.get('label')}: {note.get('title') or ''}"[:140]}

    async def op_notification_dismiss(self, op):
        note = await self.find_notification(op)
        await self.device.call("notification_dismiss", process=NOTIFIER, key=note["key"])
        return {"summary": f"dismissed {note.get('label')}: {note.get('title') or ''}"[:140]}

    async def op_notifications_clear(self, op):
        await self.device.call("notifications_clear", process=NOTIFIER)
        return {"summary": "cleared the dismissable notifications"}

    # ---- inside the running app (bridge) ----------------------------------------------------------------

    async def op_prefs(self, op):
        action = want(op.get("action"), ("list", "get", "set", "remove"))
        process = await self.process_for(op)
        if action == "list":
            r = await self.device.call("prefs_list", process=process)
        else:
            name = str(op.get("name") or "")
            if not name:
                raise OpError("name: the preferences file (see action=list)")
            if action == "get":
                r = await self.device.call("prefs_get", process=process, name=name, **({"key": op["key"]} if op.get("key") else {}))
            elif action == "remove":
                r = await self.device.call("prefs_remove", process=process, name=name, key=str(op.get("key") or ""))
            else:
                if not op.get("key") or "value" not in op:
                    raise OpError("set needs key and value (and type for a new key: bool, int, long, float, string, string_set)")
                r = await self.device.call("prefs_set", process=process, name=name, key=str(op["key"]), value=op["value"],
                                           **({"type": op["type"]} if op.get("type") else {}))
        if not r.get("ok"):
            raise OpError(r.get("error") or "the app refused")
        r.pop("id", None)
        r.pop("_process", None)
        r["summary"] = f"prefs {action} {process}" + (f" {op.get('name')}" if op.get("name") else "") + (f".{op.get('key')}" if op.get("key") else "")
        return r

    async def element_command(self, op, cmd, **args):
        screen, _ = await self.screen()
        el = resolve(screen, op.get("target"))
        response = await self.device.call(cmd, h=el.h, process=el.process, **args)
        if not response.get("ok", True):
            raise OpError(f"{response.get('error')} (on {describe_element(el)})")
        response.pop("id", None)
        response.pop("_process", None)
        return el, response

    async def op_set_value(self, op):
        if op.get("value") is None:
            raise OpError("value is required")
        el, r = await self.element_command(op, "set_value", value=str(op["value"]))
        return {**r, "summary": f"set {el.role} {el.label or el.node.get('id') or ''} to {op['value']}".replace("  ", " ")
                + (f" ({r['from']} -> {r['to']})" if "to" in r else "")
                + (" - already at the limit of its range" if r.get("at_limit") else " - the control stopped there, its limit" if r.get("limited") else "")}

    async def op_scroll_to(self, op):
        pos = op.get("pos")
        if pos is None:
            raise OpError("pos is required: top, bottom or a row number")
        el, r = await self.element_command(op, "scroll_to", pos=str(pos))
        return {**r, "summary": f"scrolled {el.node.get('id') or el.role} to row {r.get('position')} of {r.get('items')}"}

    async def op_inspect(self, op):
        el, r = await self.element_command(op, "inspect", path=str(op.get("path") or ""))
        return {**r, "summary": f"{r.get('path')} = {str(r.get('result'))[:100]}"}

    # ---- an app's own data (read only) -------------------------------------------------------------------

    async def op_list_data(self, op):
        limit = bounded(op, "limit", LIST_ROWS, 1, LIST_ROWS_MAX)
        start = bounded(op, "start", 0, 0, 100_000)
        screen, snapshot = await self.screen()
        el = resolve(screen, op.get("target"))
        where = describe_element(el)
        if (node := list_around(snapshot, el)) is None:
            raise OpError(f"{where} is not a list, nor a row of one")
        if node.get("virtual") or node.get("dom") or str(node.get("h")).startswith(("cdp:", "ua:")):
            raise OpError(f"{where} is drawn by Compose or a web page, which keeps no adapter: scroll it, or read_page a web page")
        r = await self.device.call("list_data", h=node["h"], process=el.process, limit=start + limit)
        if not r.get("ok", True):
            raise OpError(f"{r.get('error')} (on {where})")
        total = int(r.get("total") or 0)
        rows = [" ".join(masked(str(item)).split()) for item in (r.get("items") or [])[start:start + limit]]
        if rows and all(BARE_OBJECT.fullmatch(row) for row in rows):
            raise OpError(f"the rows of {where} have no readable text, only class names ({rows[0][:60]}): read them on the screen")
        lines, used = [], 0
        for number, row in enumerate(rows, start + 1):
            line = f"{number}. {row[:LIST_ROW_CHARS]}" + ("…" if len(row) > LIST_ROW_CHARS else "")
            if lines and used + len(line) > LIST_CHARS:
                break
            lines.append(line)
            used += len(line) + 1
        shown = f"rows {start + 1}-{start + len(lines)}" if lines else "no rows"
        after = total - start - len(lines)
        more = f"; {after} more after these (start={start + len(lines)})" if lines and after > 0 else ""
        return {"summary": f"list_data {where}: {total} rows in the app's data; {shown}{more}:\n" + "\n".join(lines),
                "total": total, "shown": len(lines), "adapter": r.get("adapter"), "source": r.get("source")}

    async def op_read_text(self, op):
        """The whole text of one native text view that a snapshot shows cut (bridge Dump.MAX_TEXT)."""
        start = bounded(op, "start", 0, 0, 10_000_000)
        screen, _ = await self.screen()
        el = resolve(screen, op.get("target"))
        where = describe_element(el)
        if el.node.get("virtual") or el.node.get("dom") or str(el.node.get("h")).startswith(("cdp:", "ua:")):
            raise OpError(f"{where} is drawn by Compose or a web page: read_page reads a page, and Screen shows the rest")
        r = await self.device.call("text", h=el.node["h"], process=el.process, start=start, limit=LIST_CHARS)
        if not r.get("ok", True):
            raise OpError(f"{r.get('error')} (on {where})")
        total, text = int(r.get("total") or 0), masked(str(r.get("text") or ""))
        end = start + len(text)
        more = f"; {total - end} more characters after these (start={end})" if end < total else ""
        return {"summary": f"read_text {where}: characters {start + 1}-{end} of {total}{more}:\n{text}", "total": total}

    async def op_network(self, op):
        process = await self.process_for(op)
        address, needle = str(op.get("url") or "").strip(), str(op.get("contains") or "").strip()
        limit = bounded(op, "limit", NETWORK_CALLS, 1, NETWORK_CALLS_MAX)
        chars = bounded(op, "chars", BODY_CHARS, 200, BODY_CHARS_MAX)
        r = await self.device.call("network", process=process, since=0)
        if not r.get("ok", True):
            raise OpError(r.get("error") or f"{process} did not answer")
        recorded = [c for c in r.get("calls") or [] if isinstance(c, dict)]
        if not recorded:
            return {"summary": await self.no_network(process), "recorded": 0, "matched": 0, "calls": []}
        matched = [c for c in reversed(recorded) if c.get("body") and address.lower() in str(c.get("url") or "").lower()
                   and needle.lower() in str(c["body"]).lower()]
        calls, parts, used = [], [], 0
        for c in matched[:limit]:
            url = excerpt(masked(str(c.get("url") or "")), URL_CHARS)
            body = excerpt(compact_body(c["body"]), chars, needle)
            kind = str(c.get("content_type") or "").split(";")[0].strip()
            head = f"{c.get('method')} {c.get('code')} {url}" + (f" [{kind}]" if kind else "")
            if parts and used + len(head) + len(body) > NETWORK_CHARS:
                break
            calls.append({"method": c.get("method"), "code": c.get("code"), "url": url, "content_type": kind, "body": body})
            parts.append(f"{len(parts) + 1}. {head}\n{body}")
            used += len(head) + len(body) + 8
        wanted = " and ".join(filter(None, (f"URL has {address!r}" if address else "", f"body has {needle!r}" if needle else "")))
        summary = (f"network {process}: {len(recorded)} responses kept, {len(matched)} with a text body"
                   + (f" where {wanted}" if wanted else "") + (f"; newest first:\n" + "\n".join(parts) if parts else ""))
        if not parts:
            urls = list(dict.fromkeys(excerpt(masked(str(c.get("url") or "").split("?")[0]), 90) for c in reversed(recorded)))
            summary += "; the latest URLs: " + " | ".join(urls[:8])
        return {"summary": summary, "recorded": len(recorded), "matched": len(matched), "calls": calls}

    async def no_network(self, process):
        """Why an app has recorded no responses: the state of its bridge's OkHttp hook."""
        try:
            state = ((await self.device.call("hello", process=process)).get("hooks") or {}).get("okhttp")
        except (DeviceError, ConnectionError, OSError, asyncio.TimeoutError):
            state = None
        why = ("its OkHttp hook is in place, but nothing came over OkHttp since the app started (it may use another "
               "HTTP library)" if state == "ok" else f"it has no OkHttp ({str(state)[:80]})" if state
               else "its bridge reports no OkHttp hook")
        return f"network {process}: no responses recorded; {why}. Read the screen instead."

    # ---- waiting and checking -----------------------------------------------------------------------------

    CONDITIONS = ("text", "gone", "element", "absent", "activity", "foreground", "event")

    async def evaluate(self, op, screen, snapshot, events):
        """(all met, what is still missing) for the conditions in `op`, against one reading."""
        missing = []
        text = screen.text.lower()
        if op.get("text") and str(op["text"]).lower() not in text:
            missing.append(f"text {op['text']!r} is not on screen")
        if op.get("gone") and str(op["gone"]).lower() in text:
            missing.append(f"text {op['gone']!r} is still on screen")
        if op.get("element"):
            try:
                resolve(screen, op["element"])
            except Ambiguous:
                pass  # several matches: it is there
            except OpError as exc:
                missing.append(str(exc))
        if op.get("absent"):
            try:
                resolve(screen, op["absent"])
                missing.append("the element is still there")
            except Ambiguous:
                missing.append("the element is still there")
            except OpError:
                pass
        if op.get("activity") and str(op["activity"]).lower() not in str(snapshot.get("activity") or "").lower():
            missing.append(f"activity is {snapshot.get('activity')}, not {op['activity']}")
        if op.get("foreground") and snapshot.get("pkg") != op["foreground"]:
            missing.append(f"foreground app is {snapshot.get('pkg')}, not {op['foreground']}")
        if op.get("event"):
            wanted = str(op["event"]).lower()
            if not any(wanted in " ".join(str(v) for v in e.values()).lower() for e in events):
                missing.append(f"no toast, dialog or notification mentioning {op['event']!r} yet")
        return not missing, missing

    async def poll_events(self, snapshot, cursors, collected):
        """Events (toasts, dialogs, notifications) since the wait began, from every readable process."""
        for process in [*snapshot.get("processes", []), *([NOTIFIER] if snapshot.get("notifier") else [])]:
            try:
                if process not in cursors:
                    cursors[process] = (await self.device.call("events", since=2**62, process=process)).get("seq", 0)
                    continue
                r = await self.device.call("events", since=cursors[process], process=process)
                cursors[process] = r.get("seq", cursors[process])
                collected += [e for e in r.get("events", []) if e.get("type") in ("toast", "snackbar", "dialog_shown", "popup_shown", "notification")]
            except (DeviceError, ConnectionError, OSError):
                continue

    async def next_change(self, snapshot, seconds):
        """Until the app in front changes (a layout, a screen opening or closing: its bridge's `changed`), at most
        CHANGE_WAIT seconds: what only redraws (a counter in a fixed box), another app, a toast or a notification is
        read again by then. A Chrome page, whose content changes without an Android layout, or an older bridge
        without `changed`: the plain 0.4 s pause."""
        process = snapshot.get("process")
        since = ((snapshot.get("bridge_snapshots") or {}).get(process) or {}).get("uptime")
        wait = min(seconds, CHANGE_WAIT)
        if wait <= 0:
            return
        if process and since is not None and not snapshot.get("web_page"):
            try:
                answer = await self.device.call("changed", process=process, since=since, max=int(wait * 1000),
                                                timeout=wait + 3)
                if "changed" in answer:
                    return
            except (DeviceError, ConnectionError, OSError):
                pass
        await asyncio.sleep(min(0.4, wait))

    async def wait(self, op, patient):
        """wait_for (patient) or check. A wait ends as soon as its conditions hold, or once the screen has settled
        without them (SETTLED_S with nothing changing and nothing loading): the app has stopped, so holding the
        turn to the timeout would only cost time. With no condition, a wait is for that settling alone."""
        conditions = [c for c in self.CONDITIONS if op.get(c) not in (None, "", False)]
        if not conditions and not patient:
            raise OpError("give at least one condition: " + ", ".join(self.CONDITIONS))
        timeout = min(max(float(op.get("timeout") or WAIT_MAX), 0.5), WAIT_MAX) if patient else 0
        deadline = time.monotonic() + timeout
        started = time.monotonic()
        cursors, events = {}, []
        missing = []
        reading, quiet_since, settled = None, started, False
        read_cost = 0.0  # how long the last read of the screen took
        while True:
            before = time.monotonic()
            screen, snapshot = await self.screen()
            now = time.monotonic()
            read_cost = now - before
            seen = (screen.text, snapshot.get("activity"), snapshot.get("pkg"))
            if seen != reading:
                reading, quiet_since = seen, now
            if op.get("event") or events:
                await self.poll_events(snapshot, cursors, events)
            settled = now - quiet_since >= SETTLED_S and "loading" not in screen.text.lower()
            if conditions:
                met, missing = await self.evaluate(op, screen, snapshot, events)
            else:
                met, missing = settled, ["the screen was still changing"]
            if met or now >= deadline or settled:
                break
            await self.next_change(snapshot, deadline - now)
            # A read that would end past the deadline is not started: on a phone that takes seconds per read (an app
            # hanging, 2026-11-24) a 5 s wait ran 10 s and more. What was read last stands as the outcome.
            if time.monotonic() + read_cost > deadline:
                break
        waited = int((time.monotonic() - started) * 1000)
        if met:
            summary = ("is true now" if not patient else f"the screen settled after {waited}ms" if not conditions
                       else f"condition met after {waited}ms")
            return {"summary": summary, "waited_ms": waited, "met": True}
        excerpt = "\n".join(screen.text.splitlines()[:12])
        how = ("not true: " if not patient else f"gave up after {waited}ms, the screen settled without it: " if settled
               else f"timed out after {waited}ms: ")
        return {"ok": False, "summary": how + "; ".join(missing), "error": "; ".join(missing), "waited_ms": waited,
                "met": False, "settled": settled, "screen_start": excerpt}

    async def op_wait_for(self, op):
        return await self.wait(op, patient=True)

    async def op_check(self, op):
        return await self.wait(op, patient=False)
