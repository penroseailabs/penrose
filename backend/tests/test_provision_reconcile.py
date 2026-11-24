"""`device-ready`'s last step: the phone brought up to date with the shipped assets, with adb faked."""

import json

import pytest
from app import provision
from app.provision import adbui, manifest


class FakePhone:
    """Answers adb like a phone: remembers its record, logs every command."""

    def __init__(self, record=None):
        self.record = json.dumps(record) if record else ""
        self.commands = []
        self.boots = 1

    def su(self, serial, command, **kwargs):
        self.commands.append(("su", command))
        if command == "reboot":
            self.boots += 1  # a new boot, so a new kernel boot id
        if command.startswith("cat " + manifest.APPLIED):
            return self.record or "cat: no such file"
        if "| base64 -d >" in command:
            import base64

            self.record = base64.b64decode(command.split("echo ")[1].split(" |")[0]).decode()
        return ""

    def adb(self, serial, *args, **kwargs):
        if args == ("wait-for-device", "shell", f"cat {provision.BOOT_ID}"):
            return f"00000000-0000-0000-0000-{self.boots:012d}\n"
        self.commands.append(("adb", " ".join(args)))
        return "Success"

    def shell(self, serial, command, **kwargs):
        self.commands.append(("shell", command))
        return ""

    def wait_on_phone(self, serial, condition, timeout, **kwargs):
        """Booted, restarted, Vector running: whatever is waited for is there."""
        self.commands.append(("wait", condition))
        return True


@pytest.fixture
def assets(tmp_path):
    (tmp_path / "relay-bridge.apk").write_bytes(b"apk v2")
    (tmp_path / "relay-autoscope.zip").write_bytes(b"zip v2")
    return tmp_path


@pytest.fixture
def phone(monkeypatch):
    fake = FakePhone()
    monkeypatch.setattr(adbui, "su", fake.su)
    monkeypatch.setattr(adbui, "adb", fake.adb)
    monkeypatch.setattr(adbui, "shell", fake.shell)
    monkeypatch.setattr(adbui, "wait_on_phone", fake.wait_on_phone)
    monkeypatch.setattr(adbui, "notification_listener_setup", lambda serial: fake.commands.append(("notifier", "")))
    monkeypatch.setattr(provision, "enable_module", lambda serial: fake.commands.append(("enable", "")))
    monkeypatch.setattr(provision, "include_new_apps", lambda work, serial: fake.commands.append(("include", "")))
    monkeypatch.setattr(provision, "wait_boot", lambda serial, wait=0, process=None: fake.commands.append(("boot", "")))
    monkeypatch.setattr(provision.time, "sleep", lambda s: None)
    monkeypatch.setattr(provision, "emit", lambda event, **data: fake.commands.append((event, data.get("message", data.get("title", "")))))
    return fake


def test_a_phone_without_a_record_gets_the_module_then_the_apk_then_one_reboot_and_a_record(assets, phone, tmp_path):
    assert provision.reconcile("emulator-5570", assets, tmp_path / "work", "20261023120000") is True
    kinds = [kind for kind, _ in phone.commands]
    assert kinds.index("step") < kinds.index("adb")
    module_install = next(i for i, (k, c) in enumerate(phone.commands) if k == "su" and "magisk --install-module" in c)
    apk_install = next(i for i, (k, c) in enumerate(phone.commands) if k == "adb" and c.startswith("install -r"))
    reboot = next(i for i, (k, c) in enumerate(phone.commands) if k == "su" and c == "reboot")
    assert module_install < apk_install < reboot
    assert [k for k in kinds if k in ("enable", "include", "notifier")] == ["enable", "include", "notifier"]
    assert kinds.count("boot") == 1 and sum(1 for k, c in phone.commands if c == "reboot") == 1
    record = json.loads(phone.record)
    assert record["build_id"] == "20261023120000"
    assert record["artifacts"] == {a["id"]: a["sha256"] for a in manifest.expected(assets)["artifacts"]}
    assert any("jev_autoscope" in c for _, c in phone.commands)  # the pre-rename module is retired


def test_a_current_phone_is_left_alone(assets, phone, tmp_path):
    phone.record = json.dumps(manifest.record(manifest.expected(assets)))
    assert provision.reconcile("emulator-5570", assets, tmp_path / "work") is False
    assert [k for k, _ in phone.commands] == ["su"]  # only the record was read


def test_a_new_boot_script_is_executable_and_rebooted_before_recording(assets, phone, tmp_path):
    import base64

    phone.record = json.dumps(manifest.record(manifest.expected(assets)))
    script = assets / "profile.sh"
    script.write_bytes(b"#!/system/bin/sh\r\nresetprop -n ro.product.brand samsung\r\n")
    (assets / "manifest.json").write_text(json.dumps({**manifest.DEFAULT, "artifacts": [
        *manifest.DEFAULT["artifacts"], {"id": "profile", "kind": "boot-script", "file": "profile.sh"},
    ]}))
    assert provision.reconcile("emulator-5600", assets, tmp_path / "work") is True
    install = next(c for _, c in phone.commands if "chmod 0755" in c)
    assert "chown 0:0" in install
    assert "mv -f /data/adb/post-fs-data.d/profile.sh.tmp /data/adb/post-fs-data.d/profile.sh" in install
    encoded = install.split("echo ")[1].split(" |")[0]
    assert base64.b64decode(encoded) == script.read_bytes().replace(b"\r\n", b"\n")
    commands = [c for _, c in phone.commands]
    assert commands.index(install) < commands.index("reboot")
    assert "profile" in json.loads(phone.record)["artifacts"]
    phone.commands.clear()
    assert provision.reconcile("emulator-5600", assets, tmp_path / "work") is False
    assert len(phone.commands) == 1


def test_a_failed_boot_script_install_is_not_recorded(assets, phone, tmp_path, monkeypatch):
    (assets / "profile.sh").write_bytes(b"#!/system/bin/sh\n")
    (assets / "manifest.json").write_text(json.dumps({"golden_generation": 1, "artifacts": [
        {"id": "profile", "kind": "boot-script", "file": "profile.sh"},
    ]}))
    original_su = phone.su

    def su(serial, command, **kwargs):
        if "chmod 0755" in command:
            raise RuntimeError("write failed")
        return original_su(serial, command, **kwargs)

    monkeypatch.setattr(adbui, "su", su)
    with pytest.raises(RuntimeError, match="write failed"):
        provision.reconcile("emulator-5600", assets, tmp_path / "work")
    assert phone.record == ""
    assert not any(c == "reboot" for _, c in phone.commands)


def test_only_a_changed_apk_is_reinstalled(assets, phone, tmp_path):
    phone.record = json.dumps(manifest.record(manifest.expected(assets)))
    (assets / "relay-bridge.apk").write_bytes(b"apk v3")
    assert provision.reconcile("emulator-5570", assets, tmp_path / "work") is True
    assert not any("magisk --install-module" in c for _, c in phone.commands)
    assert any(c.startswith("install -r") for k, c in phone.commands if k == "adb")
    assert not any("jev_autoscope" in c for _, c in phone.commands)  # a phone with a record is past the rename


def test_an_older_base_is_reported_but_the_phone_keeps_working(assets, phone, tmp_path):
    (assets / "manifest.json").write_text(json.dumps({**manifest.DEFAULT, "golden_generation": 2}))
    phone.record = json.dumps({**manifest.record(manifest.expected(assets)), "golden_generation": 1})
    assert provision.reconcile("emulator-5570", assets, tmp_path / "work") is False
    assert ("outdated", "This version of penrose needs a fresh phone.") in phone.commands


def test_a_broken_manifest_is_a_setup_error(assets, phone, tmp_path):
    (assets / "manifest.json").write_text("{\"golden_generation\": 0}")
    with pytest.raises(provision.SetupError):
        provision.reconcile("emulator-5570", assets, tmp_path / "work")


def with_home(assets):
    (assets / "relay-home.apk").write_bytes(b"home v1")
    artifacts = manifest.DEFAULT["artifacts"] + [
        {"id": "relay-home", "kind": "apk", "file": "relay-home.apk", "package": "app.relay.home", "home": True}]
    (assets / "manifest.json").write_text(json.dumps({**manifest.DEFAULT, "artifacts": artifacts}))


def test_the_home_app_is_installed_with_its_permissions_and_made_the_home_screen(assets, phone, tmp_path, monkeypatch):
    with_home(assets)
    # A phone made before the home app shipped: everything else is current.
    current = manifest.record(manifest.expected(assets))
    current["artifacts"].pop("relay-home")
    phone.record = json.dumps(current)
    homes = []
    monkeypatch.setattr(adbui, "home_setup", lambda serial: homes.append(serial) or True)
    assert provision.reconcile("emulator-5570", assets, tmp_path / "work") is False  # no reboot for an ordinary app
    installs = [c for k, c in phone.commands if k == "adb" and c.startswith("install")]
    assert installs == [f"install -r -g {assets / 'relay-home.apk'}"]
    assert homes == ["emulator-5570"]
    assert not any(c == "reboot" for _, c in phone.commands)
    assert set(json.loads(phone.record)["artifacts"]) == {"relay-bridge", "relay-autoscope", "relay-home"}


def test_device_ready_never_opens_an_app_on_the_users_phone(assets, phone, tmp_path, monkeypatch):
    with_home(assets)
    monkeypatch.setattr(provision, "wait_framework", lambda serial: None)
    monkeypatch.setattr(adbui, "screen_lock_remove", lambda serial, report: None)
    monkeypatch.setattr(adbui, "home_setup", lambda serial: True)
    monkeypatch.setattr(provision, "wait_chrome_scoped", lambda serial: phone.commands.append(("scoped", "")))
    monkeypatch.setattr(adbui, "launch", lambda serial, package: pytest.fail(f"opened {package}"))
    provision.device_ready(provision.Layout(tmp_path), "emulator-5570", 0, assets)
    assert ("scoped", "") in phone.commands  # updated, so the scope was checked
    shown = [c for k, c in phone.commands if k == "shell" and ("am start" in c or "monkey" in c or "KEYCODE_HOME" in c)]
    assert shown == []


class HomePhone:
    def __init__(self, installed=True):
        self.installed, self.holders, self.commands = installed, "com.google.android.apps.nexuslauncher", []

    def shell(self, serial, command, **kwargs):
        self.commands.append(command)
        if command.startswith("pm path"):
            return "package:/data/app/x/base.apk" if self.installed else ""
        if "get-role-holders" in command:
            return self.holders
        if "add-role-holder" in command:
            self.holders = adbui.HOME_PACKAGE
        return ""


def test_home_setup_takes_the_home_role_without_pressing_home(monkeypatch):
    fake = HomePhone()
    monkeypatch.setattr(adbui, "shell", fake.shell)
    assert adbui.home_setup("emulator-5570") is True
    assert any("add-role-holder" in c for c in fake.commands)
    assert any(f"set-home-activity --user 0 {adbui.HOME_ACTIVITY}" in c for c in fake.commands)
    assert not any("keyevent" in c or "am start" in c for c in fake.commands)


def test_home_setup_does_nothing_without_the_app(monkeypatch):
    fake = HomePhone(installed=False)
    monkeypatch.setattr(adbui, "shell", fake.shell)
    assert adbui.home_setup("emulator-5570") is False
    assert len(fake.commands) == 1


def test_the_apps_a_look_up_asks_are_exempt_from_doze(monkeypatch):
    # Gmail, Chrome and the home app: asked in the first seconds after a wake, when Doze still keeps other apps
    # off the network. Only what is missing is added, and an app the phone does not have is left alone.
    listed, commands = ["system,com.google.android.gms,10144", "user,com.android.chrome,10146"], []

    def shell(serial, command, **kwargs):
        commands.append(command)
        if command == "dumpsys deviceidle whitelist":
            return "\n".join(listed)
        if command.startswith("pm path"):
            return "" if command.endswith(adbui.HOME_PACKAGE) else "package:/data/app/x/base.apk"
        if command.startswith("dumpsys deviceidle whitelist +"):
            listed.append(f"user,{command.partition('+')[2]},10200")
        return ""

    monkeypatch.setattr(adbui, "shell", shell)
    assert adbui.out_of_doze("emulator-5570") == []
    assert [c for c in commands if "+" in c] == ["dumpsys deviceidle whitelist +com.google.android.gm"]
    commands.clear()
    assert adbui.out_of_doze("emulator-5570") == []  # again: nothing to add (the home app is not installed)
    assert not any("+" in c for c in commands)


def test_the_boot_wait_is_one_call_on_a_phone_that_is_up(monkeypatch):
    calls = []
    monkeypatch.setattr(adbui, "wait_on_phone", lambda serial, condition, timeout, **kw: calls.append(condition) or True)
    assert provision.wait_services("emulator-5570") is True
    assert calls == [provision.SERVICES_CHECK]


def test_the_boot_wait_gives_up_without_failing(monkeypatch):
    monkeypatch.setattr(adbui, "wait_on_phone", lambda serial, condition, timeout, **kw: False)
    monkeypatch.setattr(provision, "log", lambda text: None)
    assert provision.wait_services("emulator-5570", wait=5) is False


def test_a_phone_that_closes_while_booting_is_reported_at_once(monkeypatch):
    """The boot wait is one call on the phone; the emulator closing ends it without waiting out the timeout."""
    class Closed:
        def poll(self):
            return 1

    def wait_on_phone(serial, condition, timeout, stop=None, **kw):
        assert condition == provision.BOOTED and stop is not None and stop()
        return False

    monkeypatch.setattr(adbui, "wait_on_phone", wait_on_phone)
    monkeypatch.setattr(provision, "log", lambda text: None)
    with pytest.raises(provision.SetupError, match="closed"):
        provision.wait_boot("emulator-5580", wait=60, process=Closed())


class BridgePhone:
    """A phone with the home app installed; its bridge socket shows up while the wait on the phone runs."""

    def __init__(self, running=True):
        self.commands, self.socket, self.running = [], True, running

    def shell(self, serial, command, **kwargs):
        self.commands.append(command)
        if command.startswith("pm path"):
            return "package:/data/app/home/base.apk"
        if command.startswith("pidof"):
            return "1234" if self.running else ""
        return ""

    def wait_on_phone(self, serial, condition, timeout, **kwargs):
        self.commands.append(condition)
        return self.socket and f"@{provision.HOME_SOCKET} /proc/net/unix" in condition


@pytest.fixture
def bridge_phone(monkeypatch):
    fake = BridgePhone()
    monkeypatch.setattr(adbui, "shell", fake.shell)
    monkeypatch.setattr(adbui, "wait_on_phone", fake.wait_on_phone)
    monkeypatch.setattr(provision, "wait_chrome_scoped", lambda serial: fake.commands.append("scoped"))
    monkeypatch.setattr(provision, "bridge_hello", lambda serial, name: '{"ok":true,"process":"%s"}' % name)
    monkeypatch.setattr(provision, "verify_chrome_bridge", lambda serial: pytest.fail("opened Chrome"))
    monkeypatch.setattr(provision.time, "sleep", lambda s: None)
    monkeypatch.setattr(provision, "log", lambda text: fake.commands.append("log: " + text))
    return fake


def test_the_golden_check_asks_the_home_app_not_chrome(bridge_phone):
    provision.verify_bridge("emulator-5560", golden=True)
    assert "scoped" in bridge_phone.commands
    assert any("category.HOME" in c for c in bridge_phone.commands)  # shown on the golden, nobody is looking
    assert any(c.startswith("log: bridge hello:") for c in bridge_phone.commands)


def test_the_check_on_the_users_phone_opens_nothing(bridge_phone):
    provision.verify_bridge("emulator-5570", golden=False)
    assert not any("am start" in c or "monkey" in c or "keyevent" in c for c in bridge_phone.commands)
    assert any(c.startswith("log: bridge hello:") for c in bridge_phone.commands)


def test_the_check_on_the_users_phone_skips_a_home_app_that_is_not_running(bridge_phone):
    bridge_phone.running = False
    provision.verify_bridge("emulator-5570", golden=False)
    assert not any("/proc/net/unix" in c or "am start" in c for c in bridge_phone.commands)


def test_a_home_app_without_the_bridge_fails_the_check(bridge_phone):
    bridge_phone.socket = False  # never shows up
    with pytest.raises(provision.SetupError):
        provision.verify_bridge("emulator-5560", golden=True)
