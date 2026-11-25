"""The emulator's hidden windows, kept few: past a limit the phone's emulator is restarted (a cold boot, about half
a minute, everything on the phone kept), at a moment the gateway picks.

Why (measured 2026-11-26, written up in app/phone_desktop.py): the computer's OpenGL driver keeps one hidden window
for each of the phone's drawing surfaces, in a pool inside the emulator's process that only grows. Every app
process Android kills leaves about six behind, and each time the pool runs out the driver doubles it (64, 128 ...
4096) while nothing in the phone is drawn. On a desktop of its own (where the emulator runs) that takes under 3 s
up to a pool of 512, 41 s at 1,024, and at 2,000 windows every app start takes three times as long; on the user's
desktop it was 86 to 165 s at 1,024 (Android's watchdog ends system_server) and at 4,096 the phone never came back.
The driver's newer version leaks the same. Only a new emulator process empties the pool; restarting Android inside
the emulator does not.

So the gateway reads the count with its "do you answer" (watch.py; one call into Windows, nothing sent to the
phone) and, past `phone_windows_soft`, has the desktop app restart the emulator:

- at the first moment nobody uses the phone (no lease, no sign-in being driven, asleep): the usual case, a few
  times a day on a busy phone, and nobody sees it;
- past `phone_windows_hard`, between two requests of whatever holds the phone: a string of tasks may leave no idle
  moment, and the doubling that comes next costs minutes and Android itself. The task finds the phone on its home
  screen half a minute later and opens its app again.

Requests that use the phone pass through `using()`: it counts them, so the emulator is never ended under one, and
it holds those that arrive meanwhile until the phone is back.

A plain restart, not the emulator's quick-boot snapshot. Stopped and started through its snapshot a test phone was
back in 7 s with the same screen, but the user's own phone came back offline on adb for good the first time
(2026-11-26), and half a minute now and then is cheaper than a way that can fail like that.

Never a loop: one restart per `GAP_SECONDS` at most here, one per ten minutes in the desktop app, and a new
emulator process starts with an empty pool, so a count that is high again that soon is something else and is only
logged. A phone that does not come back is the business of what watches every start (watch.py, the desktop app's
device-ready).

Only where this was measured: Windows, and an emulator that draws through NVIDIA's OpenGL driver (`DRIVER` is
loaded in its process). The pool is that driver's; what another driver's emulator holds, and whether a restart
gives it back, is not known, so its count is left alone. And a restart that does not bring the count under the
limit is the last one: what the emulator holds then is not a pool that grew."""

import asyncio
import contextlib
import ctypes
import logging
import sys
import time
from collections import Counter, defaultdict
from ctypes import wintypes

import httpx

from ..config import settings
from . import core, sleep, watch
from .emulator_frames import emulator_pids

log = logging.getLogger("gateway.budget")

GAP_SECONDS = 600  # between two restarts of one phone's emulator
BACK_SECONDS = 240  # the phone is waited for this long after its emulator was restarted (a cold boot and its setup)
HELD_SECONDS = 150  # the longest a request waits for a phone that is being restarted
QUIET_POLL_SECONDS = 2  # how often "is anyone using the phone" is looked at when nothing signals it
STUCK_SECONDS = 120  # past the hard limit with never a gap between requests for this long: say which requests
DRIVER = "nvoglv64.dll"  # NVIDIA's OpenGL driver, as a 64-bit process loads it

inflight = defaultdict(int)  # slot -> requests using the phone right now
paths = defaultdict(Counter)  # slot -> those requests by path, said when one holds the emulator up for long
pending = {}  # slot -> the task waiting for its moment
restarting = {}  # slot -> asyncio.Event, set when the phone is back (or given up on)
last = {}  # slot -> when (time.monotonic) its emulator was last restarted for this, or the restart refused
_quiet = defaultdict(asyncio.Event)  # slot -> set each time the last request in flight ends
no_use = set()  # slots whose emulator held as many windows right after a restart: not restarted for this again
_driver = {}  # an emulator's process id -> whether it draws through DRIVER


def _user_objects(pid: int) -> int | None:
    """The process's USER objects (its windows, on whatever desktop they are); None when it is gone."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    user32.GetGuiResources.restype = wintypes.DWORD
    user32.GetGuiResources.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    handle = kernel32.OpenProcess(0x0400, False, pid)  # PROCESS_QUERY_INFORMATION
    if not handle:
        return None
    try:
        return user32.GetGuiResources(handle, 1)  # GR_USEROBJECTS
    finally:
        kernel32.CloseHandle(handle)


def _loaded(pid: int, module: str) -> bool:
    """Whether the process has this DLL loaded (False too when the process cannot be read)."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    psapi.EnumProcessModulesEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                           ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
    psapi.GetModuleBaseNameW.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.LPWSTR, wintypes.DWORD]
    handle = kernel32.OpenProcess(0x0410, False, pid)  # PROCESS_QUERY_INFORMATION | PROCESS_VM_READ
    if not handle:
        return False
    try:
        modules, needed = (ctypes.c_void_p * 4096)(), wintypes.DWORD()
        if not psapi.EnumProcessModulesEx(handle, modules, ctypes.sizeof(modules), ctypes.byref(needed), 0x03):
            return False
        name = ctypes.create_unicode_buffer(260)
        for module_handle in modules[: needed.value // ctypes.sizeof(ctypes.c_void_p)]:
            if psapi.GetModuleBaseNameW(handle, module_handle, name, 260) and name.value.lower() == module:
                return True
        return False
    finally:
        kernel32.CloseHandle(handle)


def windows(serial: str) -> int | None:
    """How many windows this phone's emulator holds; None where that is not known or not this module's business
    (not Windows, no emulator, an emulator that does not draw through DRIVER)."""
    if sys.platform != "win32":
        return None
    counts = {pid: count for pid in emulator_pids(serial) if (count := _user_objects(pid)) is not None}
    if not counts:
        return None
    pid = max(counts, key=counts.get)
    if counts[pid] < max(settings.phone_windows_soft, 1):
        return counts[pid]  # few: whose they are does not matter, and the driver may not be loaded yet
    if pid not in _driver:
        _driver.clear()  # one emulator per phone: an earlier process is gone
        _driver[pid] = _loaded(pid, DRIVER)
        if not _driver[pid]:
            log.warning("%s: its emulator holds %d windows and does not draw through %s: its windows are left alone",
                        serial, counts[pid], DRIVER)
    return counts[pid] if _driver[pid] else None


def unused(slot) -> bool:
    """Nobody is using the phone and nobody is about to: no lease, nothing holding it awake, and asleep."""
    return not sleep.leased(slot) and not sleep.holds[slot] and (sleep.asleep(slot) or not sleep.enabled())


@contextlib.asynccontextmanager
async def using(slot, what=""):
    """Around a request that uses the phone (`what`: its path, for the log): waits out a phone that is being
    restarted, and counts the request so that the emulator is not ended under it."""
    if (back := restarting.get(slot)) is not None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(back.wait(), HELD_SECONDS)
    inflight[slot] += 1
    paths[slot][what] += 1
    try:
        yield
    finally:
        inflight[slot] -= 1
        paths[slot][what] -= 1
        if not inflight[slot]:
            _quiet[slot].set()


async def check(slot):
    """Read the count (asked with the phone's "do you answer") and, past the limit, wait for the moment."""
    soft = settings.phone_windows_soft
    if soft <= 0 or slot in pending or slot in restarting or slot in no_use:
        return
    try:
        count = await asyncio.to_thread(windows, core.slots[slot])
    except Exception as exc:
        log.debug("%s: could not count the emulator's windows: %s", slot, exc)
        return
    if count is None or count < soft:
        return
    since = time.monotonic() - last.get(slot, -GAP_SECONDS)
    if since < GAP_SECONDS:
        log.warning("%s: the emulator holds %d windows %d s after it was restarted for that; left as it is", slot,
                    count, since)
        return
    log.warning("%s: the emulator holds %d windows (limit %d, at once from %d): restarting the phone when %s", slot,
                count, soft, settings.phone_windows_hard,
                "no request is on it" if count >= settings.phone_windows_hard else "nobody uses it")
    pending[slot] = asyncio.create_task(_when_possible(slot))


async def _when_possible(slot):
    """Wait until the emulator may be ended (nobody uses the phone; past the hard limit, no request in flight),
    then have it restarted. The count is read again each time: it only grows, and the limit that applies with it."""
    try:
        waited, said = time.monotonic(), False
        while True:
            count = await asyncio.to_thread(windows, core.slots[slot])
            if count is None or count < settings.phone_windows_soft or not watch.ready(slot):
                return  # the emulator went (a restart of another kind emptied the pool) or the phone is not up
            hard = count >= settings.phone_windows_hard
            _quiet[slot].clear()
            if not inflight[slot] and not sleep.holds[slot] and (hard or unused(slot)):
                break
            if hard and not said and time.monotonic() - waited > STUCK_SECONDS:
                said = True  # once: what never ends is the thing to look at
                log.warning("%s: %d windows, and the phone cannot be restarted: for %d s a request has been on it "
                            "the whole time (%s)", slot, count, STUCK_SECONDS,
                            ", ".join(f"{path} x{n}" for path, n in paths[slot].items() if n) or "a sign-in")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(_quiet[slot].wait(), QUIET_POLL_SECONDS)
        # No await between the decision above and this: a request that arrives from here on waits in using().
        back = restarting[slot] = asyncio.Event()
        try:
            await _restart(slot, count)
        finally:
            del restarting[slot]
            back.set()
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("%s: restarting the phone for its emulator's windows failed", slot)
    finally:
        if pending.get(slot) is asyncio.current_task():
            del pending[slot]


async def _restart(slot, count):
    last[slot] = started = time.monotonic()
    if not (settings.desktop_control_url and settings.desktop_phone_token):
        log.warning("%s: %d windows, and no desktop app to restart the phone", slot, count)
        return
    was = watch.states.get(slot, {}).get("since")
    # What apps wrote in the last seconds is still in the phone's memory: have it written out first.
    with contextlib.suppress(Exception):
        await core.adb(slot, "shell", "sync", timeout=15)
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            reply = await client.post(f"{settings.desktop_control_url}/phone/renew",
                                      headers={"Authorization": f"Bearer {settings.desktop_phone_token}"})
        answer = reply.json() if reply.status_code == 200 else {"restarted": False, "reason": reply.text[:200]}
    except (httpx.HTTPError, OSError, ValueError) as exc:
        log.error("%s: could not ask the desktop app to restart the phone: %s", slot, exc)
        return
    if not answer.get("restarted"):
        log.warning("%s: the desktop app did not restart the phone: %s", slot, answer.get("reason"))
        return
    # adb loses the phone and finds it again; watch.py follows it to ready as after any start.
    back = await _back(slot, was)
    after = await asyncio.to_thread(windows, core.slots[slot])
    took = time.monotonic() - started
    if back:
        log.warning("%s: phone restarted in %.0f s: its emulator held %d windows, now %s", slot, took, count,
                    after if after is not None else "an unknown number")
        if after is not None and after >= settings.phone_windows_soft:
            no_use.add(slot)
            log.warning("%s: a new emulator holds %d windows at once (limit %d): restarting it does not help on "
                        "this computer, and it is not restarted for that again", slot, after,
                        settings.phone_windows_soft)
    else:
        log.error("%s: the phone is not back %.0f s after its emulator was restarted (%d windows before)", slot, took,
                  count)


async def _back(slot, was):
    """True once the phone is ready again. `was` is when the phone that was ended became ready: the one waited
    for became ready after that."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + BACK_SECONDS
    while True:
        news = watch.listen()  # before reading the state, so a change meanwhile still wakes this
        if watch.ready(slot) and watch.states[slot]["since"] != was:
            return True
        if not await watch.wait(news, deadline - loop.time()):
            return False


watch.alive_checks.append(check)


async def close():
    for task in list(pending.values()):
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
    pending.clear()
