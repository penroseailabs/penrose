"""The API the app's window talks to (`relay-server api`). It also serves the built UI.

    auth.py      who may call it: local only, signed in with the desktop app's per-launch login
    chats.py     conversations and the messages that start or continue their runs
    runs.py      answering, pausing and cancelling a run; its events and developer trace
    memory.py    what Relay remembers, and the user's profile
    models.py    the models a task can run on, for the composer's picker
    keys.py      the API keys the agents run on: which are set, and saving one (through the desktop app)
    devices.py   the phone, through the device gateway
    phone.py     the phone for Claude Code and Cowork, through the `relay` command
    companion.py linking the user's own phone (app/companion): QR codes, approvals, linked phones
    links.py     link previews: the title and picture a page names for itself, for the chat's link cards
    voice.py     voice mode: hearing the user (a transcription session), placing what they said, speaking replies
    updates.py   what the window shows besides the open chat (health, the phone, the chat list), pushed as it changes
    foryou.py    the "For you" list's actions: opening and dismissing a briefing, the apps to be briefed on
    common.py    the database, the gateway client and run bookkeeping the routers share
"""

import asyncio
import contextlib
import time
import typing
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

from .. import bell
from ..bell import RING_PATH, WORKER_PATH
from ..companion import identity as companion_identity
from ..companion import tunnel as companion_tunnel
from ..config import settings
from ..models import Action
from ..runtime import desk as runtime_desk
from . import (
    auth,
    chats,
    common,
    companion,
    desk,
    devices,
    foryou,
    keys,
    links,
    memory,
    models,
    phone,
    runs,
    updates,
    uploads,
    voice,
)


@asynccontextmanager
async def lifespan(app):
    common.store.execute("CREATE TABLE IF NOT EXISTS web_sessions (token_hash TEXT PRIMARY KEY, expires REAL NOT NULL)")
    # Events the API writes itself (stopped, queued, resumed) wake its streams at once too, and the worker: they
    # follow every write that queues or stops a run.
    common.store.on_event = written
    common.worker_news.listen()  # binds the worker's doorbell to this loop
    common.gateway = httpx.AsyncClient(
        base_url=settings.emulator_gateway_url, headers=settings.gateway_headers, timeout=45
    )
    # Started by the desktop app (app/desktop.py sets the port): the `relay` command can find this API.
    port = getattr(app.state, "port", None)
    if port:
        phone.publish(port)
        app.state.sweep = asyncio.create_task(phone.forget_stale())  # alongside startup: the gateway may be slow
    # The user's own phone (app/companion): the tunnel connects once a coordinator address is known.
    companion_tunnel.current = companion_tunnel.Tunnel(app, companion_identity.Identity(common.store))
    companion_tunnel.current.on_change = updates.changed
    companion_tunnel.current.start()
    updates.start()
    # The reception desk's events: a task that finishes, asks or fails is logged for the desk to tell the user.
    app.state.desk = asyncio.create_task(
        runtime_desk.watch(common.store, updates.changed)
    )
    yield
    app.state.desk.cancel()
    with contextlib.suppress(BaseException):
        await app.state.desk
    await updates.stop()
    if companion_tunnel.current:
        await companion_tunnel.current.stop()
        companion_tunnel.current = None
    if port:
        phone.unpublish()
        app.state.sweep.cancel()
    await phone.release_all("Relay stopped")  # the phone goes back; idle clocks stop
    if phone.devices:
        await phone.devices.close()
    await common.gateway.aclose()


app = FastAPI(title="penrose API", lifespan=lifespan)
app.middleware("http")(auth.guard)
for module in (auth, chats, runs, memory, models, keys, links, devices, phone, companion, voice, desk, updates, uploads,
               foryou):
    app.include_router(module.router)


@app.post(RING_PATH, include_in_schema=False)
async def ring(body: dict):
    """The worker's doorbell (app/bell.py): these runs have new events. Only the worker may call it (auth.guard)."""
    common.waiters.heard = True
    for run_id in (body.get("runs") or [])[:500]:
        common.waiters.ring(str(run_id))
    updates.runs_changed()
    runtime_desk.news.notify()
    return {"ok": True}


def written(run_id):
    """The API wrote an event for `run_id` (after its transaction): its streams, the worker and the chat list hear."""
    common.waiters.ring(run_id)
    runtime_desk.news.notify()
    common.worker_pending = True
    common.worker_news.notify()
    updates.runs_changed()


@app.get(WORKER_PATH, include_in_schema=False)
async def worker_doorbell(request: Request, wait: float = bell.DOORBELL_SECONDS):
    """The worker's held request (app/bell.py Doorbell): answered when the API has written anything the worker acts
    on (a queued run, a reply, a stop), or after `wait` seconds. Only the worker may call it (auth.guard). A worker
    that goes away mid-request is noticed at once: that is how the window learns it stopped."""
    async with updates.worker_holding():
        listened = common.worker_news.listen()  # before the check, so news in between still wakes it
        if common.worker_pending:
            common.worker_pending = False
            return {"news": True}
        news = asyncio.create_task(bell.wait(listened, min(max(wait, 0), 60)))
        gone = asyncio.create_task(disconnected(request))
        try:
            await asyncio.wait((news, gone), return_when=asyncio.FIRST_COMPLETED)
        finally:
            gone.cancel()
            news.cancel()
        rung = news.done() and not news.cancelled() and news.result()
        common.worker_pending = False
        return {"news": rung}


async def disconnected(request: Request):
    """Returns when the client has gone."""
    while (await request.receive())["type"] != "http.disconnect":
        pass


@app.get("/api/health")
async def health():
    try:
        response = await common.gateway.get("/health", timeout=3)
        gateway_ok = response.is_success
        # A gateway started before an Action change rejects the new kinds with 422, and one
        # started before the bridge driver cannot read the screen at all.
        status = response.json() if gateway_ok else {}
        gateway_outdated = gateway_ok and (not status.get("bridge") or bool(
            set(typing.get_args(Action.model_fields["kind"].annotation)) - set(status.get("actions", []))
        ))
    except httpx.HTTPError:
        gateway_ok = gateway_outdated = False
    last_seen = common.store.rows("SELECT MAX(heartbeat) AS last_seen FROM workers")[0]["last_seen"]
    return {
        "status": "ok",
        "deepseek_configured": bool(settings.deepseek_api_key),
        "openai_configured": bool(settings.openai_api_key),
        "anthropic_configured": bool(settings.anthropic_api_key),
        "gateway_connected": gateway_ok,
        "gateway_outdated": bool(gateway_outdated),
        "worker_last_active": last_seen,
        "worker_online": updates.worker_online() or bool(last_seen and last_seen > time.time() - 10),
        "user_id": settings.user_id,
        "desktop": settings.desktop,
        "fast_model": settings.fast_model,
        "heavy_model": settings.heavy_model,
        "stream_fps": settings.stream_fps,
    }


@app.get("/{path:path}", include_in_schema=False)
async def web_ui(path: str):
    """A file of the built UI, or index.html for the app's own routes. Registered last, after the API."""
    if not settings.frontend_dir or path.startswith("api/"):
        raise HTTPException(404, "Not found")
    ui = Path(settings.frontend_dir).resolve()
    file = (ui / path).resolve()
    if path and file.is_file() and file.is_relative_to(ui):
        return FileResponse(file)
    return FileResponse(ui / "index.html", headers={"Cache-Control": "no-store"})
