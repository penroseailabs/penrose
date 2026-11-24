"""A phone agent's session: look at the phone, decide, act, turn after turn, until it calls its final tool.

The subagent and the verifier both run this loop; they differ in prompt, tools and brief.
It joins two designs.

From the emulation harness (playground/session.py, the operator tested on the live phone):
- each turn is one short model call answered with tool calls only (agents/llm.py `act`);
- the state is plain labelled text: recent actions with what each did, the last result, events, the
  screen, and the Interactions each numbered element accepts;
- screen calls name an element and an interaction and are checked against what the screen offers
  (bridge/tools.py): there are no coordinates anywhere;
- device-level calls (open_app, open_link, wait_for, set_setting...) are one call each, not a catalog;
- a screenshot comes only when the agent asks for one (request_screenshot);
- three refused turns in a row end the session.

An app on screen that runs without the bridge (one started before it was in the bridge's scope) cannot be
read; the harness stops there and asks a person to restart it. Here the runtime restarts it once and tells
the agent.

From the backend:
- the brief, first and stable: the step, the task, facts, the orchestrator's note, the previous attempt,
  memories; a session parked for the user (NeedUser) stays open on the step and goes on where it was;
- the agent's notes (`note`), the one thing it keeps of screens it no longer sees;
- the incident: a refused or failed action stays in front of the agent until a later one works;
- every action journaled, and one whose outcome is unknown marked uncertain, so it is never replayed blindly;
- the guard against circling (the same actions on the same screen);
- continuity between turns, since each turn is one stateless model call: the screens visited so far (a
  trail of app, screen and selection) and the end of the agent's own thinking last turn;
- checkpoints, the agent's own compaction: every few turns, and as soon as it starts repeating itself, it sums
  up the session (what it found, what did not work, where it is, its plan) in one extra call; that replaces
  its notes, progress and older actions, so a long session reads as short as a new one, and whatever ends
  the session, the orchestrator gets the latest summary;
- the handover: when the runtime ends a session (circling, out of turns, refused calls), an agent given a
  handover agent gets one more turn to say what it found, where it is and why it did not finish, so the
  orchestrator decides from that and not from a bare "failed".
"""

import asyncio
import base64
import io
import json
import re
import time
from collections import Counter, defaultdict
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
from PIL import Image, ImageDraw
from pydantic import ValidationError

from .. import highways
from ..bridge.screen import clip_words, options, render, selected_labels, web_pages
from ..bridge.tools import MAX_BURST, interactions_text, resolve
from ..config import settings
from ..device.driver import Driver, fingerprint
from ..errors import DeviceError, NeedUser, PhoneSetupError, ProviderError, safe_error
from ..models import PRIVATE_MARK, PRIVATE_REF
from ..tools.base import FinalTool, TurnTool
from . import pictures
from .reading import page_on_screen, reading

# The same actions on the same screen this many times is circling: warn, then stop.
REPEAT_WARN, REPEAT_STOP = 2, 4
# A repeat brings a checkpoint only this many turns after the last one: one look back per stretch of work.
CHECKPOINT_GAP = 4
# Turns in a row whose calls could not run at all (as in the harness).
REFUSED_STOP = 3
NOTES_KEPT, NOTE_LENGTH = 20, 300
# Log lines kept since the last checkpoint: one a turn, so a checkpoint (every phone_checkpoint_turns) comes first.
LOG_KEPT = 40
PROGRESS_LENGTH = 800
# Screens on the trail, and how much of the agent's last thinking it reads again.
TRAIL_KEPT = 8
THOUGHT_KEPT = 700
# What goes back when a session ends without a final tool: fits a report's summary and a verdict's reason.
ENDING_LENGTH = 700
HANDOVER_NOTICE = (
    "The runtime has ended your session: {reason} This is your handover turn: call `handover` alone; nothing "
    "else runs. It is not a failure report. Tell the orchestrator what you found (with the details), where the "
    "phone is, why the step is not finished and what you would do next, from your log or notes, the screens "
    "so far and the screen below."
)
CHECKPOINT_NOTICE = (
    "Checkpoint: call `checkpoint` alone; nothing else runs and it costs no turn. Sum up this session so far "
    "from your checkpoint (if any), log or notes, progress, recent actions and the screen: it replaces all of "
    "them, so carry over every value you found and every route that did "
    "not work. Short lines, exact values, no narration. Then look at what is left of the step before you "
    "write the plan: if a shorter way to finish it is on offer (a sort, a filter, a search, a link, another "
    "screen) than going on as you are, the plan takes it, even after much work the old way."
)
NO_SCREEN = SimpleNamespace(elements={}, header={})
# A web page that is a wall, not the site: a bot check, a block, a captcha. Its host is reported once and
# the agent told not to go round it: the product does not bypass what a site refuses this browser.
BLOCK_PAGE = re.compile(
    r"access denied|just a moment|attention required|verify (?:that )?you are (?:a )?human|are you a (?:robot|human)|"
    r"security verification|security check|bot verification|pardon our interruption|request blocked|request unsuccessful|"
    r"403 forbidden|error 403|^403\b|\bcaptcha\b|unusual traffic|not a robot|checking your browser",
    re.I,
)
BLOCKED_NOTICE = (
    'The page at {host} is blocking this browser ("{title}"). That is final for this host in this session: do not '
    "reload it, open it through a proxy, mirror, cache, translation service or other site, or fetch it with a shell "
    "command. Use another source (a search result, the app, Google Maps), or report it as a blocked site "
    "(cause control_missing)."
)
# Android's "<app> isn't responding" dialog: its Wait button's id, how many times the runtime waits it out per look,
# and how long it gives the app each time (see PhoneAgent.wait_out_not_responding).
ANR_WAIT_ID = "aerr_wait"
ANR_WAITS = 3
ANR_PAUSE_SECONDS = 3
ANR_NOTICE = (
    "The app on screen is still not responding: penrose tapped Wait on Android's \"isn't responding\" dialog {n} "
    "times and gave it a few seconds each time. Close it there (Close app) and open it again, or go another way; "
    "do not keep tapping Wait."
)
BLANK_NOTICE = (
    "The page at {host} has come back with no readable text twice in a row (0 nodes). {picture}If the page is a "
    "real one, act on what the picture shows through swipes and keys, or wait_for its text once; if it is blank or "
    "a block, do not keep reloading it: use another site or the app, or report it."
)
USER_NOTICE = (
    "The user just wrote, while you worked: \"{message}\". It is now under user_messages in your brief. If it "
    "changes or cancels this step, stop: `report` with status `failed` and a summary that begins \"The user "
    "changed this step:\" and says what they asked and where the phone is; if it adds to this step, use it; if it is about something else, carry on: the orchestrator reads it after your report."
)
TIME_NOTICE = (
    "Your time budget ends in about {left}. Finish what the step asks with what you have: note the values you "
    "found and report (a partial answer with its gaps in issues beats no answer); do not start a new search."
)
# A restarted app has this many seconds to bring its bridge up. New installs are in the bridge's scope from
# the install itself (Vector's "include new apps"), so a restart is only for an app started without it.
REHOOK_WAIT = 10.0
SIGNINS_SHOWN = 5  # account names per kind in the "Saved sign-ins" line


def clock(seconds):
    seconds = max(0, int(seconds))
    return f"T+{seconds // 60:02d}:{seconds % 60:02d}"


# Controls that change something: what a read-only agent (the verifier) never taps. On 2026-11-14 a verifier that
# doubted a deletion deleted the second copy itself, and the task's data was lost; "never delete" was a prompt rule only.
COMMIT_WORDS = re.compile(
    r"\b(delete|remove|discard|trash|erase|confirm|send|post|pay|buy|checkout|check out|purchase|book|"
    r"subscribe|save|submit|sign in|sign out|log in|log out|logout|uninstall|archive|block|reset|start|stop|pause|"
    r"resume|record|add to cart|place order|install)\b", re.I)


def read_only(agent):
    """An agent that may only look (the verifier): it opens, scrolls and searches, and changes nothing."""
    return agent.prompt == "verifier"


SEARCH_WORDS = re.compile(r"search|find|filter|query", re.I)


def searchable(element):
    """A search box: what a read-only agent may type into, to find an item in a long list (a check of "the recipe
    was added" among 43 could not search, and failed twice, 2026-11-15). Any other field holds data."""
    names = " ".join(str(x) for x in (element.label, element.node.get("id"), element.node.get("hint"),
                                       element.node.get("desc")) if x)
    return bool(SEARCH_WORDS.search(names))


NOT_CONTROLS = ("Field", "Item", "List", "Pager", "Scroll", "Toolbar", "Heading")
CONTROL_IDS = {"btn", "button", "action", "fab", "bt", "menu"}


def commits(element):
    """The word that makes `element` a control that changes something, or None. Only a control counts: a short
    label that starts with the verb ("Delete", "Place order", "Add to cart") or an id made of it (btn_delete).
    A list row, a field or a longer text that only holds the word ("Order #1234", "Your order has shipped",
    "Start date", "Address book") is something a check opens or reads."""
    if element.list_pos or element.node.get("li") is not None or element.role in NOT_CONTROLS:
        return None
    label = str(element.label or element.node.get("desc") or "").strip()
    if (found := COMMIT_WORDS.match(label)) and len(label.split()) <= 4 and not re.search(r"\d", label):
        return found[0]
    # An id names a control when it is the verb alone or a button's name (btn_delete, action_save, fab_send);
    # text_input_start_icon or post_card only hold the word.
    words = [w for w in re.split(r"[_\W]+", str(element.node.get("id") or "").lower()) if w]
    if len(words) == 1 or CONTROL_IDS & set(words):
        return next((w for w in words if COMMIT_WORDS.fullmatch(w)), None)
    return None


def private_boxes(snapshot):
    """The bounds of every node showing a privately typed value (the gateway writes PRIVATE_MARK in its
    place), and the screen's size as the bounds tell it: ([x1, y1, x2, y2]...), width, height."""
    boxes, width, height = [], 0, 0

    def walk(node):
        nonlocal width, height
        b = node.get("b")
        if isinstance(b, list) and len(b) == 4:
            width, height = max(width, b[2]), max(height, b[3])
            if any(PRIVATE_MARK in str(node.get(k) or "") for k in ("text", "desc", "value")):
                boxes.append(b)
        for child in node.get("c", []):
            walk(child)

    for window in (snapshot or {}).get("windows", []):
        for node in window.get("nodes", []):
            walk(node)
    return boxes, width, height


def masked(jpeg, boxes, width, height, pad=8):
    """The screenshot with `boxes` (screen coordinates) blacked out, a little beyond their edges."""
    image = Image.open(io.BytesIO(jpeg)).convert("RGB")
    scale = image.width / width
    draw = ImageDraw.Draw(image)
    for x1, y1, x2, y2 in boxes:
        draw.rectangle([x1 * scale - pad, y1 * scale - pad, x2 * scale + pad, y2 * scale + pad], fill=(0, 0, 0))
    out = io.BytesIO()
    image.save(out, "JPEG", quality=85)
    return out.getvalue()


def call_line(call):
    """A tool call in a few words, for the trace: interact(element=4, interaction=tap)."""
    args = ", ".join(f"{k}={v}" for k, v in (call.get("arguments") or {}).items())
    return f"{call.get('name')}({args})"[:200]


KEY_WORDS = {"back": "Going back", "home": "Going to the home screen", "recents": "Opening the recent apps",
             "enter": "Pressing Enter", "hide_keyboard": "Hiding the keyboard"}
SETTING_WORDS = {"wifi": "Wi-Fi", "mobile_data": "mobile data", "airplane": "airplane mode", "bluetooth": "Bluetooth",
                 "location": "location", "dark_mode": "dark mode", "rotation": "screen rotation",
                 "brightness": "brightness", "dnd": "Do Not Disturb", "keep_awake": "keep awake"}
TAP_WORDS = {"tap": "Tapping", "long_press": "Pressing and holding", "focus": "Selecting", "submit": "Submitting",
             "type": "Typing into", "choose": "Choosing from", "set_value": "Adjusting"}


def short_link(uri, length=60):
    """A link without its scheme, `www.` or tracking tail, short enough for one line."""
    uri = str(uri or "").strip()
    parts = urlparse(uri)
    shown = (parts.netloc.removeprefix("www.") + parts.path.rstrip("/")) if parts.netloc else uri
    if parts.query and parts.netloc and len(shown) < length - 8:
        shown += "?" + parts.query
    return shown if len(shown) <= length else shown[:length - 1] + "…"


def element_label(menu, n):
    """What element n is called on the screen: the text after its number in any of its menu entries."""
    for description, _ in (menu or {}).values():
        _, found, label = description.partition(f"[{n}]")
        if found:
            label = label.replace("(replaces what the field holds)", "").replace("without typing", "")
            return label.strip()
    return ""


def describe_call(call, menu=None):
    """A tool call in plain words for the person watching, not the model: Opening a link: google.com/search."""
    name, args = call.get("name") or "", call.get("arguments") or {}
    quoted = lambda text: f"“{str(text).strip()[:60]}”"
    if name == "interact":
        n, how = args.get("element"), str(args.get("interaction") or "")
        label = element_label(menu, n)
        target = label or f"item {n}"
        if how.startswith("scroll_"):
            where = how.removeprefix("scroll_")
            return f"Scrolling {where} in {target}" if where in ("up", "down", "left", "right") else f"Scrolling to the {where} of {target}"
        if how == "type" and args.get("value"):
            return f"Typing {quoted(args['value'])} into {target}"
        if how == "choose" and args.get("value"):
            return f"Choosing {quoted(args['value'])} in {target}"
        return f"{TAP_WORDS.get(how, 'Using')} {target}"
    if name == "type_and_submit":
        return f"Typing {quoted(args.get('text'))} and submitting it"
    if name == "clip":
        return f"Keeping a picture: {quoted(args.get('caption') or 'the screen')}"
    if name == "press_key":
        return KEY_WORDS.get(args.get("key"), "Pressing a key")
    if name == "swipe":
        return f"Swiping {args.get('direction')}"
    if name == "drag":
        return "Dragging a finger"
    if name == "open_app":
        return f"Opening {args.get('app')}" + (f": {str(args['shortcut']).strip()[:40]}" if args.get("shortcut") else "")
    if name == "list_apps":
        return f"Looking for the {args.get('query')} app"
    if name == "list_data":
        return "Reading the whole list"
    if name == "read_text":
        return "Reading the whole text"
    if name == "network":
        about = args.get("contains") or args.get("url")
        return f"Reading what the app loaded about {quoted(about)}" if about else "Reading what the app loaded"
    if name == "open_link":
        parts = urlparse(str(args.get("uri") or "").strip())
        if parts.netloc.removeprefix("www.").startswith("google.") and parts.path == "/search":
            if query := parse_qs(parts.query).get("q"):
                return f"Searching Google for {quoted(query[0])}"
        return f"Opening a link: {short_link(args.get('uri'))}"
    if name == "wait_for":
        if args.get("text"):
            return f"Waiting for {quoted(args['text'])} to show up"
        if args.get("gone"):
            return f"Waiting for {quoted(args['gone'])} to go away"
        return "Waiting for the screen to settle"
    if name == "set_setting":
        what, value = SETTING_WORDS.get(args.get("name"), args.get("name")), str(args.get("value"))
        if value in ("on", "off"):
            return f"Turning {what} {value}"
        return f"Setting {what} to {value}"
    if name == "set_location":
        return "Setting the phone’s location"
    if name == "grant":
        return f"Allowing permissions for {args.get('package')}"
    if name == "request_screenshot":
        return "Taking a closer look at the screen"
    if name == "finish":
        return "Wrapping up"
    if name == "note":
        return "Making a note"
    if name == "log":
        return "Keeping track"
    if name == "progress":
        return "Updating progress"
    if name == "read_page":
        return "Reading the whole page"
    if name == "app_action":
        return {"open_chat": f"Opening the chat {quoted(args.get('text'))}" if args.get("text") else "Opening a chat",
                "older": "Reading earlier messages", "read": "Reading chats",
                "list": "Looking at the list"}.get(args.get("action"), "Using the app")
    if name == "checkpoint":
        return "Summing up so far"
    return name.replace("_", " ").capitalize() if name else "Working"


# Calls that only keep the agent's own record: left out of a turn's description when the turn does more.
BOOKKEEPING = ("log", "note", "progress", "checkpoint", "clip")
CLIPS_PER_SESSION = 6  # pictures one step may keep for the user
CLIPS_PER_TURN = 2


def block(value):
    """A brief item as text: prose as it is, a list of lines as bullets, flat pairs as `key: value`."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return "\n".join(f"- {v}" for v in value) or "(none)"
    if isinstance(value, dict) and all(v is None or isinstance(v, (str, int, float, bool)) for v in value.values()):
        return "\n".join(f"{k}: {v}" for k, v in value.items()) or "(none)"
    return json.dumps(value, ensure_ascii=False, default=str)


def new_session():
    return {"started": time.time(), "turns": 0, "actions": [], "notes": [], "log": [], "progress": "", "last": "",
            "incident": None, "refused": 0, "open": True, "trail": [], "thought": "",
            "checkpoint": None, "since": 0, "checkpoint_turn": 0}


def tail(text, length):
    """The end of `text`, where a model's thinking settles on what to do."""
    text = " ".join(text.split())
    return text if len(text) <= length else "…" + text[-length:]


class Refused(Exception):
    """A call that cannot run: the agent is told why, and nothing reaches the phone."""


class Lane:
    """The harness's ops lane (`await ops.run(op)` -> result dict) over the device gateway."""

    def __init__(self, session):
        self.s = session

    async def run(self, op):
        s = self.s
        try:
            results = await s.devices.ops(s.slot, s.owner, [op])
        except ValueError as exc:  # the gateway refused the op itself
            return {"op": op.get("op"), "ok": False, "summary": safe_error(exc)}
        return results[0] if results else {"op": op.get("op"), "ok": False, "summary": "the device ran nothing"}


class PhoneAgent:
    """One session of `agent` on the step's phone. `brief(session)` gives the stable part of each turn's
    state; `give_up(text)` is the final tool the runtime ends with when the agent cannot (out of turns,
    going in circles), from the reason and what the session kept. With `handover` (an agent whose one tool
    has `as_report(reason)`), the agent first gets a last turn to hand over in its own words. An agent with
    `checkpoint` among its tools sums up its session now and then (compact). An agent with `log` keeps a log
    instead of notes. A session parked for the user (NeedUser) stays open on the step and continues where it was."""

    def __init__(self, runner, state, step, agent, brief, give_up, max_turns, fresh=True, handover=None,
                 max_seconds=None, listens=False):
        self.runner, self.state, self.step, self.agent = runner, state, step, agent
        self.listens = listens  # the user's messages sent while the run works reach this agent
        self.brief, self.give_up, self.max_turns, self.handover = brief, give_up, max_turns, handover
        self.max_seconds = max_seconds  # the session's time budget in seconds; None: turns only
        self.checkpoints = "checkpoint" in agent.classes
        self.checkpoint_due = False  # set when the agent starts repeating itself
        self.devices, self.slot, self.owner, self.node_id = runner.devices, state["slot"], state["id"], step["id"]
        self.driver = Driver(
            self.devices.device(self.slot, self.owner), act=self.journal_ui_action,
            on_outcome=lambda outcome: runner.store.event(state["id"], "ui_outcome", {"node_id": self.node_id, "outcome": outcome}),
        )
        # The user's private entries, typed where the agent writes {private:N}: read at the moment of typing.
        self.driver.private = lambda n: runner.store.private_value(state["chat_id"], n)
        self.lane = Lane(self)
        sessions = step.setdefault("sessions", {})
        if fresh or not sessions.get(agent.prompt, {}).get("open"):
            sessions[agent.prompt] = new_session()
        self.mem = sessions[agent.prompt]
        if not fresh and (parked := self.mem.pop("parked_at", None)):
            self.mem["started"] += time.time() - parked  # the wait for the user is not the session's time
        self.picture_wanted = False  # the agent asked for a screenshot with its next turn
        self.auto_shot = False  # the runtime attaches a screenshot to the next turn (a page with no text)
        self.stalled = None  # why the session ends: its checkpoints find nothing new
        self.blocked = set()  # hosts that showed a block page this session
        self.blank = {}  # url -> looks in a row that read no text
        self.blank_told = set()
        self.anr_waits = 0  # times this session the runtime waited out an app that was not responding
        self.time_warned = False
        self.image = None  # this turn's screenshot (jpeg), when the agent asked for one
        self.no_shot = ""  # why the screenshot the agent asked for was not taken
        self.seen = Counter()
        self.outcomes = defaultdict(list)  # what each repeated (screen, actions) did, for the warning
        self.repeated = []  # the outcomes of the last screen turn's actions on that screen, oldest first
        self.solo_last, self.solo_run = None, []  # the last tool call and its answer, and its unbroken repeats
        self.notices = []
        self.unavailable = ""
        self.summary = ""  # what the current turn does, for the trace
        self.packages = None  # installed apps, read when a brief needs them
        self.google = None  # Google account context, read when a brief needs it
        self.location = None  # where the phone is, as the brief says it, read once
        self.rehooked = {}  # package -> whether restarting it brought the bridge up
        self.signins = {}  # web host or package -> its "Saved sign-ins" line ("" for none), read once a session
        # A log (one line a turn) stands in for notes, the trail of screens and all but the last few raw actions:
        # each line says where it was written.
        self.logs = "log" in agent.classes
        # The web page last read whole (by read_page, or by the runtime as a page opens): its text is shown every
        # turn while the screen stays on that page (same URL, same process), so the agent never scrolls to read.
        self.page_text = ""
        self.page_url = ""
        self.page_shown = False  # whether this turn's state carried the page text
        self.page_images = {}  # the images numbered in that text ({3: node}) and the process drawing the page, for clip
        self.page_process = None
        # package -> its Highway (app/highways): App data read from the app's own database while it is in front.
        self.highways = {}

    # --- looking ------------------------------------------------------------------------------------

    def elapsed(self):
        return clock(self.seconds())

    def seconds(self):
        return time.time() - self.mem["started"]

    def time_left(self):
        """Seconds left of the time budget, or None without one."""
        return None if not self.max_seconds else max(0.0, self.max_seconds - self.seconds())

    async def look(self, after_action=False):
        """Settle, then read the whole display. A screen the bridge cannot read is a state, not an error."""
        await self.observe(after_action)
        await self.recover()
        await self.wait_out_not_responding()

    def not_responding(self):
        """The Wait button of Android's "<app> isn't responding" dialog when it is on screen, else None."""
        screen = self.driver.screen
        if not screen:
            return None
        return next((e for e in screen.elements.values()
                     if str(e.node.get("id") or "").endswith(ANR_WAIT_ID) and isinstance(e.node.get("b"), list)), None)

    async def wait_out_not_responding(self):
        """An app that is slow to answer (a busy phone on a busy computer) gets Android's "isn't responding"
        dialog, often after every other tap. Waiting is the answer nearly every time, and an agent spent a turn
        on each one (a whole budget, in apps that kept doing it: 2026-11-14). So the runtime taps Wait, gives the
        app a few seconds and reads the screen again, without a turn. If the app still is not answering after
        that, the dialog is left to the agent with a notice: closing the app and opening it again is its call."""
        for _ in range(ANR_WAITS):
            wait = self.not_responding()
            if not wait:
                return
            left, top, right, bottom = wait.node["b"]
            try:
                await self.driver.device.tap((left + right) // 2, (top + bottom) // 2)
            except (DeviceError, httpx.TransportError):
                return
            self.anr_waits += 1
            await asyncio.sleep(ANR_PAUSE_SECONDS)
            await self.observe(True)
        if self.not_responding():
            self.notices.append(ANR_NOTICE.format(n=ANR_WAITS))

    async def recover(self):
        """After any look: an app on screen without the bridge is restarted, and the screen read again."""
        if self.driver.unhooked and await self.rehook():
            await self.observe(True)

    async def observe(self, after_action):
        try:
            await self.driver.observe(after_action=after_action)
            self.unavailable = ""
        except (DeviceError, httpx.TransportError) as exc:
            self.driver.screen = None
            self.driver.unhooked = []
            self.unavailable = f"unavailable: {safe_error(exc)}"
            return
        self.mark()
        if self.driver.still_loading:
            self.notices.append(
                f"The screen was still loading {self.driver.waited:.0f} s after your action "
                f"({self.driver.still_loading}). wait_for what you expect once, or go another way; do not send "
                "the action again."
            )

    def watch_web(self):
        """Two things a web page can be that the agent should not fight: a wall (a block page, a bot check),
        reported once per host with the rule not to go round it; and a page whose text cannot be read (0 nodes)
        twice in a row, which gets a screenshot and a notice instead of another two hundred turns of reloads."""
        screen = self.driver.screen
        if not screen:
            return
        for title, url, nodes in web_states(screen):
            host = urlparse(url).hostname or url or "this page"
            if BLOCK_PAGE.search(title) and host not in self.blocked:
                self.blocked.add(host)
                self.notices.append(BLOCKED_NOTICE.format(host=host, title=clip_words(title, 60)))
                self.done(f'runtime: {host} is blocking this browser ("{clip_words(title, 60)}")')
            if nodes == 0:
                self.blank[url] = self.blank.get(url, 0) + 1
                if self.blank[url] >= 2 and url not in self.blank_told:
                    self.blank_told.add(url)
                    self.auto_shot = self.sees
                    picture = "A screenshot of it is attached to this turn. " if self.sees else ""
                    self.notices.append(BLANK_NOTICE.format(host=host, picture=picture))
            elif nodes is not None:
                self.blank.pop(url, None)

    def place(self):
        """Where the phone is, in a few words: a web page's site and title, else the app and its screen, with what
        is selected there (a tab, a date, a filter). The trail and the log both say it this way."""
        screen = self.driver.screen
        if not screen:
            return ""
        if pages := web_pages(screen):
            title, url = pages[0]
            host = (urlparse(url).hostname or "").removeprefix("www.")
            place = " ".join(p for p in (host, f'"{clip_words(title, 50)}"' if title else "") if p) or "a web page"
        else:
            activity = str(screen.header.get("activity") or "").rsplit(".", 1)[-1]
            place = " ".join(p for p in (screen.header.get("app"), activity) if p)
        if selected := selected_labels(screen):
            place += " · selected: " + ", ".join(clip_words(label, 40) for label in selected[:3])
        return place

    def log_line(self, text, place):
        """One line of the agent's log, stamped with the time and, when it changed, where the agent was."""
        mem = self.mem
        stamp = self.elapsed()
        if place and place != mem.get("log_place"):
            mem["log_place"] = place
            stamp += f" @ {place}"
        mem["log"] = [*mem.get("log", []), f"{stamp} | {text}"][-LOG_KEPT:]

    async def take_clips(self, calls, menu):
        """clip: cut what the agent named out of one screenshot of this Screen and keep it for the user's answer
        (runtime/pictures.py). Returns a result line per call. Never a picture that shows a private value: its
        box is blacked out, and when its place cannot be told, nothing is taken."""
        mem, screen = self.mem, self.driver.screen
        kept = mem.setdefault("clips", [])
        lines = []
        if len(kept) >= CLIPS_PER_SESSION:
            return [f"clip -> NOT RUN: {CLIPS_PER_SESSION} pictures are kept already; that is enough for one step."]
        if not screen:
            return ["clip -> NOT RUN: the screen is not readable now."]
        boxes, hidden = pictures.private_boxes(screen.snapshot)
        if hidden:
            return ["clip -> NOT RUN: a privately entered value is on screen."]
        place = self.place()
        shot = None  # (jpeg, fresh Screen, width, height) once a screenshot is needed: only for the whole screen,
        # and for an element the app cannot draw for us

        async def screenshot():
            nonlocal shot
            if shot is None:
                # The Screen was read after the last action; a list that was still settling has moved since. The
                # boxes must come from the same moment as the picture: read the screen again just before the shot,
                # and find the agent's elements in that reading by their stable keys.
                fresh = screen
                try:
                    fresh = render(await self.driver.device.snapshot_display())
                except (DeviceError, httpx.TransportError, KeyError, TypeError):
                    pass
                width, height = (fresh.snapshot.get("screen") or [0, 0])[:2]
                if not width:
                    _, width, height = private_boxes(fresh.snapshot)
                shot = (await self.devices.screenshot(self.slot, width=1080), fresh, width, height)
            return shot

        for call in calls:
            args = call.get("arguments") or {}
            try:
                tool = self.agent.classes["clip"].model_validate({**args, "tool": "clip"})
            except ValidationError as exc:
                lines.append(f"clip -> NOT RUN: {exc.errors()[0].get('msg', 'bad arguments')}")
                continue
            n, caption = tool.element, " ".join(tool.caption.split())
            cut, via = None, "screenshot"
            if k := tool.page_image:
                # An image of the page read_page read: the page hands over its own file (Chrome's cache, the
                # WebView's drawing); it may be below the screen, so there is no screenshot to fall back on.
                node = self.page_images.get(k)
                if node is None:
                    lines.append(f"clip -> NOT RUN: the Page text shown this turn has no (image {k}).")
                    continue
                kind = "picture"
                got = await self.capture_element(self.page_process, node)
                if not got:
                    lines.append(f"clip -> FAILED: the page did not hand over image {k}.")
                    continue
                cut, via = got[:2], got[2]
            elif n == 0:
                jpeg, _, width, height = await screenshot()
                if not jpeg:
                    lines.append("clip -> FAILED: the device returned no picture.")
                    continue
                kind = "screen"
                cut = pictures.clip(jpeg, [0, 0, width, height], (width, height), boxes, kind)
            elif n in screen.elements:
                el = screen.elements[n]
                node = (pictures.picture_node(el.node) if tool.picture else None) or el.node
                kind = "picture" if node is not el.node else "card"
                if pictures.shows_private(node):
                    lines.append(f"clip -> NOT RUN: [{n}] shows a privately entered value.")
                    continue
                # The app draws the element for us (the bridge's capture: bounds and pixels from the same instant,
                # nothing floating over it); the screenshot is the fallback for what it cannot draw.
                got = await self.capture_element(el.process, node)
                if got:
                    cut, via = got[:2], got[2]
                else:
                    jpeg, fresh, width, height = await screenshot()
                    if not jpeg:
                        lines.append("clip -> FAILED: the device returned no picture.")
                        continue
                    if fresh is not screen and (now := fresh.find(el.key)):
                        node = (pictures.picture_node(now.node) if tool.picture else None) or now.node
                    box = pictures.box_of(node)
                    cut = pictures.clip(jpeg, box, (width, height), boxes, kind) if box else None
            else:
                lines.append(f"clip -> NOT RUN: there is no element [{n}] on this screen.")
                continue
            if cut is None:
                lines.append(f"clip -> NOT RUN: [{n}] is too small to show anything.")
                continue
            data, (w, h) = cut
            image_id = self.runner.store.image(self.state["id"], self.node_id, data, kind="clip", caption=caption,
                                               place=place, size=(w, h))
            kept.append({"id": image_id, "caption": caption, "kind": kind, "w": w, "h": h, "place": place,
                         "element": n, "step": self.node_id})
            self.runner.emit(self.state, "clip", {"node_id": self.node_id, "image_id": image_id, "caption": caption, "kind": kind})
            what = (f"image {tool.page_image} of the page" if tool.page_image else "the whole screen" if n == 0
                    else f"the picture inside [{n}]" if kind == "picture"
                    else f"[{n}] as drawn" + (" (no picture of its own was found in it)" if tool.picture else ""))
            lines.append(f'clip -> kept image:{image_id} "{caption}": {what}, {w}×{h}, drawn by {via}')
        return lines

    async def capture_element(self, process, node):
        """One element's picture drawn by the app itself (the bridge's capture by handle; Chrome's over DevTools),
        as (jpeg, (w, h), where it came from), or None when the app cannot draw it and the screenshot must do."""
        h = node.get("h")
        if h is None:
            return None
        try:
            answer = await self.driver.device.call("capture", process=process, h=h, timeout=8)
        except (DeviceError, httpx.TransportError):
            return None
        if not answer.get("ok") or not answer.get("jpeg"):
            return None
        try:
            got = pictures.as_jpeg(base64.b64decode(answer["jpeg"]))
        except (ValueError, OSError):
            return None
        if not got:
            return None
        source = {"cache": "the page's own image file", "bitmap": "the app's own image"}.get(answer.get("source"), "the app's drawing of it")
        return got[0], got[1], source

    async def read_new_page(self):
        """A web page that has just opened is read whole without being asked, as read_page would: the turn that
        opens a page is the turn that reads it, so the agent never spends the next one on read_page alone, and
        the text stays while it is on the page, so it never scrolls to read. Not while the page is still
        loading: the look after the next turn reads it. Returns the result line, or None when there was
        nothing new to read."""
        page = page_on_screen(self.driver.screen)
        if page is None or not page.url or page.url.startswith("about:") or page.loading:
            return None
        if page.url == self.page_url and page.process == self.page_process:
            return None
        return await self.read_page(auto=True)

    def forget_page(self):
        self.page_text, self.page_url, self.page_images = "", "", {}

    async def read_page(self, auto=False):
        """read_page: the whole web page on screen as reading text (runtime/reading.py), shown in the next turn's
        state and every turn after it while the screen stays on that page. Returns the line for the agent's
        Last result. `auto`: the runtime reads a page that has just opened (read_new_page)."""
        page = page_on_screen(self.driver.screen)
        label = "read_page (by the runtime: a new page opened)" if auto else "read_page"
        if page is None:
            line = "read_page -> NOT RUN: no web page is on screen; open it first (in Chrome, with open_link)."
            self.done(line)
            return line

        async def work():
            # One read per page: a page that cannot be read is not read again until the agent asks.
            self.page_url, self.page_process, self.page_text, self.page_images = page.url, page.process, "", {}
            answer = await self.driver.device.call("web_page", process=page.process, h=page.handle, timeout=15)
            if not answer.get("ok"):
                return False, f"FAILED: {answer.get('error') or 'the page could not be read'}", {}
            text, self.page_images = reading(answer.get("nodes") or [], settings.page_read_chars)
            if not text:
                return True, "the page has no readable text: look at the screen, or request_screenshot", {}
            title, url = answer.get("title") or page.title, answer.get("url") or page.url
            self.page_text = f"{title} {url}".strip() + "\n" + text
            caveats = [
                *(["it was still loading, so it may be incomplete: wait_for what you expect and read it again"]
                  if page.loading else []),
                *(["the bridge cut it at its node limit"] if answer.get("truncated") else []),
            ]
            return True, f"read the whole page ({len(text)} characters) into Page text" + "".join(f"; {c}" for c in caveats), {}

        _, line = await self.device_action("read_page", label, work)
        line = f"{label} -> {line}"
        self.done(line)
        return line

    def mark(self):
        """Put the screen on the trail when it is somewhere new: another app, screen, page or selection.
        Each turn sees only the screen in front of it; the trail is how it knows the way it came."""
        screen = self.driver.screen
        if not screen:
            return
        self.watch_web()
        place = self.place()
        trail = self.mem.setdefault("trail", [])
        if trail and trail[-1].split(" ", 1)[-1] == place:
            return
        trail.append(f"{self.elapsed()} {place or '(unknown screen)'}")
        del trail[:-TRAIL_KEPT]

    async def rehook(self):
        """Restart each app on screen that runs without the bridge, so its screen can be read. Each app is
        restarted once per session; after that the agent is told it cannot be read. Returns whether anything
        was restarted."""
        restarted = False
        for package in self.driver.unhooked:
            if package in self.rehooked:
                if self.rehooked[package] is not None:  # told once
                    self.rehooked[package] = None
                    self.notices.append(f"{package} still runs without the bridge, so its screen cannot be read or "
                                        "acted on. Do not guess at it: use another way, or report it as failed.")
                continue
            restarted = True
            await self.lane.run({"op": "stop", "package": package})
            await self.lane.run({"op": "launch", "package": package})
            deadline = time.monotonic() + REHOOK_WAIT
            while not (hooked := await self.hooked(package)) and time.monotonic() < deadline:
                await asyncio.sleep(0.5)
            self.rehooked[package] = hooked
            if not hooked and not await self.any_bridge():
                raise PhoneSetupError(
                    "The penrose bridge is not running on your phone, so I can't read any app on it. "
                    "Restart penrose to update the phone; if that doesn't fix it, reset the phone in Settings.")
            line = (f"runtime: {package} was running without the bridge, so its screen could not be read; it was "
                    "restarted" + (" and can be read now. It starts again from its first screen." if hooked
                                   else ", but the bridge still did not load."))
            self.done(line)
            self.notices.append(line.removeprefix("runtime: "))
            self.runner.emit(self.state, "node_progress", {"node_id": self.node_id, "description": line[:300]})
        return restarted

    async def hooked(self, package):
        try:
            bridges = await self.driver.device.bridges()
        except (DeviceError, httpx.TransportError):
            return False
        return any(p == package or p.startswith(package + ":") for p in bridges)

    async def any_bridge(self):
        """Whether any process on the phone runs the bridge. None at all is a phone that was never set up
        for it (or predates it), not an app that needs a restart. Unknown counts as yes."""
        try:
            return bool(await self.driver.device.bridges())
        except (DeviceError, httpx.TransportError):
            return True

    def private_place(self):
        """The site (a web page's host) or app on screen: where a private value typed now may stay visible."""
        screen = self.driver.screen
        if not screen:
            return ""
        url = next((u for _, u in web_pages(screen) if u.startswith(("https://", "http://"))), "")
        return (urlparse(url).hostname or "") if url else str(screen.header.get("app") or "")

    def typed_private(self):
        """A private value is being typed here: from now on this site or app is photographed only with the value
        found and blacked out (snap), since a page may show it again where the phone cannot read it."""
        places = self.state.setdefault("private_typed_on", [])
        if (where := self.private_place()) and where not in places:
            places.append(where)

    async def saved_signins(self):
        """The accounts Google Password Manager has a passkey or password for on the page or app on screen, as
        one line ("" when none, or the phone cannot say). Names only: the passwords stay in Chrome and Google."""
        self.driver.signin_offer = False
        screen = self.driver.screen
        if not screen:
            return ""
        url = next((u for _, u in web_pages(screen) if u.startswith(("https://", "http://"))), "")
        package = "" if url else str(screen.header.get("app") or "")
        key = (urlparse(url).hostname or "") if url else package
        if not key:
            return ""
        if key not in self.signins:
            found = await self.devices.saved_signins(self.slot, url, package)
            self.signins[key] = signins_line(found) if found else ""
        # A tap on this screen's fields waits for Google's offer (Driver.await_signin_offer).
        self.driver.signin_offer = bool(self.signins[key])
        return self.signins[key]

    @property
    def screen_text(self):
        return self.driver.screen.text if self.driver.screen else self.unavailable

    @property
    def sees(self):
        return self.agent.vision and settings.subagent_vision

    async def snap(self):
        """This turn's screenshot, only when the agent asked for one (request_screenshot), as in the harness.
        Never one that shows a private value: the fields holding one are blacked out, and when the value's
        place cannot be told, no picture is taken."""
        asked, self.image, self.no_shot = self.picture_wanted or self.auto_shot, None, ""
        self.picture_wanted = self.auto_shot = False
        if not asked:
            return
        if not self.sees:
            self.no_shot = "not taken: this agent is not sent pictures. Work from the screen text."
            return
        private = PRIVATE_MARK in self.screen_text
        boxes, width, height = private_boxes(self.driver.screen.snapshot) if private and self.driver.screen else ([], 0, 0)
        typed_here = self.private_place() in self.state.get("private_typed_on", [])
        if private and not (boxes and width and height):
            self.no_shot = "not taken: a privately entered value is on screen."
        elif typed_here and not boxes:
            # A card or code typed on this site may show again where the phone cannot read it (a payment overlay):
            # with nothing found to black out, no picture leaves the phone.
            self.no_shot = ("not taken: a private value was typed on this site or app and may be on screen where it "
                            "cannot be read. Work from the screen text; type_private still types into the focused field.")
        elif jpeg := await self.devices.screenshot(self.slot):
            self.image = masked(jpeg, boxes, width, height) if boxes else jpeg
        else:
            self.no_shot = ("not taken: the phone gave no picture in time (it is busy, often an app that is not "
                            "responding). Asking again at once rarely helps: work from the screen text, or wait_for "
                            "what you expect first.")

    async def state_text(self, n, final, menu, closing=""):
        """The turn's state as the agent reads it: the brief first (stable), then what changed, the screen last.
        A closing turn ("handover", "checkpoint") offers nothing to act on, so it has no Interactions; a
        checkpoint leaves the notices and events for the turn after it."""
        mem = self.mem
        parts = [f"{key}:\n{block(value)}" for key, value in (await self.brief(self)).items()]
        if checkpoint := mem.get("checkpoint"):
            parts.append(checkpoint_text(checkpoint))
        if mem.get("progress"):
            parts.append("Your progress:\n" + mem["progress"])
        if self.logs:
            parts.append(("Your log since the checkpoint" if checkpoint else "Your log")
                         + ", oldest first (time @ where you were | what you wrote):\n"
                         + ("\n".join(f"- {line}" for line in mem.get("log", [])) or "(nothing yet)"))
        else:
            if mem["notes"]:
                parts.append(("Your notes since the checkpoint:\n" if checkpoint else "Your notes:\n")
                             + "\n".join(f"- {note}" for note in mem["notes"]))
            if mem.get("trail"):
                parts.append("Screens so far, oldest first:\n" + "\n".join(f"- {place}" for place in mem["trail"]))
        if mem.get("clips"):
            parts.append("Pictures kept for the user (clip), oldest first:\n" + "\n".join(
                f"- image:{c['id']} \"{c['caption']}\" ({c['kind']}, {c['w']}×{c['h']}) @ {c['place']}" for c in mem["clips"]))
        if mem.get("thought"):
            parts.append("Your thinking last turn (its end):\n" + mem["thought"])
        since = mem.get("since", 0)
        shown = mem["actions"][since:][-(settings.phone_log_actions if self.logs else settings.phone_recent_actions):]
        hidden = len(mem["actions"]) - len(shown)
        recent = "\n".join(f"{i}. {line}" for i, line in enumerate(shown, hidden + 1)) or "(none)"
        covered = " and ".join(p for p in ("your checkpoint" if since else "", "your log" if self.logs else "") if p)
        earlier = "" if not hidden else f" ({hidden} earlier, summed up in {covered})" if covered else f" ({hidden} earlier not shown)"
        parts.append(("Recent actions since the checkpoint, oldest first" if since else "Recent actions, oldest first")
                     + earlier + f":\n{recent}")
        parts.append(f"Last result: {mem['last'] or '(no action yet)'}")
        if incident := mem["incident"]:
            times = incident["consecutive_failures"]
            parts.append(f"Incident ({times} in a row): {incident['action']}: {incident['reason']}")
        keep = closing == "checkpoint"
        notices = [*(self.step.get("notices", []) if keep else self.step.pop("notices", [])), *self.notices]
        if final:
            notices.append("This is your final turn: only your final tool is accepted.")
        left = self.time_left()
        if left is not None and not closing and not self.time_warned and left <= max(60, 0.25 * self.max_seconds):
            self.time_warned = True
            notices.append(TIME_NOTICE.format(left=f"{int(left) // 60} minutes" if left >= 120 else f"{int(left)} seconds"))
        if keep:
            notices.append(CHECKPOINT_NOTICE)
        if notices:
            parts.append("Notices:\n" + "\n".join(f"- {notice}" for notice in notices))
        if not keep:
            self.notices = []
        budget = f" of {clock(self.max_seconds)[2:]}" if self.max_seconds else ""
        # The clock is the budget; the turn ceiling only stops a runaway session, so it is not shown.
        parts.append(f"Turn: {closing or n}. Clock: {self.elapsed()}{budget}.")
        if not keep:  # a checkpoint leaves the events to the turn that acts on them
            events = self.driver.take_events()
            parts.append("Events:\n" + ("\n".join(f"- {e}" for e in events) or "(none)"))
        if not closing and (signins := await self.saved_signins()):
            parts.append(f"Saved sign-ins: {signins}")
        self.page_shown = False
        if self.page_text and not closing:
            page = page_on_screen(self.driver.screen)
            if page and page.url == self.page_url and page.process == self.page_process:
                self.page_shown = True
                parts.append("Page text (the whole page, also what is below the screen; shown every turn while you "
                             "stay on this page, so never scroll it to read it):\n" + self.page_text)
            else:
                self.forget_page()
        if not closing and (data := await highways.block(self)):
            parts.append(data)
        parts.append(f"Screen:\n{self.screen_text}")
        if not closing:
            parts.append("Interactions:\n" + (interactions_text(menu) or "(no numbered element accepts anything)"))
        if self.image:
            parts.append("Screenshot: attached, the screen now (you asked for it). It shows what the text cannot; "
                         "act through the numbered elements, keys and swipes as always. Asking again before you act "
                         "shows the same screen.")
        elif self.no_shot:
            parts.append(f"Screenshot: {self.no_shot}")
        return "\n\n".join(parts)

    # --- reading the turn -----------------------------------------------------------------------------

    def parse(self, call, menu):
        """One tool call as something to run: ("final" | "solo", tool), ("screen", (description, action,
        element)) or ("op", (label, run)). Raises Refused with the reason it cannot run."""
        name, args = call.get("name"), call.get("arguments") or {}
        if call.get("invalid_json") is not None:
            raise Refused(f"the arguments of {name} were not valid JSON")
        if cls := self.agent.classes.get(name):
            try:
                tool = cls.model_validate({**args, "tool": name})
            except ValidationError as exc:
                # A report that says too much is still the report: cut to fit rather than cost a turn.
                tool = fitted(cls, {**args, "tool": name}) if issubclass(cls, FinalTool) else None
                if tool is None:
                    problem = exc.errors()[0]
                    where = ".".join(map(str, problem.get("loc", ()))) or name
                    raise Refused(f"{name}: {where}: {problem.get('msg')}") from None
            if isinstance(tool, FinalTool):
                return "final", tool
            if isinstance(tool, TurnTool):
                return "solo", tool
            raise Refused(f"{name} cannot be used here")
        if name not in self.agent.operator:
            names = [f["function"]["name"] for f in self.agent.functions]
            raise Refused(f"unknown tool {name!r}; use one of: {', '.join(names)}")
        if name == "interact" and self.agent.interactions and args.get("interaction") not in self.agent.interactions:
            raise Refused(f"you only look: interact takes {', '.join(self.agent.interactions)} here")
        if name == "open_app" and args.get("shortcut") and read_only(self.agent):
            # A shortcut is not always a place: Clock's "Start" starts the stopwatch, a mail app's opens a draft.
            raise Refused("a check opens the app and looks: an app's shortcut can start or create something. Call "
                          "open_app without `shortcut` and go there through the app's screens")
        planned, why = resolve(call, menu)
        if planned is None:
            raise Refused(why)
        if planned["kind"] == "op":
            return "op", (planned["label"], planned["run"])
        steps = []
        for key in planned["keys"]:
            description, action = menu[key]
            self.check_private(action)
            element = self.driver.screen.elements.get(action["n"]) if self.driver.screen and "n" in action else None
            if action.get("cmd") in ("set_text", "insert_text") and element is not None and "picker" in element.flags:
                raise Refused(f"[{element.n}] is filled by the picker a tap on it opens (a date, a time, a list): text "
                              "typed into it shows, but the app keeps its own value. Tap it and choose in the picker it "
                              "opens: tap the day or time there (a month's days are listed as items, e.g. \"14 March "
                              "2026\"), or set_value on a picker that offers it (YYYY-MM-DD, HH:mm); then confirm (OK)")
            # Typing and the keyboard's action key: in a search box they search; anywhere else they change or send
            # what the field holds (a chat composer's action key sends the draft).
            if read_only(self.agent) and action.get("cmd") in ("set_text", "insert_text", "ime_action") \
                    and not (element and searchable(element)):
                raise Refused(f"{description}: a check types and submits only in a search box, never in a field that "
                              "holds data or a message")
            if read_only(self.agent) and action.get("key") == "enter" and not any(
                    "focused" in e.flags and searchable(e) for e in (self.driver.screen.elements.values() if self.driver.screen else ())):
                raise Refused(f"{description}: a check presses Enter only in a focused search box")
            if read_only(self.agent) and element is not None and (word := commits(element)):
                raise Refused(f"{description}: a check only looks, and \"{word}\" would change something. If the claim "
                              "cannot be confirmed without it, say so in your verdict")
            steps.append((description, action, element))
        return "screen", steps

    def check_private(self, action):
        """A text with {private:N} types the user's private entry N: it must be the whole text, and the entry must exist.
        The site or app it is typed on is remembered (typed_private)."""
        text = action.get("text")
        if not isinstance(text, str) or not PRIVATE_REF.search(text):
            return
        ref = PRIVATE_REF.fullmatch(text.strip())
        if not ref or action.get("cmd") != "set_text":
            raise Refused("{private:N} must be the whole value of a type (type it alone, into its own field)")
        if self.runner.store.private_value(self.state["chat_id"], int(ref[1])) is None:
            have = ", ".join(f"#{e['n']} {e['label']}" for e in self.runner.store.private_labels(self.state["chat_id"]))
            raise Refused(f"there is no private entry #{ref[1]}" + (f"; the user gave: {have}" if have else
                          "; the user has given none: report needs_input saying what is needed"))
        self.typed_private()

    def plan(self, calls, menu):
        """The turn's calls as what to run: (kind, items, notes, wait): notes say which calls were left out; `wait`
        is the wait_for that closes a turn of screen calls (the tap and what it should bring up), as (label, run).
        Screen calls run in order, at most MAX_BURST; any other call must be the only one of its turn. A call
        that cannot run stops the ones after it; when it is the first, nothing runs and Refused is raised."""
        kind, items, notes, wait = None, [], [], None
        for position, call in enumerate(calls):
            try:
                what, item = self.parse(call, menu)
            except Refused as refused:
                if not items:
                    raise
                notes.append(f"call {position + 1} ({call.get('name')}) not run: {refused}")
                break
            if what == "final" and len(calls) > 1:
                raise Refused(f"{call.get('name')} must be the only call of its turn; nothing was run")
            if what == "op" and items and call.get("name") == "wait_for":
                wait = item
                if position < len(calls) - 1:
                    notes.append("the calls after wait_for were not run: a wait closes its turn")
                break
            if what != "screen":
                if items:
                    notes.append(f"{call.get('name')} must be the only call of its turn, so it was not run")
                    break
                if len(calls) > 1:
                    notes.append(f"the other calls were not run: {call.get('name')} must be the only call of its turn")
                return what, item, notes, None
            kind = "screen"
            steps = item if isinstance(item, list) else [item]
            for i, step in enumerate(steps):
                if len(items) == MAX_BURST:
                    # Named, so the agent sends exactly those next: told only "the first 4 ran", it sent the same
                    # first four digits of a six-digit timer turn after turn and decided the keypad took four (2026-11-15).
                    dropped = [s[0] for s in steps[i:]] + [f"call {position + 2 + j} ({c.get('name')})"
                                                          for j, c in enumerate(calls[position + 1:])]
                    notes.append(f"only the first {MAX_BURST} screen actions of a turn are run; NOT RUN, send these "
                                 f"next turn: {'; '.join(dropped)}")
                    return kind, items, notes, None
                items.append(step)
        return kind, items, notes, wait

    # --- acting -------------------------------------------------------------------------------------

    def incident(self, n, category, reason, about):
        """Open or extend the incident the next turn opens with; it closes when a screen action works."""
        prior = self.mem["incident"] or {}
        self.mem["incident"] = {
            "turn": n, "category": category, "reason": reason, "action": about,
            "consecutive_failures": prior.get("consecutive_failures", 0) + 1,
        }

    def done(self, line):
        """An action that ran, for Recent actions."""
        self.mem["actions"].append(f"{self.elapsed()} {line}"[:400])

    async def screen_turn(self, n, steps, results, wait=None):
        """The turn's screen actions in order, all decided on the same screen; only the last one looks again,
        or the `wait` (a wait_for as (label, run)) that closes the turn does, once what the actions started has
        come. The first that fails stops the rest. Returns how many times these actions have now been done on
        this screen."""
        runner, state, node_id = self.runner, self.state, self.node_id
        snap = self.driver.screen
        before = fingerprint(snap) if snap else None
        runner.emit(state, "ui_started", {"node_id": node_id, "goal": self.summary[:300], "inputs": 0})
        worked, looked = True, False
        for i, (description, action, element) in enumerate(steps):
            try:
                looked = i == len(steps) - 1 and wait is None
                result = await self.driver.execute(description, action, target=element, look=looked)
            except (DeviceError, httpx.TransportError) as exc:
                result = f"{description} -> FAILED: {safe_error(exc)} It may or may not have happened: look at the screen."
                self.incident(n, "exec_error", safe_error(exc), description)
                await self.look(after_action=True)
                worked, looked = False, True
            results.append(result)
            self.done(result)
            if worked and "FAILED" in result:
                self.incident(n, "exec_error", result, description)
                worked = False
            if not worked:
                break
        else:
            self.mem["incident"] = None
        if wait is not None and worked:
            results.append(await self.op_turn(n, *wait))  # reads the screen as it waits, then looks
        elif not looked:
            await self.look(after_action=True)
        await self.recover()  # the driver looked after the last action
        self.mark()
        signature = (before, tuple(description for description, _, _ in steps))
        self.seen[signature] += 1
        self.outcomes[signature].append(clip_words(results[-1], 160) if results else "(nothing ran)")
        self.repeated = self.outcomes[signature]
        runner.emit(state, "ui_result", {
            "node_id": node_id, "status": "completed" if worked else "error", "goal": self.summary[:300],
            "reason": "; ".join(results)[:400], "actions": len(steps),
        })
        return self.seen[signature]

    async def op_turn(self, n, label, run):
        """A device-level call of the harness (open_app, wait_for, set_setting...): one journaled command."""
        met = False

        async def work():
            nonlocal met
            result = await run(self.lane)
            met = bool(result.get("met"))
            text = result.get("summary") or label
            if result.get("notifications"):
                text += " | " + "; ".join(
                    f"{x.get('label')}: {x.get('title') or ''} {x.get('text') or ''}".strip() for x in result["notifications"][:8]
                )[:700]
            ok = bool(result.get("ok", True))
            return ok, f"{label} -> {text}" if ok else f"{label} -> FAILED: {text}", {}

        ok, line = await self.device_action(label.split("(")[0], label, work)
        if not line.startswith(label):  # the gateway did not answer
            line = f"{label} -> {line}"
        if ok:
            self.mem["incident"] = None
        else:
            self.incident(n, "exec_error", line, label)
        self.done(line)
        # A wait that saw what it waited for has just read a settled screen: look at once.
        await self.look(after_action=not met)
        return line

    def circling(self, repeats, where=None):
        """A reason to end the session, or None; otherwise queue the warning the next turn opens with, with
        what each of the repeats did, so the agent sees that they all came to the same. `where` says what made
        them the same, for a call that is not a screen action."""
        if repeats >= REPEAT_STOP:
            return f"The runtime ended the session: the same actions were done {where or 'on the same screen'} {repeats} times."
        if repeats >= REPEAT_WARN:
            self.checkpoint_due = repeats == REPEAT_WARN  # step back once: what was found, what did not work
            each = "; ".join(f"({i}) {outcome}" for i, outcome in enumerate(self.repeated, 1))
            self.notices.append(
                f"You have now done these same actions {where or 'on this same screen'} {repeats} times. Each time: {each}. "
                "Doing it again will come to the same: read the screen again and choose a different route, and "
                "`note` what you have found so far. The runtime ends your session if you repeat it "
                f"{REPEAT_STOP - repeats} more time{'s' if REPEAT_STOP - repeats > 1 else ''}."
            )
        return None

    async def journal_ui_action(self, kind, description, perform):
        """Journal one screen action around `perform`. One whose outcome is unknown is marked uncertain,
        so it is never replayed blindly."""
        self.runner.cancelled(self.state)
        record = {"summary": description, "action": kind, "status": "started"}
        self.step["turns"].append(record)
        identity = {"node_id": self.node_id, "kind": kind, "description": description}
        self.runner.emit(self.state, "action_started", identity)
        try:
            how = await perform()
        except ValueError as exc:
            record["status"] = "rejected"
            self.runner.emit(self.state, "action_rejected", {**identity, "error": safe_error(exc)})
            raise
        except (DeviceError, httpx.TransportError, OSError) as exc:
            record["status"] = "uncertain"
            self.runner.emit(self.state, "action_uncertain", {**identity, "error": safe_error(exc)})
            raise
        record["status"] = "completed"
        record["result"] = how
        self.runner.emit(self.state, "action_completed", {**identity, "result": how})
        return how

    async def device_action(self, kind, command, work):
        """Journal one device command around `work()`, which returns (ok, line for the agent, extra). A failure
        is a result for the agent, not an error of the session. Returns (ok, line)."""
        self.runner.cancelled(self.state)
        record = {"summary": self.summary, "action": kind, "command": command, "status": "started"}
        self.step["turns"].append(record)
        identity = {"node_id": self.node_id, "kind": kind, "command": command}
        self.runner.emit(self.state, "action_started", {**identity, "description": self.summary})
        extra = {}
        try:
            ok, line, extra = await work()
        except (ValueError, DeviceError, httpx.TransportError) as exc:
            ok, line = False, f"FAILED: {safe_error(exc)}"
            if not isinstance(exc, ValueError):  # the gateway did not answer: it may have run
                record["status"] = "uncertain"
        if record["status"] == "started":
            record["status"] = "completed" if ok else "failed"
        record["result"] = {"ok": ok, "reason": line[:300]}
        self.runner.emit(self.state, "action_completed" if ok else "action_failed", {
            **identity, "description": self.summary, "ok": ok, "output": line, "stderr": extra.get("stderr", ""),
        })
        return ok, line

    # --- the session --------------------------------------------------------------------------------

    async def run(self):
        """The final tool the session ended with (the agent's, or `give_up`'s)."""
        parked = False
        try:
            return await self.loop()
        except NeedUser:
            parked = True  # waiting for the user: this session goes on when they answer
            self.mem["parked_at"] = time.time()
            raise
        finally:
            if not parked:
                self.mem["open"] = False

    async def loop(self):
        runner, state, mem = self.runner, self.state, self.mem
        await self.look()
        while True:
            runner.cancelled(state)
            if mem["turns"] >= self.max_turns:
                return await self.end(self.out_of_turns())
            if self.time_left() == 0:
                return await self.end(self.out_of_time())
            if self.wants_checkpoint():
                await self.compact()
                if self.stalled:
                    return await self.end(self.stalled)
            mem["turns"] += 1
            n = mem["turns"]
            self.listen()
            await self.snap()
            menu = options(self.driver.screen or NO_SCREEN, [])
            text = await self.state_text(n, n == self.max_turns, menu)
            content, calls, thought = await runner.act(state, self.agent, text, node_id=self.node_id,
                                                       images=[self.image] if self.image else None)
            mem["thought"] = tail(thought, THOUGHT_KEPT) if thought else ""
            end = await self.turn(n, content, calls, menu)
            if end is not None:
                return end

    def listen(self):
        """What the user wrote while the run worked reaches the agent before its next turn: in the brief from now
        on, and once as a notice. The orchestrator reads the same messages at its next decision."""
        if not self.listens:
            return
        for message in self.runner.hear(self.state):
            self.step.setdefault("user_messages", []).append(message)
            self.notices.append(USER_NOTICE.format(message=clip_words(message, 400)))
            self.step["turns"].append({"step": True, "summary": f"The user wrote: {clip_words(message, 200)}",
                                       "kind": "user_message", "at": time.time()})

    async def turn(self, n, content, calls, menu):
        """Run one turn's calls. Returns the final tool when the session ends."""
        runner, state, mem, step = self.runner, self.state, self.mem, self.step
        doing = [c for c in calls if c.get("name") not in BOOKKEEPING] or calls
        self.summary = content or "; ".join(describe_call(c, menu) for c in doing)
        record = {"step": True, "summary": self.summary[:300], "kind": "act", "at": time.time()}
        step["turns"].append(record)
        results, notes = [], []
        place = self.place()  # the screen this turn was decided on, which its log line is about
        logged = reading = False
        # Log lines, notes, progress, a checkpoint, read_page and a screenshot request may join any turn and cost
        # no phone action; the picture is taken and the page read after the turn's actions.
        rest, clips = [], []
        for call in calls:
            name = call.get("name")
            if name == "log" and self.logs:
                text = str((call.get("arguments") or {}).get("text", "")).strip()[:NOTE_LENGTH]
                if text and PRIVATE_MARK not in text:
                    self.log_line(" ".join(text.split()), place)
                    logged = True
            elif name == "read_page" and "read_page" in self.agent.classes:
                reading = True
            elif name == "checkpoint" and self.checkpoints:
                if (tool := own_tool(self.agent, "checkpoint", [call])) and self.apply_checkpoint(tool):
                    results.append("checkpoint kept")
            elif name == "note" and "note" in self.agent.classes:
                text = str((call.get("arguments") or {}).get("text", "")).strip()[:NOTE_LENGTH]
                if text and PRIVATE_MARK not in text:
                    mem["notes"] = [*mem["notes"], text][-NOTES_KEPT:]
                    results.append(f"noted: {text}")
            elif name in ("note", "log") and (self.logs or "note" in self.agent.classes):
                # The other agent's bookkeeping tool (a log where this one keeps notes, or the reverse): the same
                # intent, so it is kept as this agent's own rather than refused, which would cost the whole turn.
                text = str((call.get("arguments") or {}).get("text", "")).strip()[:NOTE_LENGTH]
                if text and PRIVATE_MARK not in text:
                    if self.logs:
                        self.log_line(" ".join(text.split()), place)
                        logged = True
                    else:
                        mem["notes"] = [*mem["notes"], text][-NOTES_KEPT:]
                    results.append(f"{name} -> kept as your {'log' if self.logs else 'note'} (you have no {name} tool)")
            elif name == "progress" and "progress" in self.agent.classes:
                text = str((call.get("arguments") or {}).get("text", "")).strip()[:PROGRESS_LENGTH]
                if text and PRIVATE_MARK not in text:
                    mem["progress"] = text
                    results.append("progress updated")
            elif name == "request_screenshot" and name in self.agent.operator:
                self.picture_wanted = True
            elif name == "clip" and "clip" in self.agent.classes:
                clips.append(call)
            else:
                rest.append(call)
        final = n == self.max_turns
        if self.picture_wanted and self.image and not rest and not reading:
            # Nothing would run between this turn's picture and the next one: it would show the same screen.
            self.picture_wanted = False
            return await self.refuse(n, calls, "the screenshot attached to this turn is the screen now and nothing "
                                     "has run since it was taken; act on it, or wait_for what you expect, before asking again", record)
        if clips:  # on this turn's Screen, before anything changes it
            results.extend(await self.take_clips(clips[:CLIPS_PER_TURN], menu))
        try:
            if not calls:
                raise Refused("the reply had no tool call; answer with tool calls")
            kind, items, notes, wait = self.plan(rest, menu) if rest else (None, [], [], None)
        except Refused as refused:
            return await self.refuse(n, rest or calls, str(refused), record)
        if final and kind != "final":
            return await self.end(self.out_of_turns())
        mem["refused"] = 0
        end = None
        if kind == "final":
            if refusal := await items.accept(self):
                results.append(f"NOT ACCEPTED: {refusal}")
            else:
                record.update(goal=items.tool, result={"status": "completed", "reason": self.summary[:300]})
                end = items
        elif kind == "solo":
            try:
                line = await items.run(self)
            except NeedUser as exc:
                record.update(goal=items.tool, result={"status": "needs_input", "reason": exc.question[:300]})
                self.done(f"{items.tool}: asked the user: {exc.question}")
                mem["last"] = f"{items.tool} -> asked the user: {exc.question}"
                raise
            results.append(f"{items.tool} -> {line}")
            self.done(results[-1])
            if line.startswith(("FAILED", "NOT")):
                self.incident(n, "exec_error", line, items.tool)
            else:
                mem["incident"] = None
            await self.look(after_action=True)
            # The same call with the same answer, turn after turn: a loop the screen check cannot see (one `cat` of
            # a long file ran twenty times, its output cut the same way each time, 2026-11-15). Only an unbroken
            # run counts: a force-stop or an `am start` done again later, between other actions, is no loop.
            signature = (items.model_dump_json(), line)
            self.solo_run = self.solo_run + [line] if signature == self.solo_last else [line]
            self.solo_last = signature
            self.repeated = [clip_words(x, 160) for x in self.solo_run]
            if reason := self.circling(len(self.solo_run), where="in a row and got the same answer"):
                record.update(goal=items.tool, result={"status": "failed", "reason": reason})
                mem["last"] = "; ".join(results)
                return await self.end(reason)
        if kind in ("screen", "op"):  # an action in between breaks the run; a note or a screenshot does not
            self.solo_last, self.solo_run = None, []
        if kind == "op":
            results.append(await self.op_turn(n, *items))
        elif kind == "screen":
            repeats = await self.screen_turn(n, items, results, wait=wait)
            if reason := self.circling(repeats):
                record.update(goal="; ".join(results)[:300], result={"status": "failed", "reason": reason})
                mem["last"] = "; ".join(results)
                return await self.end(reason)
        if reading and not end and kind != "final":
            results.append(await self.read_page())
        elif not end and kind in ("screen", "op", "solo") and "read_page" in self.agent.classes:
            if line := await self.read_new_page():
                results.append(line)
        if self.picture_wanted and not end:
            results.append("request_screenshot -> see Screenshot (taken after this turn's actions)")
        mem["last"] = "; ".join([*results, *notes]) or "(nothing ran)"
        if kind is None and not self.picture_wanted and calls and not any(c.get("name") == "checkpoint" for c in calls):
            if reading and self.page_shown:
                # read_page alone, on a page whose text the turn already showed: nothing new could come of it.
                self.notices.append("That turn only read the page again: a page is read for you as it opens and its text "
                                    "stays while you are on it. read_page is for a page that changed in place (more rows "
                                    "loaded, a search on the same page), and then it joins the turn that changes it.")
            elif not reading:
                # Only log, progress, clip or a note: a whole turn for what rides along with an action for free.
                free = ", ".join(t for t in ("log", "note", "progress", "clip") if t in self.agent.classes)
                self.notices.append(f"That turn ran nothing on the phone: {free} cost no turn when they share one with an "
                                    "action. Put them in the same reply as your next tap, type or key.")
        if self.logs and not logged and (kind in ("screen", "op", "solo") or reading):
            # The agent wrote no log line: the runtime keeps a short one, so the log still tells the whole session.
            self.log_line(f"(runtime) {clip_words(mem['last'], 160)}", place)
        failed = any("FAILED" in r or r.startswith("NOT") for r in results)
        record.update(
            goal=record.get("goal") or "; ".join(call_line(c) for c in calls)[:300],
            result=record.get("result") or {"status": "failed" if failed else "completed", "reason": mem["last"][:300]},
        )
        runner.emit(state, "node_progress", {"node_id": self.node_id, "description": (content or mem["last"])[:300]})
        return end

    async def refuse(self, n, calls, why, record):
        """None of the turn's calls can run: the agent is told why; three such turns in a row end the session."""
        mem = self.mem
        label = "; ".join(call_line(c) for c in calls) or "(no tool call)"
        mem["refused"] += 1
        mem["last"] = f"(refused {label}: {why.rstrip('.')}. Nothing was done.)"
        self.incident(n, "refused", why, label)
        self.runner.emit(self.state, "action_rejected", {"node_id": self.node_id, "kind": "turn", "description": why})
        record.update(goal=label[:300], result={"status": "failed", "reason": why[:300]})
        if mem["refused"] >= REFUSED_STOP:
            return await self.end(f"The runtime ended the session: {REFUSED_STOP} turns in a row had no call that "
                                  f"could run ({why.rstrip('.')}).")
        if n == self.max_turns:
            return await self.end(self.out_of_turns())
        return None

    def out_of_turns(self):
        return f"The session used all its {self.max_turns} turns."

    def out_of_time(self):
        return f"The session used its time budget of {clock(self.max_seconds)[2:]} minutes."

    # --- ending -------------------------------------------------------------------------------------

    async def end(self, reason):
        """The runtime ends the session. An agent with a handover first gets one turn to hand over in its own
        words; if it has none, or the handover does not come, what the session kept goes back with the reason."""
        if self.handover is not None:
            try:
                if final := await self.hand_over(reason):
                    return final
            except (ProviderError, DeviceError, httpx.HTTPError, ValueError):
                pass  # best effort: what the session kept still goes back
        return self.give_up(self.kept(reason))

    async def hand_over(self, reason):
        """The handover turn: the same state with the reason in Notices, and only the handover tool. Returns
        its report, or None when the reply has no valid handover."""
        self.notices.append(HANDOVER_NOTICE.format(reason=reason))
        text = await self.state_text(self.mem["turns"], False, None, closing="handover")
        _, calls, *_ = await self.runner.act(self.state, self.handover, text, node_id=self.node_id)
        record = {"step": True, "summary": "handover", "kind": "handover", "at": time.time()}
        self.step["turns"].append(record)
        name = "handover"
        tool = own_tool(self.handover, name, calls)
        if tool is None:
            record.update(goal=name, result={"status": "failed", "reason": "no valid handover in the reply"})
            return None
        record.update(goal=name, result={"status": "completed", "reason": tool.found[:300]})
        self.runner.emit(self.state, "node_progress", {"node_id": self.node_id, "description": f"handed over: {tool.found}"[:300]})
        return tool.as_report(reason)

    # --- checkpoints --------------------------------------------------------------------------------

    def wants_checkpoint(self):
        """A checkpoint is due every phone_checkpoint_turns turns, or right after a first repeat warning when the
        last one is CHECKPOINT_GAP turns back, when there are actions it has not summed up yet."""
        mem, every = self.mem, settings.phone_checkpoint_turns
        due, self.checkpoint_due = self.checkpoint_due, False
        if not self.checkpoints or len(mem["actions"]) <= mem.get("since", 0):
            return False
        since = mem["turns"] - mem.get("checkpoint_turn", 0)
        return (due and since >= CHECKPOINT_GAP) or bool(every and since >= every)

    async def compact(self):
        """The agent sums up its session (checkpoint, asked for in Notices); the summary replaces its log or notes,
        progress and the actions so far. The request is the agent's own, tools included, so the prompt cache
        covers it as it does any other turn. Best effort: without a valid checkpoint everything stays as it was,
        and it is tried again later."""
        mem = self.mem
        self.checkpoint_due = False
        mem["checkpoint_turn"] = mem["turns"]
        try:
            text = await self.state_text(mem["turns"], False, None, closing="checkpoint")
            _, calls, *_ = await self.runner.act(self.state, self.agent, text, node_id=self.node_id)
        except (ProviderError, DeviceError, httpx.HTTPError, ValueError):
            return
        if tool := own_tool(self.agent, "checkpoint", calls):
            self.apply_checkpoint(tool)

    def apply_checkpoint(self, tool):
        """The agent's checkpoint replaces its log or notes, progress and the actions so far. Returns whether it
        was kept: one that lost everything found is worse than none."""
        mem = self.mem
        found = [line.strip()[:NOTE_LENGTH] for line in tool.found if line.strip() and PRIVATE_MARK not in line]
        previous = mem.get("checkpoint") or {}
        if not found and (mem["notes"] or mem.get("log") or previous.get("found")):
            return False  # a summary that lost everything found is worse than none
        # Nothing new since the last checkpoint is a session going round: it hands over instead.
        mem["stalls"] = mem.get("stalls", 0) + 1 if previous and previous.get("found") == found else 0
        limit = settings.phone_stalled_checkpoints
        if limit and mem["stalls"] >= limit - 1:
            self.stalled = (f"The runtime ended the session: your last {limit} checkpoints found the same things, so "
                            f"the {mem['turns'] - previous.get('turn', 0)} turns since turn {previous.get('turn', 0)} "
                            "brought nothing new.")
        mem["checkpoint"] = {
            "turn": mem["turns"], "at": self.elapsed(), "found": found,
            "tried": [line.strip()[:NOTE_LENGTH] for line in tool.tried if line.strip()],
            "where": " ".join(tool.where.split()),
        }
        if tool.plan.strip() and PRIVATE_MARK not in tool.plan:
            mem["progress"] = tool.plan.strip()[:PROGRESS_LENGTH]
        mem["notes"], mem["log"] = [], []
        mem["log_place"] = None  # the next log line says where it is again
        mem["since"] = len(mem["actions"])
        mem["checkpoint_turn"] = mem["turns"]
        self.step["turns"].append({"step": True, "summary": "checkpoint", "kind": "checkpoint", "at": time.time(),
                                   "goal": "checkpoint", "result": {"status": "completed", "reason": "; ".join(found)[:300]}})
        self.runner.emit(self.state, "node_progress", {
            "node_id": self.node_id, "description": ("checkpoint: " + "; ".join(found or [mem["checkpoint"]["where"]]))[:300],
        })
        return True

    def kept(self, reason):
        """The reason with what the session kept (its log or notes, progress, the latest actions), findings first:
        what goes back when the agent does not hand over itself."""
        mem = self.mem
        own = mem.get("log", []) if self.logs else mem["notes"]
        notes = "; ".join([*((mem.get("checkpoint") or {}).get("found") or []), *own][-8:])
        recent = " | ".join(mem["actions"][-3:])
        progress = " ".join(mem.get("progress", "").split())
        return (
            f"{reason} "
            + (f"{'Its log' if self.logs else 'Its notes'}: {notes}. " if notes else "")
            + (f"Its progress: {progress}. " if progress else "")
            + f"Its latest actions: {recent or 'none'}"
        )[:ENDING_LENGTH]

def fitted(cls, args):
    """`args` validated as `cls` after cutting its strings and lists to size, or None."""
    try:
        return cls.model_validate(fit(cls, args))
    except ValidationError:
        return None


def web_states(screen):
    """(title, url, nodes) of each web page on the screen: `nodes` is how many DOM nodes its text came from
    (None when the page does not say)."""
    found = []

    def walk(node):
        if node.get("webview"):
            found.append((node.get("title") or "", node.get("url") or "", (node.get("page") or {}).get("nodes")))
        for child in node.get("c", []):
            walk(child)

    for w in screen.snapshot.get("windows", []):
        for node in w.get("nodes", []):
            walk(node)
    return found


def own_tool(agent, name, calls):
    """The agent's own tool `name` from a reply's calls (a handover, a checkpoint), validated, or None. Text or
    lists longer than a field allows are cut to fit rather than thrown away: a summary that says too much is
    still the summary."""
    cls = agent.classes.get(name)
    call = next((c for c in calls if c.get("name") == name and c.get("invalid_json") is None), None)
    if cls is None or call is None:
        return None
    args = {**(call.get("arguments") or {}), "tool": name}
    try:
        return cls.model_validate(args)
    except ValidationError:
        return fitted(cls, args)


def fit(cls, args):
    """`args` with each string and list cut to its field's max_length."""
    fitted = dict(args)
    for key, info in cls.model_fields.items():
        value, limit = fitted.get(key), next((m.max_length for m in info.metadata if hasattr(m, "max_length")), None)
        if isinstance(value, str) and limit:
            fitted[key] = value[:limit]
        elif isinstance(value, list):
            fitted[key] = value[:limit or None]
    return fitted


def signins_line(found):
    """{"site", "passkeys", "passwords"} as the agent reads it: "amazon.com (Google Password Manager): passkeys
    for a@b.com, c@d.com; passwords for a@b.com"; "" when nothing is saved."""
    parts = []
    for kind in ("passkeys", "passwords"):
        names = [str(name) for name in found.get(kind) or [] if name]
        if names:
            more = f" and {len(names) - SIGNINS_SHOWN} more" if len(names) > SIGNINS_SHOWN else ""
            parts.append(f"{kind} for {', '.join(names[:SIGNINS_SHOWN])}{more}")
    return f"{found.get('site') or 'this screen'} (Google Password Manager): {'; '.join(parts)}" if parts else ""


def checkpoint_text(checkpoint):
    """The agent's last checkpoint, as it reads it every turn after."""
    lines = [f"Your checkpoint (turn {checkpoint['turn']}, {checkpoint['at']}): your summary of the session up to then."]
    lines.append("Found:\n" + ("\n".join(f"- {line}" for line in checkpoint.get("found") or []) or "(nothing yet)"))
    if checkpoint.get("tried"):
        lines.append("Tried, did not work:\n" + "\n".join(f"- {line}" for line in checkpoint["tried"]))
    if checkpoint.get("where"):
        lines.append(f"Where you were: {checkpoint['where']}")
    return "\n".join(lines)
