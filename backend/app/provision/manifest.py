"""What the phone must carry for this build of Relay, and what it carries now.

The app ships its phone-side pieces in one folder (desktop/assets/android, `--assets`): the Xposed module the
agents read apps through, the Magisk module that scopes it, and `manifest.json` listing them. Each artifact is
identified by the SHA-256 of its file, not by a version number, so a rebuilt file is "new" exactly when its
bytes changed. The phone records what was applied to it in APPLIED (inside /data, so a phone reset clears it
and a clone of the golden image inherits the golden's record). `plan` is the difference between the two.

`golden_generation` counts the base the golden image is built from (system image, Magisk, Vector, rootAVD).
Those are not updated in place: a phone from an older generation needs a reset, which rebuilds the golden.
"""

import hashlib
import json
import re
from pathlib import Path

APPLIED = "/data/adb/relay/applied.json"
KINDS = ("apk", "magisk-module", "boot-script")

# Builds that predate manifest.json shipped these two files; the same list, so they keep updating.
DEFAULT = {
    "golden_generation": 1,
    "artifacts": [
        {"id": "relay-bridge", "kind": "apk", "file": "relay-bridge.apk", "package": "app.relay.bridge", "xposed_module": True},
        {"id": "relay-autoscope", "kind": "magisk-module", "file": "relay-autoscope.zip", "module": "relay_autoscope"},
    ],
}


class ManifestError(Exception):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load(assets: Path) -> dict:
    """The manifest from the assets folder, checked; DEFAULT when the folder has none."""
    file = assets / "manifest.json"
    manifest = json.loads(file.read_text("utf-8")) if file.exists() else DEFAULT
    if not isinstance(manifest.get("golden_generation"), int) or manifest["golden_generation"] < 1:
        raise ManifestError("manifest.json: golden_generation must be a positive integer")
    seen = set()
    for artifact in manifest.get("artifacts", []):
        if artifact.get("kind") not in KINDS or not artifact.get("id") or not artifact.get("file"):
            raise ManifestError(f"manifest.json: bad artifact {artifact!r}")
        if artifact["id"] in seen:
            raise ManifestError(f"manifest.json: artifact {artifact['id']} listed twice")
        seen.add(artifact["id"])
        if artifact["kind"] == "apk" and not artifact.get("package"):
            raise ManifestError(f"manifest.json: apk {artifact['id']} has no package")
        if artifact["kind"] == "magisk-module" and not artifact.get("module"):
            raise ManifestError(f"manifest.json: magisk-module {artifact['id']} has no module id")
        if artifact["kind"] == "boot-script" and not re.fullmatch(r"[a-zA-Z0-9_-]+\.sh", artifact["file"]):
            raise ManifestError("manifest.json: boot-script must have a plain .sh filename")
        if not (assets / artifact["file"]).exists():
            raise ManifestError(f"{artifact['file']} is missing from the app's files.")
    return manifest


def expected(assets: Path) -> dict:
    """The manifest with each artifact's hash filled in: what a phone running this build must carry."""
    manifest = load(assets)
    artifacts = [{**a, "sha256": sha256(assets / a["file"])} for a in manifest.get("artifacts", [])]
    return {"golden_generation": manifest["golden_generation"], "artifacts": artifacts}


def parse_applied(text: str) -> dict:
    """The phone's record, or an empty one when it has none (or it is unreadable)."""
    try:
        applied = json.loads(text or "{}")
    except ValueError:
        applied = {}
    if not isinstance(applied, dict):
        applied = {}
    artifacts = applied.get("artifacts")
    return {
        "golden_generation": applied.get("golden_generation") if isinstance(applied.get("golden_generation"), int) else None,
        "artifacts": artifacts if isinstance(artifacts, dict) else {},
    }


def plan(expected: dict, applied: dict) -> dict:
    """What `device-ready` has to do: the artifacts whose hash differs from the record (all of them for a phone
    with no record), and whether the phone's base is older than this build's."""
    pending = [a for a in expected["artifacts"] if applied["artifacts"].get(a["id"]) != a["sha256"]]
    # A phone with no record predates the record itself, so its base is the first generation.
    generation = applied["golden_generation"] or 1
    return {"pending": pending, "needs_reset": generation < expected["golden_generation"]}


def record(expected: dict, build_id: str = "") -> dict:
    """The record to write once everything in `expected` is on the phone."""
    return {
        "golden_generation": expected["golden_generation"],
        "artifacts": {a["id"]: a["sha256"] for a in expected["artifacts"]},
        "build_id": build_id,
    }
