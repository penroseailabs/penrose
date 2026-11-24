"""The phone, as the window uses it: its state, live view, location, Google account, and the user's own taps.
Everything goes through the device gateway (app/gateway), which alone talks to adb."""

import asyncio
import logging
import time
import typing

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from ..models import Action
from ..store import uid
from . import common, updates

router = APIRouter(prefix="/api/devices")


class LocationInput(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    accuracy: float = Field(default=10, gt=0, le=10000)
    # "phone": the user's own phone, sent with a message from the companion app (gateway/devices.py)
    source: typing.Literal["computer", "phone"] = "computer"


@router.get("")
async def devices():
    try:
        response = await common.gateway.get("/devices", timeout=6)
        response.raise_for_status()
        return response.json()
    except httpx.HTTPError:
        return []


@router.get("/{slot}/google")
async def device_google(slot: str):
    """The Google account signed in on the phone. Tasks wait until there is one: the user signs in
    themselves, once, from the phone panel. `accounts` is null when the phone cannot be read."""
    try:
        response = await common.gateway.get(f"/devices/{slot}/accounts", timeout=10)
        response.raise_for_status()
        accounts = response.json()["accounts"]
    except (httpx.HTTPError, KeyError, ValueError):
        return {"accounts": None}
    if accounts:
        common.store.set_google_account(accounts[0])
        offer_simple_signins(slot, accounts[0])
    return {"accounts": accounts}


# "Simplify sign-ins on this device" (gateway/simple_signins.py): on by default, and set up once by itself for each
# Google account that appears on the phone, right after the user signs in; the Settings switch turns it on or off.
SIMPLE_SIGNINS = "simple_signins"
simple_signin_tasks: dict[str, asyncio.Task] = {}
log = logging.getLogger(__name__)


def accounts_seen(listed):
    """The gateway's device list carries each phone's Google accounts, read when they may have changed: the first
    one is remembered for the agents' briefs, and simplified sign-ins are set up for it."""
    for device in listed:
        if device.get("accounts"):
            common.store.set_google_account(device["accounts"][0])
            offer_simple_signins(device["id"], device["accounts"][0])


updates.device_listeners.append(accounts_seen)


def simple_signins_pref():
    return {"on": True, "offered": [], **(common.store.profile_get(SIMPLE_SIGNINS, {}) or {})}


def offer_simple_signins(slot, account):
    """Right after a Google account first appears: the screen lock, and Chrome's "Turn on sync?" on the phone."""
    pref = simple_signins_pref()
    running = simple_signin_tasks.get(slot)
    if not pref["on"] or account in pref["offered"] or (running and not running.done()):
        return
    common.store.profile_set(SIMPLE_SIGNINS, {**pref, "offered": [*pref["offered"], account]})

    async def run():
        try:
            await forward_simple_signins("POST", slot, {"enabled": True})
        except HTTPException as exc:
            log.warning("simplify sign-ins for the new Google account: %s", exc.detail)

    simple_signin_tasks[slot] = asyncio.create_task(run())


async def forward_simple_signins(method, slot, body=None):
    try:
        response = await common.gateway.request(method, f"/devices/{slot}/simple-signins", json=body, timeout=90)
    except httpx.HTTPError:
        raise HTTPException(503, "Emulator gateway unavailable") from None
    if response.is_error:
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = None
        if response.status_code == 404 and detail == "Not Found":
            raise HTTPException(503, "penrose’s phone service is out of date. Restart penrose.")
        raise HTTPException(response.status_code, detail if isinstance(detail, str) else "The phone didn’t answer")
    return response.json()


class SimpleSigninsInput(BaseModel):
    enabled: bool


@router.get("/{slot}/simple-signins")
async def simple_signins_state(slot: str):
    """The Settings switch (`on`) and what the phone has: Relay's screen lock (`enabled`) and Chrome's sign-in."""
    on = simple_signins_pref()["on"]
    try:
        return {**await forward_simple_signins("GET", slot), "on": on}
    except HTTPException:
        return {"on": on, "account": None, "enabled": None, "chrome_signed_in": None, "foreign_lock": False}


@router.post("/{slot}/simple-signins")
async def change_simple_signins(slot: str, body: SimpleSigninsInput):
    """Turn simplified sign-ins on or off: the switch is saved first, so it holds even when the phone is away."""
    common.store.profile_set(SIMPLE_SIGNINS, {**simple_signins_pref(), "on": body.enabled})
    return {**await forward_simple_signins("POST", slot, body.model_dump()), "on": body.enabled}


class SignInInput(BaseModel):
    email: str = Field(min_length=1, max_length=254)
    # "prompt": "Tap Yes" on the user's own phone (their password only if Google offers no prompt); "password":
    # their password straight away.
    method: typing.Literal["prompt", "password"] = "prompt"


class PhoneInput(BaseModel):
    phone: str = Field(min_length=1, max_length=40)


class PasswordInput(BaseModel):
    password: str = Field(min_length=1, max_length=200)


async def forward_signin(method, slot, body=None, path=""):
    """The gateway's guided Google sign-in (gateway/signin.py): its state, as the Welcome step shows it."""
    try:
        response = await common.gateway.request(method, f"/devices/{slot}/google/signin{path}", json=body, timeout=15)
    except httpx.HTTPError:
        raise HTTPException(503, "Emulator gateway unavailable") from None
    if response.is_error:
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = None
        if response.status_code == 404 and detail == "Not Found":  # a gateway from before guided sign-in
            raise HTTPException(503, "penrose’s phone service is out of date. Restart penrose, or sign in on the phone.")
        raise HTTPException(response.status_code, detail if isinstance(detail, (str, list)) else "Sign-in failed")
    return response.json()


@router.post("/{slot}/google/signin")
async def start_google_signin(slot: str, body: SignInInput):
    """Sign in to Google on the phone with the user's email: the gateway types it, then asks Google for the prompt on
    the user's own phone, or asks for the user's password (need_password) and types it once."""
    return await forward_signin("POST", slot, body.model_dump())


@router.post("/{slot}/google/signin/phone")
async def answer_google_signin_phone(slot: str, body: PhoneInput):
    """The phone number on the user's Google account, when Google asks to confirm it: typed on the phone, not kept."""
    return await forward_signin("POST", slot, body.model_dump(), "/phone")


@router.post("/{slot}/google/signin/password")
async def answer_google_signin_password(slot: str, body: PasswordInput):
    """The user's Google password, when the sign-in asks for it: typed on the phone once, kept nowhere, and never
    in a log or a model's context."""
    return await forward_signin("POST", slot, body.model_dump(), "/password")


@router.post("/{slot}/google/signin/agree")
async def agree_google_signin(slot: str):
    """The user agrees to Google's terms on the sign-in: the gateway taps Google's own button."""
    return await forward_signin("POST", slot, None, "/agree")


@router.get("/{slot}/google/signin")
async def google_signin_status(slot: str):
    return await forward_signin("GET", slot)


@router.delete("/{slot}/google/signin")
async def stop_google_signin(slot: str):
    return await forward_signin("DELETE", slot)


@router.get("/{slot}/screenshot")
async def screenshot(slot: str, width: int = Query(0, ge=0, le=2400)):
    """Full-size PNG, or with ?width= a light JPEG (the live view's still frame)."""
    try:
        response = await common.gateway.get(f"/devices/{slot}/screenshot", params={"width": width} if width else None)
        response.raise_for_status()
    except httpx.HTTPError:
        raise HTTPException(503, "Emulator screenshot unavailable") from None
    return Response(
        response.content,
        media_type=response.headers.get("content-type", "image/png"),
        headers={"Cache-Control": "no-store"},
    )


@router.get("/{slot}/stream")
async def device_stream(slot: str, request: Request):
    async def generate():
        try:
            async with common.gateway.stream("GET", f"/devices/{slot}/stream", timeout=None) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    if await request.is_disconnected():
                        break
                    yield chunk
        except httpx.HTTPError:
            return

    return StreamingResponse(
        generate(), media_type="multipart/x-mixed-replace; boundary=frame", headers={"Cache-Control": "no-store"}
    )


PRESENCE_SECONDS = 60  # how long a sign of the user keeps the phone awake
PRESENCE_EVERY = 10  # signs closer together than this are one
presence_seen = {"at": float("-inf")}
waking = set()  # the wake tasks running, kept so they are not collected half way


async def wake_for_user():
    try:
        response = await common.gateway.get("/devices", timeout=6)
        phones = response.json() if response.is_success else []
    except (httpx.HTTPError, ValueError):
        return
    for phone in phones:
        if phone.get("connected"):
            try:
                await common.gateway.post(f"/devices/{phone['id']}/wake", json={"for_seconds": PRESENCE_SECONDS}, timeout=15)
            except httpx.HTTPError as exc:
                log.info("Could not wake %s for the user: %s", phone["id"], exc)


@router.post("/presence")
async def presence():
    """The user is about to ask for something: the window or the phone app came up, or they are typing. Each
    phone wakes now and stays awake a minute (gateway/sleep.py keep_awake), so the task that follows finds it
    out of Doze, its apps back on the network, instead of waking it then. Answers at once; at most every 10 s."""
    now = time.monotonic()
    if now - presence_seen["at"] < PRESENCE_EVERY:
        return {"waking": False}
    presence_seen["at"] = now
    task = asyncio.create_task(wake_for_user())
    waking.add(task)
    task.add_done_callback(waking.discard)
    return {"waking": True}


@router.post("/{slot}/wake")
async def wake_device(slot: str):
    """Wake the phone from sleep (gateway/sleep.py): the user pressed it in the window."""
    try:
        response = await common.gateway.post(f"/devices/{slot}/wake")
    except httpx.HTTPError:
        raise HTTPException(503, "Emulator gateway unavailable") from None
    if response.is_error:
        raise HTTPException(response.status_code, "Could not wake the phone")
    return response.json()


@router.post("/{slot}/location")
async def set_device_location(slot: str, body: LocationInput):
    try:
        response = await common.gateway.post(f"/devices/{slot}/location", json=body.model_dump())
    except httpx.HTTPError:
        raise HTTPException(503, "Emulator gateway unavailable") from None
    if response.is_error:
        raise HTTPException(response.status_code, "Could not set the emulator location")
    return response.json()


@router.post("/{slot}/location/release")
async def release_device_location(slot: str):
    """A message from the computer: the phone goes back to the computer's position."""
    try:
        response = await common.gateway.post(f"/devices/{slot}/location/release")
    except httpx.HTTPError:
        raise HTTPException(503, "Emulator gateway unavailable") from None
    if response.is_error:
        raise HTTPException(response.status_code, "Could not set the emulator location")
    return response.json()


@router.post("/{slot}/actions")
async def manual_action(slot: str, body: Action):
    """The user's own tap, key or text on the phone panel. Refused while an agent holds the phone, unless its
    run is waiting for the user (who may need to act on the phone to answer). A Claude Code session
    (api/phone.py, owner `cli:`) acts for the user, who may need to type a code for it: allowed."""
    owner = f"manual:{slot}:{uid()}"
    temporary = False
    try:
        lease = await common.gateway.get(f"/leases/{slot}")
        if lease.is_error:
            raise HTTPException(lease.status_code, "Unable to check device ownership")
        existing_owner = lease.json()["owner"]
        if existing_owner:
            owner = existing_owner
            state = None if owner.startswith("cli:") else common.store.load(owner.partition(":")[0])
            if not owner.startswith("cli:") and (not state or state["status"] not in ("interrupted", "waiting_user")):
                raise HTTPException(
                    409,
                    "An agent controls this emulator. Wait until it requests your input or cancel its run.",
                )
        else:
            lease = await common.gateway.post("/leases", json={"owner": owner, "slot": slot})
            if lease.is_error:
                raise HTTPException(lease.status_code, "The device is unavailable or already in use")
            temporary = True
        response = await common.gateway.post(
            f"/devices/{slot}/actions", json={"owner": owner, "action": body.model_dump()}
        )
        if response.is_error:
            raise HTTPException(
                response.status_code,
                "Emulator action failed. Check device connectivity and supported input.",
            )
        return response.json()
    except httpx.HTTPError:
        raise HTTPException(503, "Emulator gateway unavailable") from None
    finally:
        if temporary:
            await common.release([owner])
