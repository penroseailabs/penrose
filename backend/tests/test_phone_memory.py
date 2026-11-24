"""A dozing phone's emulator hands its memory back to this computer (app/gateway/memory.py)."""

import os
import sys

import pytest
from app.gateway import emulator_frames, memory


@pytest.fixture
def discovery(tmp_path, monkeypatch):
    monkeypatch.setattr(emulator_frames, "_discovery_dirs", lambda: [tmp_path])
    return tmp_path


def test_emulator_pids_come_from_the_discovery_file_names(discovery):
    (discovery / "pid_24744.ini").write_text("port.serial=5570\ngrpc.port=8570\ngrpc.token=t\n")
    (discovery / "pid_1200.ini").write_text("port.serial=5570\n")  # left by one that crashed
    (discovery / "pid_999.ini").write_text("port.serial=5600\n")
    (discovery / "pid_x.ini").write_text("port.serial=5570\n")
    assert sorted(emulator_frames.emulator_pids("emulator-5570")) == [1200, 24744]
    assert emulator_frames.emulator_pids("emulator-5590") == []
    assert emulator_frames.discover("emulator-5570") == (8570, "t")


async def test_nothing_to_trim_without_a_running_emulator(discovery):
    assert await memory.trim("emulator-5570") is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_a_process_that_is_no_emulator_is_left_alone():
    # A stale discovery file's process id may now belong to anything: only qemu is trimmed.
    assert memory._trim_process(os.getpid()) is None
    assert memory._trim_process(0x7FFFFFF0) is None  # gone


@pytest.mark.skipif(sys.platform == "win32", reason="the other platforms")
async def test_does_nothing_off_windows(discovery):
    (discovery / f"pid_{os.getpid()}.ini").write_text("port.serial=5570\n")
    assert await memory.trim("emulator-5570") is False
