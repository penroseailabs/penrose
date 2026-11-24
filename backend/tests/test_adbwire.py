"""bridge/adbwire.py against a stand-in for adb's server: the requests it sends and what it makes of the answers."""

import asyncio
import struct

import pytest
from app.bridge import adbwire
from app.bridge.devices import Adb, DeviceError
from app.config import settings
from app.gateway import core


class Server:
    """Answers like adb's server. `shell`: command -> (stdout, stderr, exit code), None to hang up mid-command,
    "never" to leave it unanswered."""

    def __init__(self, serials=("emulator-5554",), shell=None):
        self.serials, self.shell, self.requests = serials, shell or {}, []

    async def read(self, reader):
        return (await reader.readexactly(int(await reader.readexactly(4), 16))).decode()

    async def serve(self, reader, writer):
        try:
            request = await self.read(reader)
            self.requests.append(request)
            if request.startswith("host:transport:"):
                if request.split(":", 2)[2] not in self.serials:
                    message = b"device not found"
                    writer.write(b"FAIL%04x" % len(message) + message)
                    return
                writer.write(b"OKAY")
                service = await self.read(reader)
                self.requests.append(service)
                writer.write(b"OKAY")
                answer = self.shell.get(service.removeprefix("shell,v2,raw:"), (b"", b"", 0))
                assert await reader.readexactly(5) == struct.pack("<BI", 4, 0)  # end of input, sent first
                if answer is None:
                    writer.write(struct.pack("<BI", 1, 4) + b"part")
                    return
                if answer == "never":
                    await asyncio.sleep(30)
                out, err, code = answer
                for kind, data in ((1, out[:3]), (2, err), (1, out[3:])):
                    if data:
                        writer.write(struct.pack("<BI", kind, len(data)) + data)
                writer.write(struct.pack("<BI", 3, 1) + bytes([code]))
            elif ":forward:" in request:
                writer.write(b"OKAYOKAY" + (b"000550123" if "tcp:0;" in request else b""))
            elif ":killforward:" in request:
                writer.write(b"OKAYOKAY")
            else:
                writer.write(b"FAIL0000")
        finally:
            await writer.drain()
            writer.close()


@pytest.fixture
async def server(monkeypatch):
    fake = Server()
    listener = await asyncio.start_server(fake.serve, "127.0.0.1", 0)
    monkeypatch.setenv("ANDROID_ADB_SERVER_PORT", str(listener.sockets[0].getsockname()[1]))
    monkeypatch.setattr(settings, "adb_direct", True)
    yield fake
    listener.close()


async def test_a_shell_command_returns_both_outputs_and_the_exit_code(server):
    server.shell["echo hi; exit 3"] = (b"hi there\n", b"oops\n", 3)
    assert await adbwire.run(["-s", "emulator-5554", "shell", "echo hi; exit 3"]) == (b"hi there\n", b"oops\n", 3)
    assert server.requests == ["host:transport:emulator-5554", "shell,v2,raw:echo hi; exit 3"]


async def test_arguments_after_shell_are_joined_as_adb_joins_them(server):
    await adbwire.run(["-s", "emulator-5554", "shell", "input", "tap", 5, 6])
    assert server.requests[-1] == "shell,v2,raw:input tap 5 6"


async def test_a_forward_returns_the_port_and_is_removed(server):
    assert await adbwire.run(["-s", "emulator-5554", "forward", "tcp:0", "localabstract:x"]) == (b"50123\n", b"", 0)
    assert await adbwire.run(["-s", "emulator-5554", "forward", "--remove", "tcp:50123"]) == (b"", b"", 0)
    assert server.requests == ["host-serial:emulator-5554:forward:tcp:0;localabstract:x",
                               "host-serial:emulator-5554:killforward:tcp:50123"]


@pytest.mark.parametrize("args", [
    ["-s", "emulator-5554", "push", "a", "b"], ["-s", "emulator-5554", "exec-out", "screencap"],
    ["-s", "emulator-5554", "shell", "-T", "cat"], ["devices"], ["-s", "emulator-5554", "forward", "--list"],
])
async def test_anything_else_is_left_to_the_adb_program(server, args):
    with pytest.raises(adbwire.Unavailable):
        await adbwire.run(args)
    assert server.requests == []


async def test_an_unknown_phone_and_no_server_are_left_to_the_adb_program(server, monkeypatch):
    with pytest.raises(adbwire.Unavailable, match="device not found"):
        await adbwire.run(["-s", "emulator-9999", "shell", "true"])
    monkeypatch.setenv("ANDROID_ADB_SERVER_PORT", "1")
    with pytest.raises(adbwire.Unavailable, match="not answering"):
        await adbwire.run(["-s", "emulator-5554", "shell", "true"])


async def test_turned_off_nothing_is_sent(server, monkeypatch):
    monkeypatch.setattr(settings, "adb_direct", False)
    with pytest.raises(adbwire.Unavailable):
        await adbwire.run(["-s", "emulator-5554", "shell", "true"])
    assert server.requests == []


async def test_a_shell_that_dies_under_the_command_is_a_failure_not_a_second_run(server):
    server.shell["reboot"] = None
    assert await adbwire.run(["-s", "emulator-5554", "shell", "reboot"]) == (b"part", b"", 1)


async def test_a_command_past_its_time_raises_timeout(server):
    server.shell["sleep 9"] = "never"
    with pytest.raises(TimeoutError):
        await adbwire.run(["-s", "emulator-5554", "shell", "sleep 9"], timeout=0.2)


async def test_the_bridge_driver_and_the_gateway_use_it_and_keep_their_errors(server, monkeypatch):
    server.shell["id"] = (b"uid=2000(shell)\n", b"", 0)
    server.shell["false"] = (b"", b"no\n", 1)
    adb = Adb(path="adb-that-does-not-exist", serial="emulator-5554")
    assert await adb.shell("id") == "uid=2000(shell)\n"
    with pytest.raises(DeviceError, match="failed: no"):
        await adb.shell("false")
    assert await adb.run("shell", "false", check=False) == ""
    assert await core.command_result("-s", "emulator-5554", "shell", "id") == (b"uid=2000(shell)\n", b"", 0)

    async def process_result(executable, *args, **kwargs):
        return b"from the program", b"", 0
    monkeypatch.setattr(core, "process_result", process_result)
    assert (await core.command_result("-s", "emulator-5554", "push", "a", "b"))[0] == b"from the program"
    assert (await core.command_result("-s", "emulator-9999", "shell", "id"))[0] == b"from the program"
