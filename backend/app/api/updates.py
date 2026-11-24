"""What the window shows besides the open chat, pushed as it changes: the services' health, the phone, the chat
list and the companion's status (`GET /api/updates`, server-sent events). The window used to ask for each of these
every few seconds.

Where each change comes from:
- the phone: the gateway's own stream of its device list (gateway/devices.py `/devices/updates`), held open here;
  it also says whether the gateway is up;
- the chat list: every event a run writes (the worker's ring, the API's own writes), checked for a change at most
  once per CHATS_GAP;
- the worker: the request it holds open to the API (bell.Doorbell), so the API knows at once when it stops;
- the companion: its tunnel says when a phone connects, asks to link, or the coordinator connection changes.

Each event names what changed: {"health": ...}, {"devices": [...]}, {"chats": [...]}, {"companion": ...},
{"for_you": ...} (the briefings and the chats that wait for the user: app/briefings). The first events carry
everything."""

import asyncio
import contextlib
import json
import logging
import time
import typing

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from .. import bell
from ..config import settings
from ..models import Action
from . import common

router = APIRouter()
log = logging.getLogger(__name__)

CHATS_GAP = 1.0  # seconds: while a task works its events come many a second; the list is read at most this often
WORKER_GRACE = 3.0  # seconds between two of the worker's held requests before it counts as stopped
KEEPALIVE = 15

news = bell.Signal()
devices: list = []
gateway = {"connected": False, "outdated": False}
worker = {"holding": 0, "last": 0.0, "ever": False}
_chats_due = None
_tasks: list[asyncio.Task] = []
# Told the device list whenever it changes (api/devices.py records the Google account from it).
device_listeners: list[typing.Callable[[list], None]] = []


def changed():
    news.notify()


# --- the worker ---------------------------------------------------------------------------------------------------

def worker_online():
    if worker["holding"] or time.monotonic() - worker["last"] < WORKER_GRACE:
        return True
    if worker["ever"]:
        return False
    # A worker that never held the doorbell (started without API_URL): its heartbeat says.
    last_seen = common.store.rows("SELECT MAX(heartbeat) AS last_seen FROM workers")[0]["last_seen"]
    return bool(last_seen and last_seen > time.time() - 10)


@contextlib.asynccontextmanager
async def worker_holding():
    """The worker's held request (api/__init__.py): while one is open, the worker runs."""
    worker["holding"] += 1
    if not worker["ever"] or worker["holding"] == 1:
        worker["ever"] = True
        changed()
    try:
        yield
    finally:
        worker["holding"] -= 1
        worker["last"] = time.monotonic()
        if not worker["holding"]:
            # It sends the next one at once; if none comes within the grace, it has stopped.
            asyncio.get_running_loop().call_later(WORKER_GRACE + 0.1, changed)


# --- the chat list ------------------------------------------------------------------------------------------------

def runs_changed():
    """A run wrote events: the chat list (statuses, order) may have changed. Read it soon, not for each event."""
    global _chats_due
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = news.loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(runs_changed)
        return
    if _chats_due is None:
        _chats_due = loop.call_later(CHATS_GAP, _chats_now)


def _chats_now():
    global _chats_due
    _chats_due = None
    changed()


# --- the gateway's device list --------------------------------------------------------------------------------

async def follow_gateway():
    """Hold the gateway's stream of its device list; reconnect when it restarts."""
    global devices
    pause = 0.5
    while True:
        try:
            health = (await common.gateway.get("/health", timeout=3)).json()
            gateway["outdated"] = not health.get("bridge") or bool(
                set(typing.get_args(Action.model_fields["kind"].annotation)) - set(health.get("actions", [])))
            async with common.gateway.stream("GET", "/devices/updates", timeout=httpx.Timeout(10, read=60)) as response:
                response.raise_for_status()
                gateway["connected"] = True
                changed()
                pause = 0.5
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        devices = json.loads(line[6:])
                        for listener in device_listeners:
                            with contextlib.suppress(Exception):
                                listener(devices)
                        changed()
        except asyncio.CancelledError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            log.debug("gateway device stream: %s", exc)
        if gateway["connected"]:
            gateway["connected"] = False
            devices = [{**d, "connected": False, "state": "offline"} for d in devices]
            changed()
        await asyncio.sleep(pause)  # the gateway is starting or restarting
        pause = min(pause * 2, 5)


def health():
    return {
        "status": "ok",
        "deepseek_configured": bool(settings.deepseek_api_key),
        "openai_configured": bool(settings.openai_api_key),
        "anthropic_configured": bool(settings.anthropic_api_key),
        "gateway_connected": gateway["connected"],
        "gateway_outdated": gateway["outdated"],
        "worker_online": worker_online(),
        "user_id": settings.user_id,
        "desktop": settings.desktop,
        "fast_model": settings.fast_model,
        "heavy_model": settings.heavy_model,
        "stream_fps": settings.stream_fps,
        "voice_desk": settings.desk_on,
    }


def companion_status():
    from ..companion import tunnel

    if not tunnel.current:
        return None
    return {**tunnel.current.status(), "phones": tunnel.current.identity.phones()}


def snapshot():
    from ..briefings import feed as for_you
    from ..runtime import desk
    from . import chats

    return {
        "health": health(), "devices": devices, "chats": chats.chats(), "companion": companion_status(),
        # What penrose has for the user (app/briefings/feed.py): the worker rings when a briefing is sorted.
        "for_you": for_you.feed(common.store),
        # The reception desk's latest event (runtime/desk.py): the phone fetches the new ones when it moves.
        "desk": desk.latest_event_id(common.store) if settings.desk_on else 0,
    }


async def stream(companion=True):
    """Each change as {kind: value}, everything first; None after KEEPALIVE seconds without one. The phone app
    (`companion` False, over its tunnel) is not told the companion's own status."""
    sent = {}
    while True:
        listened = news.listen()
        for key, value in snapshot().items():
            if key == "companion" and not companion:
                continue
            text = json.dumps(value)
            if sent.get(key) != text:
                sent[key] = text
                yield {key: value}
        if not await bell.wait(listened, KEEPALIVE):
            yield None


@router.get("/api/updates")
async def updates(request: Request):
    async def generate():
        changes = stream()
        try:
            async for change in changes:
                if await request.is_disconnected():
                    return
                yield ": keepalive\n\n" if change is None else f"data: {json.dumps(change)}\n\n"
        finally:
            await changes.aclose()

    return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


def start():
    news.listen()  # binds the signal to the API's loop
    _tasks.append(asyncio.create_task(follow_gateway()))


async def stop():
    for task in _tasks:
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
    _tasks.clear()
