"""The phone's kernel on its fast clock. Under Windows' hypervisor it can give up the processor's counter (tsc)
for the emulated timer (hpet) and is then 1.5 to 2 times slower until it starts again. device-ready starts such a
kernel again before the phone is in use (app/provision, fast_clock); the gateway only keeps the kernel's own
words when it happens to a running phone (app/gateway/watch.py), and restarts nothing."""

import logging

import pytest
from app import provision
from app.provision import adbui

GUARDED = "console=ttynull bootconfig tsc=nowatchdog"
PLAIN = "console=ttynull bootconfig clocksource=pit"


class Kernel:
    """A phone whose kernel answers with a clock source and a command line, one pair for each boot."""

    def __init__(self, *boots):
        self.boots = list(boots)
        self.restarts = 0
        self.asked = []

    def su(self, serial, command, **kwargs):
        self.asked.append(command)
        source, cmdline = self.boots[min(self.restarts, len(self.boots) - 1)]
        if command.startswith("dmesg"):
            return "clocksource: timekeeping watchdog on CPU3: Marking clocksource 'tsc' as unstable\n"
        return f"{source}\n{cmdline}\n"

    def restart(self, serial):
        self.restarts += 1


@pytest.fixture
def said(monkeypatch):
    lines, steps = [], []
    monkeypatch.setattr(provision, "log", lines.append)
    monkeypatch.setattr(provision, "step", lambda step_id, title: steps.append(title))
    return lines, steps


def check(monkeypatch, kernel, guarded=True):
    monkeypatch.setattr(adbui, "su", kernel.su)
    return provision.fast_clock("emulator-5600", guarded, restart=kernel.restart)


def test_a_kernel_on_its_fast_clock_is_left_alone(monkeypatch, said):
    kernel = Kernel(("tsc", GUARDED))
    assert check(monkeypatch, kernel) == 0 and kernel.restarts == 0 and said == ([], [])
    # Another computer's clock (KVM on Linux, a Mac's counter) is none of this.
    for source in ("kvm-clock", "arch_sys_counter"):
        assert check(monkeypatch, Kernel((source, PLAIN)), guarded=False) == 0


def test_a_kernel_that_came_up_on_the_slow_clock_is_started_again_until_it_is_fast(monkeypatch, said):
    lines, steps = said
    kernel = Kernel(("hpet", GUARDED), ("hpet", GUARDED), ("tsc", GUARDED))  # a boot lands on hpet about once in three
    assert check(monkeypatch, kernel) == 2 and kernel.restarts == 2
    assert steps == ["Restarting your phone"] * 2
    # The kernel's own words are kept each time, while they are still in its log.
    assert sum("Marking clocksource 'tsc' as unstable" in line for line in lines) == 2
    assert "fast clock (tsc) after 2 restart(s)" in lines[-1]


def test_a_kernel_that_stays_slow_is_used_as_it_is_after_a_few_restarts(monkeypatch, said):
    lines, _ = said
    kernel = Kernel(("hpet", GUARDED))
    most = provision.CLOCK_RESTARTS
    assert check(monkeypatch, kernel) == most and kernel.restarts == most  # never a loop: the phone still works, slower
    assert 3 <= most <= 6 and f"still on its slow clock after {most} restarts" in lines[-1]


def test_a_kernel_from_an_older_snapshot_is_started_once_to_get_the_option(monkeypatch, said):
    lines, _ = said
    # Resumed from a quick-boot snapshot saved before the emulator was started with tsc=nowatchdog: on tsc for now,
    # with the comparison that drops it still running.
    kernel = Kernel(("tsc", PLAIN), ("tsc", GUARDED))
    assert check(monkeypatch, kernel) == 1 and "started without tsc=nowatchdog" in lines[0]
    # An emulator that was not started with the option (adopted from an earlier build, another computer's
    # hypervisor): a restart would not add it, so there is none.
    assert check(monkeypatch, Kernel(("tsc", PLAIN)), guarded=False) == 0
    # And when a restart did not bring it (the option never reached the kernel), once is all.
    stuck = Kernel(("tsc", PLAIN))
    assert check(monkeypatch, stuck) == 1 and stuck.restarts == 1


def test_a_phone_that_does_not_say_is_left_alone(monkeypatch, said):
    class Silent(Kernel):
        def su(self, serial, command, **kwargs):
            return "su: inaccessible or not found\n"

    assert check(monkeypatch, Silent(("", ""))) == 0
    assert provision.read_clock("emulator-5600") == ("", "")


def test_device_ready_takes_the_flag(monkeypatch):
    seen = {}
    monkeypatch.setattr(provision, "device_ready",
                        lambda layout, serial, wait, assets, build_id, guarded: seen.update(guarded=guarded))
    monkeypatch.setattr(provision, "emit", lambda *a, **k: None)
    assert provision.main(["device-ready", "--home", "x", "--fast-clock"]) == 0 and seen == {"guarded": True}
    assert provision.main(["device-ready", "--home", "x"]) == 0 and seen == {"guarded": False}


def test_the_clock_is_checked_after_the_update_that_restarts_the_phone(tmp_path, monkeypatch):
    """The user's phone, 2026-11-22: checked first and fast, then updated, and the restart the update needs landed
    on the slow clock, where the phone stayed. The check comes after the last restart device-ready makes."""
    from test_provision_reconcile import FakePhone

    class Phone(FakePhone):
        clocks = {2: "hpet"}  # the boot after the update's restart is the slow one

        def su(self, serial, command, **kwargs):
            if command.startswith(f"cat {provision.CLOCK_SOURCE}"):
                self.commands.append(("clock", self.boots))
                return f"{self.clocks.get(self.boots, 'tsc')}\n{GUARDED}\n"
            return super().su(serial, command, **kwargs)

    phone = Phone()
    (tmp_path / "relay-bridge.apk").write_bytes(b"apk v2")
    (tmp_path / "relay-autoscope.zip").write_bytes(b"zip v2")
    for name in ("su", "adb", "shell", "wait_on_phone"):
        monkeypatch.setattr(adbui, name, getattr(phone, name))
    monkeypatch.setattr(adbui, "notification_listener_setup", lambda serial: None)
    monkeypatch.setattr(adbui, "screen_lock_remove", lambda serial, report: None)
    monkeypatch.setattr(adbui, "home_setup", lambda serial: True)
    monkeypatch.setattr(provision, "enable_module", lambda serial: None)
    monkeypatch.setattr(provision, "include_new_apps", lambda work, serial: None)
    monkeypatch.setattr(provision, "wait_boot", lambda serial, wait=0, process=None: None)
    monkeypatch.setattr(provision, "wait_framework", lambda serial: phone.commands.append(("framework", phone.boots)))
    monkeypatch.setattr(provision, "wait_chrome_scoped", lambda serial: phone.commands.append(("scoped", phone.boots)))
    monkeypatch.setattr(provision.time, "sleep", lambda s: None)
    monkeypatch.setattr(provision, "emit", lambda event, **data: None)
    provision.device_ready(provision.Layout(tmp_path / "home"), "emulator-5570", 0, tmp_path, guarded_clock=True)
    # Three boots: the one it started with, the update's, and one more because the update's was slow.
    assert phone.boots == 3 and [c for k, c in phone.commands if k == "clock"] == [2, 3]
    # And what follows a restart (the framework up, the bridge checked) is done on the boot that stays.
    assert ("framework", 3) in phone.commands and ("scoped", 3) in phone.commands


# --- the gateway: a running phone whose clock turns slow is written down, not restarted ---------------------------


class Phone:
    def __init__(self, source):
        self.source = source
        self.commands = []

    async def adb(self, slot, *args, timeout=25, **kwargs):
        command = " ".join(args)
        self.commands.append(command)
        if "dmesg" in command:
            return b"clocksource: Clocksource 'tsc' skewed 387706803 ns (387 ms) over watchdog 'hpet'\n"
        if "current_clocksource" in command:
            if self.source is None:
                raise TimeoutError
            return self.source.encode() + b"\n"
        return b""


async def test_the_gateway_keeps_the_kernels_words_once_when_a_running_phones_clock_turns_slow(monkeypatch, caplog):
    from app.gateway import core, watch

    monkeypatch.setattr(watch, "clocks", {})
    phone = Phone("tsc")
    monkeypatch.setattr(core, "adb", phone.adb)
    asked = []
    monkeypatch.setattr(watch, "ask_restart", lambda slot: asked.append(slot))
    with caplog.at_level(logging.DEBUG, logger=watch.log.name):
        await watch._clock("device-1")
        assert not caplog.records  # fast: nothing to say
        phone.source = "hpet"
        await watch._clock("device-1")
        await watch._clock("device-1")  # still slow a minute later: said once
        errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1 and "hpet, was tsc" in errors[0] and "skewed 387706803 ns" in errors[0]
        assert sum("dmesg" in c for c in phone.commands) == 1
        # A phone that does not answer the question, or has no root: nothing kept, nothing raised.
        phone.source = None
        await watch._clock("device-1")
        phone.source = "su: inaccessible or not found"
        await watch._clock("device-1")
    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1
    assert not asked  # never restarted from here: the phone may be in a task
