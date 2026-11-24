"""The doorbell: the worker rings the API when a run has new events, and the API's event streams wake at once."""

import asyncio

from app import bell
from app.bell import RING_PATH, SERVICE_HEADER, Bell, Waiters
from app.config import settings
from fastapi.testclient import TestClient


def test_a_stream_wakes_on_a_ring_even_one_that_came_while_it_read():
    async def scenario():
        waiters = Waiters()
        rung = waiters.listen("run")  # taken before reading
        waiters.ring("run")  # the ring lands between the read and the wait
        assert await bell.wait(rung, 1)
        quiet = waiters.listen("run")
        assert not await bell.wait(quiet, 0.05)  # no ring: the slow check reads anyway

    asyncio.run(scenario())


def test_a_ring_from_another_thread_wakes_the_stream():
    async def scenario():
        waiters = Waiters()
        rung = waiters.listen("run")
        await asyncio.to_thread(waiters.ring, "run")
        assert await bell.wait(rung, 1)

    asyncio.run(scenario())


def test_the_worker_rings_once_for_a_burst_of_events():
    sent = []

    async def scenario():
        b = Bell("http://api", "secret")
        b.start()

        async def post(url, json):
            sent.append((url, json))

        b.client.post = post
        for _ in range(20):
            b.ring("run")
        b.ring("other")
        await asyncio.sleep(Bell.GAP * 4)
        await b.close()

    asyncio.run(scenario())
    assert sent == [("http://api" + RING_PATH, {"runs": ["other", "run"]})]


def test_only_the_worker_may_ring(store, monkeypatch):
    from app import api

    monkeypatch.setattr(api.common, "store", store)
    monkeypatch.setattr(settings, "gateway_token", "secret")
    with TestClient(api.app) as client:
        assert client.post(RING_PATH, json={"runs": ["run"]}).status_code == 401
        assert client.post(RING_PATH, json={"runs": ["run"]}, headers={SERVICE_HEADER: "wrong"}).status_code == 401
        assert client.post(RING_PATH, json={"runs": ["run"]}, headers={SERVICE_HEADER: "secret"}).status_code == 200
