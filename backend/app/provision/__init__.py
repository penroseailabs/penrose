"""First-run Android setup for the desktop app, and the checks it runs on every launch.

    relay-server provision setup --home <dir> --assets <dir> [--sdk <dir>] [--accept-licenses] [--build-id <id>]
    relay-server provision device-ready --home <dir> --assets <dir> --serial emulator-5570 [--build-id <id>]
    relay-server provision status --home <dir> [--sdk <dir>]

`setup` makes the user's phone, each step skipped when already done:
  java         a Java runtime for Google's SDK tools (Temurin 17 JRE)
  sdk          Android command-line tools, platform-tools, the emulator, the Google Play system image
  tools        Magisk, Vector (the Xposed framework) and rootAVD, pinned versions
  golden       the golden AVD: Play image rooted with Magisk on a private ramdisk copy, Zygisk on, Vector,
               relay-bridge (the Xposed module the agents read apps through) with "include new apps",
               relay-autoscope; checked by asking Chrome's bridge for `hello`
  phone        the user's emulator, cloned from the golden AVD

`device-ready` runs after every boot and ends by bringing the phone up to date with the shipped assets
(manifest.py): each artifact whose hash differs from the phone's record is installed again, the phone reboots
once, and the record is rewritten, so "Update Relay" is the only update the user ever runs. A phone built on
an older base (`golden_generation`) is reported as `outdated`; it keeps working until the user resets it.

This is emulation_harness/provision/golden.py made to run on a user's machine: every path comes from
--home, nothing is cloned with git, and Windows, macOS and Linux are told apart. relay-bridge.apk and
relay-autoscope.zip are built in the harness and shipped with the app (--assets); they are not built here.

Progress goes to stdout as JSON lines for the Electron window:
  {"event": "step", "id", "title"}  {"event": "progress", "id", "percent", "doing"?}  {"event": "log", "text"}
`doing` names the part of a long step under way (see build_golden); the window words it for the user.
  {"event": "done", ...}  {"event": "error", "message", "hint"}  {"event": "outdated", "message", "hint"}
"""

import argparse
import base64
import contextlib
import hashlib
import json
import os
import platform
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import time
import traceback
import urllib.request
import zipfile
from pathlib import Path

from . import adbui, manifest

API = 35
GOLDEN = "relay-golden"
PHONE = "relay"
GOLDEN_SERIAL = "emulator-5580"  # off the default 5554, so it never meets an emulator the user runs
CMDLINE_TOOLS = "11076708"
MAGISK = ("v30.7", "https://github.com/topjohnwu/Magisk/releases/download/v30.7/Magisk-v30.7.apk")
VECTOR = ("v2.2", "https://github.com/JingMatrix/Vector/releases/download/v2.2/Vector-v2.2-3080-Release.zip")
ROOTAVD_COMMIT = "613caa44371f85e1a461bc030e07ddc2d71afe32"
ROOTAVD = f"https://gitlab.com/newbit/rootAVD/-/archive/{ROOTAVD_COMMIT}/rootAVD-{ROOTAVD_COMMIT}.zip"
VECTOR_CLI = "sh /data/adb/modules/zygisk_vector/cli"
WINDOWS = sys.platform == "win32"
MAC = sys.platform == "darwin"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class SetupError(Exception):
    """`message` and `hint` are shown to the user as they are: plain words, nothing to type or delete.
    Details for us go to the log."""

    def __init__(self, message, hint=""):
        super().__init__(message)
        self.hint = hint


# What the user does next, where the error is shown: the setup window has a Try again button; after launch,
# the phone panel has none. main() sets it for the command it runs.
AGAIN = "Press Try again. Setup picks up where it stopped and fixes what it can by itself."


# --- reporting -----------------------------------------------------------------------------------------


def emit(event, **data):
    """One JSON line for the window. `at` (local wall-clock time) lets the setup log show how long each part
    took, for us and for the setup agent."""
    print(json.dumps({"event": event, **data, "at": time.strftime("%H:%M:%S")}), flush=True)


def log(text):
    for line in str(text).splitlines():
        if line.strip():
            emit("log", text=line.rstrip())


def step(step_id, title):
    emit("step", id=step_id, title=title)


def doing(step_id, part, percent):
    """A long step's part starting: `percent` is how far the step is once this part begins."""
    emit("progress", id=step_id, percent=percent, doing=part)


# --- the host --------------------------------------------------------------------------------------------


def abi():
    """The system image's ABI: arm64 on Apple Silicon and ARM Linux, x86_64 everywhere else."""
    return "arm64-v8a" if platform.machine().lower() in ("arm64", "aarch64") else "x86_64"


def image_package():
    return f"system-images;android-{API};google_apis_playstore;{abi()}"


def host_os():
    return "windows" if WINDOWS else "mac" if MAC else "linux"


def exe(path: Path, name):
    """A tool in an SDK folder: name.exe / name.bat on Windows."""
    if WINDOWS:
        for suffix in (".exe", ".bat", ".cmd"):
            if (candidate := path / (name + suffix)).exists():
                return candidate
    return path / name


class Layout:
    """Where everything lives. `home` is the app's Android folder; `sdk` may be an SDK the user already has."""

    def __init__(self, home: Path, sdk: Path | None = None):
        self.home = home
        self.sdk = sdk or home / "sdk"
        self.tools = home / "tools"
        self.golden_home = home / "golden"
        self.sysimg = home / f"sysimg-android-{API}-{abi()}"
        self.avd_home = home / "avd"
        self.logs = home / "logs"

    @property
    def adb(self):
        return exe(self.sdk / "platform-tools", "adb")

    @property
    def emulator(self):
        return exe(self.sdk / "emulator", "emulator")

    @property
    def sdkmanager(self):
        return exe(self.sdk / "cmdline-tools" / "latest" / "bin", "sdkmanager")

    @property
    def avdmanager(self):
        return exe(self.sdk / "cmdline-tools" / "latest" / "bin", "avdmanager")

    @property
    def java_home(self):
        jre = next(iter(sorted(self.tools.glob("jdk-17*"))), None)
        if jre and MAC and (jre / "Contents" / "Home").exists():
            return jre / "Contents" / "Home"
        return jre

    def env(self, avd_home=None):
        env = {**os.environ, "ANDROID_HOME": str(self.sdk), "ANDROID_SDK_ROOT": str(self.sdk),
               "ANDROID_AVD_HOME": str(avd_home or self.avd_home)}
        if self.java_home:
            env["JAVA_HOME"] = str(self.java_home)
            env["PATH"] = str(self.java_home / "bin") + os.pathsep + env.get("PATH", "")
        env["PATH"] = str(self.sdk / "platform-tools") + os.pathsep + env["PATH"]
        return env

    def status(self):
        return {
            "sdk": str(self.sdk),
            "java": bool(self.java_home),
            "sdk_ready": self.adb.exists() and self.emulator.exists() and self.image_dir().exists(),
            "golden_ready": (self.golden_home / f"{GOLDEN}.avd" / "provisioned").exists(),
            "phone_ready": (self.avd_home / f"{PHONE}.ini").exists(),
            "abi": abi(),
            "emulator": str(self.emulator),
            "adb": str(self.adb),
            "avd_home": str(self.avd_home),
            "avd": PHONE,
        }

    def image_dir(self):
        return self.sdk / "system-images" / f"android-{API}" / "google_apis_playstore" / abi()


# --- downloads ---------------------------------------------------------------------------------------------


def fetch(url, dest: Path, step_id):
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    log(f"Downloading {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "relay-desktop"})
    with urllib.request.urlopen(request, timeout=60) as response, open(tmp, "wb") as out:
        total = int(response.headers.get("Content-Length") or 0)
        done, last = 0, -1
        while chunk := response.read(1 << 20):
            out.write(chunk)
            done += len(chunk)
            if total and (percent := done * 100 // total) != last:
                last = percent
                emit("progress", id=step_id, percent=percent)
    tmp.replace(dest)
    return dest


def extract(archive: Path, into: Path):
    into.mkdir(parents=True, exist_ok=True)
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                path = z.extract(info, into)
                mode = info.external_attr >> 16  # zip keeps unix permissions here; extract() drops them
                if mode & 0o111:
                    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    else:
        with tarfile.open(archive) as t:
            t.extractall(into, filter="data")


def run(args, step_id=None, env=None, cwd=None, input_text=None, check=True, timeout=None):
    """Run a tool, streaming its output as log lines; sdkmanager's percent bars become progress."""
    log("> " + " ".join(str(a) for a in args))
    proc = subprocess.Popen(
        [str(a) for a in args], env=env, cwd=cwd, stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
        creationflags=NO_WINDOW,
    )
    if input_text is not None:
        try:
            proc.stdin.write(input_text)
            proc.stdin.close()
        except OSError:
            pass
    output, last = [], -1
    buffer = ""
    deadline = time.monotonic() + timeout if timeout else None
    while True:
        char = proc.stdout.read(1)
        if not char:
            break
        if char not in "\r\n":
            buffer += char
            continue
        line, buffer = buffer.strip(), ""
        if not line:
            continue
        output.append(line)
        if step_id and "%" in line and "[" in line:  # sdkmanager: [=======    ] 45% Downloading...
            try:
                percent = int(line.split("]")[1].strip().split("%")[0])
            except (IndexError, ValueError):
                percent = None
            if percent is not None and percent != last:
                last = percent
                emit("progress", id=step_id, percent=percent)
            continue
        log(line)
        if deadline and time.monotonic() > deadline:
            proc.kill()
            log(f"{Path(str(args[0])).name} did not finish within {timeout} seconds")
            raise SetupError("A setup step took too long.", AGAIN)
    code = proc.wait()
    if check and code:
        log(f"{Path(str(args[0])).name} failed (exit {code})")
        raise SetupError("A setup step did not finish.",
                         f"Check that this computer is online and has free disk space. {AGAIN}")
    return "\n".join(output)


# --- steps -------------------------------------------------------------------------------------------------


def ensure_java(layout: Layout):
    if layout.java_home:
        return
    step("java", "Java runtime for the Android SDK tools")
    arch = "aarch64" if abi() == "arm64-v8a" else "x64"
    url = f"https://api.adoptium.net/v3/binary/latest/17/ga/{host_os()}/{arch}/jre/hotspot/normal/eclipse"
    archive = fetch(url, layout.tools / ("temurin17-jre" + (".zip" if WINDOWS else ".tar.gz")), "java")
    extract(archive, layout.tools)
    archive.unlink()
    if not layout.java_home:
        raise SetupError("A download did not unpack properly.", AGAIN)


def ensure_sdk(layout: Layout, accept_licenses: bool):
    if not layout.sdkmanager.exists():
        step("cmdline-tools", "Android SDK command-line tools")
        name = {"windows": "win", "mac": "mac", "linux": "linux"}[host_os()]
        archive = fetch(f"https://dl.google.com/android/repository/commandlinetools-{name}-{CMDLINE_TOOLS}_latest.zip",
                        layout.tools / "cmdline-tools.zip", "cmdline-tools")
        unpack = layout.tools / "cmdline-tools-unpack"
        shutil.rmtree(unpack, ignore_errors=True)
        extract(archive, unpack)
        target = layout.sdk / "cmdline-tools" / "latest"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(target, ignore_errors=True)
        shutil.move(str(unpack / "cmdline-tools"), target)
        shutil.rmtree(unpack, ignore_errors=True)
        archive.unlink()
    packages = []
    if not layout.adb.exists():
        packages.append("platform-tools")
    if not layout.emulator.exists():
        packages.append("emulator")
    if not layout.image_dir().exists():
        packages.append(image_package())
    if not packages:
        return
    if not accept_licenses:
        raise SetupError("The Android SDK licenses have not been accepted.", "licenses")
    step("sdk", "Android emulator and Google Play system image (about 3 GB)")
    run([layout.sdkmanager, "--licenses"], env=layout.env(), input_text="y\n" * 20, check=False)
    run([layout.sdkmanager, "--install", *packages], step_id="sdk", env=layout.env())
    if not layout.image_dir().exists():
        raise SetupError("Android did not finish downloading.",
                         f"Check that this computer is online and has about 12 GB free. {AGAIN}")


def check_acceleration(layout: Layout):
    result = subprocess.run([str(layout.emulator), "-accel-check"], env=layout.env(), capture_output=True, text=True,
                            errors="replace", creationflags=NO_WINDOW, timeout=60)
    log(result.stdout + result.stderr)
    if result.returncode == 0:
        return
    hint = {
        "windows": "Open the Start menu, search for “Turn Windows features on or off”, tick “Windows Hypervisor "
                   "Platform”, press OK and restart the computer. Then open penrose and press Try again.",
        "mac": "penrose needs macOS 12 (Monterey) or newer. Update macOS in System Settings, then press Try again.",
        "linux": "Turn on KVM virtualization (the kvm kernel module and access to /dev/kvm), then press Try again.",
    }[host_os()]
    raise SetupError("This computer can’t run the Android phone yet.", hint)


def ensure_tools(layout: Layout):
    step("tools", "Root and Xposed tools (Magisk, Vector, rootAVD)")
    magisk = fetch(MAGISK[1], layout.tools / f"Magisk-{MAGISK[0]}.apk", "tools")
    vector = fetch(VECTOR[1], layout.tools / Path(VECTOR[1]).name, "tools")
    rootavd = layout.tools / "rootAVD"
    if not (rootavd / "rootAVD.sh").exists():
        archive = fetch(ROOTAVD, layout.tools / "rootAVD.zip", "tools")
        unpack = layout.tools / "rootAVD-unpack"
        shutil.rmtree(unpack, ignore_errors=True)
        extract(archive, unpack)
        shutil.rmtree(rootavd, ignore_errors=True)
        shutil.move(str(next(unpack.iterdir())), rootavd)
        shutil.rmtree(unpack, ignore_errors=True)
        archive.unlink()
    (rootavd / "rootAVD.sh").chmod(0o755)
    # rootAVD installs whatever Magisk.zip sits next to it; pin the version fetched above.
    shutil.copyfile(magisk, rootavd / "Magisk.zip")
    rootavd_offline(rootavd / "rootAVD.sh")
    return {"magisk": magisk, "vector": vector, "rootavd": rootavd}


ROOTAVD_ONLINE_CHECK = "CheckAVDIsOnline() {\n"
ROOTAVD_OFFLINE = "AVDIsOnline=false  # Relay: Magisk is pinned and next to rootAVD; never look online\n"


def rootavd_offline(script: Path):
    """Have rootAVD take the phone as offline. Online, it asks GitHub for the Magisk versions there before using
    the pinned one next to it anyway: seconds at best, and over two minutes when GitHub was slow to answer from
    inside the phone. Its check is skipped when AVDIsOnline is already set. rootAVD.sh is what runs inside the
    phone on every platform (rootAVD.bat pushes it too). Idempotent."""
    text = script.read_text(encoding="utf-8")
    if ROOTAVD_OFFLINE in text:
        return
    if ROOTAVD_ONLINE_CHECK not in text:
        log("rootAVD has no online check to skip; it may look for Magisk versions online")
        return
    with open(script, "w", encoding="utf-8", newline="") as f:  # it runs in the phone's shell: keep its LF lines
        f.write(text.replace(ROOTAVD_ONLINE_CHECK, ROOTAVD_OFFLINE + ROOTAVD_ONLINE_CHECK, 1))


def private_image(layout: Layout):
    """The golden AVD boots from its own copy of the system image: hard links, except ramdisk.img, which
    Magisk patches. The SDK's shared image (other AVDs, Android Studio) is never modified."""
    source, target = layout.image_dir(), layout.sysimg
    target.mkdir(parents=True, exist_ok=True)
    for f in source.iterdir():
        dest = target / f.name
        if dest.exists():
            continue
        if f.is_dir():
            shutil.copytree(f, dest)
        elif f.name == "ramdisk.img":
            shutil.copyfile(f, dest)
        else:
            try:
                os.link(f, dest)
            except OSError:  # another drive or a filesystem without hard links
                shutil.copyfile(f, dest)


def create_golden_avd(layout: Layout):
    ini = layout.golden_home / f"{GOLDEN}.ini"
    if ini.exists():
        return
    layout.golden_home.mkdir(parents=True, exist_ok=True)
    run([layout.avdmanager, "create", "avd", "-n", GOLDEN, "-k", image_package(), "-d", "pixel_6"],
        env=layout.env(layout.golden_home), input_text="no\n")
    config = layout.golden_home / f"{GOLDEN}.avd" / "config.ini"
    lines = [f"image.sysdir.1={layout.sysimg}{os.sep}" if line.startswith("image.sysdir.1=") else line
             for line in config.read_text().splitlines()]
    config.write_text("\n".join(lines) + "\n")
    avd_config(config, {**AVD_SETTINGS, **DISK})


# The phone's storage (/data), set when the image is built: Android encrypts and formats it on that first boot, and
# every phone cloned from the image inherits it. The disk file on the computer grows only as the phone fills it, so
# 64 GB reserves nothing; avdmanager's default of a few GB ran out once the user's apps were installed. Not in
# AVD_SETTINGS: changing the size of a phone that already holds data would grow a disk Android has encrypted.
DISK = {"disk.dataPartition.size": "64G"}

# 720p rather than the Pixel 6's 1080x2400: the phone draws and the emulator captures about 55% fewer pixels.
# 280 dpi keeps the Pixel 6's 411 dp width, so apps lay out as they did at 1080p.
SCREEN = {"hw.lcd.width": "720", "hw.lcd.height": "1600", "hw.lcd.density": "280"}
# A front camera, as every phone has (the Galaxy S25 of relay-device-profile.sh too): selfies and ID checks need
# one, and the Pixel 6 AVD comes without. "emulated" shows the emulator's test scene; "webcam0" would show the
# computer's own camera.
CAMERA = {"hw.camera.front": "emulated"}
AVD_SETTINGS = {**SCREEN, **CAMERA}


def avd_config(config: Path, settings=AVD_SETTINGS):
    """Give an AVD's config.ini the settings (by default AVD_SETTINGS: the screen size, the front camera). They take
    effect on the next start (a cold boot, as the quick-boot snapshot was saved with the old hardware). True when it
    changed anything."""
    try:
        lines = config.read_text().splitlines()
    except OSError:
        return False
    values = dict(line.split("=", 1) for line in lines if "=" in line)
    if all(values.get(key, "").strip() == value for key, value in settings.items()):
        return False
    kept = [line for line in lines if line.split("=", 1)[0].strip() not in settings]
    config.write_text("\n".join(kept + [f"{key}={value}" for key, value in settings.items()]) + "\n")
    return True


def booted(serial):
    try:
        return adbui.adb(serial, "shell", "getprop sys.boot_completed", check=False, timeout=15).strip() == "1"
    except subprocess.TimeoutExpired:
        return False


BOOTED = '[ "$(getprop sys.boot_completed)" = 1 ]'


def wait_boot(serial, wait=420, process=None):
    deadline = time.monotonic() + wait

    def closed():
        return process is not None and process.poll() is not None

    while time.monotonic() < deadline:
        if closed():
            log("the emulator exited while booting; see golden-emulator.log")
            raise SetupError("The Android phone closed while it was starting.",
                             f"Close other large apps to free up memory. {AGAIN}")
        # One wait on the phone until it has booted. It ends early only when adb loses the phone while it starts,
        # and then it is asked again.
        if adbui.wait_on_phone(serial, BOOTED, deadline - time.monotonic(), stop=closed):
            # On a phone with a screen-lock PIN (Relay's own for simplified sign-ins, or the old fixed one, which
            # device_ready then removes), app data opens once relay-bridge has checked it in SystemUI. On a busy
            # computer SystemUI can start before Vector is ready to hand it modules, and then nothing enters
            # the PIN: SystemUI started again after a while gets the bridge.
            if not adbui.wait_unlocked(serial, timeout=min(60, max(10, deadline - time.monotonic()))):
                log("the phone booted locked; restarting SystemUI so the bridge can enter the PIN")
                adbui.restart_systemui(serial)
                adbui.shell(serial, "input keyevent 224", check=False)
                if not adbui.wait_unlocked(serial, timeout=max(30, deadline - time.monotonic())):
                    raise SetupError("The phone started but stayed locked.",
                                     f"penrose’s screen-lock helper didn’t start. {AGAIN}")
            wait_services(serial)
            return
        time.sleep(1)
    log(f"Android did not finish booting within {wait} seconds")
    raise SetupError("The Android phone took too long to start.",
                     f"This can happen when the computer is busy. Close other large apps. {AGAIN}")


SERVICES_CHECK = ('pm path android | grep -q package: && [ "$(am get-current-user)" = 0 ] '
                  '&& [ "$(settings get global device_provisioned)" = 1 ]')


def wait_services(serial, wait=10):
    """The package, activity and settings services answer, which is what the steps after boot use (`am
    set-debug-app` fails before the activity manager is up). One adb call, which on a phone that has been up for a
    while answers at once: this replaces a fixed 10 s pause after every boot, a quick-boot resume included."""
    if adbui.wait_on_phone(serial, SERVICES_CHECK, wait, every=0.5):  # each check starts three Java tools
        return True
    log("Android's services were slow to answer after boot; carrying on")
    return False


BOOT_ID = "/proc/sys/kernel/random/boot_id"


def read_boot_id(serial, wait=15):
    """The kernel's id for this boot: a new one every time Android starts. A phone that is down is waited for, up
    to `wait` seconds; empty when it does not come back in that time."""
    try:
        value = adbui.adb(serial, "wait-for-device", "shell", f"cat {BOOT_ID}", check=False, timeout=wait).strip()
    except subprocess.TimeoutExpired:
        return ""
    return value if len(value) == 36 and value.count("-") == 4 else ""


def boot_id(serial, wait=120):
    """The current boot's id, from a phone that is up. How a restart is told apart from a phone that has not
    gone down yet, on any computer at any speed, instead of pausing for a guessed number of seconds."""
    deadline = time.monotonic() + wait
    while not (value := read_boot_id(serial, max(1, deadline - time.monotonic()))):
        if time.monotonic() > deadline:
            log("the phone does not answer adb")
            raise SetupError("The Android phone stopped responding.", AGAIN)
        time.sleep(1)  # adb lost the phone just after finding it
    return value


def wait_reboot(serial, before, wait=420):
    """Wait until the phone has restarted since `before` (its boot id then) and finished booting.
    False when it never went down."""
    deadline = time.monotonic() + 240
    # Waited for on the phone: the wait ends when the phone goes down, and the next one waits for it to come back.
    restarted = f'b=$(cat {BOOT_ID}); [ -n "$b" ] && [ "$b" != {before} ]'
    while not adbui.wait_on_phone(serial, restarted, deadline - time.monotonic()):
        if time.monotonic() > deadline:
            return False
        time.sleep(1)
    wait_boot(serial, wait)
    return True


def reboot(serial, wait=420):
    before = boot_id(serial)
    adbui.su(serial, "reboot", check=False)
    if not wait_reboot(serial, before, wait):
        log("the phone was asked to restart and did not")
        raise SetupError("The Android phone did not restart.", AGAIN)


# --- the phone's clock ---------------------------------------------------------------------------------------------
# An x86 phone's kernel keeps time by the processor's counter (tsc), which costs nothing to read. When it stops
# trusting it, it falls back to the emulated timer (hpet), where every read of the clock is a trip out to the
# emulator: apps start 1.5 to 2 times slower and stop responding (Amazon, 2026-11-22), and it stays that way until
# the kernel starts again. Two ways it gets there under Windows' hypervisor, both reproduced on test phones:
# - while running: the kernel compares tsc with hpet twice a second. The emulator's hpet stops whenever the
#   emulator stops the phone for a moment, tsc does not, and a stop of a few tenths of a second reads as tsc having
#   run fast ("tsc skewed 387 ms over watchdog hpet"). FAST_CLOCK on the kernel's command line switches that
#   comparison off. Not tsc=reliable: that also drops the kernel's correction of each processor's counter at boot,
#   and time then ran backwards between processors (system_server restarted, 2026-11-12).
# - at boot: the emulator sets each processor's counter at a different moment; the kernel corrects it and now and
#   then finds a few ticks left (6 boots in 20), and starts on hpet. No kernel option helps: the phone is started
#   again, here, before it is in use.
# A quick-boot snapshot carries the kernel as it was, the slow clock included.
CLOCK_SOURCE = "/sys/devices/system/clocksource/clocksource0/current_clocksource"
FAST_CLOCK = "tsc=nowatchdog"  # given to the kernel by whoever starts the emulator (desktop/src/main.js, scripts/test_phone.py)
CLOCK_RESTARTS = 5  # a boot lands on the slow clock 6 times in 20 with 4 processors and more often with 6 (3 of the
# user's phone's 4 on 2026-11-22, while penrose itself was starting): six in a row is rare


def read_clock(serial):
    """(the kernel's clock source, its command line); empty strings when the phone does not say (no root yet)."""
    try:
        out = adbui.su(serial, f"cat {CLOCK_SOURCE}; cat /proc/cmdline", check=False, timeout=30)
    except subprocess.TimeoutExpired:
        return "", ""
    lines = [line.strip() for line in (out or "").splitlines() if line.strip()]
    if len(lines) < 2 or " " in lines[0]:  # an error message, not a clock source
        return "", ""
    return lines[0], lines[1]


def fast_clock(serial, guarded=False, restart=None):
    """After boot, before anything uses the phone: a kernel on the slow clock is started again (a restart of
    Android inside the running emulator, about 25 s), up to CLOCK_RESTARTS times. `guarded`: the emulator was
    started with FAST_CLOCK, so a kernel that came from an older quick-boot snapshot without it is started again
    once too, to run with it from now on. Returns how many times the phone was restarted. Never called on a phone
    in use: device-ready holds the phone until it is done."""
    restart = restart or reboot
    for restarts in range(CLOCK_RESTARTS + 1):
        source, cmdline = read_clock(serial)
        slow = source == "hpet"
        unguarded = guarded and source == "tsc" and FAST_CLOCK not in cmdline.split() and restarts == 0
        if not slow and not unguarded:
            if restarts:
                log(f"the phone's kernel is on its fast clock ({source}) after {restarts} restart(s)")
            return restarts
        if restarts == CLOCK_RESTARTS:
            break
        if slow:
            # Why, in the kernel's words, while they are still in its log: they scroll out within hours.
            said = adbui.su(serial, 'dmesg | grep -v audit | grep -iE "clocksource|tsc" | tail -n 8', check=False, timeout=30) or ""
            log("the phone's kernel is on its slow clock (hpet): restarting it. Kernel: " + (" | ".join(line.strip() for line in said.splitlines() if line.strip()) or "nothing in its log"))
        else:
            log(f"the phone's kernel was started without {FAST_CLOCK} (an earlier quick-boot snapshot): restarting it once")
        step("clock", "Restarting your phone")
        restart(serial)
    log(f"the phone's kernel is still on its slow clock after {CLOCK_RESTARTS} restarts; it is used as it is (slower)")
    return CLOCK_RESTARTS


def start_golden(layout: Layout):
    if booted(GOLDEN_SERIAL):
        return None
    layout.logs.mkdir(parents=True, exist_ok=True)
    port = GOLDEN_SERIAL.split("-")[1]
    process = subprocess.Popen(
        [str(layout.emulator), "-avd", GOLDEN, "-port", port, "-no-window", "-no-audio", "-no-boot-anim",
         "-gpu", "host", "-memory", "3072", "-no-snapshot"],
        env=layout.env(layout.golden_home), stdout=open(layout.logs / "golden-emulator.log", "w"),
        stderr=subprocess.STDOUT, creationflags=NO_WINDOW,
    )
    wait_boot(GOLDEN_SERIAL, process=process)
    return process


def golden_running():
    return GOLDEN_SERIAL in adbui.adb(GOLDEN_SERIAL, "devices", check=False)


def shut_down_golden(wait=300):
    """Power the phone off the way Android does, so what it wrote last (an app it just installed) is saved.
    `emu kill` cuts the power at once and can lose that; it is used only if the phone never goes off."""
    if golden_running():
        adbui.shell(GOLDEN_SERIAL, "reboot -p", check=False, timeout=60)
        wait_gone(wait)
    stop_golden()


def wait_gone(wait):
    """Wait until adb no longer lists the golden phone: one adb call that returns when it goes."""
    with contextlib.suppress(subprocess.TimeoutExpired):
        adbui.adb(GOLDEN_SERIAL, "wait-for-disconnect", check=False, timeout=wait)


def stop_golden():
    adbui.adb(GOLDEN_SERIAL, "emu", "kill", check=False)
    wait_gone(60)
    # The emulator exits just after adb lets go of it (about 50 ms here), and only then frees its files, which
    # Windows will not delete while it runs: its ports are free once it has exited. At most the 4 s this used to be.
    port = int(GOLDEN_SERIAL.split("-")[1])
    deadline = time.monotonic() + 4
    while not (ports_free(port, port + 1) or time.monotonic() > deadline):
        time.sleep(0.1)


def ports_free(*ports):
    """No program on this computer listens on these ports (checked here, without starting anything)."""
    for port in ports:
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                return False
    return True


def patch_ramdisk(layout: Layout, rootavd: Path):
    if (layout.sysimg / "ramdisk.img.backup").exists():
        return False
    # rootAVD resolves the ramdisk relative to ANDROID_HOME and uses adb from PATH.
    env = {**layout.env(layout.golden_home), "ANDROID_HOME": str(layout.sysimg.parent), "ANDROID_SERIAL": GOLDEN_SERIAL}
    relative = f"{layout.sysimg.name}{os.sep}ramdisk.img"
    if WINDOWS:
        # rootAVD.bat calls Windows' find.exe; a Unix `find` earlier on PATH (Git, MSYS) breaks its checks.
        system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
        env["PATH"] = f"{system32}{os.pathsep}{env['PATH']}"
        # By full path: cmd may not search the current folder (NoDefaultCurrentDirectoryInExePath).
        run(["cmd", "/c", rootavd / "rootAVD.bat", relative], env=env, cwd=rootavd, input_text="\n", timeout=900)
    else:
        # By name from its own folder: rootAVD.sh works out its folder from how it was called, and unquoted,
        # so a full path with a space in it (macOS's "Application Support") would split.
        run(["bash", "rootAVD.sh", relative], env=env, cwd=rootavd, input_text="\n", timeout=900)
    if not (layout.sysimg / "ramdisk.img.backup").exists():
        log("rootAVD finished without patching the ramdisk")
        raise SetupError("The phone image could not be prepared.", AGAIN)
    # rootAVD has just installed the Magisk app and asks the phone to shut down, which can take a while on a
    # slow computer. Power it off cleanly (a hard stop can lose the new app), so the next start is a cold
    # boot from the patched ramdisk.
    shut_down_golden()
    return True


MAGISK_APP = "com.topjohnwu.magisk"
# How long root may take in all. Each wait inside is generous too: on a slow computer (seen on a MacBook Air)
# Magisk takes about 30 seconds to open and every screen read takes 7 to 15 seconds.
ROOT_WAIT = 900


def su_installed(serial=GOLDEN_SERIAL):
    """Magisk puts `su` in place once its first-run setup is done; until then its log says
    "Magisk environment incomplete" and the app asks for "additional setup"."""
    return "/su" in adbui.shell(serial, "which su", check=False)


def magisk_launcher(serial):
    found = adbui.shell(serial, f"cmd package resolve-activity --brief -c android.intent.category.LAUNCHER {MAGISK_APP}",
                        check=False).strip().splitlines()
    return found[-1] if found and found[-1].startswith(MAGISK_APP + "/") else None


def ensure_magisk_app(serial, apk: Path | None):
    """The Magisk app must be installed and openable. A phone switched off right after rootAVD installed it
    can keep the app's files but not its entry ("missing metadata"): then it never opens, so reinstall it."""
    if magisk_launcher(serial) or apk is None:
        return
    log("the Magisk app cannot be opened; installing it again")
    log(adbui.adb(serial, "install", "-r", str(apk), check=False, timeout=300))


def open_magisk(serial):
    adbui.shell(serial, "input keyevent KEYCODE_WAKEUP", check=False)
    found = [magisk_launcher(serial)]
    try:
        if found[-1]:
            adbui.shell(serial, f"am start -W -n {found[-1]}", check=False, timeout=120)  # returns once it is shown
        else:
            adbui.shell(serial, f"monkey -p {MAGISK_APP} -c android.intent.category.LAUNCHER 1", check=False)
    except subprocess.TimeoutExpired:
        pass  # still opening; the waits that follow cover it


def magisk_additional_setup(serial):
    """Answer OK to Magisk's "Requires additional setup": it finishes installing itself and restarts the phone."""
    before = boot_id(serial)
    open_magisk(serial)
    if adbui.tap_text(serial, "OK", when="Requires additional setup", timeout=180):
        log("Magisk is finishing its setup; the phone restarts")
        if not wait_reboot(serial, before):
            log("the phone did not restart after Magisk's setup")
    else:
        log("Magisk did not ask for its additional setup yet")


def request_root(serial):
    """Ask for root from the adb shell and press Grant on Magisk's prompt. True once granted."""
    request = subprocess.Popen([adbui.ADB, "-s", serial, "shell", "su -c id"], stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                               creationflags=NO_WINDOW)
    # Granted before (a resumed setup) answers at once; otherwise the prompt waits for Grant.
    adbui.tap_text(serial, "Grant", timeout=120, stop=lambda: request.poll() is not None)
    try:
        output, _ = request.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        request.kill()
        output, _ = request.communicate()
    return "uid=0" in (output or "")


def allow_shell_in_magisk(serial):
    """A prompt that was refused or timed out can be remembered as a refusal: switch the shell back on in
    Magisk's Superuser list."""
    open_magisk(serial)
    if not adbui.tap_text(serial, "Superuser", timeout=120):
        return
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:  # the list loads after the tab opens
        switches = [n for n in adbui.nodes(serial) if n.get("checkable") == "true" and "Switch" in n.get("class", "")]
        for node in switches:
            if node.get("checked") == "false":
                x, y = node["center"]
                adbui.shell(serial, f"input tap {x} {y}")
        if switches:
            time.sleep(3)
            return
        time.sleep(2)


def finish_magisk(serial=GOLDEN_SERIAL, apk: Path | None = None):
    """Root for the adb shell. Driven by what the phone shows, not by fixed pauses: Magisk's first-run
    setup (it restarts the phone), then its Grant prompt, then its Superuser list if a refusal was
    remembered; each repeated until it works or ROOT_WAIT runs out, so a slow computer only takes longer."""
    deadline = time.monotonic() + ROOT_WAIT
    while time.monotonic() < deadline:
        ensure_magisk_app(serial, apk)
        adbui.shell(serial, f"pm grant {MAGISK_APP} android.permission.POST_NOTIFICATIONS", check=False)
        if not su_installed(serial):
            magisk_additional_setup(serial)
            continue
        if request_root(serial):
            log("root granted")
            return
        log("root not granted yet; checking Magisk's Superuser list")
        allow_shell_in_magisk(serial)
    raise SetupError("Android did not give the setup the access it needs.", AGAIN)


def install_module(serial, zip_path: Path, module_id):
    """Install (or upgrade in place) a Magisk module. Magisk stages it and applies it at the next boot."""
    remote = f"/data/local/tmp/{module_id}.zip"
    adbui.adb(serial, "push", str(zip_path), remote)
    log(adbui.su(serial, f"magisk --install-module {remote}"))
    adbui.shell(serial, f"rm {remote}", check=False)


def install_framework(assets: Path, vector: Path | None, serial=GOLDEN_SERIAL):
    """Zygisk on, Vector + relay-autoscope as Magisk modules, relay-bridge installed. True when a reboot is
    needed: Magisk applies module installs at boot."""
    changed = False
    adbui.su(serial, "magisk --sqlite \"REPLACE INTO settings (key,value) VALUES('zygisk',1)\"")
    modules = adbui.su(serial, "ls /data/adb/modules", check=False).split()
    for zip_path, module_id in ((vector, "zygisk_vector"), (assets / "relay-autoscope.zip", "relay_autoscope")):
        if module_id in modules:
            continue
        if zip_path is None:
            raise SetupError("Your phone is missing a part penrose needs.", "Reset the phone in Settings to rebuild it.")
        install_module(serial, zip_path, module_id)
        changed = True
    # A run picked up after a failure installs the bridge only when the phone's copy differs, and then restarts
    # so the apps that start at boot (SystemUI, which enters the PIN) load the new code.
    if not bridge_installed(serial, assets / "relay-bridge.apk"):
        log(adbui.adb(serial, "install", "-r", str(assets / "relay-bridge.apk")))
        changed = True
    vector_running = "vectord" in adbui.shell(serial, "ps -A", check=False)
    return changed or not vector_running


def bridge_installed(serial, apk: Path):
    """The phone has exactly this relay-bridge.apk installed."""
    path = adbui.shell(serial, "pm path app.relay.bridge", check=False).strip().removeprefix("package:")
    if not path.endswith(".apk"):
        return False
    on_phone = adbui.shell(serial, f"sha256sum {path}", check=False).split()
    return bool(on_phone) and on_phone[0] == hashlib.sha256(apk.read_bytes()).hexdigest()


def enable_module(serial=GOLDEN_SERIAL):
    # Asked again on the phone until Vector takes it (it may still be starting), in one call.
    enable = f"{VECTOR_CLI} modules enable app.relay.bridge | grep -qF \"Enabled: [app.relay.bridge]\""
    if "module-enabled" in adbui.su(serial, f"timeout 90 sh -c 'until {enable}; do sleep 2; done' && echo module-enabled",
                                    check=False, timeout=120):
        return
    log("Vector did not accept the relay-bridge module")
    raise SetupError("A part of the phone did not switch on.", AGAIN)


def include_new_apps(work: Path, serial=GOLDEN_SERIAL):
    """Vector's "include new apps" for relay-bridge (no CLI switch exists): back up Vector's database, set
    modules.auto_include in the copy, restore it.

    True when it restored the database."""
    import sqlite3

    remote, shared = "/data/local/tmp/relay-vector.db", "/sdcard/relay-vector.db"
    work.mkdir(parents=True, exist_ok=True)
    local = work / "vector.db"

    def backup():
        out = adbui.su(serial, f"{VECTOR_CLI} db backup {remote} && cp {remote} {shared} && chmod 644 {shared}")
        if "backed up" not in out:
            log(f"Vector did not back up its database: {out.strip()}")
            raise SetupError("A part of the phone did not switch on.", AGAIN)
        local.unlink(missing_ok=True)
        adbui.adb(serial, "pull", shared, str(local))
        with contextlib.closing(sqlite3.connect(local)) as db, db:
            row = db.execute("SELECT auto_include FROM modules WHERE module_pkg_name='app.relay.bridge'").fetchone()
        if row is None:
            log("relay-bridge is not a Vector module on the phone")
            raise SetupError("A part of the phone did not switch on.", AGAIN)
        return bool(row[0])

    try:
        if backup():
            return False
        with contextlib.closing(sqlite3.connect(local)) as db, db:
            db.execute("UPDATE modules SET auto_include=1 WHERE module_pkg_name='app.relay.bridge'")
        adbui.adb(serial, "push", str(local), shared)
        if "restored" not in adbui.su(serial, f"cp {shared} {remote} && {VECTOR_CLI} db restore {remote}"):
            log("Vector did not restore its database")
            raise SetupError("A part of the phone did not switch on.", AGAIN)
        if not backup():
            log("Vector's 'include new apps' is still off after the restore")
            raise SetupError("A part of the phone did not switch on.", AGAIN)
        rescope(serial)
        return True
    finally:
        adbui.su(serial, f"rm -f {remote} {shared}", check=False)
        shutil.rmtree(work, ignore_errors=True)


AUTOSCOPE = "sh /data/adb/modules/relay_autoscope/service.sh"


def rescope(serial):
    """Run relay-autoscope's once-per-boot scope pass now. Vector's database restore (include_new_apps) puts back
    the scope as it was at the backup: when autoscope's pass at boot landed between the two, Chrome and the other
    apps it added were gone, and autoscope does not run again until the next boot. The pass is idempotent."""
    adbui.su(serial, AUTOSCOPE, check=False, timeout=180)


def wait_chrome_scoped(serial=GOLDEN_SERIAL):
    """relay-autoscope has put Chrome in the bridge's scope, so Chrome loads the bridge the next time it starts.
    If it has not within a while, its pass is run once more rather than waiting for a boot that never comes."""
    if chrome_scoped(serial, 30):
        return
    log("Chrome is not in the bridge's scope yet; running relay-autoscope again")
    rescope(serial)
    if not chrome_scoped(serial, 90):
        log("relay-autoscope did not add Chrome to the bridge's scope")
        raise SetupError("The phone's final check did not pass.", AGAIN)


def chrome_scoped(serial, wait):
    """Chrome is in the bridge's scope within `wait` seconds: one root call that checks on the phone."""
    scoped = f"{VECTOR_CLI} --json scope ls app.relay.bridge | grep -qF com.android.chrome"
    return "chrome-scoped" in adbui.su(serial, f"timeout {wait} sh -c 'until {scoped}; do sleep 1; done' && echo chrome-scoped",
                                       check=False, timeout=wait + 30)


HOME_SOCKET = f"relaybridge.{adbui.HOME_PACKAGE}"


def bridge_hello(serial, name):
    """One `hello` to a bridge socket, through an adb forward removed after. The reply line, or "" when none."""
    port = adbui.adb(serial, "forward", "tcp:0", f"localabstract:{name}").strip()
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=10) as s:
            s.sendall(b'{"cmd": "hello"}\n')
            return s.makefile().readline().strip()
    except OSError:
        return ""
    finally:
        adbui.adb(serial, "forward", "--remove", f"tcp:{port}", check=False)


def verify_bridge(serial=GOLDEN_SERIAL, golden=True):
    """End to end: the bridge loaded into an app and answering `hello`. Through the Relay home app, which runs as
    the home screen, so nothing has to be opened: a socket appears in it a few seconds after boot. Root,
    Vector and the module are the same for every app, so this stands for Chrome too, whose scope is checked
    on its own. On the golden image (nobody looking) the home screen is shown first, in case it has not
    started yet. On the user's phone nothing is opened: if the home app is not running there is nothing to ask,
    and the next look at it shows any problem. Golden images built without the home app still use Chrome."""
    wait_chrome_scoped(serial)
    if "package:" not in adbui.shell(serial, f"pm path {adbui.HOME_PACKAGE}", check=False):
        if golden:
            verify_chrome_bridge(serial)
        return
    if golden:
        adbui.shell(serial, "am start -W -a android.intent.action.MAIN -c android.intent.category.HOME", check=False)
    elif not adbui.shell(serial, f"pidof {adbui.HOME_PACKAGE}", check=False).strip():
        log("the penrose home app is not running; the bridge check waits for the next look")
        return
    if not adbui.wait_on_phone(serial, f"grep -qF @{HOME_SOCKET} /proc/net/unix", 60):
        log("the penrose home app started without the bridge")
        raise SetupError("The phone's final check did not pass.", AGAIN)
    reply = bridge_hello(serial, HOME_SOCKET)
    if not reply:
        log("the bridge in the penrose home app did not answer hello")
        raise SetupError("The phone's final check did not pass.", AGAIN)
    log("bridge hello: " + reply)


def verify_chrome_bridge(serial=GOLDEN_SERIAL):
    """The old check: launch Chrome and ask its bridge for `hello`. Golden image only (it opens Chrome)."""
    adbui.shell(serial, "am force-stop com.android.chrome")
    # Chrome's first start on a fresh phone is slow, more so on a slow computer: wait for the bridge's socket
    # rather than a fixed time, and open Chrome again if it has gone away meanwhile.
    deadline = time.monotonic() + 240
    started = False
    while "@relaybridge.com.android.chrome" not in adbui.shell(serial, "cat /proc/net/unix", check=False):
        running = bool(adbui.shell(serial, "pidof com.android.chrome", check=False).strip())
        started = started or running
        if time.monotonic() > deadline:
            log("Chrome started without the bridge" if started else "Chrome did not start")
            raise SetupError("The phone's final check did not pass.", AGAIN)
        if not running:
            adbui.launch(serial, "com.android.chrome")
        time.sleep(3)
    log("bridge hello: " + bridge_hello(serial, "relaybridge.com.android.chrome"))


def install_home(assets: Path, expected: dict, serial=GOLDEN_SERIAL):
    """The Relay home app on the golden image, as its home screen; every phone cloned from it starts there."""
    for artifact in expected["artifacts"]:
        if artifact.get("home"):
            log(adbui.adb(serial, "install", "-r", "-g", str(assets / artifact["file"])))
            if not adbui.home_setup(serial):
                log("the penrose home app did not become the home screen")
                raise SetupError("A part of the phone did not switch on.", AGAIN)


def clean_golden():
    """No session traces in the image every phone starts from."""
    serial = GOLDEN_SERIAL
    adbui.shell(serial, "am force-stop com.android.chrome", check=False)
    adbui.shell(serial, "pm clear com.android.chrome", check=False)
    adbui.shell(serial, "rm -f /sdcard/relay-ui.xml /data/local/tmp/*.zip", check=False)
    adbui.shell(serial, "cmd overlay enable-exclusive --user current --category com.android.internal.systemui.navbar.threebutton", check=False)
    adbui.chrome_debug_setup(serial)
    adbui.chrome_reset_tabs(serial)
    adbui.notification_listener_setup(serial)
    adbui.disable_unused_apps(serial)
    adbui.play_auto_updates_off(serial)
    adbui.keyboard_not_stylus(serial)
    adbui.remove_display_cutout(serial)


def golden_generation(layout: Layout):
    """The base the golden image was built on; goldens from before the marker are the first generation."""
    marker = layout.golden_home / f"{GOLDEN}.avd" / "generation"
    try:
        return int(marker.read_text().strip())
    except (OSError, ValueError):
        return 1


def build_golden(layout: Layout, assets: Path, tools, build_id=""):
    done_marker = layout.golden_home / f"{GOLDEN}.avd" / "provisioned"
    if done_marker.exists():
        return
    expected = manifest.expected(assets)
    step("golden", "Building the agent-ready Android image (10-20 minutes, once)")
    # Each part is announced as it starts, so the window can say what is happening during the long waits.
    doing("golden", "image", 0)
    private_image(layout)
    create_golden_avd(layout)
    doing("golden", "boot", 5)
    start_golden(layout)
    doing("golden", "root", 15)
    if patch_ramdisk(layout, tools["rootavd"]):
        doing("golden", "restart", 35)
        start_golden(layout)
    doing("golden", "access", 40)
    finish_magisk(apk=tools["magisk"])
    doing("golden", "framework", 55)
    scripts = [a for a in expected["artifacts"] if a["kind"] == "boot-script"]
    for artifact in scripts:
        apply_artifact(GOLDEN_SERIAL, assets, layout.home / "vector-work", artifact)
    if install_framework(assets, tools["vector"]) or scripts:
        reboot(GOLDEN_SERIAL)
    doing("golden", "apps", 70)
    enable_module()
    include_new_apps(layout.home / "vector-work")
    install_home(assets, expected)  # after "include new apps", so the bridge scopes it too
    # No screen lock: simplified sign-ins (gateway/simple_signins.py) set one after the user's Google sign-in.
    doing("golden", "chrome", 80)
    verify_bridge()
    doing("golden", "tidy", 90)
    clean_golden()
    write_applied(GOLDEN_SERIAL, manifest.record(expected, build_id))  # inherited by every phone cloned from it
    doing("golden", "save", 95)
    # Powered off the way Android does: `emu kill` cut the power before the record above reached the disk, and
    # every new phone then installed the bridge and autoscope again and restarted on its first start.
    shut_down_golden()
    (layout.golden_home / f"{GOLDEN}.avd" / "generation").write_text(str(expected["golden_generation"]))
    done_marker.write_text(time.strftime("%Y-%m-%dT%H:%M:%S"))
    (layout.golden_home / "failed-builds").unlink(missing_ok=True)


# Runtime state the emulator recreates; the quickboot snapshot is large and the app cold-boots anyway.
SKIP = {"snapshots", "tmpAdbCmds", "hardware-qemu.ini", "hardware-qemu.ini.lock", "multiinstance.lock",
        "emu-launch-params.txt", "bootcompleted.ini", "read-snapshot.txt", "emulator-user.ini", "provisioned"}


def clone_phone(layout: Layout):
    ini = layout.avd_home / f"{PHONE}.ini"
    if ini.exists():
        return
    step("phone", "Creating your phone")
    target = layout.avd_home / f"{PHONE}.avd"
    shutil.rmtree(target, ignore_errors=True)
    layout.avd_home.mkdir(parents=True, exist_ok=True)
    shutil.copytree(layout.golden_home / f"{GOLDEN}.avd", target,
                    ignore=lambda d, names: [n for n in names if n in SKIP or n.endswith(".lock")])
    avd_config(target / "config.ini")  # a golden built before 720p or the front camera
    ini.write_text(f"avd.ini.encoding=UTF-8\npath={target}\npath.rel=avd{os.sep}{PHONE}.avd\ntarget=android-{API}\n")


def setup(layout: Layout, assets: Path, accept_licenses: bool, build_id="", start_over=False):
    try:
        expected = manifest.expected(assets)
    except manifest.ManifestError as exc:
        log(str(exc))
        raise SetupError("This copy of penrose is incomplete.", "Download penrose again and reinstall it.") from exc
    layout.home.mkdir(parents=True, exist_ok=True)
    ensure_java(layout)
    ensure_sdk(layout, accept_licenses)
    adbui.ADB = str(layout.adb)
    step("accel", "Checking hardware acceleration")
    check_acceleration(layout)
    golden = layout.golden_home / f"{GOLDEN}.avd"
    if (golden / "provisioned").exists() and golden_generation(layout) < expected["golden_generation"]:
        # This build's base (system image, Magisk, Vector) is newer than the golden's: build it again.
        log(f"golden image is generation {golden_generation(layout)}, this build needs {expected['golden_generation']}")
        discard_golden(layout)
    if not (golden / "provisioned").exists():
        tools = ensure_tools(layout)
        if start_over:
            log("starting the phone image over, as asked")
            discard_golden(layout)
        elif failed_builds(layout) >= 2:
            # Resumed once and failed again: whatever the half-built image holds is in the way. Start it over,
            # which is what a person would otherwise be told to do by hand.
            log("the phone image failed to build twice; starting it over from scratch")
            discard_golden(layout)
        try:
            build_golden(layout, assets, tools, build_id)
        except Exception:
            note_failed_build(layout)
            with contextlib.suppress(Exception):
                stop_golden()  # not left running hidden, holding memory (and, on Windows, its files)
            raise
    clone_phone(layout)


def failed_builds(layout: Layout):
    try:
        return int((layout.golden_home / "failed-builds").read_text().strip())
    except (OSError, ValueError):
        return 0


def note_failed_build(layout: Layout):
    with contextlib.suppress(OSError):
        layout.golden_home.mkdir(parents=True, exist_ok=True)
        (layout.golden_home / "failed-builds").write_text(str(failed_builds(layout) + 1))


def discard_golden(layout: Layout):
    """The golden image and its private system image copy, gone, so the next build starts clean. The emulator
    is stopped first: Windows cannot delete the files of a running one."""
    with contextlib.suppress(Exception):
        stop_golden()
    shutil.rmtree(layout.golden_home, ignore_errors=True)
    shutil.rmtree(layout.sysimg, ignore_errors=True)


# Magisk modules from before the jev -> relay rename. They scope the old bridge package, which is gone.
RETIRED_MODULES = ("jev_autoscope",)


def wait_framework(serial):
    """Vector's daemon is running: one wait on the phone, which answers at once on a phone that is up."""
    if not adbui.wait_on_phone(serial, "ps -A | grep -q vectord", 60, every=1):
        raise SetupError("Your phone started without a part penrose needs, so agents can’t read apps.",
                         "Reset the phone in Settings to rebuild it.")


def read_applied(serial):
    return manifest.parse_applied(adbui.su(serial, f"cat {manifest.APPLIED}", check=False))


def write_applied(serial, record: dict):
    encoded = base64.b64encode(json.dumps(record, indent=1).encode()).decode()
    adbui.su(serial, f"mkdir -p {Path(manifest.APPLIED).parent.as_posix()} && echo {encoded} | base64 -d > {manifest.APPLIED}")


def apply_artifact(serial, assets: Path, work: Path, artifact: dict):
    """Put one shipped artifact on the phone. Every kind needs the reboot that follows: Magisk applies module
    installs at boot, and the Xposed module's new code only loads into apps started after it."""
    file = assets / artifact["file"]
    log(f"applying {artifact['id']} ({artifact['sha256'][:12]})")
    if artifact["kind"] == "magisk-module":
        install_module(serial, file, artifact["module"])
    elif artifact["kind"] == "boot-script":
        # Normalize Windows line endings before sending an executable Android shell script.
        encoded = base64.b64encode(file.read_bytes().replace(b"\r\n", b"\n")).decode()
        target = f"/data/adb/post-fs-data.d/{artifact['file']}"
        adbui.su(serial, f"mkdir -p /data/adb/post-fs-data.d && "
                 f"echo {encoded} | base64 -d > {target}.tmp && "
                 f"chown 0:0 {target}.tmp && chmod 0755 {target}.tmp && mv -f {target}.tmp {target}")
    elif artifact["kind"] == "apk":
        log(adbui.adb(serial, "install", "-r", *(("-g",) if artifact.get("home") else ()), str(file)))
        if artifact.get("home"):
            adbui.home_setup(serial)
        if artifact.get("xposed_module"):
            enable_module(serial)
            include_new_apps(work, serial)
            adbui.notification_listener_setup(serial)


def reconcile(serial, assets: Path | None, work: Path, build_id=""):
    """Bring the phone up to date with the shipped assets (manifest.py): install what differs from the phone's
    record, retire what the record predates, reboot once when something needs it, rewrite the record. True when
    it rebooted. Magisk modules apply at boot and new Xposed module code loads only into apps started after it,
    so those need the reboot; an ordinary app (the home app) is restarted by its own install."""
    if assets is None:
        return False
    try:
        expected = manifest.expected(assets)
    except manifest.ManifestError as exc:
        log(str(exc))
        raise SetupError("This copy of penrose is incomplete.", "Download penrose again and reinstall it.") from exc
    applied = read_applied(serial)
    todo = manifest.plan(expected, applied)
    if todo["needs_reset"]:
        emit("outdated", message="This version of penrose needs a fresh phone.",
             hint="Reset the phone in Settings. Its apps, sign-ins and data are erased.")
    if not todo["pending"]:
        return False
    step("update", "Updating your phone (about a minute)")
    log("pending: " + ", ".join(a["id"] for a in todo["pending"]))
    if applied["golden_generation"] is None:  # no record: a phone from before the records, or before the rename
        for module_id in RETIRED_MODULES:
            adbui.su(serial, f"[ -d /data/adb/modules/{module_id} ] && touch /data/adb/modules/{module_id}/remove", check=False)
        adbui.su(serial, "magisk --sqlite \"REPLACE INTO settings (key,value) VALUES('zygisk',1)\"", check=False)
    # Modules first; the Xposed module's enable step needs the framework, which is already running.
    for artifact in sorted(todo["pending"], key=lambda a: a["kind"] != "magisk-module"):
        apply_artifact(serial, assets, work, artifact)
    restart = applied["golden_generation"] is None or any(
        a["kind"] in ("magisk-module", "boot-script") or a.get("xposed_module") for a in todo["pending"])
    if restart:
        reboot(serial)
        wait_framework(serial)
    write_applied(serial, manifest.record(expected, build_id))
    return restart


def device_ready(layout: Layout, serial, wait, assets: Path | None = None, build_id="", guarded_clock=False):
    """After each launch: wait for boot (and, on a phone with a screen-lock PIN, relay-bridge's unlock), then
    three-button navigation, Chrome set up (no first-run screens, its DevTools socket open for bridge/chrome.py),
    the Xposed framework running, the shipped artifacts current (without them the agents cannot read apps), the
    kernel on its fast clock (fast_clock), the Relay home app as the home screen with the Pixel Launcher still on
    (it draws the nav bar), and no screen lock left from older phones (Relay's own for simplified sign-ins stays).
    Nothing here opens an app: the user may be in the middle of something on the phone (a Google sign-in)."""
    adbui.ADB = str(layout.adb)
    step("boot", "Starting your phone")
    wait_boot(serial, wait)
    adbui.shell(serial, "cmd overlay enable-exclusive --user current --category com.android.internal.systemui.navbar.threebutton", check=False)
    adbui.chrome_debug_setup(serial)
    wait_framework(serial)
    rebooted = reconcile(serial, assets, layout.home / "vector-work", build_id)
    # After the update, not before it: an update restarts the phone, and each start of the kernel can land on the
    # slow clock (the user's phone, 2026-11-22: checked and fast, then updated, restarted and slow for the session).
    if fast_clock(serial, guarded_clock):
        wait_framework(serial)
        rebooted = True
    adbui.home_setup(serial)  # also puts it back if something (a setup wizard) took the home screen
    if rebooted:
        adbui.chrome_debug_setup(serial)
        verify_bridge(serial, golden=False)  # through the running home app; opens nothing
    adbui.screen_lock_remove(serial, report=log)
    adbui.disable_unused_apps(serial)
    if not adbui.play_auto_updates_off(serial):
        log("could not switch Play Store's app auto-updates off; its Settings > Network preferences still can")
    if not adbui.keyboard_not_stylus(serial):
        log("could not set Gboard to keep its on-screen keyboard; it is tried again on the next start")
    if not adbui.chrome_site_notifications(serial):
        log("Chrome's default for website notifications is not Allow (the user's own choice, or its settings could not be read)")
    if adbui.keep_pixel_launcher(serial):
        log("the Pixel Launcher is on again (it draws the nav bar); restarting SystemUI to show it")
        adbui.restart_systemui(serial)
    adbui.remove_display_cutout(serial)
    if avd_config(layout.avd_home / f"{PHONE}.avd" / "config.ini"):
        log("the phone's screen (720x1600) or front camera changed; it applies from the next start")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="relay-server provision")
    parser.add_argument("command", choices=("setup", "device-ready", "status", "doctor"))
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--sdk", type=Path)
    parser.add_argument("--assets", type=Path)
    parser.add_argument("--accept-licenses", action="store_true")
    parser.add_argument("--serial", default="emulator-5570")
    parser.add_argument("--wait", type=int, default=420)
    parser.add_argument("--build-id", default="", help="the app build these assets came with, for the phone's record")
    parser.add_argument("--start-over", action="store_true", help="setup: build the phone image again from scratch")
    parser.add_argument("--logs", type=Path, help="doctor: the app's logs folder (provision.log)")
    parser.add_argument("--fast-clock", action="store_true",
                        help=f"device-ready: the emulator was started with {FAST_CLOCK}; see that the kernel runs with it")
    args = parser.parse_args(argv)
    layout = Layout(args.home, args.sdk)
    if args.command == "doctor":
        from . import doctor

        return doctor.main(layout, args.logs)
    global AGAIN
    if args.command == "device-ready":  # shown in the phone panel, which has no Try again button
        AGAIN = "Quit penrose and open it again. If this keeps happening, reset the phone in Settings."
    try:
        if args.command == "status":
            emit("status", **layout.status())
        elif args.command == "setup":
            setup(layout, args.assets, args.accept_licenses, args.build_id, args.start_over)
            emit("done", **layout.status())
        else:
            device_ready(layout, args.serial, args.wait, args.assets, args.build_id, args.fast_clock)
            emit("done", serial=args.serial)
        return 0
    except SetupError as exc:
        emit("error", message=str(exc), hint=exc.hint or AGAIN)
    except Exception:  # something unforeseen: the details go to the log, plain words to the window
        log(traceback.format_exc())
        what = "setting up" if args.command == "setup" else "starting"
        emit("error", message=f"Something went wrong while {what} the phone.", hint=AGAIN)
    return 1
