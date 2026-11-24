"""What a service starts ends with it, however it ends (app/processes.py); adb's server, started with OUTLIVE, stays."""

import subprocess
import sys
import textwrap
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="job objects are Windows only")

SERVICE = textwrap.dedent("""
    import subprocess, sys, time
    from app import processes
    processes.die_with_this_process()
    assert processes.joined()
    held = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    kept = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                            creationflags=processes.outlive_flags())
    print(held.pid, kept.pid, flush=True)
    time.sleep(60)
""")


def alive(pid):
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
    return str(pid) in out


def test_killing_a_service_ends_what_it_started_except_what_must_outlive_it():
    service = subprocess.Popen([sys.executable, "-c", SERVICE], stdout=subprocess.PIPE, text=True)
    held, kept = map(int, service.stdout.readline().split())
    try:
        service.kill()  # as the desktop app ends a service: no cleanup of its own runs
        service.wait()
        for _ in range(20):
            if not alive(held):
                break
            time.sleep(0.1)
        assert not alive(held)
        assert alive(kept)
    finally:
        subprocess.run(["taskkill", "/F", "/PID", str(kept)], capture_output=True)
        subprocess.run(["taskkill", "/F", "/PID", str(held)], capture_output=True)


def test_outside_a_job_nothing_asks_to_break_away():
    from app import processes

    if not processes.joined():
        assert processes.outlive_flags() == 0
