"""`adbui.screen_lock_remove`: the old fixed screen-lock PIN comes off older phones after boot, while Relay's own random
PIN for simplified sign-ins (gateway/simple_signins.py) stays, with adb and the clock faked."""

import pytest
from app.provision import adbui


class LockPhone:
    """Answers adb like a phone whose keyguard either goes away by itself (the bridge enters the PIN) or stays."""

    def __init__(self, credential="NONE", unlocker=True, relay_pin=False):
        self.credential = credential
        self.unlocker = unlocker
        self.relay_pin = relay_pin
        self.keyguard = False
        self.commands = []

    def shell(self, serial, command, **kwargs):
        self.commands.append(command)
        if command == "dumpsys lock_settings":
            return f"  CredentialType: {self.credential}\n"
        if command.startswith("dumpsys window"):
            return f"    isKeyguardShowing={'true' if self.keyguard else 'false'}\n"
        if command == "getprop sys.user.0.ce_available":
            return "true\n"
        if command == f"getprop {adbui.PIN_PROP}":
            return "73019265\n" if self.relay_pin else "\n"
        if command.startswith("locksettings set-pin"):
            self.credential = "PIN"
        elif command.startswith("locksettings clear"):
            self.credential = "NONE"
        elif command == "input keyevent 223":
            self.keyguard = True
        elif command == "input keyevent 224" and self.credential == "PIN" and self.unlocker:
            self.keyguard = False
        elif "wm dismiss-keyguard" in command and self.credential == "NONE":
            self.keyguard = False
        return ""

    def su(self, serial, command, **kwargs):
        self.commands.append("su " + command)
        return ""


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(adbui.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(adbui.time, "sleep", lambda s: now.__setitem__(0, now[0] + s))
    return now


def install(monkeypatch, phone):
    monkeypatch.setattr(adbui, "shell", phone.shell)
    monkeypatch.setattr(adbui, "su", phone.su)
    return phone


def test_the_old_pin_is_removed_after_boot(monkeypatch, clock):
    phone = install(monkeypatch, LockPhone(credential="PIN"))
    phone.keyguard = True
    assert adbui.screen_lock_remove("emulator-5570", report=pytest.fail) is True
    assert f"locksettings clear --old {adbui.OLD_PIN}" in phone.commands
    assert phone.credential == "NONE" and not phone.keyguard
    assert not any("pkill" in c or "scope" in c for c in phone.commands)


def test_relays_own_pin_for_simplified_signins_stays(monkeypatch, clock):
    phone = install(monkeypatch, LockPhone(credential="PIN", relay_pin=True))
    assert adbui.screen_lock_remove("emulator-5570", report=pytest.fail) is True
    assert phone.credential == "PIN"
    assert not any(c.startswith("locksettings") for c in phone.commands)


def test_a_phone_without_a_lock_is_left_alone_by_the_removal(monkeypatch, clock):
    phone = install(monkeypatch, LockPhone())
    assert adbui.screen_lock_remove("emulator-5570", report=pytest.fail) is True
    assert phone.commands == ["dumpsys lock_settings"]


def test_the_removal_leaves_a_lock_the_user_chose(monkeypatch, clock):
    phone = install(monkeypatch, LockPhone(credential="PATTERN"))
    warnings = []
    assert adbui.screen_lock_remove("emulator-5570", report=warnings.append) is False
    assert not any(c.startswith("locksettings") for c in phone.commands)
    assert warnings and "PATTERN" in warnings[0]
