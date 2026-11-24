"""An emulator that freezes sends no change: adb lists it and its connections stay open. The gateway asks it whether
it still answers (app/gateway/watch.py), stops counting a silent one as ready, and asks the desktop app to restart it."""

import asyncio

import pytest
from app.store import Store


class Phone:
    """adb for one phone that can stop answering: every call to a frozen one runs out of time."""

    def __init__(self):
        self.frozen = False
        self.commands = []

    async def adb(self, slot, *args, timeout=25, **kwargs):
        command = " ".join(args)
        self.commands.append(command)
        if self.frozen:
            raise TimeoutError
        if "echo alive" in command:
            return b"alive\n"
        if "pm path" in command:
            return b""  # no home app: nothing to hold open
        if "wm size" in command:
            return b"Physical size: 720x1600\n"
        return b""


@pytest.fixture
def phone(tmp_path, monkeypatch):
    from app.gateway import core, framework, watch

    for name in ("states", "setup", "tasks", "watchdogs", "doubts"):
        monkeypatch.setattr(watch, name, {})
    monkeypatch.setattr(watch, "frozen", set())
    monkeypatch.setattr(watch, "_news", None)  # made in another test's event loop
    monkeypatch.setattr(watch, "_on_ready", [])
    monkeypatch.setattr(framework, "failed_restarts", {})
    monkeypatch.setattr(framework, "restarting", {})
    monkeypatch.setattr(core, "store", Store(tmp_path / "gateway"))
    monkeypatch.setattr(core, "slots", {"device-1": "emulator-5600"})
    monkeypatch.setattr(watch, "ALIVE_PAUSE", 0)
    monkeypatch.setattr(watch.settings, "phone_alive_seconds", 30)
    fake = Phone()
    monkeypatch.setattr(core, "adb", fake.adb)

    class Bridge:
        async def call(self, cmd, **kwargs):
            return {"ok": True}

        async def reset(self):
            pass

    monkeypatch.setattr(core, "phones", {"device-1": Bridge()})
    asked = []

    async def ask_restart(slot):
        asked.append(slot)

    monkeypatch.setattr(watch, "ask_restart", ask_restart)
    return watch, fake, asked


async def settle(condition, seconds=2):
    """Let the gateway's tasks run until `condition` holds."""
    deadline = asyncio.get_running_loop().time() + seconds
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, "the gateway's tasks did not get there"
        await asyncio.sleep(0.01)


async def test_a_phone_that_answers_stays_ready_and_is_asked_only_once_per_interval(phone):
    watch, fake, asked = phone
    watch.present("device-1", True)
    await settle(lambda: watch.ready("device-1") and "shell echo alive" in fake.commands)
    await asyncio.sleep(0.05)
    assert fake.commands.count("shell echo alive") == 1 and not asked and not watch.frozen
    watch.present("device-1", False)


async def test_a_frozen_phone_stops_being_ready_and_the_desktop_app_is_asked_to_restart_it(phone):
    watch, fake, asked = phone
    watch.present("device-1", True)
    await settle(lambda: watch.ready("device-1") and "shell echo alive" in fake.commands)
    fake.frozen = True
    watch.doubt("device-1")  # what an adb call that ran out of time does (core.adb)
    await settle(lambda: asked)
    assert asked == ["device-1"] and "device-1" in watch.frozen
    assert not watch.ready("device-1")
    assert watch.view("device-1")["state"] == watch.STARTING and watch.view("device-1")["detail"] == watch.FROZEN
    # Silent three times in a row, not once: a slow answer alone restarts nothing.
    assert fake.commands.count("shell echo alive") == 1 + watch.ALIVE_TRIES
    # Restarted, adb loses the phone and finds it again: it is followed from boot like any phone.
    watch.present("device-1", False)
    assert watch.state("device-1") == watch.OFFLINE and not watch.frozen and not watch.watchdogs
    fake.frozen = False
    watch.present("device-1", True)
    await settle(lambda: watch.ready("device-1"))
    watch.present("device-1", False)


async def test_one_slow_answer_restarts_nothing(phone, monkeypatch):
    watch, fake, asked = phone
    real, slow = fake.adb, [True]

    async def adb(slot, *args, **kwargs):
        if "echo alive" in " ".join(args) and slow and watch.ready(slot):
            slow.pop()
            fake.commands.append("slow answer")
            raise TimeoutError
        return await real(slot, *args, **kwargs)

    monkeypatch.setattr(watch.core, "adb", adb)
    watch.present("device-1", True)
    await settle(lambda: watch.ready("device-1"))
    watch.doubt("device-1")
    await settle(lambda: "slow answer" in fake.commands)
    await asyncio.sleep(0.05)
    assert watch.ready("device-1") and not asked and not watch.frozen
    watch.present("device-1", False)


async def test_a_phone_left_running_that_answers_again_is_checked_afresh(phone):
    watch, fake, asked = phone
    watch.present("device-1", True)
    await settle(lambda: watch.ready("device-1") and "shell echo alive" in fake.commands)
    fake.frozen = True
    watch.doubt("device-1")
    await settle(lambda: asked)
    # Nobody restarted it (no desktop app), and it was only that slow: the next question finds it answering.
    fake.frozen = False
    watch.doubt("device-1")
    await settle(lambda: watch.ready("device-1"))
    assert not watch.frozen
    watch.present("device-1", False)


async def test_a_search_without_a_lease_waits_for_a_phone_that_is_starting(phone):
    watch, fake, asked = phone
    waiting = asyncio.create_task(watch.until_ready("device-1", 2))
    await asyncio.sleep(0.02)
    assert not waiting.done()  # offline: the phone is being restarted
    watch.present("device-1", True)
    assert await waiting is True
    watch.present("device-1", False)
    assert await watch.until_ready("device-1", 0.05) is False  # still gone: the search says so instead of trying


async def test_the_look_ups_searches_wait_for_the_phone_and_say_so_when_it_does_not_come(phone, monkeypatch):
    import httpx
    from app import gateway
    from app.gateway import home, sleep

    watch, fake, asked = phone
    monkeypatch.setattr(gateway, "READY_WAIT_SECONDS", 1)
    monkeypatch.setattr(sleep.settings, "phone_sleep_seconds", 0)
    searched = []

    async def ask(port, queries, timeout, email=True):
        searched.append(queries)
        return [{"query": q, "found": ""} for q in queries]

    async def adb(slot, *args, **kwargs):
        return b"5555\n" if args[0] == "forward" else await fake.adb(slot, *args, **kwargs)

    async def command_result(*args, **kwargs):
        return b"", b"", 0

    monkeypatch.setattr(home, "ask", ask)
    monkeypatch.setattr(watch.core, "adb", adb)
    monkeypatch.setattr(watch.core, "command_result", command_result)
    async with httpx.AsyncClient(base_url="http://gateway", transport=httpx.ASGITransport(app=gateway.app)) as c:
        # Being restarted: the search waits, and is answered once the phone is ready.
        waiting = asyncio.create_task(c.post("/devices/device-1/home/search", json={"queries": ["torch"]}))
        await asyncio.sleep(0.05)
        assert not waiting.done() and not searched
        watch.present("device-1", True)
        assert (await waiting).json() == {"results": [{"query": "torch", "found": ""}]}
        # Gone and not coming back in time: it says the phone is starting, and nothing is sent to it.
        watch.present("device-1", False)
        before = len(fake.commands)
        refused = await c.post("/devices/device-1/web/search", json={"queries": ["torch"]})
        assert refused.status_code == 503 and "starting" in refused.json()["detail"]
        assert len(fake.commands) == before


async def test_waking_a_frozen_phone_does_not_wait_on_it(phone, monkeypatch):
    watch, fake, asked = phone
    from app.gateway import sleep

    monkeypatch.setattr(sleep, "state", {"device-1": sleep.DOZING})
    monkeypatch.setattr(sleep.settings, "phone_sleep_seconds", 15)
    watch.frozen.add("device-1")
    before = len(fake.commands)
    assert await sleep.wake("device-1") is False
    assert len(fake.commands) == before  # nothing sent to a phone that answers nothing
