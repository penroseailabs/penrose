"""Start the phone's emulator on a desktop of its own (Windows): `relay-server emulator [--detach] [--log FILE] -- <emulator> <arguments>`.

Why. With `-gpu host` the emulator draws through the computer's OpenGL driver, and that driver keeps one hidden
top-level window for each drawing surface (NVIDIA: class NVOpenGLPbuffer). An app process that Android kills leaves
about six of them behind, in a pool that only grows and that the driver doubles each time it runs out (64, 128 ...
4096). On the desktop the user works on, creating one such window took 57 to 121 ms (measured 2026-11-26, any
program, more with every window already there), and while the driver makes a pool's worth of them nothing in the
phone is drawn: 9 s at 64, 26 to 37 s at 256, 86 to 165 s at 1,024, where Android's watchdog ends system_server
("the phone crashed"). The same windows also made every other program's windows slow to open.

A desktop nobody looks at has no other windows: there a window took 0.45 ms, the phone ran the same test with no
pause over 3 s up to a pool of 512 and started apps twice as fast, and none of its windows are on the user's
desktop. It still draws on the graphics card, with a hidden window (`-qt-hide-window`) or none (`-no-window`).
The pool still grows there, so the gateway keeps the count down (gateway/budget.py).

The emulator is told which desktop to use when it is created, and what it starts (qemu, its helpers) follows it.
Node cannot say that, so the desktop app starts the emulator through this command:

- as the app's own phone: this process waits for the emulator and ends with its exit code, passing on what it
  writes, so the app follows it as it would follow the emulator itself;
- `--detach` (development's phone, test phones): this process leaves once the emulator has a window on the
  desktop, which keeps the desktop alive from then on.

The emulator is handed its output and nothing else of this process's (an explicit handle list): a phone that
outlives the app must not keep one of the app's ports open. If the desktop cannot be made, the emulator starts on
the user's desktop as before, and one line says so. Elsewhere than Windows this only starts the emulator.

No imports beyond the standard library: scripts/test_phone.py runs this file by itself.
"""

import argparse
import subprocess
import sys

DESKTOP = "penrose-phone"
READY_WAIT_SECONDS = 60  # --detach: how long to hold the desktop for the emulator's first window


def _windows_start(command, desktop, detach, log):
    import ctypes
    import msvcrt
    import time
    from ctypes import wintypes as w

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class StartupInfo(ctypes.Structure):
        _fields_ = [("cb", w.DWORD), ("lpReserved", w.LPWSTR), ("lpDesktop", w.LPWSTR), ("lpTitle", w.LPWSTR),
                    ("dwX", w.DWORD), ("dwY", w.DWORD), ("dwXSize", w.DWORD), ("dwYSize", w.DWORD),
                    ("dwXCountChars", w.DWORD), ("dwYCountChars", w.DWORD), ("dwFillAttribute", w.DWORD),
                    ("dwFlags", w.DWORD), ("wShowWindow", w.WORD), ("cbReserved2", w.WORD),
                    ("lpReserved2", ctypes.c_void_p), ("hStdInput", w.HANDLE), ("hStdOutput", w.HANDLE),
                    ("hStdError", w.HANDLE)]

    class StartupInfoEx(ctypes.Structure):
        _fields_ = [("StartupInfo", StartupInfo), ("lpAttributeList", ctypes.c_void_p)]

    class ProcessInformation(ctypes.Structure):
        _fields_ = [("hProcess", w.HANDLE), ("hThread", w.HANDLE), ("dwProcessId", w.DWORD),
                    ("dwThreadId", w.DWORD)]

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [("nLength", w.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p), ("bInheritHandle", w.BOOL)]

    user32.CreateDesktopW.restype = w.HANDLE
    user32.CreateDesktopW.argtypes = [w.LPCWSTR, w.LPCWSTR, ctypes.c_void_p, w.DWORD, w.DWORD, ctypes.c_void_p]
    user32.CloseDesktop.argtypes = [w.HANDLE]
    user32.GetProcessWindowStation.restype = w.HANDLE
    user32.GetUserObjectInformationW.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD,
                                                 ctypes.POINTER(w.DWORD)]
    enum_proc = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    user32.EnumDesktopWindows.argtypes = [w.HANDLE, enum_proc, w.LPARAM]
    kernel32.CreateFileW.restype = w.HANDLE
    kernel32.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.POINTER(SecurityAttributes), w.DWORD,
                                     w.DWORD, w.HANDLE]
    kernel32.SetHandleInformation.argtypes = [w.HANDLE, w.DWORD, w.DWORD]
    kernel32.InitializeProcThreadAttributeList.argtypes = [ctypes.c_void_p, w.DWORD, w.DWORD,
                                                           ctypes.POINTER(ctypes.c_size_t)]
    kernel32.UpdateProcThreadAttribute.argtypes = [ctypes.c_void_p, w.DWORD, ctypes.c_size_t, ctypes.c_void_p,
                                                   ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p]
    kernel32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    kernel32.CreateProcessW.restype = w.BOOL
    kernel32.CreateProcessW.argtypes = [w.LPCWSTR, w.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, w.BOOL, w.DWORD,
                                        ctypes.c_void_p, w.LPCWSTR, ctypes.c_void_p,
                                        ctypes.POINTER(ProcessInformation)]
    kernel32.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
    kernel32.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
    kernel32.CloseHandle.argtypes = [w.HANDLE]

    GENERIC_ALL, GENERIC_READ, FILE_APPEND_DATA = 0x10000000, 0x80000000, 0x0004
    SHARE_ALL, OPEN_EXISTING, OPEN_ALWAYS = 0x7, 3, 4
    HANDLE_FLAG_INHERIT, STARTF_USESTDHANDLES = 0x1, 0x100
    EXTENDED_STARTUPINFO_PRESENT, CREATE_NO_WINDOW, CREATE_NEW_PROCESS_GROUP = 0x00080000, 0x08000000, 0x200
    PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
    INVALID = w.HANDLE(-1).value
    inherit = SecurityAttributes(ctypes.sizeof(SecurityAttributes), None, True)

    def opened(path, access, how):
        handle = kernel32.CreateFileW(path, access, SHARE_ALL, ctypes.byref(inherit), how, 0, None)
        return None if handle in (None, INVALID) else handle

    def own(stream):
        """This process's output handle, made inheritable; None when it has none (started with no output)."""
        try:
            handle = msvcrt.get_osfhandle(stream.fileno())
        except (OSError, ValueError, AttributeError):
            return None
        return handle if kernel32.SetHandleInformation(handle, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT) else None

    # The desktop: made, or opened when an emulator already runs on it. Named with its window station (WinSta0).
    desk = user32.CreateDesktopW(desktop, None, None, 0, GENERIC_ALL, None)
    where = None
    if desk:
        name, needed = ctypes.create_unicode_buffer(256), w.DWORD()
        station = user32.GetProcessWindowStation()
        if station and user32.GetUserObjectInformationW(station, 2, name, ctypes.sizeof(name), ctypes.byref(needed)):
            where = f"{name.value}\\{desktop}"
    if not where:
        print(f"phone_desktop: no desktop of its own ({ctypes.get_last_error()}): the emulator starts on this one",
              file=sys.stderr, flush=True)

    nul_in = opened("NUL", GENERIC_READ, OPEN_EXISTING)
    out = err = opened(log, FILE_APPEND_DATA, OPEN_ALWAYS) if log else None
    if not log:
        out, err = own(sys.stdout), own(sys.stderr)
    nul_out = opened("NUL", FILE_APPEND_DATA, OPEN_EXISTING) if out is None or err is None else None
    out, err = out if out is not None else nul_out, err if err is not None else nul_out

    startup = StartupInfoEx()
    startup.StartupInfo.cb = ctypes.sizeof(StartupInfoEx)
    startup.StartupInfo.lpDesktop = where
    startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES
    startup.StartupInfo.hStdInput, startup.StartupInfo.hStdOutput, startup.StartupInfo.hStdError = nul_in, out, err
    handles = list(dict.fromkeys(h for h in (nul_in, out, err) if h is not None))
    listed = (w.HANDLE * len(handles))(*handles)
    size = ctypes.c_size_t()
    kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
    attributes = ctypes.create_string_buffer(size.value)
    if not kernel32.InitializeProcThreadAttributeList(attributes, 1, 0, ctypes.byref(size)) or \
            not kernel32.UpdateProcThreadAttribute(attributes, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST, listed,
                                                   ctypes.sizeof(listed), None, None):
        raise OSError(ctypes.get_last_error(), "could not prepare the emulator's handle list")
    startup.lpAttributeList = ctypes.addressof(attributes)

    info = ProcessInformation()
    line = ctypes.create_unicode_buffer(subprocess.list2cmdline(command))
    # A console without a window, which the emulator's helpers (qemu, netsimd) share: with none at all each of
    # them would open its own. Its own process group: Ctrl+C in a terminal that started this does not reach it.
    flags = EXTENDED_STARTUPINFO_PRESENT | CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    try:
        if not kernel32.CreateProcessW(None, line, None, None, True, flags, None, None, ctypes.byref(startup),
                                       ctypes.byref(info)):
            raise OSError(ctypes.get_last_error(), f"could not start {command[0]}")
    finally:
        kernel32.DeleteProcThreadAttributeList(attributes)
    kernel32.CloseHandle(info.hThread)

    def exit_code():
        code = w.DWORD()
        kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
        return code.value

    if not detach:
        kernel32.WaitForSingleObject(info.hProcess, 0xFFFFFFFF)
        return exit_code()
    # Detached: this process holds the desktop only until the emulator has a window on it; an emulator that ends
    # before that (a wrong argument) is reported with its exit code.
    seen = []
    collect = enum_proc(lambda window, _: seen.append(window) or False)
    deadline = time.monotonic() + READY_WAIT_SECONDS
    while desk and where and time.monotonic() < deadline:
        if kernel32.WaitForSingleObject(info.hProcess, 200) == 0:
            return exit_code()
        user32.EnumDesktopWindows(desk, collect, 0)
        if seen:
            break
    return 0


def start(command, desktop=DESKTOP, detach=False, log=None):
    """Start `command` (the emulator and its arguments); the exit code to end with."""
    if sys.platform == "win32":
        return _windows_start(command, desktop, detach, log)
    if detach:
        out = open(log, "ab") if log else subprocess.DEVNULL  # noqa: SIM115 (the emulator keeps it)
        subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                         start_new_session=True)
        return 0
    return subprocess.call(command)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="relay-server emulator", description=__doc__.split("\n\n")[0])
    parser.add_argument("--detach", action="store_true", help="leave the emulator running and return")
    parser.add_argument("--log", help="append what the emulator writes to this file (with --detach)")
    parser.add_argument("--desktop", default=DESKTOP, help="the desktop's name (Windows)")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- <emulator> <arguments>")
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("no emulator command given")
    return start(command, args.desktop, args.detach, args.log)


if __name__ == "__main__":
    sys.exit(main())
