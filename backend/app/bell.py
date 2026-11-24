"""The doorbells between the services: "run X has new events", and "the worker has something to do".

Events are rows in the database, written by the worker (and a few by the API). The worker and the API are
separate processes and SQLite cannot tell one when the other wrote, so the writer rings: the worker sends the
run ids to the API (`Bell`, a POST to /api/internal/ring, a few per second at most), and the API wakes the
event streams of those runs (`Waiters`), which read the new rows. The database stays the only record: a ring
carries no events, and a stream that misses one still reads its rows on its next slow check.

The other way: the API queues a run (a message, a reply) or stops one, and the worker must hear of it. The worker
holds a request open to the API (`Doorbell`, GET /api/internal/worker), which answers when the API has written
anything (`Signal`); the worker then reads the jobs and statuses it acts on. That open request is also how the API
knows the worker is running.
"""

import asyncio
import contextlib
import logging

import httpx

log = logging.getLogger(__name__)

RING_PATH = "/api/internal/ring"
WORKER_PATH = "/api/internal/worker"
INTERNAL_PREFIX = "/api/internal/"
SERVICE_HEADER = "X-Relay-Service"
# How long the worker's open request waits for news before it is sent again.
DOORBELL_SECONDS = 25


class Signal:
    """"Something changed", for whoever waits: each wait ends at the first notify after its listen(). Safe to
    notify from any thread."""

    def __init__(self):
        self.event = None
        self.loop = None

    def listen(self):
        """The event to wait on. Take it before reading the state you act on, so a change during the read counts."""
        loop = asyncio.get_running_loop()
        # An event is bound to the loop that first waits on it: one from an earlier loop (the tests start the API
        # more than once) cannot be waited on here.
        if self.event is None or self.loop is not loop:
            self.event = asyncio.Event()
        self.loop = loop
        return self.event

    def notify(self):
        loop = self.loop
        if loop is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._fire()
        else:
            loop.call_soon_threadsafe(self._fire)

    def _fire(self):
        event, self.event = self.event, None
        if event:
            event.set()


class Waiters:
    """In the API: the event streams waiting for news of their run."""

    def __init__(self):
        self.events = {}  # run id -> the asyncio.Event its waiting streams share
        self.loop = None
        self.heard = False  # a worker has rung: streams can rely on rings and check only now and then

    def listen(self, run_id):
        """The event to wait on for `run_id`'s next ring. Take it before reading, so a ring that comes between
        the read and the wait is not missed."""
        self.loop = asyncio.get_running_loop()
        return self.events.setdefault(run_id, asyncio.Event())

    def ring(self, run_id):
        """Wake every stream waiting on `run_id`. Safe from any thread."""
        loop = self.loop
        if loop is None:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self.wake(run_id)
        elif not loop.is_closed():
            loop.call_soon_threadsafe(self.wake, run_id)

    def wake(self, run_id):
        if event := self.events.pop(run_id, None):
            event.set()


async def wait(event, seconds):
    """Wait for a ring, at most `seconds`. True when rung."""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(event.wait(), seconds)
        return True
    return False


class Bell:
    """In the worker: rings the API for runs with new events, a few times a second at most."""

    GAP = 0.03  # rings within this long go out together

    def __init__(self, api_url, token):
        self.api_url, self.token = api_url.rstrip("/"), token
        self.pending = set()
        self.loop = self.wanted = self.task = None
        self.client = None

    def start(self):
        if not self.api_url:
            return  # no API to ring (tests, a bare worker): streams read on their slow check
        self.loop = asyncio.get_running_loop()
        self.wanted = asyncio.Event()
        self.client = httpx.AsyncClient(timeout=2, headers={SERVICE_HEADER: self.token} if self.token else {})
        self.task = asyncio.create_task(self.send())

    def ring(self, run_id):
        """Note that `run_id` has new events. Safe from any thread; never waits."""
        if self.loop is None or self.loop.is_closed():
            return
        if self._on_loop():
            self._ring_on_loop(run_id)
        else:
            self.loop.call_soon_threadsafe(self._ring_on_loop, run_id)

    def _on_loop(self):
        try:
            return asyncio.get_running_loop() is self.loop
        except RuntimeError:
            return False

    def _ring_on_loop(self, run_id):
        self.pending.add(run_id)
        self.wanted.set()

    async def send(self):
        while True:
            await self.wanted.wait()
            await asyncio.sleep(self.GAP)
            self.wanted.clear()
            runs, self.pending = sorted(self.pending), set()
            try:
                await self.client.post(self.api_url + RING_PATH, json={"runs": runs})
            except httpx.HTTPError as exc:  # the API is restarting: its streams catch up on their own
                log.debug("could not ring the API: %s", exc)

    async def close(self):
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        if self.client:
            await self.client.aclose()


class Doorbell:
    """In the worker: a request held open to the API, answered when the API has queued or stopped something.
    `news` is notified then (and by the worker itself). `connected`: the API is answering, so the worker can rely
    on it and check the database only now and then."""

    def __init__(self, api_url, token):
        self.api_url, self.token = api_url.rstrip("/"), token
        self.news = Signal()
        self.connected = False
        self.task = self.client = None

    def start(self):
        self.news.listen()  # binds the signal to this loop
        if not self.api_url:
            return
        self.client = httpx.AsyncClient(timeout=DOORBELL_SECONDS + 10,
                                        headers={SERVICE_HEADER: self.token} if self.token else {})
        self.task = asyncio.create_task(self.hold())

    async def hold(self):
        pause = 0.5
        while True:
            try:
                r = await self.client.get(self.api_url + WORKER_PATH, params={"wait": DOORBELL_SECONDS})
                r.raise_for_status()
                if not self.connected:
                    self.connected = True
                    self.news.notify()  # (re)connected: whatever was written meanwhile is read now
                pause = 0.5
                if r.json().get("news"):
                    self.news.notify()
            except httpx.HTTPError as exc:  # the API is starting or restarting: the worker checks often meanwhile
                if self.connected:
                    log.info("the API's doorbell is unavailable: %s", exc)
                self.connected = False
                await asyncio.sleep(pause)
                pause = min(pause * 2, 5)

    async def close(self):
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        if self.client:
            await self.client.aclose()
