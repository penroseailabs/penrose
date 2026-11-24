import asyncio

import pytest
from app.config import settings
from app.device import driver
from app.store import Store


class _InstantSleep:
    """The asyncio module for app.device.driver, with its pause before a look after an action taken
    instantly so sessions run at test speed. Other modules keep the real sleep."""

    def __getattr__(self, name):
        return getattr(asyncio, name)

    @staticmethod
    async def sleep(seconds, result=None):
        await asyncio.sleep(0)
        return result


@pytest.fixture(autouse=True)
def instant_look(monkeypatch):
    monkeypatch.setattr(driver, "asyncio", _InstantSleep())


@pytest.fixture(autouse=True)
def looks_never_wait_for_loading(monkeypatch):
    """A look after an action hands the screen over at once; test_driver.py turns the loading wait on."""
    monkeypatch.setattr(settings, "loading_wait_seconds", 0)


@pytest.fixture(autouse=True)
def adb_only_as_tests_fake_it(monkeypatch):
    """Tests stand in for the adb program; nothing may reach a real adb server (and through it a real phone) around
    them. test_adbwire.py turns the direct path on against a server of its own."""
    monkeypatch.setattr(settings, "adb_direct", False)


@pytest.fixture(autouse=True)
def phone_never_sleeps(monkeypatch):
    """The gateway's sleep countdown would send real adb commands; test_phone_sleep.py turns it on."""
    monkeypatch.setattr(settings, "phone_sleep_seconds", 0)


@pytest.fixture(autouse=True)
def development_auth(monkeypatch):
    monkeypatch.setattr(settings, "login_username", "")
    monkeypatch.setattr(settings, "login_password", "")


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path)
