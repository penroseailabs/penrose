"""Each phone's notifications, heard as they arrive: one read held open on the bridge module's listener.

The listener (the module's Notifier) numbers its events. For each phone that is up, `follow` keeps a connection
of its own to it and asks for what came after the last number it knows, with `wait`: the listener answers the
moment a notification is posted, so nothing here asks on a timer. The reads of everyone else (the worker's
intake, through actions.py) wait on `changed` until that number moves, then read the events themselves.

A connection of its own, because a bridge socket answers one request at a time: a held read on the shared one
would keep an agent's notification ops waiting. Holding it neither wakes a sleeping phone nor keeps it awake.

A listener older than relay-bridge 0.5.19 cannot hold a read (its answer has no `held`): it is asked every
`notification_seconds` instead, until the phone has the newer module.
"""

import asyncio
import contextlib
import json
import logging

from ..bridge.opscatalog import NOTIFIER
from ..config import settings
from . import core, watch

log = logging.getLogger("gateway.notices")

# One read is held this long, then asked again: a dead connection is noticed within it.
HOLD_SECONDS = 50
# Past any event: a read from here returns none, with the listener's current number.
NOWHERE = 2**62
RETRY_SECONDS, MAX_RETRY_SECONDS = 1, 30

seqs = {}  # slot -> the listener's last event number; missing: not heard from (the phone is down or starting)
tasks = {}  # slot -> the task following the phone
_news = {}  # slot -> asyncio.Event, replaced each time the number changes


def seq(slot):
    return seqs.get(slot)


def _event(slot):
    if slot not in _news:
        _news[slot] = asyncio.Event()
    return _news[slot]


def _set(slot, number):
    if seqs.get(slot) == number and (number is not None or slot not in seqs):
        return
    if number is None:
        seqs.pop(slot, None)
    else:
        seqs[slot] = number
    _event(slot).set()
    _news[slot] = asyncio.Event()


async def changed(slot, since, seconds):
    """Wait until the listener's number is known and is not `since`, at most `seconds`. True when it is."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while True:
        news = _event(slot)  # before the look, so a change meanwhile still wakes this
        if seqs.get(slot) not in (None, since):
            return True
        left = deadline - loop.time()
        if left <= 0:
            return False
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(news.wait(), left)


async def _hear(slot):
    """Hold reads on the listener until the connection ends."""
    port = (await core.adb(slot, "forward", "tcp:0", f"localabstract:relaybridge.{NOTIFIER}", timeout=10)).decode().strip()
    writer = None
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", int(port))
        known, request = None, 0
        while True:
            request += 1
            ask = {"cmd": "events", "id": request, "since": NOWHERE if known is None else known,
                   "wait": 0 if known is None else HOLD_SECONDS * 1000}
            writer.write((json.dumps(ask) + "\n").encode())
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), HOLD_SECONDS + 15)
            if not line:
                raise ConnectionError("the notification listener closed the connection")
            answer = json.loads(line)
            if not answer.get("ok"):
                raise ConnectionError(answer.get("error") or "the notification listener refused the read")
            first, known = known is None, int(answer.get("seq", 0))
            _set(slot, known)
            if not first and not answer.get("held"):  # an older listener answered at once: ask again later
                await asyncio.sleep(max(settings.notification_seconds, 1))
    finally:
        if writer:
            writer.close()
        with contextlib.suppress(Exception):
            await core.adb(slot, "forward", "--remove", f"tcp:{port}", timeout=5)


async def follow(slot):
    """While the phone is up, hear its listener; when the connection ends (the phone restarted, the module was
    updated), wait for the phone to be ready and connect again."""
    pause = RETRY_SECONDS
    while True:
        while watch.state(slot) != watch.READY:
            await watch.changed(60)
        try:
            await _hear(slot)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("%s: the notification listener is not heard (again in %d s): %s", slot, pause, exc)
        heard = slot in seqs
        _set(slot, None)
        pause = RETRY_SECONDS if heard else min(pause * 2, MAX_RETRY_SECONDS)
        await asyncio.sleep(pause)


async def start(slot):
    """Called when a phone becomes ready (watch.on_ready): follow it, once."""
    if settings.notification_seconds > 0 and (slot not in tasks or tasks[slot].done()):
        tasks[slot] = asyncio.create_task(follow(slot))


async def close():
    for task in tasks.values():
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
    tasks.clear()


watch.on_ready(start)
