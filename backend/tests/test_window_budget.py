"""The emulator's hidden windows are kept few (app/gateway/budget.py): the computer's OpenGL driver keeps a pool of
them that grows as Android kills app processes, the phone stops drawing each time the pool doubles, and only a new
emulator process empties it. Past a count the gateway has the desktop app restart the phone (a cold boot): when
nobody uses it, or, past a higher count, between two requests of whatever holds it."""

import asyncio
import sys

import pytest


@pytest.fixture
def phone(monkeypatch):
    from app.gateway import budget, core, sleep, watch

    slot = "device-1"
    monkeypatch.setattr(core, "slots", {slot: "emulator-5600"})
    monkeypatch.setattr(watch, "states", {slot: {"state": watch.READY, "detail": "", "since": 1.0}})
    monkeypatch.setattr(watch, "setup", {})
    monkeypatch.setattr(watch, "_news", None)  # made in another test's event loop
    for name in ("inflight", "paths", "_quiet"):
        getattr(budget, name).clear()
    for name in ("pending", "restarting", "last", "_driver"):
        monkeypatch.setattr(budget, name, {})
    monkeypatch.setattr(budget, "no_use", set())
    monkeypatch.setattr(budget, "QUIET_POLL_SECONDS", 0.01)
    monkeypatch.setattr(budget.settings, "phone_windows_soft", 350)
    monkeypatch.setattr(budget.settings, "phone_windows_hard", 600)

    class Phone:
        count = 100  # the emulator's windows
        leased = False
        restarted = []  # the counts at which the phone was restarted

    monkeypatch.setattr(budget, "windows", lambda serial: Phone.count)
    monkeypatch.setattr(sleep, "leased", lambda s: Phone.leased)
    monkeypatch.setattr(sleep, "state", {slot: sleep.DOZING})
    monkeypatch.setattr(sleep, "holds", {slot: 0})
    monkeypatch.setattr(sleep.settings, "phone_sleep_seconds", 15)

    async def restart(s, count):
        budget.last[s] = budget.time.monotonic()
        Phone.restarted.append(count)
        await asyncio.sleep(0.05)  # the emulator stops and starts
        Phone.count = 120  # a new process: the pool is empty

    Phone.restarted = []
    monkeypatch.setattr(budget, "_restart", restart)
    return budget, Phone, slot


async def settle(condition, seconds=2):
    deadline = asyncio.get_running_loop().time() + seconds
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, "the gateway's tasks did not get there"
        await asyncio.sleep(0.01)


async def test_a_phone_with_few_windows_is_left_alone(phone):
    budget, emulator, slot = phone
    emulator.count = 349
    await budget.check(slot)
    assert not budget.pending and not emulator.restarted


async def test_past_the_limit_the_emulator_is_started_again_once_nobody_uses_the_phone(phone):
    budget, emulator, slot = phone
    emulator.count, emulator.leased = 400, True  # a task holds the phone
    await budget.check(slot)
    await asyncio.sleep(0.05)
    assert slot in budget.pending and not emulator.restarted  # not under a task: its moment has not come
    emulator.leased = False  # the task ended, and the phone is asleep
    await settle(lambda: emulator.restarted)
    assert emulator.restarted == [400]
    await settle(lambda: slot not in budget.pending)
    # The new process starts with an empty pool: nothing more to do.
    await budget.check(slot)
    assert not budget.pending


async def test_an_awake_phone_without_a_lease_is_still_in_use(phone):
    budget, emulator, slot = phone
    from app.gateway import sleep

    emulator.count = 400
    sleep.state[slot] = sleep.AWAKE  # the user is around: a task may start any moment
    await budget.check(slot)
    await asyncio.sleep(0.05)
    assert not emulator.restarted
    sleep.state[slot] = sleep.OFF
    await settle(lambda: emulator.restarted)


async def test_past_the_hard_limit_it_is_started_again_between_two_requests_of_a_running_task(phone):
    budget, emulator, slot = phone
    emulator.count, emulator.leased = 650, True
    order = []

    async def step(name, seconds):
        async with budget.using(slot, name):
            order.append(f"{name} on the phone with {emulator.count} windows")
            await asyncio.sleep(seconds)

    first = asyncio.create_task(step("first step", 0.1))
    await asyncio.sleep(0.01)
    await budget.check(slot)
    await asyncio.sleep(0.05)
    assert not emulator.restarted  # never under a request
    await first
    await settle(lambda: slot in budget.restarting)
    second = asyncio.create_task(step("second step", 0))  # arrives while the emulator is down
    await asyncio.sleep(0.01)
    assert len(order) == 1  # held, not failed
    await second
    assert emulator.restarted == [650]
    assert order == ["first step on the phone with 650 windows", "second step on the phone with 120 windows"]


async def test_a_sign_in_being_driven_is_not_interrupted(phone):
    budget, emulator, slot = phone
    from app.gateway import sleep

    emulator.count, sleep.holds[slot] = 650, 1
    await budget.check(slot)
    await asyncio.sleep(0.05)
    assert not emulator.restarted
    sleep.holds[slot] = 0
    await settle(lambda: emulator.restarted)


async def test_never_twice_in_a_row(phone):
    budget, emulator, slot = phone
    emulator.count = 400
    await budget.check(slot)
    await settle(lambda: emulator.restarted and slot not in budget.pending)
    emulator.count = 700  # high again at once: not the pool (a new process starts empty), so it is only logged
    await budget.check(slot)
    await asyncio.sleep(0.05)
    assert emulator.restarted == [400] and not budget.pending


async def test_a_pool_emptied_by_another_restart_ends_the_wait(phone):
    budget, emulator, slot = phone
    emulator.count, emulator.leased = 400, True
    await budget.check(slot)
    await asyncio.sleep(0.03)
    emulator.count = 110  # the phone was restarted for another reason meanwhile
    await settle(lambda: slot not in budget.pending)
    assert not emulator.restarted


async def test_the_count_is_unknown_off_windows_and_nothing_is_done(phone, monkeypatch):
    budget, emulator, slot = phone
    monkeypatch.undo()
    monkeypatch.setattr(budget.sys, "platform", "darwin")
    assert budget.windows("emulator-5600") is None


async def test_the_desktop_app_is_asked_and_the_phone_waited_for(phone, monkeypatch):
    budget, emulator, slot = phone
    from app.gateway import watch

    monkeypatch.undo()  # the real _restart
    monkeypatch.setattr(budget, "last", {})
    monkeypatch.setattr(budget, "no_use", set())
    monkeypatch.setattr(watch, "states", {slot: {"state": watch.READY, "detail": "", "since": 1.0}})
    monkeypatch.setattr(watch, "setup", {})
    monkeypatch.setattr(watch, "_news", None)
    monkeypatch.setattr(budget.core, "slots", {slot: "emulator-5600"})
    monkeypatch.setattr(budget, "windows", lambda serial: 123)
    monkeypatch.setattr(budget.settings, "desktop_control_url", "http://desktop")
    monkeypatch.setattr(budget.settings, "desktop_phone_token", "secret")
    asked, sent = [], []

    class Reply:
        status_code = 200

        def json(self):
            return {"restarted": True, "pids": [1]}

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers):
            asked.append((url, headers["Authorization"]))
            # adb loses the phone and finds it again a moment later
            watch._set(slot, watch.OFFLINE)
            asyncio.get_running_loop().call_later(0.05, watch._set, slot, watch.READY)
            return Reply()

    async def adb(s, *args, **kwargs):
        sent.append(" ".join(args))
        return b""

    monkeypatch.setattr(budget.httpx, "AsyncClient", Client)
    monkeypatch.setattr(budget.core, "adb", adb)
    await budget._restart(slot, 689)
    assert asked == [("http://desktop/phone/renew", "Bearer secret")]
    assert watch.ready(slot) and sent == ["shell sync"]  # its files written out first, then the phone waited for
    assert slot in budget.last and not budget.no_use  # 123 windows now: the restart emptied the pool

    # A new emulator that holds as many at once: that is no pool that grew, and it is not restarted for it again.
    monkeypatch.setattr(budget, "windows", lambda serial: 700)
    monkeypatch.setattr(budget, "last", {})
    await budget._restart(slot, 700)
    assert budget.no_use == {slot} and len(asked) == 2
    await budget.check(slot)
    assert not budget.pending


@pytest.mark.skipif(sys.platform != "win32", reason="the count is read from Windows")
def test_only_an_emulator_that_draws_through_the_measured_driver_is_counted(phone, monkeypatch):
    budget, emulator, slot = phone
    monkeypatch.undo()  # the real windows()
    monkeypatch.setattr(budget, "_driver", {})
    monkeypatch.setattr(budget.settings, "phone_windows_soft", 350)
    monkeypatch.setattr(budget, "emulator_pids", lambda serial: [41])
    held, asked = {"count": 200}, []
    monkeypatch.setattr(budget, "_user_objects", lambda pid: held["count"])

    def loaded(pid, module):
        asked.append((pid, module))
        return pid == 41

    monkeypatch.setattr(budget, "_loaded", loaded)
    assert budget.windows("emulator-5600") == 200 and not asked  # few: nothing to decide yet
    held["count"] = 500
    assert budget.windows("emulator-5600") == 500 and budget.windows("emulator-5600") == 500
    assert asked == [(41, budget.DRIVER)]  # asked once per emulator process
    monkeypatch.setattr(budget, "emulator_pids", lambda serial: [42])  # another driver's emulator
    assert budget.windows("emulator-5600") is None
