"""The image build tells the window what it is doing: each part announced, in order, before it runs."""

from app import provision
from app.provision import manifest


def test_the_image_build_announces_each_part_before_it_runs(monkeypatch, tmp_path):
    events, ran = [], []
    monkeypatch.setattr(provision, "emit", lambda event, **data: events.append((event, data)))
    monkeypatch.setattr(manifest, "expected", lambda assets: {
        "golden_generation": 2, "artifacts": [{"kind": "boot-script"}],
    })
    monkeypatch.setattr(manifest, "record", lambda expected, build_id: {})

    def runs(name, result=None):
        def fake(*args, **kwargs):
            # The part a function belongs to has been announced before it runs.
            ran.append((name, events[-1][1].get("doing")))
            return result
        monkeypatch.setattr(provision, name, fake)

    runs("private_image")
    runs("create_golden_avd")
    runs("start_golden")
    runs("patch_ramdisk", result=True)
    runs("finish_magisk")
    runs("apply_artifact")
    runs("install_framework", result=True)
    runs("reboot")
    runs("enable_module")
    runs("include_new_apps")
    runs("install_home")
    runs("verify_bridge")
    runs("clean_golden")
    runs("write_applied")
    runs("shut_down_golden")

    layout = provision.Layout(tmp_path)
    (layout.golden_home / f"{provision.GOLDEN}.avd").mkdir(parents=True)
    provision.build_golden(layout, tmp_path, {"rootavd": tmp_path, "magisk": None, "vector": None})

    parts = [data["doing"] for event, data in events if event == "progress"]
    assert parts == ["image", "boot", "root", "restart", "access", "framework", "apps", "chrome", "tidy", "save"]
    percents = [data["percent"] for event, data in events if event == "progress"]
    assert percents == sorted(percents)
    assert ("start_golden", "boot") in ran and ("start_golden", "restart") in ran
    assert ("verify_bridge", "chrome") in ran
    assert ("install_home", "apps") in ran
    assert ran.index(("apply_artifact", "framework")) < ran.index(("reboot", "framework"))
    assert ("shut_down_golden", "save") in ran  # powered off gracefully, so the record is kept
