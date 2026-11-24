"""The phone for a second brain on this computer (Claude Code, Cowork) through the `relay` command (app/cli.py).

The same hands the phone subagent has: the screen as numbered text, one checked action on an element, an app
or link opened, a wait, the notifications; each call is checked against what the screen offers
(bridge/tools.py) and done through the screen driver (device/driver.py), so the guardrails of the tool
layer hold here too. The phone is leased per CLI client (owner `cli:<client>`) and given back after a few
idle minutes or on `release`, with the settings and the location the session changed put back.

Reached with the integration token (auth.py) as a bearer; the `relay` command reads it, with the API's
address, from the discovery file this process writes at start (publish)."""

import asyncio
import base64
import contextlib
import json
import os

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import cli, skills
from ..bridge.screen import options
from ..bridge.tools import MAX_BURST, SETTINGS, interactions_text, resolve
from ..config import settings
from ..delivery import YIELDS_PHONE
from ..device.client import Devices
from ..device.driver import Driver
from ..errors import DeviceError, safe_error
from ..models import PRIVATE_MARK
from ..runtime.phone import masked, private_boxes
from . import auth, common

router = APIRouter(prefix="/api/phone")

IDLE_SECONDS = 300  # a client that sends nothing for this long gives the phone back
CLI_FILE = "cli.json"
# A setting the session changes, as the `state` op reports it (for putting it back on release).
STATE_KEYS = {"wifi": "wifi", "mobile_data": "mobile_data", "airplane": "airplane", "bluetooth": "bluetooth",
              "location": "location", "dark_mode": "dark_mode", "rotation": "rotation", "brightness": "brightness_percent",
              "dnd": "do_not_disturb"}

devices: Devices | None = None  # the gateway client, opened on first use (tests put a fake here)
sessions: dict = {}  # client -> Session
taken_back: set = set()  # clients whose phone the user took back from Relay's window, to be told once


def gateway():
    global devices
    if devices is None:
        devices = Devices()
    return devices


class Session:
    """One CLI client's hold on the phone: its lease, its screen driver (event cursors, the last screen) and
    what it changed on the phone."""

    def __init__(self, client):
        self.client = client
        self.owner = f"cli:{client}"
        self.slot = None
        self.driver = None
        self.originals = {}  # setting -> its value before this session changed it
        self.moved = False  # the session set the phone's location
        self.lock = asyncio.Lock()
        self.timer = None

    def touch(self):
        """Start the idle clock again."""
        if self.timer and self.timer is not asyncio.current_task():
            self.timer.cancel()
        self.timer = asyncio.create_task(self.idle())

    async def idle(self):
        await asyncio.sleep(IDLE_SECONDS)
        async with self.lock:
            await release(self, "idle")


class Lane:
    """The harness's ops lane (`await ops.run(op)` -> result dict) for one session."""

    def __init__(self, session):
        self.s = session

    async def run(self, op):
        try:
            results = await gateway().ops(self.s.slot, self.s.owner, [op])
        except ValueError as exc:  # the gateway refused the op itself
            return {"op": op.get("op"), "ok": False, "summary": safe_error(exc)}
        return results[0] if results else {"op": op.get("op"), "ok": False, "summary": "the device ran nothing"}


# --- the lease ------------------------------------------------------------------------------------------

def busy(owner):
    """Why the phone cannot be had, for the model to tell the user."""
    kind, _, rest = owner.partition(":")
    if kind == "cli":
        return (f"The phone is busy with another Claude session ({rest}). Ask the user which session should have it; "
                "`relay phone release` there gives it back.")
    if kind == "manual":
        return "The user is using the phone in penrose's window. Try again in a moment."
    rows = common.store.rows("SELECT c.title FROM runs r LEFT JOIN chats c ON c.id=r.chat_id WHERE r.id=?", (kind,))
    title = (rows[0]["title"] or "") if rows else ""
    return (f"The phone is busy with a penrose task{': ' + title if title else ''}. "
            "Ask the user to finish or cancel it in penrose, then try again.")


async def take_phone(session):
    """Lease the phone for this session, or say who has it (409) or that it is not up (503). A Relay run that
    only waits for the user, or ended without letting go, yields the phone, as it does to Relay's own runs
    (runtime/runner.py take_phone); another CLI session or the user's own taps never do."""
    if session.client in taken_back:
        taken_back.discard(session.client)
        raise HTTPException(409, "penrose took the phone back: the user wants it. Ask them before going on.")
    gw = gateway()
    slot = await gw.acquire(session.owner)
    holders = {}
    if slot is None:
        holders = await gw.holders()
        for owner in holders.values():
            run_id = owner.partition(":")[0]
            if run_id not in ("cli", "manual") and common.store.status(run_id) in YIELDS_PHONE:
                with contextlib.suppress(Exception):
                    await gw.release(owner)
                slot = await gw.acquire(session.owner)
                break
    if slot is None:
        if not holders:
            raise HTTPException(503, "The phone is starting or not connected. Check penrose's window.")
        raise HTTPException(409, busy(next(iter(holders.values()))))
    # A new slot, or one someone else used since (`kept` False): the screen and the event cursors start over.
    if session.driver is None or session.slot != slot or not getattr(slot, "kept", True):
        session.slot, session.driver = slot, Driver(gw.device(slot, session.owner))
    return session


async def held(client):
    """The client's session with the phone leased and its idle clock reset."""
    session = sessions.get(client) or Session(client)
    await take_phone(session)  # a client that could not have the phone holds no session
    sessions[client] = session
    session.touch()
    return session


async def release(session, why="released"):
    """Give the phone back: the settings and location the session changed are put back first."""
    sessions.pop(session.client, None)
    if session.timer and session.timer is not asyncio.current_task():
        session.timer.cancel()
    if session.slot:
        lane = Lane(session)
        for name, value in session.originals.items():
            with contextlib.suppress(Exception):
                await lane.run({"op": name, "value": value})
        if session.moved:  # the gateway sends the computer's position again
            with contextlib.suppress(Exception):
                await gateway().location(session.slot, refresh=True)
        with contextlib.suppress(Exception):
            await gateway().release(session.owner)
    return {"released": True, "why": why}


async def release_all(why):
    for session in list(sessions.values()):
        async with session.lock:
            await release(session, why)


async def forget_stale(patience=60):
    """Leases of CLI sessions nobody holds (this process makes them all; a previous one may have died with
    them), so no Relay run waits for a phone nobody is using. Started with the gateway, this API may ask before
    the gateway listens or while it still checks its phone: asked again each second until it lists one, for
    `patience` seconds, and a lease counts whether or not the phone is connected yet."""
    gw = gateway()
    deadline = asyncio.get_running_loop().time() + patience
    while True:
        phones = await gw.phones()
        for owner in {p.get("owner") for p in phones if p.get("owner")}:
            if owner.startswith("cli:") and owner.partition(":")[2] not in sessions:
                with contextlib.suppress(Exception):
                    await gw.release(owner)
        if phones or asyncio.get_running_loop().time() >= deadline:
            return
        await asyncio.sleep(1)


# --- looking and acting -----------------------------------------------------------------------------------

async def look(session, after_action=False):
    try:
        await session.driver.observe(after_action=after_action)
    except (DeviceError, httpx.TransportError) as exc:
        session.driver.screen = None
        raise HTTPException(503, f"The phone could not be read: {safe_error(exc)}") from None


def view(session, **extra):
    """What a command answers with: the results, then the events since the last look, the screen and what
    each numbered element accepts."""
    screen = session.driver.screen
    return {
        "ok": True, "results": [], "notes": [], **extra,
        "app": screen.header.get("app") if screen else None,
        "activity": screen.header.get("activity") if screen else None,
        "events": session.driver.take_events(),
        "screen": screen.text if screen else "(the screen could not be read)",
        "interactions": interactions_text(options(screen, [])) if screen else "",
    }


async def shot(session):
    """A JPEG of the screen (base64), never one that shows a privately typed value: those fields are blacked
    out, and when their place cannot be told, no picture is taken. Returns (base64 or None, why not)."""
    screen = session.driver.screen
    private = bool(screen) and PRIVATE_MARK in screen.text
    boxes, width, height = private_boxes(screen.snapshot) if private else ([], 0, 0)
    if private and not (boxes and width and height):
        return None, "not taken: a privately entered value is on screen."
    jpeg = await gateway().screenshot(session.slot)
    if not jpeg:
        return None, "not taken: the device returned none."
    return base64.b64encode(masked(jpeg, boxes, width, height) if boxes else jpeg).decode(), ""


def normal(value):
    if isinstance(value, bool):
        return "on" if value else "off"
    return value


async def remember(session, op):
    """What the phone was before this session changes it, so release can put it back."""
    name = op.get("op")
    if name in SETTINGS and name not in session.originals and name in STATE_KEYS:
        state = (await Lane(session).run({"op": "state"})).get("state") or {}
        value = normal(state.get(STATE_KEYS[name]))
        if value is not None and not (name == "rotation" and value not in ("auto", "portrait", "landscape")):
            session.originals[name] = str(value) if name == "brightness" else value
    elif name == "geo":
        session.moved = True


async def run_op(session, planned):
    """A device-level call (open_app, wait_for, set_setting...): (ok, line, result)."""
    label = planned["label"]
    await remember(session, planned["op"])
    result = await planned["run"](Lane(session))
    text = result.get("summary") or label
    ok = bool(result.get("ok", True))
    return ok, (f"{label} -> {text}" if ok else f"{label} -> FAILED: {text}"), result


# --- the API ------------------------------------------------------------------------------------------------

class ClientInput(BaseModel):
    client: str = Field(default="default", pattern=r"^[A-Za-z0-9_.-]{1,64}$")


class ScreenInput(ClientInput):
    shot: bool = False


class Call(BaseModel):
    name: str = Field(max_length=40)
    arguments: dict = Field(default_factory=dict)


class ActInput(ClientInput):
    calls: list[Call] = Field(min_length=1, max_length=MAX_BURST)


@router.get("/status")
async def status(client: str = "default"):
    """The phone, who holds it, the Google account and the location. Takes no lease."""
    gw = gateway()
    phones = await gw.phones()
    phone = next((p for p in phones if p.get("connected")), None) or (phones[0] if phones else None)
    owner = phone.get("owner") if phone else None
    mine = owner == f"cli:{client}"
    out = {"client": client, "phone": {
        "connected": bool(phone and phone.get("connected")), "holder": owner, "yours": mine,
        "busy": busy(owner) if owner and not mine else None,
    }, "google_account": None, "location": None}
    if out["phone"]["connected"]:
        accounts = await gw.accounts(phone["id"], owner)
        out["google_account"] = accounts[0] if accounts else None
        out["location"] = await gw.location(phone["id"])
    return out


@router.post("/screen")
async def screen(body: ScreenInput):
    session = await held(body.client)
    async with session.lock:
        await look(session)
        out = view(session)
        if body.shot:
            out["shot"], out["no_shot"] = await shot(session)
        return out


@router.post("/act")
async def act(body: ActInput):
    """Up to MAX_BURST screen calls (interact, type_and_submit, press_key, swipe), numbered from the last
    screen and run in order with one look at the end; or one device-level call. Then the new screen."""
    session = await held(body.client)
    async with session.lock:
        driver = session.driver
        if driver.screen is None:
            await look(session)
        menu = options(driver.screen, [])
        steps, notes, op = [], [], None
        for position, call in enumerate(body.calls):
            planned, why = resolve({"name": call.name, "arguments": call.arguments}, menu)
            if planned is not None and planned["kind"] in ("finish", "shot"):
                planned, why = None, ("finish is penrose's own tool: when the work is done, just stop and tell the user"
                                      if planned["kind"] == "finish" else "a picture comes from `relay phone screen --shot FILE`")
            if planned is None:
                if not steps:
                    raise HTTPException(400, why)
                notes.append(f"call {position + 1} ({call.name}) not run: {why}")
                break
            if planned["kind"] == "op":
                if steps:
                    notes.append(f"{call.name} must be the only call of its turn, so it was not run")
                    break
                op = planned
                if len(body.calls) > 1:
                    notes.append(f"the other calls were not run: {call.name} must be the only call of its turn")
                break
            for key in planned["keys"]:
                description, action = menu[key]
                element = driver.screen.elements.get(action["n"]) if "n" in action else None
                steps.append((description, action, element))
        ok, results, extra = True, [], {}
        if op:
            ok, line, result = await run_op(session, op)
            results.append(line)
            if result.get("packages") is not None:
                extra["packages"] = result["packages"]
            await look(session, after_action=True)
        for i, (description, action, element) in enumerate(steps):
            try:
                result = await driver.execute(description, action, target=element, look=i == len(steps) - 1)
            except (DeviceError, httpx.TransportError, ValueError) as exc:
                result = f"{description} -> FAILED: {safe_error(exc)} It may or may not have happened: look at the screen."
                with contextlib.suppress(DeviceError, httpx.TransportError):
                    await driver.observe(after_action=True)
            results.append(result)
            if "FAILED" in result:
                ok = False
                break
        return view(session, ok=ok, results=results, notes=notes, **extra)


@router.post("/release")
async def release_phone(body: ClientInput):
    session = sessions.get(body.client)
    if not session:
        # The gateway may still hold this client's lease without a session here: this API restarted (a dev edit)
        # while the client held the phone, and its startup sweep ran before the gateway answered. Left, the lease
        # keeps every Relay run waiting for a phone nobody is using.
        owner = f"cli:{body.client}"
        if owner in (await gateway().holders()).values():
            with contextlib.suppress(Exception):
                await gateway().release(owner)
            return {"released": True, "why": "stale lease"}
        return {"released": False, "why": "this client holds no phone"}
    async with session.lock:
        return await release(session)


@router.delete("/sessions")
async def take_back():
    """The user takes the phone back from every CLI session (Relay's window); each is told on its next command."""
    taken_back.update(sessions)
    await release_all("taken back")
    return {"released": True}


class SetupInput(BaseModel):
    skill: bool = False  # write the android-phone skill for Claude Code
    path: bool = False  # put the `relay` launcher on the user's PATH
    app: bool = False  # add Relay's MCP server to the Claude app's config (Cowork)
    remove_app: bool = False  # take it out again
    skill_zip: bool = False  # write the Cowork skill as a ZIP for claude.ai's Customize > Skills


def bin_dir():
    """Where the desktop app writes the `relay` launcher: <data>/bin, beside <data>/data (main.js writeLauncher)."""
    return settings.data_dir.parent / "bin"


@router.get("/setup")
async def setup_state():
    """What the Connect Claude page shows: Claude Code and the desktop app found or not, the skill, the launcher
    and the PATH, and which CLI sessions hold the phone now."""
    return {**cli.installation(bin_dir()), "sessions": sorted(sessions)}


@router.post("/setup")
async def setup_apply(body: SetupInput):
    """The page's buttons: install the skill, put `relay` on the PATH, add Relay to the Claude app, export the
    Cowork skill. Answers with the new state and, for the PATH, what the user still has to do."""
    done = {}
    try:
        if body.skill:
            done["skill_written"] = str(cli.install_skill())
        if body.path:
            done["path_note"] = cli.put_on_path(bin_dir())
        if body.app:
            done["app_config_written"] = str(cli.register_claude_app(cli.mcp_entry(settings.data_dir / CLI_FILE)))
        if body.remove_app:
            done["app_config_removed"] = cli.unregister_claude_app()
        if body.skill_zip:
            done["skill_zip"] = str(cli.write_cowork_zip(settings.data_dir.parent / "claude" / f"{cli.SKILL_NAME}.zip"))
    except (cli.Stop, OSError) as exc:
        raise HTTPException(500, f"Could not finish the setup: {exc}") from None
    return {**done, **cli.installation(bin_dir()), "sessions": sorted(sessions)}


@router.get("/knowhow")
async def knowhow(app: str):
    """What is written down about an app (backend/skills/apps): deep links, login, where things are."""
    return {
        "app": app, "skill": skills.for_app({"name": app, "package": app}),
        "known_apps": [s["name"] for s in skills.index().get("apps", [])],
    }


# --- the discovery file --------------------------------------------------------------------------------------

def publish(port):
    """Tell the `relay` command where this API is: <data dir>/cli.json with the address and the integration
    token, readable by this user only. Removed on shutdown (unpublish)."""
    path = settings.data_dir / CLI_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"url": f"http://127.0.0.1:{port}", "token": auth.integration_token(), "pid": os.getpid()}))
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)
    return path


def unpublish():
    with contextlib.suppress(OSError):
        (settings.data_dir / CLI_FILE).unlink()
