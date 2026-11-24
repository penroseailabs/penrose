"""The phone runtime waits out Android's "isn't responding" dialog itself, so the agent spends no turns on it."""

from types import SimpleNamespace

from app.bridge.screen import Element, Screen
from app.runtime import phone
from app.runtime.phone import PhoneAgent


def screen(dialog):
    elements = {}
    if dialog:
        node = {"id": "android:id/aerr_wait", "b": [100, 200, 300, 260]}
        elements[1] = Element(n=1, h=0, node=node, window="Clock isn't responding", role="Button", label="Wait")
    return Screen(text="", elements=elements, snapshot={}, header={})


def agent(screens):
    """A stand-in with just what the method uses: each look shows the next screen."""
    taps = []

    async def tap(x, y):
        taps.append((x, y))

    fake = SimpleNamespace(notices=[], anr_waits=0, taps=taps)
    fake.driver = SimpleNamespace(screen=screens.pop(0), device=SimpleNamespace(tap=tap))

    async def observe(after_action):
        fake.driver.screen = screens.pop(0) if screens else fake.driver.screen

    fake.observe = observe
    fake.not_responding = lambda: PhoneAgent.not_responding(fake)
    return fake


async def test_waits_out_a_slow_app(monkeypatch):
    monkeypatch.setattr(phone, "ANR_PAUSE_SECONDS", 0)
    fake = agent([screen(True), screen(False)])
    await PhoneAgent.wait_out_not_responding(fake)
    assert fake.taps == [(200, 230)]  # the Wait button's centre
    assert fake.anr_waits == 1
    assert fake.notices == []


async def test_leaves_a_stuck_app_to_the_agent(monkeypatch):
    monkeypatch.setattr(phone, "ANR_PAUSE_SECONDS", 0)
    fake = agent([screen(True)])  # every look shows the dialog again
    await PhoneAgent.wait_out_not_responding(fake)
    assert len(fake.taps) == phone.ANR_WAITS
    assert len(fake.notices) == 1 and "Close app" in fake.notices[0]


async def test_nothing_to_do_without_the_dialog():
    fake = agent([screen(False)])
    await PhoneAgent.wait_out_not_responding(fake)
    assert fake.taps == [] and fake.notices == []
