"""What a service starts ends with it, however the service ends.

The services start long-running children: adb shells held open for as long as the phone is up (the install watch in
gateway/grants.py, the screen recording, the call-audio helper), ffmpeg. On Windows nothing ends a child when its
parent ends, and the desktop app ends a service by killing it (and `exit_with_parent` by os._exit), so no cleanup
runs. Every restart of the gateway left its adb shells running, each holding a root process on the phone: 15 of
them by 2026-11-09. The venv's python.exe launcher keeps its own job, but that job lets the service's children
break away silently, so it does not help.

`die_with_this_process()` puts the service in a job object that kills every process in it when the service's last
handle to it closes, which Windows does when the service exits by any means. Children join the job when they start.
The one exception is adb's server, which other programs share: the call that may start it passes `OUTLIVE` so the
server it forks stays out of the job.

Elsewhere this does nothing: the services run on Windows, and a POSIX parent's death closes its children's pipes."""

import logging
import sys

log = logging.getLogger("processes")

_job = None  # kept open for the life of the process: closing it would end every process in the job
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def joined():
    return _job is not None


def outlive_flags():
    """creationflags for a child that must outlive this service (adb's server): 0 outside the job."""
    return CREATE_BREAKAWAY_FROM_JOB if _job is not None else 0


def die_with_this_process():
    """Put this process, and so every process it starts from now on, in a job that ends with it. Best effort:
    a failure is logged and the service runs as before."""
    global _job
    if sys.platform != "win32" or _job is not None:
        return
    import ctypes
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class Extended(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", ctypes.c_uint64 * 6),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        log.warning("could not create a job for this service's processes: error %s", ctypes.get_last_error())
        return
    info = Extended()
    # Kill everything in the job when it closes; let a child that asks for it (adb's server) start outside.
    info.BasicLimitInformation.LimitFlags = 0x2000 | 0x800  # KILL_ON_JOB_CLOSE | BREAKAWAY_OK
    if not (kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))  # Extended limits
            and kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess())):
        log.warning("could not tie this service's processes to it: error %s", ctypes.get_last_error())
        kernel32.CloseHandle(job)
        return
    _job = job
