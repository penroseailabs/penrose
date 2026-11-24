"""The phone-side update plan: what the app ships (manifest.json) against what the phone carries."""

import json
from pathlib import Path

import pytest
from app.provision import manifest


@pytest.fixture
def assets(tmp_path):
    (tmp_path / "relay-bridge.apk").write_bytes(b"apk v1")
    (tmp_path / "relay-autoscope.zip").write_bytes(b"zip v1")
    (tmp_path / "relay-home.apk").write_bytes(b"home v1")
    (tmp_path / "relay-device-profile.sh").write_bytes(b"#!/system/bin/sh\n")
    return tmp_path


def test_a_folder_without_a_manifest_ships_the_two_classic_artifacts(assets):
    expected = manifest.expected(assets)
    assert expected["golden_generation"] == 1
    assert [a["id"] for a in expected["artifacts"]] == ["relay-bridge", "relay-autoscope"]
    assert expected["artifacts"][0]["sha256"] == manifest.sha256(assets / "relay-bridge.apk")


def test_the_shipped_manifest_lists_what_the_desktop_app_bundles(assets):
    shipped = json.loads((Path(__file__).parents[2] / "desktop" / "assets" / "android" / "manifest.json").read_text())
    (assets / "manifest.json").write_text(json.dumps(shipped))
    expected = manifest.expected(assets)
    assert {a["file"] for a in expected["artifacts"]} == {
        "relay-bridge.apk", "relay-autoscope.zip", "relay-home.apk", "relay-device-profile.sh",
    }
    assert [a["id"] for a in expected["artifacts"] if a.get("home")] == ["relay-home"]
    for artifact in expected["artifacts"]:  # each one is actually bundled
        assert (Path(__file__).parents[2] / "desktop" / "assets" / "android" / artifact["file"]).exists()


@pytest.mark.parametrize("bad", [
    {"golden_generation": 0, "artifacts": []},
    {"golden_generation": 1, "artifacts": [{"id": "x", "kind": "deb", "file": "relay-bridge.apk"}]},
    {"golden_generation": 1, "artifacts": [{"id": "x", "kind": "apk", "file": "relay-bridge.apk"}]},  # no package
    {"golden_generation": 1, "artifacts": [{"id": "x", "kind": "magisk-module", "file": "relay-autoscope.zip"}]},
    {"golden_generation": 1, "artifacts": [{"id": "x", "kind": "boot-script", "file": "../profile.sh"}]},
    {"golden_generation": 1, "artifacts": [{"id": "x", "kind": "boot-script", "file": "profile;id.sh"}]},
    {"golden_generation": 1, "artifacts": [{"id": "x", "kind": "apk", "file": "gone.apk", "package": "p"}]},
    {"golden_generation": 1, "artifacts": [
        {"id": "x", "kind": "apk", "file": "relay-bridge.apk", "package": "p"},
        {"id": "x", "kind": "apk", "file": "relay-bridge.apk", "package": "p"}]},
])
def test_a_broken_manifest_is_refused(assets, bad):
    (assets / "manifest.json").write_text(json.dumps(bad))
    with pytest.raises(manifest.ManifestError):
        manifest.load(assets)


def test_a_phone_with_no_record_gets_everything_but_no_reset(assets):
    todo = manifest.plan(manifest.expected(assets), manifest.parse_applied(""))
    assert [a["id"] for a in todo["pending"]] == ["relay-bridge", "relay-autoscope"]
    assert todo["needs_reset"] is False


def test_a_current_phone_has_nothing_to_do(assets):
    expected = manifest.expected(assets)
    applied = manifest.parse_applied(json.dumps(manifest.record(expected, "20261023120000")))
    assert manifest.plan(expected, applied) == {"pending": [], "needs_reset": False}


def test_only_the_artifact_whose_bytes_changed_is_pending(assets):
    before = manifest.record(manifest.expected(assets))
    (assets / "relay-bridge.apk").write_bytes(b"apk v2")
    todo = manifest.plan(manifest.expected(assets), manifest.parse_applied(json.dumps(before)))
    assert [a["id"] for a in todo["pending"]] == ["relay-bridge"]


def test_a_newer_base_asks_for_a_reset_and_an_unreadable_record_counts_as_empty(assets):
    (assets / "manifest.json").write_text(json.dumps({**manifest.DEFAULT, "golden_generation": 2}))
    expected = manifest.expected(assets)
    old = manifest.parse_applied(json.dumps({"golden_generation": 1, "artifacts": {}}))
    assert manifest.plan(expected, old)["needs_reset"] is True
    garbage = manifest.parse_applied("cat: /data/adb/relay/applied.json: No such file")
    assert garbage == {"golden_generation": None, "artifacts": {}}
    assert manifest.plan(expected, garbage)["needs_reset"] is True  # no record: generation 1


def test_the_record_names_the_build_that_wrote_it(assets):
    record = manifest.record(manifest.expected(assets), "20261023120000")
    assert record["build_id"] == "20261023120000"
    assert set(record["artifacts"]) == {"relay-bridge", "relay-autoscope"}
