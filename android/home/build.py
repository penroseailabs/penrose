"""Build the penrose home app (the agent-first launcher) into relay-home.apk with SDK tools only, the same way as
../bridge/build.py: javac, d8, aapt2, zipalign, apksigner. No Gradle.

    python android/home/build.py [--sdk D:/Android/Sdk] [--jdk <jdk 17>] [--out android/home/build]

Resources (res/) go through aapt2 compile + link, which also generates R.java, so the views carry named ids the
bridge dump shows (id=search, id=results). `python android/build.py` builds it into desktop/assets/android with the
rest; the desktop app installs it and makes it the home screen (backend/app/provision).
"""

import argparse
import shutil
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from toolchain import Tools, run  # noqa: E402

OUT = HERE / "build"


def build(out: Path = OUT, tools: Tools | None = None) -> Path:
    tools = tools or Tools()
    out = Path(out).resolve()
    shutil.rmtree(out, ignore_errors=True)
    classes, dex, gen = out / "classes", out / "dex", out / "gen"
    for d in (classes, dex, gen):
        d.mkdir(parents=True)

    # Resources first: link writes R.java, which the sources need.
    compiled = out / "res.zip"
    run(tools.tool("aapt2"), "compile", "--dir", HERE / "res", "-o", compiled)
    unsigned = out / "unsigned.apk"
    run(tools.tool("aapt2"), "link", "-o", unsigned, "--manifest", HERE / "AndroidManifest.xml",
        "-I", tools.android_jar, "--java", gen, "--auto-add-overlay", compiled)

    run(tools.javac, *tools.java8(), "-d", classes, *sorted((HERE / "src").rglob("*.java")), *sorted(gen.rglob("*.java")))
    run(tools.tool("d8"), "--release", "--min-api", "30", "--lib", tools.android_jar, "--output", dex,
        *sorted(classes.rglob("*.class")))
    with zipfile.ZipFile(unsigned, "a") as apk:
        apk.write(dex / "classes.dex", "classes.dex")
    aligned = out / "aligned.apk"
    run(tools.tool("zipalign"), "-f", "-p", "4", unsigned, aligned)
    final = out / "relay-home.apk"
    tools.sign(aligned, final)
    print(f"\nBuilt {final}")
    return final


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sdk")
    parser.add_argument("--jdk")
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args()
    build(Path(args.out), Tools(args.sdk, args.jdk))


if __name__ == "__main__":
    main()
