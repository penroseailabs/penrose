"""adb without a process per command: the same requests the adb program sends, written straight to adb's server
(127.0.0.1:5037), which is already running and already connected to the phone.

`adb -s X shell ...` starts a program each time: about 40 ms on Windows before the phone hears anything (measured
2026-11-12: `input tap` 68 ms through the program, 33 ms here; the window list of a look 105 ms against 67 ms). A
look at the screen makes one or two such calls and every tap, key and swipe one more. Here a command is one local
connection: the request, the phone's output, the exit code.

Only what the server's protocol makes simple is done here: one shell command, a port forward and its removal.
Everything else (push, install, exec-out, emu), and any failure to reach the server or the phone, stays with the adb
program: `run` raises `Unavailable` and the caller starts adb as before, which also starts a server that is not
running and words the error. `ADB_DIRECT=false` (settings.adb_direct) turns all of it off.

One difference from the adb program, on Windows only: that program rewrites every line end of a shell command's
output to CR LF; here the output is what the phone wrote, as on macOS and Linux."""

import asyncio
import os
import struct

from ..config import settings

ADB_PORT = 5037
STDOUT, STDERR, EXIT = 1, 2, 3
CLOSE_STDIN = 4


class Unavailable(Exception):
    """Not done here: use the adb program. Raised before the phone has been asked to do anything, so the caller
    can never run a command twice by falling back."""


def shell_request(args):
    """(serial, command) when `args`, as given to the adb program, are one plain shell command; else None."""
    serial, args = _serial(args)
    if len(args) < 2 or args[0] != "shell" or args[1].startswith("-"):
        return None
    return serial, " ".join(args[1:])


def _serial(args):
    args = [str(a) for a in args]
    return (args[1], args[2:]) if len(args) >= 2 and args[0] == "-s" else (None, args)


async def run(args, timeout=20):
    """What `adb <args>` prints and returns, without starting adb: (stdout, stderr, exit code), bytes.
    `Unavailable` for anything it does not do; TimeoutError past `timeout`."""
    if not settings.adb_direct:
        raise Unavailable("turned off")
    shell_args = shell_request(args)
    if shell_args:
        return await asyncio.wait_for(_shell(*shell_args), timeout)
    serial, rest = _serial(args)
    if rest[:1] == ["forward"]:
        if len(rest) == 3 and not rest[1].startswith("-"):
            port = await asyncio.wait_for(_host(serial, f"forward:{rest[1]};{rest[2]}", answer=True), timeout)
            return (port + "\n").encode() if port else b"", b"", 0
        if len(rest) == 3 and rest[1] == "--remove":
            await asyncio.wait_for(_host(serial, f"killforward:{rest[2]}"), timeout)
            return b"", b"", 0
    raise Unavailable("not a request made here")


async def _connect(limit=2 ** 16):
    try:
        return await asyncio.open_connection("127.0.0.1", int(os.environ.get("ANDROID_ADB_SERVER_PORT") or ADB_PORT),
                                             limit=limit)
    except OSError as exc:
        raise Unavailable(f"adb's server is not answering: {exc}") from None


async def _status(reader):
    """One OKAY, or `Unavailable` with the server's reason."""
    status = await reader.readexactly(4)
    if status == b"OKAY":
        return
    reason = b""
    if status == b"FAIL":
        reason = await reader.readexactly(int(await reader.readexactly(4), 16))
    raise Unavailable(reason.decode(errors="replace") or f"adb answered {status!r}")


async def _send(reader, writer, text):
    data = text.encode()
    writer.write(b"%04x" % len(data) + data)
    await writer.drain()
    await _status(reader)


async def _shell(serial, command):
    reader, writer = await _connect()
    try:
        try:
            await _send(reader, writer, f"host:transport:{serial}" if serial else "host:transport-any")
            await _send(reader, writer, f"shell,v2,raw:{command}")
        except (OSError, asyncio.IncompleteReadError, ValueError) as exc:
            raise Unavailable(str(exc) or type(exc).__name__) from None
        # From here the phone runs the command: whatever happens, there is no falling back.
        out, err, code = bytearray(), bytearray(), 0
        try:
            # Nothing is typed into the command: it reads end of input, as under the adb program without a console.
            writer.write(struct.pack("<BI", CLOSE_STDIN, 0))
            await writer.drain()
            while True:
                kind, size = struct.unpack("<BI", await reader.readexactly(5))
                data = await reader.readexactly(size) if size else b""
                if kind == STDOUT:
                    out += data
                elif kind == STDERR:
                    err += data
                elif kind == EXIT:
                    code = data[0] if data else 0
                    break
        except (OSError, asyncio.IncompleteReadError):
            # The connection ended without an exit code: the phone's shell died under the command (a reboot, adbd
            # restarting). The adb program returns 1 for this, with what had arrived.
            code = code or 1
        return bytes(out), bytes(err), code
    finally:
        writer.close()


async def _host(serial, request, answer=False):
    """A request adb's server answers itself (forwards). `answer`: the text it sends back (the port of tcp:0)."""
    reader, writer = await _connect()
    try:
        await _send(reader, writer, f"host-serial:{serial}:{request}" if serial else f"host:{request}")
        await _status(reader)  # the second answer: the forward itself
        if not answer:
            return ""
        size = await reader.read(4)  # the chosen port follows only for tcp:0
        return (await reader.readexactly(int(size, 16))).decode() if len(size) == 4 else ""
    except (OSError, asyncio.IncompleteReadError, ValueError) as exc:
        raise Unavailable(str(exc) or type(exc).__name__) from None
    finally:
        writer.close()
