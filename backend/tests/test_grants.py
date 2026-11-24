import asyncio

from app.config import settings
from app.gateway import grants


def test_the_default_groups_are_notifications_and_location():
    assert grants.wanted() == [
        "android.permission.POST_NOTIFICATIONS",
        "android.permission.ACCESS_FINE_LOCATION",
        "android.permission.ACCESS_COARSE_LOCATION",
    ]


def test_an_empty_setting_grants_nothing_and_starts_no_watch(monkeypatch):
    monkeypatch.setattr(settings, "auto_grant", "")
    assert grants.wanted() == []
    asyncio.run(grants.became_ready("device-1"))
    assert "device-1" not in grants.tasks


def test_unknown_groups_are_ignored(monkeypatch):
    monkeypatch.setattr(settings, "auto_grant", "notifications, teleport")
    assert grants.wanted() == ["android.permission.POST_NOTIFICATIONS"]


def test_one_script_grants_every_app_and_keeps_it_off_auto_revoke():
    script = grants.grant_script(["com.slack", "us.zoom.videomeetings"])
    assert "for pkg in com.slack us.zoom.videomeetings;" in script
    assert "pm grant $pkg $p 2>/dev/null" in script
    assert "AUTO_REVOKE_PERMISSIONS_IF_UNUSED ignore" in script
    assert script.endswith("true")  # an app that asks for none of them is not a failure


def test_the_watch_ends_any_earlier_one_on_the_phone_before_it_starts():
    command = grants.watch_command()
    clear, start = command.index('pkill -f "^inotifyd - /data/app "'), command.index("exec inotifyd - /data/app:ny")
    assert clear < start  # the pattern is anchored, so the shell running both lines is not matched


def test_an_install_is_the_folder_moved_into_data_app():
    assert grants.new_install("y\t/data/app\t~~Ab3dE-f==\r\n") == "~~Ab3dE-f=="
    assert grants.new_install("n\t/data/app\t~~Xy==") == "~~Xy=="
    assert grants.new_install("n\t/data/app\tvmdl123.tmp") is None  # the staging folder, before it is moved in
    assert grants.new_install("x\t/data/app") is None


def test_the_package_name_is_read_from_the_folder_inside():
    assert grants.PACKAGE_DIR.match("com.Slack-wkqdQGrgtrFH5rXWAYxzAQ==").group(1) == "com.Slack"
    assert grants.PACKAGE_DIR.match("base.apk") is None
