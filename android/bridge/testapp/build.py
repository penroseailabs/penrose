"""Build the WebView fixture app (testapp/) into webfixture.apk, with the same SDK-tools-only
approach as ../build.py.

    python android/bridge/testapp/build.py [--sdk D:/Android/Sdk] [--jdk <jdk 17>] [--out android/bridge/testapp/build]

Install it on an emulator that has the relay-bridge module; relay-autoscope scopes it within a few
seconds, then launch app.relay.webfixture/.MainActivity.
"""

import argparse
import shutil
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
from toolchain import Tools, run  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sdk")
    parser.add_argument("--jdk")
    parser.add_argument("--out", default=str(HERE / "build"))
    args = parser.parse_args()
    tools = Tools(args.sdk, args.jdk)

    out = Path(args.out).resolve()
    shutil.rmtree(out, ignore_errors=True)
    classes, dex = out / "classes", out / "dex"
    for d in (classes, dex):
        d.mkdir(parents=True)

    run(tools.javac, *tools.java8(), "-d", classes, *sorted((HERE / "src").rglob("*.java")))
    run(tools.tool("d8"), "--release", "--min-api", "30", "--lib", tools.android_jar, "--output", dex,
        *sorted(classes.rglob("*.class")))
    unsigned = out / "unsigned.apk"
    run(tools.tool("aapt2"), "link", "-o", unsigned, "--manifest", HERE / "AndroidManifest.xml",
        "-I", tools.android_jar, "-A", HERE / "assets")
    with zipfile.ZipFile(unsigned, "a") as apk:
        apk.write(dex / "classes.dex", "classes.dex")
    aligned = out / "aligned.apk"
    run(tools.tool("zipalign"), "-f", "-p", "4", unsigned, aligned)
    final = out / "webfixture.apk"
    tools.sign(aligned, final)
    print(f"\nBuilt {final}")


if __name__ == "__main__":
    main()
