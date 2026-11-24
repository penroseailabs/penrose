"""Seeing and touching the leased phone through the Xposed bridge: read the whole display a moment after
each action, collect what the apps reported (toasts, dialogs, crashes, notifications), and perform one
checked action and say what it changed.

The device itself is in the gateway: `BridgeDevice` offers bridge.devices.XposedDevice's methods over HTTP.
"""

import asyncio
import contextlib
import re
import time

from ..bridge.opscatalog import NOTIFIER
from ..bridge.screen import (
    body,
    changes,
    collapse,
    describe_event,
    events_for_agent,
    loading,
    performed,
    refusals,
    render,
    window_key,
)
from ..config import settings
from ..errors import DeviceError
from ..models import PRIVATE_REF

# System processes the bridge is never loaded in: never restarted to load it.
NEVER_HOOKED = {"android", "com.android.systemui", "com.android.phone", "com.android.stk"}
# A swipe's fling keeps moving the screen after the finger lifts: the look waits this long before it settles.
GESTURE_MS = 300
# One wait for the app's next change while it loads (bridge `changed`): the screen is read again between them.
CHANGE_WAIT = 1.5
# Google's offer of a saved sign-in (chips in the keyboard, Android's fill dialog, Google's sheet) comes from other
# processes than the app's, about a second after a field takes focus (seen 0.6-1.3 s): on a screen Google has a
# saved sign-in for, the look after a tap that leaves a sign-in field focused waits for it this long at most.
OFFER_WAIT = 2.5
# A field a saved sign-in fills besides a password: the account's email, user name or phone number, known by its type,
# name, id, hint or label, the cues autofill goes by. A search box or an address field is not one.
SIGNIN_FIELD = re.compile(r"e-?mail|user|log-?in|sign-?in|phone|mobile", re.I)


def signin_field(el):
    if "password" in el.flags or el.node.get("type") in ("email", "password"):
        return True
    return bool(SIGNIN_FIELD.search(" ".join(str(el.node.get(k) or "") for k in ("id", "name", "hint")) + " " + el.label))

ELEMENT_LINE = re.compile(r'^\s*\[\d+\]\s+(\S+)(?:\s+"([^"]*)")?(?:.*?\svalue="([^"]*)")?')


TEXT_LINE = re.compile(r'^\s*(?:heading |dialog )?"([^"]*)"')


def fingerprint(screen):
    """What a screen shows, without the numbering, the page stats or anything else that changes
    while the content does not: the app, the activity, each element's role, label and value, and the
    plain text. The text matters: a long list of look-alike rows (times, prices, "Add") reads the same
    in every part of it but for the titles between them, so scrolling it is not standing still."""
    lines = body(screen.text)
    return (screen.header.get("app"), screen.header.get("activity"),
            tuple(m.groups() for line in lines if (m := ELEMENT_LINE.match(line))),
            tuple(m.group(1) for line in lines if (m := TEXT_LINE.match(line))))


class BridgeDevice:
    """bridge.devices.XposedDevice's interface for one leased device, through the gateway."""

    def __init__(self, devices, slot, owner):
        self.devices, self.slot, self.owner = devices, slot, owner

    async def display(self):
        return await self.devices.bridge(self.slot, self.owner, "display")

    async def bridges(self):
        return await self.devices.bridge(self.slot, self.owner, "bridges")

    async def snapshot_display(self, visible=None, bridges=None, settle=None, events_since=None):
        # What the caller has just read is passed along: each is an adb round trip in the gateway.
        args = {k: v for k, v in (("visible", visible), ("bridges", bridges), ("settle", settle),
                                  ("events_since", events_since)) if v is not None}
        return await self.devices.bridge(self.slot, self.owner, "snapshot_display", args=args)

    async def call(self, cmd, process=None, timeout=10, private=False, **args):
        # private: `args` hold one of the user's private values; the gateway hides it from every read after.
        return await self.devices.bridge(
            self.slot, self.owner, "call", cmd=cmd, process=process, timeout=timeout, args=args,
            **({"private": True} if private else {}),
        )

    async def key(self, name):
        await self.devices.action(self.slot, self.owner, {"kind": name})

    async def tap(self, x, y):
        await self.devices.action(self.slot, self.owner, {"kind": "tap", "x": int(x), "y": int(y)})

    async def type_text(self, text):
        """Type into the field that has focus, through the keyboard (a point the bridge cannot name)."""
        await self.devices.action(self.slot, self.owner, {"kind": "type_text", "text": text})

    async def swipe(self, x, y, x2, y2, duration_ms=350):
        await self.devices.action(self.slot, self.owner, {
            "kind": "swipe", "x": int(x), "y": int(y), "x2": int(x2), "y2": int(y2), "duration_ms": int(duration_ms),
        })


async def direct(kind, description, perform):
    return await perform()


class Driver:
    """One phone agent's view of and hands on the device. Event cursors carry over between looks, so each
    look reports only what is new. `act(kind, description, perform)` wraps every action so the caller can
    journal it; `on_outcome` hears what each action did."""

    def __init__(self, device, act=direct, on_outcome=None):
        self.device, self.act, self.on_outcome = device, act, on_outcome
        self.seq = {}  # process -> last event read from its bridge
        self.screen = None
        self.pending_events = []
        self.last_result = ""
        self.unhooked = []  # apps on screen running without the bridge (see observe)
        self.focused = None  # the process of the focused window at the last look
        self.waited = 0.0  # seconds the last look went on waiting for the app to finish loading
        self.still_loading = None  # why it was still loading when that wait gave up, or None
        self.gave_up = None  # (process, activity, why) of the screen that wait gave up on: not waited on again
        self.signin_offer = False  # Google has a saved sign-in for the screen in front (set by the phone runtime)
        # n -> the user's private entry N, or None (set by the phone runtime): typed where an action's text is {private:N}.
        self.private = None

    # --- looking ------------------------------------------------------------------------------------

    async def visible_processes(self):
        """Processes drawing a visible window that the bridge can read, focused one first."""
        windows = await self.device.display()
        bridges = set(await self.device.bridges())
        return self.readable(windows, bridges)

    @staticmethod
    def readable(windows, bridges):
        ordered = sorted(windows, key=lambda w: not w.get("focused"))
        return [p for p in dict.fromkeys(w.get("process") for w in ordered) if p in bridges]

    def note_operator(self, text):
        """A device command changed the phone ("location off"): the agent hears of it with its next look,
        so a screen that shifted under it is not a mystery."""
        self.pending_events.append({"type": "ops", "text": text, "t": 0})

    async def collect_events(self, process):
        known = self.seq.get(process, 0)
        response = await self.device.call("events", since=known, process=process)
        if response.get("seq", 0) < known:  # the app restarted: its counter began again
            response = await self.device.call("events", since=0, process=process)
        self.seq[process] = response.get("seq", known)
        return [{**e, "process": process} for e in response.get("events", [])]

    def settle_for(self, process=None, cap=None, gesture=False):
        """How a look after an action waits (devices.XposedDevice.settle), at most `cap` (look_delay_seconds).
        An element command went through `process`'s bridge, which knows when its screen stops changing. A key or a
        device command (a launch waits for the app's first frame itself, `am start -W`) has landed by the time adb
        returns (`input` waits until the app has handled the key): the app in front settles from then, a new screen
        it opens included (Settle.java). Usually 150-250 ms either way. A swipe's fling goes on after the finger
        lifts, so it waits a fixed GESTURE_MS first. Never longer than the old fixed wait."""
        cap = int((settings.look_delay_seconds if cap is None else cap) * 1000)
        return {"quiet": settings.settle_quiet_ms, "max": cap, "min": min(GESTURE_MS, cap) if gesture else 0,
                "process": process or self.focused}

    async def observe(self, after_action=False, settle_in=None, gesture=False):
        # After an action the screen is read once it has stopped changing (settle_for). Nothing judges
        # "finished loading" in general: no signal says that for every app, so the agent waits for what it expects
        # to see (wait_for). Three signs are sure enough to wait on, within loading_wait_seconds (screen.loading):
        # the app is between two screens, a spinner stands where its content will be, or a screen that has just
        # opened has empty content areas. Read in between, a screen is taken for the app's answer: a chat just
        # opened read as a bare composer and a search as a spinner, and the agent went back or searched again.
        settle = self.settle_for(settle_in, gesture=gesture) if after_action else None
        snapshot = await self.read(settle)
        self.waited, self.still_loading = 0.0, None
        new_screen = self.screen is None or (snapshot.get("process"), snapshot.get("activity")) != (
            self.focused, self.screen.header.get("activity"))
        # The windows the action opened over the same screen (a sheet, a dialog) are just-opened screens too.
        before = {window_key(w) for w in self.screen.snapshot.get("windows", [])} if self.screen else set()
        opened = {window_key(w) for w in snapshot.get("windows", [])} - before
        why = loading(snapshot, new_screen=new_screen, opened=opened) if after_action else None
        # A spinner that outlasted one wait is the app's steady state (a value that never comes, a stall): the
        # screen it stands on is not waited on again while it stays. The agent was told once; wait_for is its call.
        state = (snapshot.get("process"), snapshot.get("activity"), why) if why else None
        if why and settings.loading_wait_seconds > 0 and state != self.gave_up:
            started = time.monotonic()
            deadline = started + settings.loading_wait_seconds
            while why and (left := deadline - time.monotonic()) > 0:
                await self.next_change(snapshot, left)
                snapshot = await self.read(self.settle_for(snapshot.get("process"), cap=0.3))
                why = loading(snapshot, new_screen=new_screen, opened=opened)
            self.waited, self.still_loading = time.monotonic() - started, why
            self.gave_up = (snapshot.get("process"), snapshot.get("activity"), why) if why else None
        elif not why:
            self.gave_up = None
        # An app screen drawn by a process the bridge is not loaded in (an app opened before its hook scope
        # applied, as a freshly installed one is): it cannot be read or acted on until it is restarted.
        self.unhooked = list(dict.fromkeys(
            w["package"] for w in snapshot.get("windows", [])
            if w.get("kind") == "unreadable" and w.get("why") == "no bridge in this process"
            and w.get("wm_type") == "BASE_APPLICATION" and w.get("package") and w["package"] not in NEVER_HOOKED
        ))
        self.focused = snapshot.get("process")
        self.screen = render(snapshot)

    async def await_signin_offer(self, before):
        """After a tap left a sign-in field focused on a screen Google has a saved sign-in for: look again until its offer
        is there (a chip in the keyboard, or a window the screen before the tap did not have, as Android's fill
        dialog or Google's sheet), the field lost focus, or OFFER_WAIT passed. Read sooner, the screen showed a
        bare field and the agent asked the user for a password Google was about to offer."""
        def windows(screen):
            return {(w.get("package"), w.get("title"), w.get("wm_type")) for w in screen.snapshot.get("windows", [])
                    if w.get("wm_type") != "INPUT_METHOD"} if screen else set()

        shown = windows(before)

        def waiting(screen):
            elements = screen.elements.values()
            return (any(el.editable and "focused" in el.flags and signin_field(el) for el in elements)
                    and not any(el.node.get("inline") for el in elements) and not windows(screen) - shown)

        deadline = time.monotonic() + OFFER_WAIT
        while self.screen and waiting(self.screen) and time.monotonic() < deadline:
            await asyncio.sleep(0.3)
            await self.observe()

    async def next_change(self, snapshot, seconds):
        """Until the app in front lays out or changes screen (its bridge's `changed`), at most CHANGE_WAIT seconds;
        a plain pause when it cannot be asked (a Chrome page, an older bridge)."""
        process = snapshot.get("process")
        since = (snapshot.get("uptimes") or {}).get(process)
        wait = min(seconds, CHANGE_WAIT)
        if process and since is not None and not snapshot.get("web_page"):
            with contextlib.suppress(DeviceError, ConnectionError, OSError):
                answer = await self.device.call("changed", process=process, since=since, max=int(wait * 1000),
                                                timeout=wait + 3)
                if "changed" in answer:
                    return
        await asyncio.sleep(min(0.4, wait))

    async def read(self, settle):
        """One gateway call: the window list, every app's snapshot and its events since the last look."""
        snapshot = await self.device.snapshot_display(settle=settle, events_since=dict(self.seq))
        inline = snapshot.get("events") or {}
        events = []
        for process in snapshot["processes"]:
            if (snapshot.get("bridge_snapshots") or {}).get(process, {}).get("error"):
                continue  # its bridge did not answer the snapshot (a hung app): asking for events would wait as long
            known, reply = self.seq.get(process, 0), inline.get(process)
            if reply is None or reply.get("seq") is None or reply["seq"] < known:  # older bridge, or the app restarted
                try:
                    events += await self.collect_events(process)
                except (DeviceError, OSError):
                    pass  # one app's events are not worth the whole look: its screen is read all the same
                continue
            self.seq[process] = reply["seq"]
            events += [{**e, "process": process} for e in reply.get("events", [])]
        if snapshot.get("notifier"):  # notifications that arrived, from the module's listener service
            try:
                if NOTIFIER in self.seq:
                    events += await self.collect_events(NOTIFIER)
                else:  # first look: only what arrives from now on is news, not what is already in the shade
                    self.seq[NOTIFIER] = (await self.device.call("events", since=2**62, process=NOTIFIER)).get("seq", 0)
            except (DeviceError, OSError):
                pass
        events.sort(key=lambda e: e.get("t", 0))
        self.pending_events += events
        return snapshot

    def take_events(self):
        """What the bridges reported since the last look, in words, handed over once."""
        events = [describe_event(e) for e in collapse(events_for_agent(self.pending_events))]
        self.pending_events.clear()
        return events

    # --- acting -------------------------------------------------------------------------------------

    def gesture(self, direction):
        """A finger swipe across the middle of the screen, as the harness's device does it; `direction` is
        where the finger moves."""
        frames = [w.get("frame") for w in (self.screen.snapshot.get("windows", []) if self.screen else [])]
        frames = [f for f in frames if isinstance(f, (list, tuple)) and len(f) == 4]
        w, h = (max(f[2] for f in frames), max(f[3] for f in frames)) if frames else (1080, 2400)
        return {
            "up": (w // 2, int(h * .7), w // 2, int(h * .2)), "down": (w // 2, int(h * .2), w // 2, int(h * .7)),
            "left": (int(w * .85), h // 2, int(w * .15), h // 2), "right": (int(w * .15), h // 2, int(w * .85), h // 2),
        }[direction]

    async def execute(self, description, action, target=None, look=True):
        """Do one checked action, look again, and set `last_result` to what it changed. `action` is a menu
        action of bridge.screen.options (an element command, a key or a gesture); `target` is the element it was checked
        against (a stale handle is found again by its key). With look=False (a step inside a burst) the screen
        is not read again: only the burst's last step looks."""
        before = self.screen

        async def perform():
            if "gesture" in action:
                await self.device.swipe(*self.gesture(action["gesture"]), 300)
                return f"swiped {action['gesture']}"
            if "drag" in action:
                # Strokes inside one element's box: the screen-wide swipe could only draw along the middle of the
                # screen, so a canvas got the same two lines whatever was asked (2026-11-15).
                box = (target.node.get("b") if target else None) or []
                if len(box) != 4 or box[2] <= box[0] or box[3] <= box[1]:
                    return "dragged nothing: the element has no box on the screen"
                at = [(box[0] + x * (box[2] - box[0]), box[1] + y * (box[3] - box[1])) for x, y in action["drag"]]
                for (x1, y1), (x2, y2) in zip(at, at[1:]):
                    await self.device.swipe(x1, y1, x2, y2, 400)
                return f"dragged {len(at) - 1} stroke{'s' if len(at) > 2 else ''} in [{action['n']}]"
            if "key" in action:
                await self.device.key(action["key"])
                return f"pressed {action.get('label', action['key'])}"
            args = {k: v for k, v in action.items() if k not in ("cmd", "n")}
            private = {}
            if isinstance(args.get("text"), str) and (ref := PRIVATE_REF.fullmatch(args["text"].strip())):
                # The value goes in only here, on its way to the phone: never into the action, a log or a result.
                value = self.private(int(ref[1])) if self.private else None
                if value is None:
                    return f"typed nothing: there is no private entry #{ref[1]}"
                args["text"], private = value, {"private": True}
            if target is None:  # a command on the whole screen (hide_keyboard): every visible app's bridge is asked
                processes = await self.visible_processes()
                answers = await asyncio.gather(*(self.device.call(action["cmd"], process=p, **args) for p in processes),
                                               return_exceptions=True)
                answers = [a for a in answers if isinstance(a, dict)]
                response = next((a for a in answers if a.get("was_visible")), answers[0] if answers else
                                {"ok": False, "error": "no window with a readable bridge is visible"})
            else:
                response = await self.device.call(action["cmd"], h=target.h, process=target.process, **private, **args)
                if response.get("error") == "stale handle":
                    # The element was redrawn between reading and acting: find it again by its key.
                    fresh = render(await self.device.snapshot_display()).find(target.key)
                    if fresh:
                        response = await self.device.call(action["cmd"], h=fresh.h, process=fresh.process, **private, **args)
            return performed(action, target, response)

        kind = action.get("cmd") or action.get("key") or "gesture"
        how = await self.act(kind, description, perform)
        # The app the command went through can tell when its screen has stopped changing; keys and gestures
        # come from outside it.
        through = target.process if target is not None and "cmd" in action else None
        gesture = "gesture" in action
        if not look:
            settle = self.settle_for(through, cap=0.3, gesture=gesture)
            if settle["min"]:
                await asyncio.sleep(settle["min"] / 1000)
            settled = False
            if settle["process"] and not gesture:
                with contextlib.suppress(DeviceError, OSError):
                    await self.device.call("settle", process=settle["process"], timeout=3, quiet=settle["quiet"],
                                           max=settle["max"], from_call=True)
                    settled = True
            if not settled and not gesture:
                await asyncio.sleep(0.3)  # no readable app to ask: the old fixed wait
            self.last_result = f"{how} -> (no look: part of a burst)"
            return self.last_result
        await self.observe(after_action=True, settle_in=through, gesture=gesture)
        if self.signin_offer and action.get("cmd") in ("click", "focus"):
            await self.await_signin_offer(before)
        outcome = changes(before, self.screen, target) if before else ""
        # What the app showed in answer to the action.
        shown = [describe_event(e) for e in collapse(events_for_agent(self.pending_events))
                 if e.get("type") in ("toast", "snackbar", "dialog_shown", "crash")]
        shown = refusals(self.pending_events, how) + shown
        self.last_result = (f"{how} -> {outcome}" if outcome else how) + (f"; {'; '.join(shown[:3])}" if shown else "")
        if self.still_loading:
            self.last_result += f"; still loading after {self.waited:.0f} s ({self.still_loading})"
        elif self.waited >= 1:
            self.last_result += f" (read after waiting {self.waited:.1f} s for it to load)"
        if self.on_outcome:
            self.on_outcome(self.last_result)
        return self.last_result
