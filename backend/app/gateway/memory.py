"""Hand a sleeping phone's memory back to this computer.

Asleep, the emulator keeps every page it ever touched: the phone's RAM (a file-mapped ram.img that Android fills
with its file cache) and its own, about 7 GB for a 6 GB phone. Windows can take that back without stopping
anything: EmptyWorkingSet moves the emulator's pages out of its working set (written to ram.img and the pagefile,
and kept in the standby cache meanwhile), and the phone pulls back in only the pages it touches. Measured on a 4 GB
phone (2026-11-03): 5.0 GB to 30 MB in 0.5 s; asleep it creeps back to about 400 MB in a few minutes; waking it
and switching apps took no longer than without a trim. If this computer needs that memory for something else
meanwhile, the phone reads it back from disk after waking, and that is slow (measured 2026-11-20 on a 6 GB phone,
after 9.5 GB of other files had been read on this computer: the wake took 4.7 s for 0.3 s, four Gmail searches at
once 25 s for 6 s with one failing, frames of 16 s, for about half a minute; a look-up in that time got nothing
from the phone). So the gateway trims only a phone that has dozed for a good while (`phone_trim_after_seconds`,
gateway/sleep.py), not one the user may come back to in a minute.

Windows only: elsewhere trim() does nothing."""

import asyncio
import ctypes
import logging
import sys
from ctypes import wintypes
from pathlib import Path

from .emulator_frames import emulator_pids

log = logging.getLogger("gateway.memory")

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_SET_QUOTA = 0x0100


class _Counters(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _working_set(psapi, handle) -> int:
    counters = _Counters(cb=ctypes.sizeof(_Counters))
    return counters.WorkingSetSize if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb) else 0


def _trim_process(pid: int) -> tuple[int, int] | None:
    """(working set before, after) in bytes, or None when the process is gone or is no emulator (a discovery file
    left by one that crashed, whose process id now belongs to something else)."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                    ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    psapi.EmptyWorkingSet.argtypes = [wintypes.HANDLE]
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Counters), wintypes.DWORD]
    handle = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_SET_QUOTA, False, pid)
    if not handle:
        return None
    try:
        size = wintypes.DWORD(1024)
        name = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size)):
            return None
        if not Path(name.value).name.lower().startswith("qemu-system"):
            return None
        before = _working_set(psapi, handle)
        if not psapi.EmptyWorkingSet(handle):
            raise OSError(ctypes.get_last_error(), "EmptyWorkingSet failed")
        return before, _working_set(psapi, handle)
    finally:
        kernel32.CloseHandle(handle)


def _trim(serial: str) -> list[tuple[int, int]]:
    return [result for pid in emulator_pids(serial) if (result := _trim_process(pid))]


async def trim(serial: str) -> bool:
    """Trim the emulator with this adb serial (emulator-5570); True when one was trimmed. Never raises: a phone
    that keeps its memory is no reason to fail anything."""
    if sys.platform != "win32":
        return False
    try:
        trimmed = await asyncio.to_thread(_trim, serial)
    except Exception as exc:
        log.warning("Could not hand %s's memory back: %s", serial, exc)
        return False
    for before, after in trimmed:
        log.info("Handed %s's memory back: %d MB to %d MB", serial, before >> 20, after >> 20)
    return bool(trimmed)
