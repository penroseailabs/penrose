"""The lighter phone: the 720p screen and front camera in an AVD's config.ini (`avd_config`) and the Google app
switched off (`adbui.disable_unused_apps`), with adb faked."""

import re

from app import provision
from app.provision import adbui

GOOGLE = "com.google.android.googlequicksearchbox"


def test_a_pixel_6_config_gets_the_720p_screen_and_a_front_camera_once(tmp_path):
    config = tmp_path / "config.ini"
    config.write_text("hw.camera.back=emulated\nhw.camera.front=none\nhw.camera.front.orientation=90\n"
                      "hw.lcd.width=1080\nhw.lcd.height=2400\nhw.lcd.density=420\nhw.ramSize=2048\n")
    assert provision.avd_config(config) is True
    lines = config.read_text().splitlines()
    assert {"hw.ramSize=2048", "hw.camera.back=emulated", "hw.camera.front.orientation=90"} <= set(lines)
    assert [line for line in lines if line.startswith("hw.lcd.")] == ["hw.lcd.width=720", "hw.lcd.height=1600", "hw.lcd.density=280"]
    assert [line for line in lines if line.startswith("hw.camera.front=")] == ["hw.camera.front=emulated"]
    assert provision.avd_config(config) is False  # already there: left as is


def test_a_720p_phone_from_before_the_front_camera_gets_it(tmp_path):
    config = tmp_path / "config.ini"
    config.write_text("hw.camera.front=none\nhw.lcd.width=720\nhw.lcd.height=1600\nhw.lcd.density=280\n")
    assert provision.avd_config(config) is True
    assert "hw.camera.front=emulated" in config.read_text().splitlines()


def test_a_new_image_gets_a_64_gb_disk_but_a_phone_with_data_keeps_its_size(tmp_path, monkeypatch):
    layout = provision.Layout(tmp_path)
    avd = layout.golden_home / f"{provision.GOLDEN}.avd"

    def avdmanager(args, **kwargs):  # what `avdmanager create avd` writes, a few GB of storage
        avd.mkdir(parents=True)
        (avd / "config.ini").write_text("disk.dataPartition.size=6G\nimage.sysdir.1=system-images/x/\n")
    monkeypatch.setattr(provision, "run", avdmanager)
    provision.create_golden_avd(layout)
    lines = (avd / "config.ini").read_text().splitlines()
    assert [line for line in lines if line.startswith("disk.dataPartition.size")] == ["disk.dataPartition.size=64G"]
    assert "hw.lcd.width=720" in lines

    phone = tmp_path / "phone.ini"  # a phone already holding the user's data, from an image built at 10 GB
    phone.write_text("disk.dataPartition.size=10G\n")
    provision.avd_config(phone)
    assert "disk.dataPartition.size=10G" in phone.read_text().splitlines()


def test_a_missing_config_is_not_an_error(tmp_path):
    assert provision.avd_config(tmp_path / "none" / "config.ini") is False


class AppsPhone:
    def __init__(self, installed=True, shell_can_disable=True):
        self.installed, self.shell_can_disable, self.disabled = installed, shell_can_disable, False
        self.commands = []

    def shell(self, serial, command, **kwargs):
        self.commands.append(command)
        if command == f"pm path {GOOGLE}":
            return "package:/product/priv-app/Velvet/Velvet.apk\n" if self.installed else ""
        if command == "pm list packages -d":
            return f"package:com.example.off\npackage:{GOOGLE}\n" if self.disabled else "package:com.example.off\n"
        if command == f"pm disable-user --user 0 {GOOGLE}" and self.shell_can_disable:
            self.disabled = True
        return ""

    def su(self, serial, command, **kwargs):
        self.commands.append("su " + command)
        if command == f"pm disable-user --user 0 {GOOGLE}":
            self.disabled = True
        return ""


def install(monkeypatch, phone):
    monkeypatch.setattr(adbui, "shell", phone.shell)
    monkeypatch.setattr(adbui, "su", phone.su)
    return phone


def test_the_google_app_is_switched_off(monkeypatch):
    phone = install(monkeypatch, AppsPhone())
    assert adbui.disable_unused_apps("emulator-5570") == []
    assert phone.disabled and not any(c.startswith("su ") for c in phone.commands)


def test_root_switches_it_off_when_the_shell_cannot(monkeypatch):
    phone = install(monkeypatch, AppsPhone(shell_can_disable=False))
    assert adbui.disable_unused_apps("emulator-5570") == []
    assert f"su pm disable-user --user 0 {GOOGLE}" in phone.commands


def test_an_app_already_off_or_missing_is_left_alone(monkeypatch):
    phone = install(monkeypatch, AppsPhone())
    phone.disabled = True
    assert adbui.disable_unused_apps("emulator-5570") == []
    assert not any("disable-user" in c for c in phone.commands)
    phone = install(monkeypatch, AppsPhone(installed=False))
    assert adbui.disable_unused_apps("emulator-5570") == []
    assert phone.commands == [f"pm path {package}" for package in adbui.UNUSED_APPS]  # looked for, nothing more


class FullPhone:
    """Every unused app installed; `disabled` the packages switched off."""

    def __init__(self, disabled=()):
        self.disabled, self.commands = set(disabled), []

    def shell(self, serial, command, **kwargs):
        self.commands.append(command)
        if command.startswith("pm path "):
            return f"package:/product/priv-app/{command.split()[-1]}/base.apk\n"
        if command == "pm list packages -d":
            return "".join(f"package:{p}\n" for p in self.disabled)
        if command.startswith("pm disable-user --user 0 "):
            self.disabled.add(command.split()[-1])
        if command.startswith("pm enable --user 0 "):
            self.disabled.discard(command.split()[-1])
        return ""

    def su(self, serial, command, **kwargs):
        return self.shell(serial, command)


def test_every_unused_app_is_switched_off(monkeypatch):
    phone = install(monkeypatch, FullPhone())
    assert adbui.disable_unused_apps("emulator-5570") == []
    assert phone.disabled == set(adbui.UNUSED_APPS)
    assert "com.google.android.apps.messaging" in phone.disabled


def test_the_pixel_launcher_is_switched_back_on_where_an_earlier_relay_turned_it_off(monkeypatch):
    phone = install(monkeypatch, FullPhone(disabled={adbui.PIXEL_LAUNCHER}))
    assert adbui.keep_pixel_launcher("emulator-5570") is True
    assert adbui.PIXEL_LAUNCHER not in phone.disabled
    assert adbui.keep_pixel_launcher("emulator-5570") is False  # on already: nothing to do, no SystemUI restart


def test_apps_agents_use_are_never_switched_off():
    kept = {"com.google.android.gms", "com.android.vending", "com.android.chrome", "com.google.android.gm",
            "com.google.android.apps.maps", "com.google.android.calendar", "com.google.android.contacts",
            "com.google.android.inputmethod.latin", "com.android.settings", "com.google.android.documentsui",
            adbui.HOME_PACKAGE, "app.relay.bridge", adbui.PIXEL_LAUNCHER}  # the launcher draws the nav bar
    assert not kept & set(adbui.UNUSED_APPS)


def test_the_display_cutout_is_blanked_once(monkeypatch):
    """The punch hole is placed for a 1080-wide screen: on 720 it is a black circle over the status bar."""
    on, ran = set(), []

    def shell(serial, command, **kwargs):
        if command == "cmd overlay list":
            return "".join(f"[{'x' if n in on else ' '}] com.android.shell:{n}\n" for n in adbui.NO_CUTOUT)
        return ""

    def su(serial, command, **kwargs):
        ran.append(command)
        if command.startswith("cmd overlay enable"):
            on.add(command.split(":")[-1])
        return ""

    monkeypatch.setattr(adbui, "shell", shell)
    monkeypatch.setattr(adbui, "su", su)
    assert adbui.remove_display_cutout("emulator-5570") is True
    assert sum("fabricate" in c and "0x03 ''" in c for c in ran) == 2
    ran.clear()
    assert adbui.remove_display_cutout("emulator-5570") is True
    assert ran == []  # already off: nothing done


def test_rootavd_never_looks_online_for_magisk(tmp_path):
    """Online, rootAVD asked GitHub for Magisk versions before using the pinned one: 2 min 20 s once."""
    script = tmp_path / "rootAVD.sh"
    script.write_bytes(b'#!/bin/bash\nCheckAVDIsOnline() {\n\tif [ -z $AVDIsOnline ]; then\n\t\techo check\n\tfi\n}\n')
    provision.rootavd_offline(script)
    provision.rootavd_offline(script)
    text = script.read_bytes()
    assert text.count(b"AVDIsOnline=false  # Relay") == 1
    assert text.index(b"AVDIsOnline=false") < text.index(b"CheckAVDIsOnline() {")
    assert b"\r\n" not in text


class PlayPhone:
    """Play Store with its preference file; `prefs` is the file's text, `installed` whether Play is there."""

    def __init__(self, installed=True, prefs="<map>\n</map>"):
        self.installed, self.prefs, self.commands = installed, prefs, []

    def shell(self, serial, command, **kwargs):
        self.commands.append(command)
        if command == f"pm path {adbui.PLAY_STORE}":
            return "package:/product/priv-app/Phonesky/Phonesky.apk\n" if self.installed else ""
        return ""

    def su(self, serial, command, **kwargs):
        self.commands.append("su " + command)
        if command.startswith("grep -o"):
            m = re.search(adbui.PLAY_AUTO_UPDATE_KEY + r'" value="[a-z]*"', self.prefs)
            return m.group(0) + "\n" if m else ""
        if command.startswith(f"am force-stop {adbui.PLAY_STORE}"):  # the write script, as sed would leave the file
            self.prefs = re.sub(r'\n *<boolean name="' + adbui.PLAY_AUTO_UPDATE_KEY + r'"[^\n]*', "", self.prefs)
            self.prefs = self.prefs.replace("</map>", f'    <boolean name="{adbui.PLAY_AUTO_UPDATE_KEY}" value="false" />\n</map>')
        return ""


def test_play_auto_updates_are_switched_off_in_its_own_preferences(monkeypatch):
    phone = install(monkeypatch, PlayPhone())
    assert adbui.play_auto_updates_off("emulator-5570") is True
    assert '<boolean name="auto_update_enabled" value="false" />' in phone.prefs
    assert any(c.startswith(f"su am force-stop {adbui.PLAY_STORE}") for c in phone.commands)  # stopped before the write


def test_play_already_off_or_missing_is_left_alone(monkeypatch):
    phone = install(monkeypatch, PlayPhone(prefs='<map>\n    <boolean name="auto_update_enabled" value="false" />\n</map>'))
    assert adbui.play_auto_updates_off("emulator-5570") is True
    assert not any("force-stop" in c for c in phone.commands)
    phone = install(monkeypatch, PlayPhone(installed=False))
    assert adbui.play_auto_updates_off("emulator-5570") is False
    assert phone.commands == [f"pm path {adbui.PLAY_STORE}"]


def test_a_renamed_key_reports_failure(monkeypatch):
    phone = install(monkeypatch, PlayPhone())
    monkeypatch.setattr(adbui, "PLAY_AUTO_UPDATE_KEY", "something_else")  # the fake keeps writing nothing Play reads
    phone.su = lambda serial, command, **kw: ""
    monkeypatch.setattr(adbui, "su", phone.su)
    assert adbui.play_auto_updates_off("emulator-5570") is False


class KeyboardPhone:
    """Android's stylus-writing setting (None when unset) and Gboard's preference file; `installed`: Gboard is there."""

    def __init__(self, stylus=None, prefs="<map>\n</map>", installed=True):
        self.stylus, self.prefs, self.installed, self.commands = stylus, prefs, installed, []

    def shell(self, serial, command, **kwargs):
        self.commands.append(command)
        if command == f"settings get secure {adbui.STYLUS_WRITING_SETTING}":
            return "null\n" if self.stylus is None else f"{self.stylus}\n"
        if command.startswith(f"settings put secure {adbui.STYLUS_WRITING_SETTING} "):
            self.stylus = command.split()[-1]
        if command == f"pm path {adbui.GBOARD}":
            return "package:/product/app/LatinIMEGooglePrebuilt/LatinIMEGooglePrebuilt.apk\n" if self.installed else ""
        return ""

    def su(self, serial, command, **kwargs):
        self.commands.append("su " + command)
        key = adbui.GBOARD_SHOW_KEYBOARD_KEY
        if command.startswith("grep -o"):
            return f'name="{key}"\n' if f'name="{key}"' in self.prefs else ""
        if command.startswith(f"am force-stop {adbui.GBOARD}"):  # the write script
            self.prefs = self.prefs.replace("</map>", f'    <boolean name="{key}" value="true" />\n</map>')
        return ""


def test_the_keyboard_stays_on_screen_and_stylus_writing_is_off(monkeypatch):
    """The emulator's touchscreen says it takes a pen: on the Samsung profile Gboard showed stylus prompts."""
    phone = install(monkeypatch, KeyboardPhone())
    assert adbui.keyboard_not_stylus("emulator-5570") is True
    assert phone.stylus == "0"
    assert '<boolean name="pk_always_show_vk" value="true" />' in phone.prefs
    assert any(c.startswith(f"su am force-stop {adbui.GBOARD}") for c in phone.commands)  # stopped before the write


def test_choices_the_user_made_are_kept(monkeypatch):
    phone = install(monkeypatch, KeyboardPhone(stylus="1", prefs='<map>\n    <boolean name="pk_always_show_vk" value="false" />\n</map>'))
    assert adbui.keyboard_not_stylus("emulator-5570") is True
    assert phone.stylus == "1"
    assert 'value="false"' in phone.prefs
    assert not any("force-stop" in c or "settings put" in c for c in phone.commands)  # nothing changed, Gboard left running


def test_no_gboard_or_a_file_that_cannot_be_written(monkeypatch):
    phone = install(monkeypatch, KeyboardPhone(installed=False))
    assert adbui.keyboard_not_stylus("emulator-5570") is True
    assert phone.stylus == "0"
    phone = install(monkeypatch, KeyboardPhone())
    monkeypatch.setattr(adbui, "su", lambda serial, command, **kw: "")  # Gboard has never run: no folder to write in
    assert adbui.keyboard_not_stylus("emulator-5570") is False
