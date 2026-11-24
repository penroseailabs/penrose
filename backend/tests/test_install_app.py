import zipfile
from pathlib import Path

import httpx
import pytest
from app.models import Action
from app.store import Store


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    from app import gateway

    lease_store = Store(tmp_path)
    lease_store.lease(["device-1"], "agent")
    monkeypatch.setattr(gateway.core, "store", lease_store)
    monkeypatch.setattr(gateway.core, "slots", {"device-1": "emulator-5554"})
    monkeypatch.setattr(gateway.core, "locks", {"device-1": gateway.core.asyncio.Lock()})
    return gateway


def fake_adb(gateway, monkeypatch, installed=False, install_code=0):
    """Record ADB calls; the package appears once an install command succeeds."""
    state = {"installed": installed, "calls": []}

    async def command_result(*args, **kwargs):
        args = args[2:]
        state["calls"].append(args)
        if args[0] in ("install", "install-multiple"):
            state["installed"] = not install_code
            return b"", b"" if not install_code else b"Failure [INSTALL_FAILED_NO_MATCHING_ABIS]", install_code
        if "pm list packages" in args[-1] and state["installed"]:
            return b"package:com.dd.doordash\n", b"", 0
        if "getprop" in args[-1]:
            return b"x86_64,arm64-v8a\n", b"", 0
        return b"", b"", 0

    monkeypatch.setattr(gateway.core, "command_result", command_result)
    return state


def fake_apkeep(gateway, monkeypatch, filename=None, splits=(), versions="4.1.0, 4.2.0"):
    async def process_result(executable, *args, **kwargs):
        if "-l" in args:
            return f"Versions available for {args[1]} on APKPure:\n| {versions}\n".encode(), b"", 0
        assert args[-2] == "arch=x86_64"
        directory = Path(args[-1])
        if filename and filename.endswith(".xapk"):
            with zipfile.ZipFile(directory / filename, "w") as archive:
                archive.writestr("manifest.json", "{}")
                for split in splits:
                    archive.writestr(split, b"apk")
        elif filename:
            (directory / filename).write_bytes(b"apk")
        return b"", b"", 0

    monkeypatch.setattr(gateway.core, "process_result", process_result)


async def install(gateway, package="com.dd.doordash", owner="agent"):
    async with httpx.AsyncClient(base_url="http://gateway", transport=httpx.ASGITransport(app=gateway.app)) as client:
        action = Action(kind="install_app", package=package).model_dump()
        return await client.post("/devices/device-1/actions", json={"owner": owner, "action": action})


async def test_installs_xapk_splits_for_device_abi_as_play(gateway, monkeypatch):
    adb = fake_adb(gateway, monkeypatch)
    fake_apkeep(gateway, monkeypatch, "com.dd.doordash.xapk", [
        "com.dd.doordash.apk", "config.arm64_v8a.apk", "config.x86.apk", "config.armeabi_v7a.apk",
        "config.x86_64.apk", "config.xxhdpi.apk",
    ])
    response = await install(gateway)
    assert response.status_code == 200
    assert response.json()["status"] == "installed"
    assert response.json()["ok"] is True
    command = next(c for c in adb["calls"] if c[0] == "install-multiple")
    assert command[:5] == ("install-multiple", "-r", "-g", "-i", "com.android.vending")
    names = [Path(p).name.split("-", 1)[1] for p in command[5:]]
    assert names == ["com.dd.doordash.apk", "config.x86_64.apk", "config.xxhdpi.apk"]
    assert ("shell", "settings put global package_verifier_enable 0") in adb["calls"]


async def test_plain_apk_uses_single_install(gateway, monkeypatch):
    adb = fake_adb(gateway, monkeypatch)
    fake_apkeep(gateway, monkeypatch, "com.dd.doordash.apk")
    assert (await install(gateway)).json()["status"] == "installed"
    assert any(c[0] == "install" and c[-1].endswith("com.dd.doordash.apk") for c in adb["calls"])


async def test_missing_download_reports_play_store_fallback(gateway, monkeypatch):
    fake_adb(gateway, monkeypatch)
    fake_apkeep(gateway, monkeypatch, None)
    result = (await install(gateway)).json()
    assert result["ok"] is False and result["status"] == "failed"
    assert "market://details?id=com.dd.doordash" in result["next_step"]


async def test_rejected_install_reports_android_error(gateway, monkeypatch):
    fake_adb(gateway, monkeypatch, install_code=1)
    fake_apkeep(gateway, monkeypatch, "com.dd.doordash.apk")
    result = (await install(gateway)).json()
    assert result["status"] == "failed" and result["reason"] == "rejected"
    assert "INSTALL_FAILED_NO_MATCHING_ABIS" in result["detail"]
    assert "CPU architecture" in result["message"]


async def test_app_missing_from_source_fails_fast_without_download(gateway, monkeypatch):
    fake_adb(gateway, monkeypatch)
    calls = []

    async def process_result(executable, *args, **kwargs):
        calls.append(args)
        return b"Versions available for com.instacart.client on APKPure:\n| \n", b"", 0

    monkeypatch.setattr(gateway.core, "process_result", process_result)
    result = (await install(gateway, package="com.instacart.client")).json()
    assert result["ok"] is False and result["reason"] == "not_available"
    assert "(APKPure) has no builds" in result["message"]
    assert "Do not call install_app again" in result["next_step"]
    assert "market://details?id=com.instacart.client" in result["next_step"]
    assert len(calls) == 1 and "-l" in calls[0]


async def test_unclear_availability_check_still_tries_download(gateway, monkeypatch):
    fake_adb(gateway, monkeypatch)

    async def process_result(executable, *args, **kwargs):
        if "-l" in args:
            return b"", b"connection reset", 1
        (Path(args[-1]) / "com.dd.doordash.apk").write_bytes(b"apk")
        return b"", b"", 0

    monkeypatch.setattr(gateway.core, "process_result", process_result)
    assert (await install(gateway)).json()["status"] == "installed"


async def test_missing_downloader_is_reported_as_setup_problem(gateway, monkeypatch):
    fake_adb(gateway, monkeypatch)

    async def process_result(executable, *args, **kwargs):
        raise FileNotFoundError(executable)

    monkeypatch.setattr(gateway.core, "process_result", process_result)
    result = (await install(gateway)).json()
    assert result["reason"] == "tool_missing"
    assert "apkeep" in result["message"] and "setup problem" in result["message"]


async def test_already_installed_skips_download(gateway, monkeypatch):
    fake_adb(gateway, monkeypatch, installed=True)

    async def process_result(*args, **kwargs):
        raise AssertionError("downloaded an installed app")

    monkeypatch.setattr(gateway.core, "process_result", process_result)
    assert (await install(gateway)).json()["status"] == "already_installed"


async def test_install_requires_lease_and_valid_package(gateway, monkeypatch):
    fake_adb(gateway, monkeypatch)
    assert (await install(gateway, owner="other")).status_code == 409
    assert (await install(gateway, package="bad package")).status_code == 422
