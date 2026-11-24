"""What the phone builds share: the Android SDK and JDK tools, and signing with penrose's release key.

The key is in android/signing/, which git ignores: it is never committed. `signing.json` there (or the file
PENROSE_SIGNING names) lists it, with paths relative to the json:

    {"keystore": "penrose-release.p12", "alias": "penrose", "password": "...",
     "previous": {"keystore": "relay-debug.keystore", "alias": "relaybridge", "password": "android"},
     "lineage": "penrose.lineage"}

`previous` and `lineage` are the key rotation (APK Signature Scheme v3.1): APKs are signed by the release key with
proof that the previous key handed over to it, so phones that installed the apps under the previous key take them
as ordinary updates. Without them an APK is signed by the release key alone.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

EXE = ".exe" if os.name == "nt" else ""
BAT = ".bat" if os.name == "nt" else ""
SIGNING = Path(os.environ.get("PENROSE_SIGNING") or Path(__file__).resolve().parent / "signing" / "signing.json")
ASSETS = Path(__file__).resolve().parents[1] / "desktop" / "assets" / "android"


def newest(path: Path) -> Path:
    versions = sorted((p for p in path.iterdir() if p.is_dir()), key=lambda p: p.name)
    if not versions:
        sys.exit(f"Nothing installed under {path}")
    return versions[-1]


def run(*args, env=None):
    print(">", " ".join(str(a) for a in args))
    subprocess.run([str(a) for a in args], check=True, env=env)


class Tools:
    """The SDK's newest build-tools and platform, and a JDK 17+ (d8 and apksigner need it too, not whatever `java`
    is on PATH)."""

    def __init__(self, sdk=None, jdk=None):
        sdk = Path(sdk or os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or "D:/Android/Sdk")
        self.build_tools = newest(sdk / "build-tools")
        self.android_jar = newest(sdk / "platforms") / "android.jar"
        jdk = jdk or os.environ.get("JAVA_HOME") or "D:/Android/tools/jdk-17.0.20.1+1"
        javac = Path(jdk) / "bin" / f"javac{EXE}"
        if not javac.exists():
            found = shutil.which("javac")
            if not found:
                sys.exit("javac not found: install a JDK 17+ and pass --jdk or set JAVA_HOME.")
            javac = Path(found).resolve()
        self.javac = javac
        self.jdk = javac.parents[1]
        self.keytool = self.jdk / "bin" / f"keytool{EXE}"
        os.environ["JAVA_HOME"] = str(self.jdk)
        os.environ["PATH"] = str(self.jdk / "bin") + os.pathsep + os.environ["PATH"]

    def tool(self, name):
        return self.build_tools / f"{name}{BAT if name in ('d8', 'apksigner') else EXE}"

    def java8(self):
        # javac needs LambdaMetafactory.metafactory, which android.jar lacks; build-tools ships it.
        bootclasspath = os.pathsep.join(map(str, (self.android_jar, self.build_tools / "core-lambda-stubs.jar")))
        return ["-source", "8", "-target", "8", "-bootclasspath", bootclasspath, "-encoding", "UTF-8", "-nowarn",
                "-Xlint:-options"]

    def sign(self, aligned: Path, final: Path):
        """Sign with the release key (and the rotation from the previous key, when signing.json has it). The
        passwords go to apksigner through the environment, so they are neither printed nor in the process list."""
        key = signing()
        env = {**os.environ, "PENROSE_KEY_PASS": key["password"]}
        args = []
        if previous := key.get("previous"):
            env["PENROSE_PREVIOUS_KEY_PASS"] = previous["password"]
            args += ["--ks", previous["keystore"], "--ks-key-alias", previous["alias"],
                     "--ks-pass", "env:PENROSE_PREVIOUS_KEY_PASS", "--next-signer"]
        args += ["--ks", key["keystore"], "--ks-key-alias", key["alias"], "--ks-pass", "env:PENROSE_KEY_PASS"]
        if previous:
            args += ["--lineage", key["lineage"]]
        run(self.tool("apksigner"), "sign", *args, "--out", final, aligned, env=env)
        run(self.tool("apksigner"), "verify", "--min-sdk-version", "30", final)


def signing():
    if not SIGNING.exists():
        sys.exit(f"No signing key: {SIGNING} is missing. See android/README.md (Signing).")
    key = json.loads(SIGNING.read_text(encoding="utf-8"))
    here = SIGNING.resolve().parent
    for entry, field in ((key, "keystore"), (key, "lineage"), (key.get("previous") or {}, "keystore")):
        if entry.get(field):
            entry[field] = str(here / entry[field])  # an absolute path stays as it is
    return key
