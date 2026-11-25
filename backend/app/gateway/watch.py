"""Each phone's state, kept by the gateway as the phone changes, never asked for on every request.

    offline   adb does not list it (not started, or gone)
    starting  listed, and Android is booting or the screen lock still holds its storage
    hooking   up, waiting for relay-bridge in the home app (and restarting the phone when it never comes)
    ready     anyone may lease it
    broken    relay-bridge never came back, even after restarts (framework.py): nothing on it can be read; or the
              phone froze or lost Android again after each of several restarts (MAX_RESTARTS, below)

`setup` is separate: while the desktop app brings the phone up to date (provision device-ready installs, reboots,
restarts System UI), it holds the phone, and leases wait (PUT /devices/{slot}/setup).

What moves a phone from one state to the next, all of it pushed, nothing checked on a timer:
- adb's device list, as adb's own server announces each change (`host:track-devices`, a connection held open).
- One wait that runs on the phone itself, from boot to relay-bridge's socket (a shell loop there, not an adb call
  from here per check).
- A connection held open to relay-bridge in the home app, which always runs: when its process dies (Android's
  system restarted, Magisk dropped Zygisk, the phone rebooted) the connection closes and the phone is checked again.

One thing sends no change at all: an emulator that freezes. adb keeps listing it and every connection to it stays
open. So the one question "does it answer adb?" is asked on a timer, and at once when an adb call to it runs out of
time; a phone that stays silent stops counting as ready and the desktop app is asked to restart it (below). With
the same question: whether Android is still there under a phone that answers (it can be gone for good), and how
many hidden windows the emulator holds (gateway/budget.py).

Waiters (a lease that waits for the phone, the API's update stream) wait on `changed()`."""

import asyncio
import contextlib
import logging
import os
import re
import time

import httpx

from ..config import settings
from . import core, framework

log = logging.getLogger("gateway.watch")

OFFLINE, STARTING, HOOKING, READY, BROKEN = "offline", "starting", "hooking", "ready", "broken"
HOME = framework.HOME
HOME_SOCKET = "@relaybridge." + HOME
# A booted phone with Relay's screen-lock PIN unlocks within seconds (relay-bridge's unlocker in System UI).
LOCK_GRACE = framework.LOCK_GRACE
HOOK_GRACE = framework.HOOK_GRACE
ADB_PORT = 5037
# An adb call that failed while adb still lists the phone: check it again after this, doubling up to the most.
RETRY_SECONDS, MAX_RETRY_SECONDS = 1, 30

states = {}  # slot -> {"state", "detail", "since"}
setup = {}  # slot -> when the desktop app said it began updating the phone
dimensions = core.dimensions
tasks = {}  # slot -> the task checking or watching the phone
_news = None  # asyncio.Event, replaced on each change
_version = 0
_tracker = None
_on_ready = []  # callbacks(slot) for a phone that has just become ready


def state(slot):
    return states.get(slot, {}).get("state", OFFLINE)


def ready(slot):
    """Leasable: up, hooked, and not being set up."""
    return state(slot) == READY and slot not in setup


def view(slot):
    """The phone as the window and the agents see it."""
    s = states.get(slot, {"state": OFFLINE, "detail": "", "since": 0})
    return {"state": "setup" if slot in setup and s["state"] != OFFLINE else s["state"], "detail": s["detail"],
            "since": s["since"], "connected": ready(slot)}


def version():
    return _version


def _event():
    global _news
    if _news is None:
        _news = asyncio.Event()
    return _news


def notify():
    """Something changed (a phone's state, a lease, setup): wake everyone waiting."""
    global _news, _version
    _version += 1
    if _news is not None:
        _news.set()
    _news = asyncio.Event()


async def changed(seconds):
    """Wait for the next change, at most `seconds`. True when something changed. Take this before reading the
    state you will decide on: call `listen()` first, then read, then `wait(listened, seconds)`."""
    return await wait(listen(), seconds)


def listen():
    return _event()


async def wait(event, seconds):
    if seconds <= 0:
        return event.is_set()
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(event.wait(), seconds)
        return True
    return False


def _set(slot, new, detail=""):
    old = states.get(slot)
    if old and old["state"] == new and old["detail"] == detail:
        return
    states[slot] = {"state": new, "detail": detail, "since": time.time()}
    if not old or old["state"] != new:
        log.info("%s: %s%s", slot, new, f" ({detail})" if detail else "")
    notify()


def set_setup(slot, active):
    """The desktop app began (True) or finished (False) updating the phone."""
    if active and slot not in setup:
        setup[slot] = time.time()
        log.info("%s: setup is updating the phone; leases wait", slot)
        notify()
    elif not active and slot in setup:
        del setup[slot]
        log.info("%s: setup finished", slot)
        notify()


def on_ready(callback):
    _on_ready.append(callback)


# --- adb's device list ------------------------------------------------------------------------------------------

async def _adb_server():
    """A connection to adb's server, started if it is not running."""
    port = int(os.environ.get("ANDROID_ADB_SERVER_PORT") or ADB_PORT)
    try:
        return await asyncio.open_connection("127.0.0.1", port)
    except OSError:
        await core.process_result(settings.adb_path, "start-server", timeout=30, outlive=True)
        return await asyncio.open_connection("127.0.0.1", port)


async def track():
    """adb's server writes the whole device list each time it changes; the phones follow it."""
    pause = 0.5
    while True:
        writer = None
        try:
            reader, writer = await _adb_server()
            request = b"host:track-devices"
            writer.write(b"%04x" % len(request) + request)
            await writer.drain()
            if await reader.readexactly(4) != b"OKAY":
                raise ConnectionError("adb refused to track devices")
            pause = 0.5
            while True:
                size = int(await reader.readexactly(4), 16)
                listing = (await reader.readexactly(size)).decode(errors="replace") if size else ""
                seen = dict(line.split("\t")[:2] for line in listing.splitlines() if line.count("\t") >= 1)
                for slot, serial in core.slots.items():
                    present(slot, seen.get(serial) == "device")
                    listed(slot, seen.get(serial))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # adb's server restarted or is starting: the phones are unknown until it lists them
            log.debug("adb device tracking: %s", exc)
        finally:
            if writer:
                writer.close()
        for slot in core.slots:
            present(slot, False)
            listed(slot, None)
        await asyncio.sleep(pause)
        pause = min(pause * 2, 5)


def present(slot, up):
    if up:
        if slot not in tasks or tasks[slot].done():
            tasks[slot] = asyncio.create_task(_follow(slot))
        if settings.phone_alive_seconds > 0 and (slot not in watchdogs or watchdogs[slot].done()):
            watchdogs[slot] = asyncio.create_task(_alive(slot))
        return
    for running in (tasks, watchdogs):
        if task := running.pop(slot, None):
            task.cancel()
    frozen.discard(slot)
    if state(slot) != OFFLINE:
        _set(slot, OFFLINE)
        # Its processes, forwards and sockets are gone with it.
        asyncio.create_task(core.phones[slot].reset())


# --- one phone, from boot to ready, and while ready ----------------------------------------------------------------

def _until(condition):
    """A shell loop that runs on the phone until `condition` holds: one adb call, however long it takes."""
    return f"until {condition}; do sleep 0.2; done"


async def _on_phone(slot, script, seconds):
    """Run `script` on the phone; False when it did not finish within `seconds` (the loop is ended with it)."""
    try:
        await core.adb(slot, "shell", script, timeout=seconds)
        return True
    except TimeoutError:
        return False


async def _follow(slot):
    """Check the phone until it is ready, then hold relay-bridge in its home app until that closes; again."""
    pause = RETRY_SECONDS
    while True:
        try:
            await _check(slot)
            if state(slot) != READY:
                return  # restarting (the tracker brings it back) or given up on
            pause = RETRY_SECONDS
            await _hold(slot)
            # The home app's process ended: Android restarted under it, or the phone is going down.
            await core.phones[slot].reset()
            _set(slot, STARTING, "Android restarted")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # adb still lists the phone (else the tracker would have cancelled this), so no change will come from
            # there to start the checks again: an adb call failed in passing (a phone still coming up). Try again.
            log.warning("%s: could not check the phone (again in %d s): %s", slot, pause, exc)
            _set(slot, STARTING, str(exc)[:200])
            await asyncio.sleep(pause)
            pause = min(pause * 2, MAX_RETRY_SECONDS)


async def _check(slot):
    _set(slot, STARTING, "booting")
    while not await _on_phone(slot, _until('[ "$(getprop sys.boot_completed)" = 1 ]'), 600):
        pass
    pin = (await core.adb(slot, "shell", "getprop persist.relay.lock_pin", timeout=10)).strip() != b""
    _set(slot, STARTING, "unlocking")
    # Without a PIN, storage unlocks with the boot. With Relay's PIN, relay-bridge's unlocker enters it.
    if not await _on_phone(slot, _until('[ "$(getprop sys.user.0.ce_available)" = true ]'), LOCK_GRACE if pin else 600):
        _restart(slot, f"still locked {LOCK_GRACE} s after booting: relay-bridge's unlocker is not running")
        return
    _set(slot, HOOKING)
    answer = await core.adb(slot, "shell", f"pm path {HOME}", include_stderr=True, timeout=10)
    installed = b"package:" in answer
    if not installed and b"find service" in answer:
        # "cmd: Can't find service: package": Android is not up under a phone that says it booted (system_server
        # is starting again, or gone for good). Not "no home app": that counted such a phone as ready.
        raise RuntimeError("Android's package service is not up")
    if installed:
        # The home app is the home screen and starts with Android; its relay-bridge socket says Vector is in Android.
        if not await _on_phone(slot, _until(f"grep -q '{HOME_SOCKET}$' /proc/net/unix"), HOOK_GRACE):
            _restart(slot, f"the home app has run {HOOK_GRACE} s and has no relay-bridge")
            return
        try:
            await core.phones[slot].call("hello", process=HOME, timeout=10)
        except Exception as exc:
            _restart(slot, f"relay-bridge in the home app does not answer: {exc}")
            return
    else:
        log.warning("%s: no Relay home app, so relay-bridge cannot be checked; counting the phone as ready", slot)
    size = (await core.adb(slot, "shell", "wm size", timeout=10)).decode(errors="replace")
    if matches := re.findall(r"(\d+)x(\d+)", size):
        dimensions[slot] = dict(zip(("width", "height"), map(int, matches[-1]), strict=True))
    if framework.failed_restarts.pop(slot, 0):
        log.info("%s: relay-bridge is back after restarting the phone", slot)
    _set(slot, READY)
    for callback in _on_ready:
        with contextlib.suppress(Exception):
            await callback(slot)


def _restart(slot, why):
    if slot in setup:
        # The desktop app's update reboots and restarts System UI itself; judge the phone once it is done.
        _set(slot, HOOKING, "waiting for setup to finish")
        tasks[slot] = asyncio.create_task(_after_setup(slot))
        return
    if framework.failed_restarts.get(slot, 0) >= framework.MAX_FAILED_RESTARTS:
        _set(slot, BROKEN, why)
    else:
        _set(slot, STARTING, "restarting: " + why)
    framework.restart(slot, why)


async def _after_setup(slot):
    while slot in setup:
        await changed(60)
    await _follow(slot)


async def _hold(slot):
    """Until the home app's relay-bridge closes its connection. Without the home app, until adb loses the phone."""
    if b"package:" not in await core.adb(slot, "shell", f"pm path {HOME}", timeout=10):
        await asyncio.Event().wait()  # cancelled by the tracker when the phone goes
    port = (await core.adb(slot, "forward", "tcp:0", f"localabstract:relaybridge.{HOME}", timeout=10)).decode().strip()
    reader = writer = None
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", int(port))
        writer.write(b'{"cmd":"hello","id":1}\n')
        await writer.drain()
        if not await reader.readline():
            raise ConnectionError("relay-bridge closed the connection")
        while await reader.read(4096):  # nothing more is sent; it ends when the home app's process does
            pass
    except (OSError, ValueError) as exc:
        # Not even connected: check the phone again shortly rather than at once, over and over.
        log.warning("%s: could not hold relay-bridge in the home app: %s", slot, exc)
        await asyncio.sleep(2)
    finally:
        if writer:
            writer.close()
        with contextlib.suppress(Exception):
            await core.adb(slot, "forward", "--remove", f"tcp:{port}", timeout=5)


# --- a phone that stops answering ----------------------------------------------------------------------------------
# Seen 2026-11-09: this computer ran out of virtual memory, the emulator could not allocate and froze with its
# process still there. adb listed it, the phone counted as ready, and for nine minutes every call to it (waking it,
# the home app's search, Chrome, each step) ran into its own timeout, until the task asked the user to look.

ALIVE_TIMEOUT = 10  # seconds an answer may take: a dozing phone whose memory was handed back answers in under one
ALIVE_TRIES = 3  # unanswered in a row before the phone counts as frozen
ALIVE_PAUSE = 3  # seconds between those
FROZEN = "restarting: the phone stopped responding"
watchdogs = {}  # slot -> the task that asks
doubts = {}  # slot -> asyncio.Event: an adb call to the phone ran out of time, so it is asked now
frozen = set()  # slots that stopped answering, until they answer again or adb loses them


def doubt(slot):
    """An adb call to this phone got no answer in time: ask now whether it answers at all. Most such calls are
    waits that ran out on a healthy phone (a lock screen still up), and then the question costs one round trip."""
    if event := doubts.get(slot):
        event.set()


core.unanswered.append(doubt)


async def _answers(slot):
    """Whether the phone answers adb at all (its adb daemon, no part of Android above it). An adb error, as from
    a phone that is going down, raises: the device list reports that."""
    try:
        return b"alive" in await core.adb(slot, "shell", "echo alive", timeout=ALIVE_TIMEOUT)
    except TimeoutError:
        return False


async def _alive(slot):
    """While adb lists the phone: ask whether it answers, now, every phone_alive_seconds, and whenever an adb call
    to it ran out of time. Silent ALIVE_TRIES times in a row, it is frozen."""
    doubted = doubts[slot] = asyncio.Event()
    try:
        while True:
            try:
                silent = 0
                while silent < ALIVE_TRIES and not await _answers(slot):
                    silent += 1
                    await asyncio.sleep(ALIVE_PAUSE)
                if silent == ALIVE_TRIES:
                    await _frozen(slot)
                else:
                    if slot in frozen:
                        _thawed(slot)
                    await _clock(slot)
                    if await _android_gone(slot):
                        await _stopped(slot)
                    else:
                        _steady(slot)
                        for check in alive_checks:
                            await check(slot)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # adb itself failed: the phone is going or gone, and the device list says so
                log.debug("%s: could not ask the phone whether it answers: %s", slot, exc)
            doubted.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(doubted.wait(), settings.phone_alive_seconds)
    finally:
        if doubts.get(slot) is doubted:
            del doubts[slot]


# The phone's clock. Its kernel can give up the processor's counter (tsc) for the emulated timer (hpet), and is
# then 1.5 to 2 times slower until it starts again (why, and what starts it on a kernel that does not:
# app/provision, fast_clock). The kernel says why in one line that scrolls out of its log within hours, so each
# time it happened the reason was gone when someone looked. Asked with the phone's "do you answer", it costs one
# more command a minute. Nothing is restarted for it here: the phone may be in a task, or about to get one from a
# notification.
CLOCK = "su -c 'cat /sys/devices/system/clocksource/clocksource0/current_clocksource'"
CLOCK_SAID = "su -c 'dmesg | grep -v audit | grep -iE \"clocksource|tsc\" | tail -n 8'"  # not SELinux's lines about the file
clocks = {}  # slot -> the clock source last read


async def _clock(slot):
    """Keep the kernel's own words when the phone's clock turns to the slow one (once for each time it does)."""
    try:
        source = (await core.adb(slot, "shell", CLOCK, timeout=ALIVE_TIMEOUT)).decode(errors="replace").strip()
        before, clocks[slot] = clocks.get(slot), source
        if source != "hpet" or before == "hpet":
            return
        said = (await core.adb(slot, "shell", CLOCK_SAID, timeout=ALIVE_TIMEOUT)).decode(errors="replace")
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # no root, a phone going down: nothing to keep
        log.debug("%s: could not read the phone's clock source: %s", slot, exc)
        return
    log.error("%s: the phone's kernel is on its slow clock (hpet%s) and stays on it until the phone starts again. "
              "Kernel: %s", slot, f", was {before}" if before else ", as found", " | ".join(line.strip() for line in said.splitlines() if line.strip()) or "nothing in its log")


# --- an Android that is gone under a phone that still answers -------------------------------------------------------
# Seen 2026-11-26: the emulator's drawing stalled (gateway/budget.py), Android's watchdog ended system_server, and
# system_server stayed a zombie: one of its threads never left the kernel, so Android never started again. adb
# answered every question, the phone counted as ready, and nothing was done for 40 minutes. Android normally
# starts again by itself within seconds of losing system_server; only one that stays gone is restarted here, the
# same way as a frozen phone (its emulator ended and started: a cold boot, nothing written to the phone is lost).

ANDROID = "getprop sys.boot_completed; service check activity"  # "1", then "Service activity: found" / "not found"
ANDROID_GRACE = 90  # seconds Android may be gone after it had booted: longer than its own restart takes
ANDROID_PAUSE = 10  # between the questions meanwhile
STOPPED = "restarting: Android stopped"
alive_checks = []  # callbacks(slot), asked with "do you answer" on a phone whose Android is up (gateway/budget.py)


async def _android_up(slot):
    """True: Android's services answer. False: the phone booted and they are gone. None: still booting (or the
    question got no usable answer: nothing is decided on that)."""
    try:
        said = (await core.adb(slot, "shell", ANDROID, timeout=ALIVE_TIMEOUT)).decode(errors="replace").split("\n", 1)
    except TimeoutError:
        return None
    if said[0].strip() != "1" or len(said) < 2 or "Service activity" not in said[1]:
        return None
    return "not found" not in said[1]


async def _android_gone(slot):
    """Whether Android has been gone for ANDROID_GRACE on a phone that had booted. While the desktop app updates
    the phone it restarts Android itself (device-ready), so nothing is judged then."""
    if slot in setup or await _android_up(slot) is not False:
        return False
    deadline = time.monotonic() + ANDROID_GRACE
    while time.monotonic() < deadline:
        await asyncio.sleep(ANDROID_PAUSE)
        if slot in setup or await _android_up(slot) is not False:
            log.info("%s: Android was gone for a moment and is starting again by itself", slot)
            return False
    return True


async def _stopped(slot):
    if _spent(slot):
        return
    _set(slot, STARTING, STOPPED)
    log.error("%s: the phone answers adb, but Android has been gone for %d s (no activity service) and is not "
              "starting again. Asking the desktop app to restart the phone", slot, ANDROID_GRACE)
    await ask_restart(slot)


# --- a phone adb lists but cannot talk to --------------------------------------------------------------------------
# Seen 2026-11-26: an emulator started from its quick-boot snapshot came up "offline" on adb and stayed so (its
# console answered, restarting adb's server changed nothing). To everything above that is a phone that has not
# started, so nothing was done and the window said "Starting" for 35 minutes. A booting emulator is offline for
# seconds; one that is listed and still no device after UNREACHABLE_SECONDS is restarted like a frozen one, and
# asked about again that often while it stays so (the desktop app restarts at most once in ten minutes).

UNREACHABLE_SECONDS = 120
UNREACHABLE = "restarting: the phone is offline on adb"
unreachable = {}  # slot -> the task counting, while adb lists the phone as something other than a device


def listed(slot, status):
    """adb's word for the phone ("device", "offline", "unauthorized"...), or None when it does not list it."""
    if status and status != "device":
        if slot not in unreachable or unreachable[slot].done():
            unreachable[slot] = asyncio.create_task(_unreachable(slot, status))
    elif task := unreachable.pop(slot, None):
        task.cancel()


async def _unreachable(slot, status):
    while True:
        await asyncio.sleep(UNREACHABLE_SECONDS)
        if _spent(slot):
            return
        _set(slot, STARTING, UNREACHABLE)
        log.error("%s: adb has listed the phone as %s for %d s: it is not coming up. Asking the desktop app to "
                  "restart it", slot, status, UNREACHABLE_SECONDS)
        await ask_restart(slot)


# Never a loop. The desktop app restarts a phone at most once in ten minutes (PHONE_RESTART_GAP_MS), and here the
# restarts are counted: a phone that froze or lost Android again after each of MAX_RESTARTS restarts in a row is
# left as it is and shown as broken, for the user to look at. A phone that then stays ready for STEADY_SECONDS
# counts as recovered, and so does one the user starts again with the app (this process starts with it).
MAX_RESTARTS = 3
STEADY_SECONDS = 600
restarted = {}  # slot -> restarts the desktop app did since the phone was last steady


def _spent(slot):
    """Whether this phone has used up its restarts; then it is marked broken (once) and not restarted again."""
    if restarted.get(slot, 0) < MAX_RESTARTS:
        return False
    if state(slot) != BROKEN:
        log.error("%s: restarted %d times in a row and it stopped again each time: not restarting it again",
                  slot, MAX_RESTARTS)
        _set(slot, BROKEN, f"the phone stopped again after each of {MAX_RESTARTS} restarts")
    return True


def _steady(slot):
    if slot in restarted and state(slot) == READY and time.time() - states[slot]["since"] >= STEADY_SECONDS:
        del restarted[slot]
        log.info("%s: the phone has stayed up since it was restarted", slot)


async def _frozen(slot):
    """The phone does not answer: it stops counting as ready, and the desktop app is asked to restart it. When it
    is restarted, adb loses it and finds it again, and it is followed from boot as any phone. Left running (no
    desktop app, or one that will not restart it again so soon), it is asked again later: see _thawed."""
    first = slot not in frozen
    frozen.add(slot)
    if _spent(slot):
        return
    _set(slot, STARTING, FROZEN)
    if first:
        log.error("%s: adb lists the phone, but it did not answer %d times in a row (%d s each): it is frozen. "
                  "Asking the desktop app to restart it", slot, ALIVE_TRIES, ALIVE_TIMEOUT)
    await ask_restart(slot)


def _thawed(slot):
    """A phone counted as frozen answers again (it was that slow, on a computer short of memory): check it afresh."""
    frozen.discard(slot)
    log.warning("%s: the phone answers again", slot)
    if task := tasks.get(slot):
        task.cancel()
    tasks[slot] = asyncio.create_task(_follow(slot))


_asking = {}  # slot -> the request to the desktop app in flight


async def ask_restart(slot):
    """Ask the desktop app, which owns the emulator, to end the frozen one and start the phone again (it finds
    the emulator's processes itself). When it does, adb loses the phone and the task that asked ends with it,
    usually before the answer is back: the request is a task of its own, so its answer is still read (a restart
    that was done is counted, see MAX_RESTARTS); an answer also says why the phone was not restarted."""
    if slot not in _asking or _asking[slot].done():
        _asking[slot] = asyncio.create_task(_ask_restart(slot))
    await asyncio.shield(_asking[slot])


async def _ask_restart(slot):
    if not (settings.desktop_control_url and settings.desktop_phone_token):
        log.error("%s: frozen, and no desktop app to restart it", slot)
        return
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            reply = await client.post(f"{settings.desktop_control_url}/phone/restart",
                                      headers={"Authorization": f"Bearer {settings.desktop_phone_token}"})
        log.warning("%s: the desktop app answered the restart request: %s", slot, reply.text[:200])
        with contextlib.suppress(ValueError, AttributeError):
            if reply.json().get("restarted"):
                restarted[slot] = restarted.get(slot, 0) + 1
    except (httpx.HTTPError, OSError) as exc:
        log.error("%s: could not ask the desktop app to restart the phone: %s", slot, exc)


async def until_ready(slot, seconds):
    """True once the phone is ready, waiting up to `seconds` for one that is starting or being restarted. For what
    uses the phone without a lease (the look-up's searches); a lease waits the same way (devices.acquire)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while True:
        news = listen()  # before reading the state, so a change meanwhile still wakes this
        if ready(slot) or state(slot) == BROKEN:
            return ready(slot)
        if not await wait(news, deadline - loop.time()):
            return ready(slot)


async def start():
    global _tracker
    if core.slots and _tracker is None:
        _tracker = asyncio.create_task(track())


async def close():
    global _tracker
    for task in [_tracker, *tasks.values(), *watchdogs.values(), *_asking.values(), *unreachable.values()]:
        if task:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
    _tracker = None
    tasks.clear()
    watchdogs.clear()
    _asking.clear()
    unreachable.clear()
    frozen.clear()
