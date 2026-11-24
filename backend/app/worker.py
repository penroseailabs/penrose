import asyncio
import contextlib
import logging
import time

from .bell import Bell, Doorbell
from .bell import wait as rung
from .briefings import prepare
from .briefings.triage import Sorter
from .config import settings
from .notifications import watch
from .runtime.runner import Runner
from .store import HALTED, Store, uid

logging.basicConfig(level=logging.INFO)
# httpx logs every request at INFO: each ring to the API and model call, many a second while a run works.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("worker")

# New jobs, stops and discards reach the worker through the API's doorbell (app/bell.py Doorbell) at once. Its
# database checks are only for news the doorbell could not bring: CHECK_SECONDS while the API answers it, and
# OFFLINE_CHECK_SECONDS while it does not (the API is restarting, or there is none: a bare worker).
CHECK_SECONDS = 15
OFFLINE_CHECK_SECONDS = 0.5
# The worker says it is alive this often (stale jobs are recovered after 90 s without), and frees the jobs of a
# worker that died this often.
HEARTBEAT_SECONDS = 5
RECOVER_SECONDS = 30


async def main():
    store, owner = Store(), uid()
    runner = Runner(store)
    # Tell the API when a run has new events, so the window's streams show them at once.
    bell = Bell(settings.api_url, settings.gateway_token)
    bell.start()
    store.on_event = bell.ring
    # And hear from the API when it queues or stops a run.
    doorbell = Doorbell(settings.api_url, settings.gateway_token)
    doorbell.start()

    def check_after():
        return CHECK_SECONDS if doorbell.connected else OFFLINE_CHECK_SECONDS

    def heartbeat():
        store.execute("INSERT OR REPLACE INTO workers VALUES (?,?)", (owner, time.time()))

    async def keep_alive():
        """The heartbeat, and the jobs of a worker that died: on a clock of their own, whatever the worker does."""
        recovered = 0.0
        while True:
            heartbeat()
            if time.monotonic() - recovered >= RECOVER_SECONDS:
                recovered = time.monotonic()
                for run_id in store.recover_stale():
                    with contextlib.suppress(Exception):
                        await runner.devices.release(run_id)
            await asyncio.sleep(HEARTBEAT_SECONDS)

    # Memory, compaction and reflection passes, one run at a time (two at once could save the same memory
    # twice), alongside the next jobs.
    memory_lock, memory_passes = asyncio.Lock(), set()

    async def remember(state):
        try:
            async with memory_lock:
                await runner.afterwards(state)
        finally:
            memory_passes.discard(asyncio.current_task())
            store.turns_written.pop(state["id"], None)

    # The phone's new notifications are kept, and passed to the chats they belong to (app/notifications.py).
    # One a chat's watch waits for may wake the chat (app/watches.py): its task is queued here, so the loop below
    # is told at once.
    # The ones no chat takes, from the apps the user is briefed on, join briefings, which the sorter looks at once
    # they go quiet (app/briefings). It also hears the API's news: the user choosing an app is one. One it sorts as
    # worth the user's attention wakes the loop below, which prepares it when it has nothing else to do.
    sorter = Sorter(runner, doorbell.news.listen, on_brief=doorbell.news.notify)
    sorting = asyncio.create_task(sorter.run())
    notifications = asyncio.create_task(watch(store, runner.devices, runner, doorbell.news.notify, on_briefing=sorter.poke))
    alive = asyncio.create_task(keep_alive())
    try:
        while True:
            news = doorbell.news.listen()  # before the read, so a job queued during it still wakes the worker
            job = store.claim(owner) if store.queued() else None
            if not job:
                # Nothing to do: a briefing worth the user's attention is prepared before they look (briefings/prepare.py).
                try:
                    if prepare.begin_next(store):
                        continue
                except Exception:
                    log.exception("Could not begin preparing a briefing")
                await rung(news, check_after())
                continue
            state = store.load(job["run_id"])
            if not state or state["status"] in HALTED:
                store.execute("UPDATE jobs SET status='done' WHERE id=?", (job["id"],))
                continue
            # The memory passes run after the job is over, so a run waiting for the user takes their reply at once.
            task = asyncio.create_task(runner.advance(state))

            async def monitor(task=task, job=job):
                """Stop the run when the window stops or discards it (the API rings), and keep its job alive."""
                beat = 0.0
                while not task.done():
                    news = doorbell.news.listen()
                    # One penrose began on its own gives way to the user's: paused, it is stopped (below) and queued
                    # again once theirs has the worker.
                    if store.background(job["run_id"]) and store.users_waiting():
                        prepare.pause(store, job["run_id"])
                    if store.status(job["run_id"]) in HALTED:
                        task.cancel()
                        return
                    if time.monotonic() - beat >= HEARTBEAT_SECONDS:
                        store.execute(
                            "UPDATE jobs SET heartbeat=? WHERE id=? AND owner=?",
                            (time.time(), job["id"], owner),
                        )
                        beat = time.monotonic()
                    await rung(news, min(check_after(), HEARTBEAT_SECONDS))

            watcher = asyncio.create_task(monitor())
            finished = False
            try:
                await task
                finished = True
            except asyncio.CancelledError:
                log.info("Run stopped: %s", job["run_id"])
            finally:
                watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watcher
                store.execute(
                    "UPDATE jobs SET status='done' WHERE id=? AND owner=?", (job["id"], owner)
                )
                # A reply that came in while the job was ending waits for this (api/runs.py settled).
                bell.ring(job["run_id"])
                # What this process wrote of the run's turns: kept per run, it grew with every run for days. The
                # next save of the run reads it again (as after any load).
                store.turns_written.pop(job["run_id"], None)
                # What the user sent that the run never read goes out now, as the next message.
                try:
                    if after := store.settle(job["run_id"]):
                        store.event(after["id"], "queued", {})
                except Exception:
                    log.exception("Could not send the messages waiting for run %s", job["run_id"])
                try:
                    prepare.resume(store, job["run_id"])  # one paused for the user's task: after it
                except Exception:
                    log.exception("Could not queue run %s again", job["run_id"])
            if finished:
                memory_passes.add(asyncio.create_task(remember(state)))
    finally:
        alive.cancel()
        await doorbell.close()
        notifications.cancel()
        sorting.cancel()
        for pass_ in memory_passes:
            pass_.cancel()
        store.execute("DELETE FROM workers WHERE id=?", (owner,))
        await runner.close()
        await bell.close()


if __name__ == "__main__":
    asyncio.run(main())
