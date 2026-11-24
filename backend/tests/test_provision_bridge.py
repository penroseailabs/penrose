"""relay-bridge is installed on the image build only when the phone's copy differs, and then the phone restarts:
installing it while Vector runs leaves apps started later without the bridge until a reboot."""

import hashlib
from contextlib import closing

import pytest
from app import provision
from app.provision import adbui


def fake_phone(monkeypatch, installed_sha):
    calls = []

    def shell(serial, command, **kwargs):
        if command.startswith("pm path"):
            return "package:/data/app/~~x/app.relay.bridge-y/base.apk\n" if installed_sha else ""
        if command.startswith("sha256sum"):
            return f"{installed_sha}  /data/app/~~x/app.relay.bridge-y/base.apk\n"
        if command == "ps -A":
            return "system 504 1 vectord\n"
        return ""

    monkeypatch.setattr(adbui, "shell", shell)
    monkeypatch.setattr(adbui, "su", lambda serial, command, **k: "zygisk_vector\nrelay_autoscope\n" if "ls" in command else "")
    monkeypatch.setattr(adbui, "adb", lambda serial, *args, **k: calls.append(args) or "Success")
    return calls


def test_the_same_bridge_is_not_installed_again(monkeypatch, tmp_path):
    (tmp_path / "relay-bridge.apk").write_bytes(b"apk")
    calls = fake_phone(monkeypatch, hashlib.sha256(b"apk").hexdigest())
    assert provision.install_framework(tmp_path, None) is False
    assert not [c for c in calls if c[0] == "install"]


def test_a_different_or_missing_bridge_is_installed_and_the_phone_restarts(monkeypatch, tmp_path):
    (tmp_path / "relay-bridge.apk").write_bytes(b"apk v2")
    for on_phone in (hashlib.sha256(b"apk v1").hexdigest(), ""):
        calls = fake_phone(monkeypatch, on_phone)
        assert provision.install_framework(tmp_path, None) is True
        assert [c for c in calls if c[0] == "install"]


def test_each_root_command_travels_in_a_file_of_its_own(monkeypatch):
    """The setup doctor checks the phone while setup runs: a shared script file let one run the other's command."""
    pushed, ran = [], []

    def adb(serial, *args, **kwargs):
        if args[0] == "push":
            pushed.append(args[2])
        else:
            ran.append(args[-1])
        return ""

    monkeypatch.setattr(adbui, "adb", adb)
    adbui.su("emulator-5580", "magisk --install-module /data/local/tmp/zygisk_vector.zip")
    adbui.su("emulator-5580", "id")
    assert len(set(pushed)) == 2
    assert all(f"sh {remote};" in command and f"rm -f {remote}" in command for remote, command in zip(pushed, ran, strict=True))


def test_apps_open_with_am_start_not_monkey(monkeypatch):
    """monkey opens nothing while any app on the phone is not responding, which a busy phone often has."""
    ran = []

    def shell(serial, command, **kwargs):
        ran.append(command)
        return "priority=0 preferredOrder=0\ncom.android.chrome/com.google.android.apps.chrome.Main\n" if "resolve-activity" in command else ""

    monkeypatch.setattr(adbui, "shell", shell)
    adbui.launch("emulator-5580", "com.android.chrome")
    assert ran[-1].startswith("am start -n com.android.chrome/com.google.android.apps.chrome.Main")
    assert not any("monkey" in c for c in ran)


def test_a_phone_that_boots_locked_gets_systemui_restarted_once(monkeypatch):
    """SystemUI can start before Vector is ready on a busy computer; started again, it gets the bridge."""
    calls = []
    unlocked = iter([False, True])
    monkeypatch.setattr(adbui, "wait_on_phone", lambda serial, condition, timeout, **kw: True)
    monkeypatch.setattr(provision.time, "sleep", lambda s: None)
    monkeypatch.setattr(adbui, "wait_unlocked", lambda serial, timeout: next(unlocked))
    monkeypatch.setattr(adbui, "restart_systemui", lambda serial: calls.append("restart"))
    monkeypatch.setattr(adbui, "shell", lambda *a, **k: "")
    monkeypatch.setattr(provision, "log", lambda text: None)
    monkeypatch.setattr(provision, "wait_services", lambda serial: True)
    provision.wait_boot("emulator-5570", wait=60)
    assert calls == ["restart"]


def test_the_scope_is_put_back_after_vectors_database_restore(monkeypatch, tmp_path):
    """The restore brings back the scope as it was at the backup; autoscope's boot pass may have landed in between,
    and it does not run again until the next boot. So its pass runs right after the restore."""
    import sqlite3

    ran = []

    def su(serial, command, **kwargs):
        ran.append(command)
        if "db backup" in command:
            return "backed up"
        if "db restore" in command:
            return "restored"
        return ""

    def adb(serial, *args, **kwargs):
        if args[0] == "pull":
            with closing(sqlite3.connect(args[2])) as db, db:
                db.execute("CREATE TABLE IF NOT EXISTS modules (module_pkg_name TEXT, auto_include INT)")
                db.execute("DELETE FROM modules")
                db.execute("INSERT INTO modules VALUES ('app.relay.bridge', ?)", (int(any("db restore" in c for c in ran)),))
        return ""

    monkeypatch.setattr(adbui, "su", su)
    monkeypatch.setattr(adbui, "adb", adb)
    assert provision.include_new_apps(tmp_path / "work") is True
    restore = next(i for i, c in enumerate(ran) if "db restore" in c)
    assert any(c == provision.AUTOSCOPE for c in ran[restore:])


def test_a_missing_chrome_scope_gets_autoscope_run_again(monkeypatch):
    ran = []

    def su(serial, command, **kwargs):
        ran.append(command)
        if "scope ls" in command:  # the wait runs on the phone and says when Chrome is in
            return "chrome-scoped\n" if provision.AUTOSCOPE in ran else ""
        return ""

    monkeypatch.setattr(adbui, "su", su)
    monkeypatch.setattr(provision, "log", lambda text: None)
    provision.wait_chrome_scoped("emulator-5580")
    assert ran.count(provision.AUTOSCOPE) == 1
    waits = [c for c in ran if "scope ls" in c]
    assert len(waits) == 2 and waits[0].startswith("timeout 30 ") and waits[1].startswith("timeout 90 ")


def test_a_chrome_scope_that_never_comes_fails_the_check(monkeypatch):
    monkeypatch.setattr(adbui, "su", lambda serial, command, **kwargs: "")
    monkeypatch.setattr(provision, "log", lambda text: None)
    with pytest.raises(provision.SetupError):
        provision.wait_chrome_scoped("emulator-5580")


class Waiter:
    """Stands in for the one adb process wait_on_phone starts."""

    def __init__(self, args, **kwargs):
        Waiter.args, Waiter.killed = args, False

    def wait(self, timeout=None):
        return Waiter.code

    def poll(self):
        return Waiter.code

    def kill(self):
        Waiter.killed = True


def test_a_wait_on_the_phone_is_one_adb_call_that_waits_there(monkeypatch):
    monkeypatch.setattr(adbui.subprocess, "Popen", Waiter)
    Waiter.code = 0
    assert adbui.wait_unlocked("emulator-5570", timeout=60) is True
    assert Waiter.args[1:4] == ["-s", "emulator-5570", "wait-for-device"]
    assert Waiter.args[4:] == ["shell", "timeout 60 sh -c 'until [ \"$(getprop sys.user.0.ce_available)\" = true ]; "
                                        "do sleep 0.2; done'"]
    Waiter.code = 124  # the phone's own timeout
    assert adbui.wait_unlocked("emulator-5570", timeout=60) is False


def test_systemui_is_restarted_with_the_log_read_from_just_before(monkeypatch):
    """The wait reads the log from before the restart, so the lock screen's ready line cannot be missed; no fixed
    pause. True when relay-bridge's unlocker loaded."""
    ran = []
    monkeypatch.setattr(adbui, "su", lambda serial, command, **kwargs: ran.append(command)
                        or "I/relay_unlocker( 4682): installed: {unlock_keyguard=ok}\n")
    monkeypatch.setattr(adbui.time, "sleep", lambda s: pytest.fail("a fixed pause"))
    assert adbui.restart_systemui("emulator-5600") is True
    script = ran[0].splitlines()
    assert script[0].startswith("t=$(date") and script[1] == "pkill -f com.android.systemui"
    assert "SYSTEM_READY" in script[2] and '-T "$t" -m 1' in script[2]
    monkeypatch.setattr(adbui, "su", lambda serial, command, **kwargs: "")  # no bridge in SystemUI, or no root
    assert adbui.restart_systemui("emulator-5600") is False


def test_a_reboot_is_waited_for_on_the_phone_across_the_restart(monkeypatch):
    """The first wait ends when the phone goes down; the next waits for it to come back with a new boot id."""
    answers = iter([False, True])
    conditions = []
    monkeypatch.setattr(adbui, "wait_on_phone", lambda serial, condition, timeout, **kw:
                        conditions.append(condition) or next(answers))
    monkeypatch.setattr(provision.time, "sleep", lambda s: None)
    monkeypatch.setattr(provision, "wait_boot", lambda serial, wait: conditions.append("booted"))
    assert provision.wait_reboot("emulator-5600", "1234") is True
    assert conditions[-1] == "booted" and len(conditions) == 3 and "!= 1234" in conditions[0]
