"""The phone sleeps when nobody uses it, and wakes before anything does.

Left on, the emulator keeps drawing whatever an app shows (a page animating in Chrome held a core for days), and
apps keep syncing in the background. So, once nobody holds a lease on the phone and nothing has used it for
`phone_sleep_seconds`, the gateway presses Home and turns the screen off; `phone_doze_seconds` later it puts the
phone in Doze (Android's deep sleep: apps' background jobs, alarms and network wait for a maintenance window).
Measured on a test phone, the emulator went from about 30% of a core awake and idle to about 3% asleep.
Once it has dozed for `phone_trim_after_seconds` it also hands its memory back to this computer (gateway/memory.py):
7 GB down to a few hundred MB. Not at once: a phone woken after that may have to read its memory back from disk.

Notifications still arrive in Doze, in two ways (measured on a phone in forced Doze, 2026-11-06). What an app's
server sends as urgent (a message, an email, a delivery) comes through Play services at once. Everything else
Google holds back while Play services reports the phone dozing, and hands over in Android's maintenance window,
which Doze opens by itself for about half a minute: after 15 minutes, then 30, then an hour, up to six. So the
gateway sets that pace before it forces Doze (`phone_doze_window_seconds`, the same gap every time): nothing
waits longer than that, and no timer here has to bring the phone out of Doze.

Anything that uses the phone wakes it first: a lease for a run or a Claude Code session, the ops lane, the bridge,
the home app's search, sign-ins (gateway/__init__.py's middleware, gateway/devices.py's lease routes). Earlier
still, signs that the user is about to ask for something wake it and keep it awake a while (`keep_awake`): the
penrose window or phone app coming up, typing, a task starting (api/devices.py presence, the worker). Waking
takes a fraction of a second, but apps coming out of Doze need a few more before their searches are quick. The user's
own tap on a sleeping phone only wakes it (gateway/actions.py), so it cannot land on a screen they have not seen.
With Relay's screen-lock PIN, relay-bridge's unlocker enters it as the keyguard shows, as after any screen-off.

The phone's own screen timeout stays off (stay_on_while_plugged_in): only the gateway turns the screen off, so it
never goes dark under a running task. The state is this process's: after a restart it is unknown, and the first
wake sends its commands anyway (on an awake phone they do nothing)."""

import asyncio
import contextlib
import logging
import time
from collections import defaultdict

from ..config import settings
from . import core, memory, watch, web

log = logging.getLogger("gateway.sleep")

AWAKE, OFF, DOZING = "awake", "off", "dozing"
# After the phone first comes up, the countdown waits this much longer: the desktop app is still bringing the
# phone up to date (provision device-ready) and the window is still opening.
STARTUP_GRACE_SECONDS = 120
KEYGUARD_WAIT_SECONDS = 5

state = {}  # slot -> AWAKE, OFF or DOZING; missing: unknown
timers = {}  # slot -> the task counting down to sleep
holds = defaultdict(int)  # slot -> work that needs the phone awake without a lease (a Google sign-in being driven)
awake_until = {}  # slot -> when (time.monotonic) the user's presence stops keeping the phone awake
locks = defaultdict(asyncio.Lock)


def enabled():
    return settings.phone_sleep_seconds > 0


def doze_pace():
    """The shell commands that make Doze open its maintenance window every phone_doze_window_seconds ('' to
    leave Android's own pace, which grows to six hours)."""
    window = settings.phone_doze_window_seconds * 1000
    if not window:
        return ""
    return (f"cmd device_config put device_idle idle_to {window} >/dev/null 2>&1; "
            f"cmd device_config put device_idle max_idle_to {window} >/dev/null 2>&1; ")


def asleep(slot):
    return state.get(slot) in (OFF, DOZING)


def leased(slot):
    return bool(core.store.rows("SELECT 1 FROM leases WHERE slot=?", (slot,)))


def forget(slot):
    """The phone has just booted or Android restarted: what it was is unknown, so the next wake does everything a
    wake does (Chrome's browser among it), whatever this process last knew."""
    state.pop(slot, None)


def cancel(slot):
    if task := timers.pop(slot, None):
        task.cancel()


def idle(slot, delay=None):
    """Start (or restart) the countdown to sleep, unless someone holds the phone; they call this again when done.
    While the user is around (`keep_awake`), the countdown ends no sooner than their window does."""
    if not enabled() or slot not in core.slots or holds[slot] or leased(slot):
        cancel(slot)
        return
    cancel(slot)
    delay = settings.phone_sleep_seconds if delay is None else delay
    delay = max(delay, awake_until.get(slot, 0) - time.monotonic())
    timers[slot] = asyncio.create_task(_sleep_later(slot, delay))


def keep_awake(slot, seconds):
    """The user is about to use penrose (the window or phone app came up, they are typing, a task started): keep
    the phone awake for `seconds` from now, so the task that follows finds it awake, out of Doze, with apps
    back on the network. Each sign moves the window on; the usual countdown follows it."""
    awake_until[slot] = max(awake_until.get(slot, 0), time.monotonic() + seconds)


async def _sleep_later(slot, delay):
    try:
        await asyncio.sleep(delay)
        if holds[slot] or leased(slot):
            return
        async with locks[slot]:
            state[slot] = OFF  # first: a wake that cuts in must send its commands
            # Home first, so no app is left in front, drawing, when the screen comes back.
            await core.adb(slot, "shell", "input keyevent KEYCODE_HOME; input keyevent KEYCODE_SLEEP", timeout=10)
        await asyncio.sleep(settings.phone_doze_seconds)
        if holds[slot] or leased(slot):
            return
        async with locks[slot]:
            state[slot] = DOZING
            await core.adb(slot, "shell", doze_pace() + "dumpsys deviceidle force-idle deep", timeout=10)
        # Dozing on (phone_trim_after_seconds): the emulator's memory goes back to this computer, and again now and
        # then as it creeps back. Not sooner: what was handed back may have to be read from disk at the wake, which
        # makes the phone slow just when it is wanted. A wake cancels this task, so it stops as soon as anything
        # uses the phone.
        if settings.phone_trim_seconds > 0:
            await asyncio.sleep(settings.phone_trim_after_seconds)
        while settings.phone_trim_seconds > 0 and state.get(slot) == DOZING:
            await memory.trim(core.slots[slot])
            await asyncio.sleep(settings.phone_trim_seconds)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("Could not put %s to sleep: %s", slot, exc)
    finally:
        if timers.get(slot) is asyncio.current_task():
            timers.pop(slot, None)


async def wake(slot):
    """Out of Doze, screen on and keyguard gone, before the phone is used; stops the countdown. True when the
    phone was asleep (or its state unknown)."""
    if not enabled() or slot not in core.slots:
        return False
    cancel(slot)
    if state.get(slot) == AWAKE:
        return False
    if slot in watch.frozen:
        return False  # it answers nothing (watch.py): waiting on it here would only hold up what reports that
    async with locks[slot]:
        if state.get(slot) == AWAKE:
            return False
        try:
            # One call that returns once the keyguard is gone: with a screen-lock PIN it shows until relay-bridge's
            # unlocker has entered it, so the phone waits for that itself (a loop there, not an adb call per check).
            await core.adb(slot, "shell", "dumpsys deviceidle unforce >/dev/null; input keyevent KEYCODE_WAKEUP; "
                           "wm dismiss-keyguard; "
                           "while dumpsys window | grep -q isKeyguardShowing=true; do sleep 0.1; done",
                           timeout=KEYGUARD_WAIT_SECONDS)
            # Before it counts as awake: a look-up that comes meanwhile waits for this lock, then finds Chrome ready.
            await web.ensure_browser(slot)
            state[slot] = AWAKE
        except TimeoutError:
            # Still locked: not marked awake, so the next use tries again; what uses it now sees the lock screen.
            log.warning("%s still shows its lock screen %d s after waking", slot, KEYGUARD_WAIT_SECONDS)
        except Exception as exc:  # the phone is starting or gone: whatever uses it next reports that
            log.warning("Could not wake %s: %s", slot, exc)
        return True


@contextlib.asynccontextmanager
async def held(slot):
    """Keep the phone awake for work that holds no lease."""
    holds[slot] += 1
    try:
        await wake(slot)
        yield
    finally:
        holds[slot] -= 1
        idle(slot)


async def close():
    for slot in list(timers):
        cancel(slot)
