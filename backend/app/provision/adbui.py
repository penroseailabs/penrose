"""Tiny adb + uiautomator helpers for the one-time provisioning dialogs (Magisk's superuser
prompt, notification permission). Not used at runtime. Ported from emulation_harness/provision/adbui.py."""

import json
import os
import re
import subprocess
import tempfile
import time
import uuid

ADB = os.environ.get("ADB_PATH", "adb")


def adb(serial, *args, check=True, timeout=120):
    result = subprocess.run([ADB, "-s", serial, *args], capture_output=True, text=True, timeout=timeout,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                            encoding="utf-8", errors="replace")
    if check and result.returncode:
        raise RuntimeError(f"adb {' '.join(args)}: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def shell(serial, command, **kwargs):
    return adb(serial, "shell", command, **kwargs)


def wait_on_phone(serial, condition, timeout, stop=None, every=0.2):
    """Wait until the shell test `condition` holds on the phone: True, or False after `timeout` seconds. One adb call
    that waits on the phone itself, checking `every` seconds there (a shell test costs next to nothing), rather than
    a new adb process every second or two from here. A phone that is not connected yet, or is restarting, is waited
    for first. `stop`: checked here twice a second; True ends the wait (False). `condition` has no single quotes."""
    seconds = max(1, round(timeout))
    script = f"timeout {seconds} sh -c 'until {condition}; do sleep {every}; done'"
    process = subprocess.Popen([ADB, "-s", serial, "wait-for-device", "shell", script], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    deadline = time.monotonic() + seconds + 5  # the phone's own timeout ends it first, unless it never connects
    try:
        while True:
            try:
                return process.wait(timeout=0.5) == 0
            except subprocess.TimeoutExpired:
                if (stop is not None and stop()) or time.monotonic() > deadline:
                    return False
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def launch(serial, package):
    """Open an app's launcher activity with `am start`. Not `monkey`: it gives up whenever any app on the phone
    is not responding, which a busy phone on a slow or memory-starved computer often has (the Google app),
    and then opens nothing."""
    found = shell(serial, f"cmd package resolve-activity --brief -c android.intent.category.LAUNCHER {package}", check=False)
    component = next((line.strip() for line in reversed(found.splitlines()) if "/" in line and " " not in line.strip()), "")
    if component:
        return shell(serial, f"am start -n {component} -a android.intent.action.MAIN -c android.intent.category.LAUNCHER",
                     check=False)
    return shell(serial, f"monkey --ignore-timeouts --ignore-crashes -p {package} -c android.intent.category.LAUNCHER 1",
                 check=False)


def su(serial, command, **kwargs):
    """Run as root through Magisk (shell must already be granted). The command travels as a
    script file, so its quotes never meet a second round of shell parsing. Each call has a file of its
    own: the setup doctor checks the phone while setup runs, and a shared file let one call run the other's
    command (Vector went uninstalled because the doctor's `id` replaced its install)."""
    remote = f"/data/local/tmp/relay-su-{os.getpid()}-{uuid.uuid4().hex[:12]}.sh"
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False, newline="\n") as f:
        f.write(command + "\n")
    try:
        adb(serial, "push", f.name, remote)
    finally:
        os.unlink(f.name)
    return adb(serial, "shell", f"su -c 'sh {remote}; s=$?; rm -f {remote}; exit $s'", **kwargs)


CHROME_FLAGS = "_ --disable-fre --no-default-browser-check --no-first-run"


def chrome_debug_setup(serial):
    """Let the playground read Chrome's own pages (playground/chrome.py). Chrome takes a command
    line only as the device's debug app, from /data/local/tmp/chrome-command-line; --disable-fre
    skips the first-run screens (sign-in and Terms of Service) that would otherwise stand before
    every page. Chrome's DevTools socket (@chrome_devtools_remote) opens while adb is enabled.
    Idempotent; takes effect the next time Chrome starts."""
    shell(serial, f"echo '{CHROME_FLAGS}' > /data/local/tmp/chrome-command-line && chmod 644 /data/local/tmp/chrome-command-line")
    shell(serial, "am set-debug-app --persistent com.android.chrome")


def chrome_reset_tabs(serial):
    """Forget Chrome's open tabs so the next start opens one blank tab instead of restoring every
    page earlier runs left behind (they pile up: 70+ after a few sessions). Only the saved tab state
    goes; cookies, logins and settings stay. Chrome is stopped first, or it would write the state back."""
    shell(serial, "am force-stop com.android.chrome", check=False)
    su(serial, "rm -rf /data/data/com.android.chrome/app_tabs/* /data/data/com.android.chrome/cache/*", check=False)


NOTIFIER = "app.relay.bridge/app.relay.bridge.Notifier"


def notification_listener_setup(serial):
    """Let relay-bridge read the shade (its Notifier service, see Notifier.java): the playground and the
    subagent's ops lane list, wait for and tap notifications through it. The setting persists in the
    image; this is idempotent and also fixes clones of an older golden image. The service must be
    installed. Returns True when the listener is enabled afterwards."""
    def enabled():
        return NOTIFIER in shell(serial, "settings get secure enabled_notification_listeners", check=False)

    if not enabled():
        shell(serial, f"cmd notification allow_listener {NOTIFIER}", check=False)
    if not enabled():  # some images only take it from root
        su(serial, f"cmd notification allow_listener {NOTIFIER}", check=False)
    return enabled()


HOME_PACKAGE = "app.relay.home"
HOME_ACTIVITY = f"{HOME_PACKAGE}/.HomeActivity"
HOME_PERMISSIONS = ("android.permission.READ_CONTACTS", "android.permission.CALL_PHONE")
HOME_ROOT_SETTING = "relay_home_root"  # Settings.Global: the home app may run su (home_root granted it)


# Apps no agent uses, most of them running in the background anyway: together about 430 MB of the phone's 3 GB,
# which a swapping computer feels in every app. Agents search in Chrome (skills/apps/google-search.md).
# The Pixel Launcher is not among them although the Relay home app is the home screen: on this Android image it
# draws the three-button nav bar (its Taskbar, through SystemUI's overview proxy), and without it there is none.
PIXEL_LAUNCHER = "com.google.android.apps.nexuslauncher"
UNUSED_APPS = (
    "com.google.android.googlequicksearchbox",  # the Google app (Assistant, Discover, Lens): often not responding
    "com.google.android.apps.wellbeing",  # Digital Wellbeing: crashed over a Google sign-in
    "com.google.android.tts",  # Speech Services: text-to-speech and voice typing
    "com.google.android.as",  # Android System Intelligence: Live Caption, smart replies and text selection
    "com.google.android.as.oss",  # Private Compute Services, which only serves System Intelligence
    "com.google.android.apps.safetyhub",  # Personal Safety: crash detection, emergency sharing
    "com.google.android.apps.messaging",  # Messages: an emulator has no phone number, so no texts ever arrive
    "com.google.android.marvin.talkback",  # TalkBack
    "com.google.android.projection.gearhead",  # Android Auto
    "com.google.android.apps.restore",  # Data Restore Tool (Copy apps & data), which cannot reach a real phone
    "com.google.android.feedback",  # Google Feedback
)


# The emulator's display has a punch-hole camera cutout (RanchuCommonOverlay) placed for the Pixel 6's 1080-wide
# screen: on the 720-wide one it sits off-centre over the status bar as a black circle, and makes the status bar 128 px
# tall. No phone camera is there, so it goes: two fabricated overlays blank the cutout. They apply at once and persist.
NO_CUTOUT = {"relay_no_cutout": "config_mainBuiltInDisplayCutout",
             "relay_no_cutout_rect": "config_mainBuiltInDisplayCutoutRectApproximation"}


def remove_display_cutout(serial):
    """Blank the display cutout (NO_CUTOUT). Idempotent. True when both overlays are on afterwards."""
    def on():
        listed = shell(serial, "cmd overlay list", check=False)
        return all(f"[x] com.android.shell:{name}" in listed for name in NO_CUTOUT)

    if on():
        return True
    for name, resource in NO_CUTOUT.items():
        su(serial, f"cmd overlay fabricate --target android --name {name} android:string/{resource} 0x03 ''", check=False)
        su(serial, f"cmd overlay enable --user 0 com.android.shell:{name}", check=False)
    return on()


def disable_unused_apps(serial):
    """Switch off UNUSED_APPS for the user (`pm disable-user`, undone with `pm enable`). Idempotent; an app the
    image does not have is skipped. Returns the packages still enabled afterwards."""
    left = []
    for package in UNUSED_APPS:
        if "package:" not in shell(serial, f"pm path {package}", check=False):
            continue
        if f"package:{package}" in shell(serial, "pm list packages -d", check=False).split():
            continue
        shell(serial, f"pm disable-user --user 0 {package}", check=False)
        if f"package:{package}" not in shell(serial, "pm list packages -d", check=False).split():
            su(serial, f"pm disable-user --user 0 {package}", check=False)  # some images only take it from root
        if f"package:{package}" not in shell(serial, "pm list packages -d", check=False).split():
            left.append(package)
    return left


PLAY_STORE = "com.android.vending"
PLAY_PREFS = f"/data/data/{PLAY_STORE}/shared_prefs/finsky.xml"
# What Play Store writes when the user picks "Don't auto-update apps" (Settings > Network preferences >
# Auto-update apps); found by diffing finsky.xml around that choice on Play Store 41.3 (2026-10-30).
PLAY_AUTO_UPDATE_KEY = "auto_update_enabled"


# Writes one value into an app's own SharedPreferences file, as root, with the app stopped first so it does not
# overwrite the file from memory; the app reads it back when it next starts. The file takes the owner and SELinux
# label of the app's data folder (two levels up), so the app can still read it. An app that has never run has no
# shared_prefs folder: then nothing is written.
APP_PREF_WRITE = r"""
am force-stop {package}
f={prefs}
d=$(dirname "$f")
[ -d "$d" ] || exit 0
[ -f "$f" ] || printf '%s\n' "<?xml version='1.0' encoding='utf-8' standalone='yes' ?>" '<map>' '</map>' > "$f"
sed -i '/name="{key}"/d' "$f"
sed -i 's#</map>#    <{kind} name="{key}" value="{value}" />\n</map>#' "$f"
chown $(stat -c %u:%g $(dirname "$d")) "$f"
chcon $(ls -Zd $(dirname "$d") | cut -d' ' -f1) "$f"
""".strip()


def play_auto_updates_off(serial):
    """Stop Play Store from updating apps on its own: the image ships app versions Relay has handling for
    (the home app reads Gmail's provider; the bridge knows each app's screens), and a silent update can
    break that (Gmail 2026.10 refused the provider queries Gmail 2024.04 answered). Written as root into
    Play's own preference file, exactly what its "Don't auto-update apps" setting writes, with Play
    stopped first so it does not overwrite the file from memory; Play reads it back when it next starts
    and shows that choice selected. Idempotent. Returns True when the file says updates are off, False
    when Play Store is missing or the key could not be written (a Play update may rename it: then the
    setting is still reachable in its UI)."""
    if "package:" not in shell(serial, f"pm path {PLAY_STORE}", check=False):
        return False
    read = f"grep -o '{PLAY_AUTO_UPDATE_KEY}\" value=\"[a-z]*\"' {PLAY_PREFS} 2>/dev/null"
    if 'value="false"' in su(serial, read, check=False):
        return True
    su(serial, APP_PREF_WRITE.format(package=PLAY_STORE, prefs=PLAY_PREFS, key=PLAY_AUTO_UPDATE_KEY,
                                     kind="boolean", value="false"), check=False)
    return 'value="false"' in su(serial, read, check=False)


CHROME = "com.android.chrome"
CHROME_PROFILE = f"/data/data/{CHROME}/app_chrome/Default"
CHROME_PREFS_TMP = "/data/local/tmp/relay-chrome-preferences"
# Chrome's content settings: profile.default_content_setting_values.<type> = 1 is "Allow".
CONTENT_SETTING_ALLOW = 1


def chrome_site_notifications(serial):
    """Let websites send notifications without Chrome asking: its default for the Notifications site setting
    becomes Allow, and Chrome itself holds Android's notification permission. A site that asks (most do when the
    user signs in or orders) is then granted at once, and what it sends later (an order shipped, a reply) is a
    notification on the phone like an app's, which penrose keeps and passes on (app/notifications.py); it says
    which site it is from. Nobody is there to answer Chrome's prompt on this phone, and an unanswered prompt is a
    site that never notifies.

    The default lives in Chrome's Preferences file (JSON), which Chrome reads when it starts and writes as it
    runs: so Chrome is stopped for the write (its tabs come back when it next opens), and only when there is
    something to write. A default already there is left alone, whatever it says: the user may have chosen it in
    Chrome's settings. On a phone where Chrome has not run yet, the file is made with just this in it; Chrome
    fills in the rest. Returns True when the default is Allow."""
    shell(serial, f"pm grant {CHROME} android.permission.POST_NOTIFICATIONS", check=False)
    prefs_path = f"{CHROME_PROFILE}/Preferences"

    def read():
        raw = su(serial, f"cat {prefs_path} 2>/dev/null", check=False)
        if not raw.strip():
            return {}
        try:
            found = json.loads(raw)
        except ValueError:
            return None  # being written this moment, or not what Chrome writes any more: left alone
        return found if isinstance(found, dict) else None

    def default(prefs):
        return ((prefs or {}).get("profile") or {}).get("default_content_setting_values") or {}

    if (prefs := read()) is None:
        return False
    if "notifications" in default(prefs):
        return default(prefs)["notifications"] == CONTENT_SETTING_ALLOW
    if "package:" not in shell(serial, f"pm path {CHROME}", check=False):
        return False
    shell(serial, f"am force-stop {CHROME}", check=False)
    if (prefs := read()) is None:  # read again now that Chrome no longer writes it
        return False
    prefs.setdefault("profile", {}).setdefault("default_content_setting_values", {})["notifications"] = CONTENT_SETTING_ALLOW
    with tempfile.TemporaryDirectory() as folder:
        local = os.path.join(folder, "Preferences")
        with open(local, "w", encoding="utf-8", newline="") as f:
            json.dump(prefs, f, ensure_ascii=False, separators=(",", ":"))
        adb(serial, "push", local, CHROME_PREFS_TMP)
    # Written into the file that is there (it keeps its owner and label); a new one is given those of Chrome's
    # data folder (the label with its categories: under the default one Chrome could read the file but not write it).
    su(serial, f"d=/data/data/{CHROME}; o=$(stat -c %u:%g $d); new=0; [ -f {prefs_path} ] || new=1; "
               f"mkdir -p {CHROME_PROFILE} && cat {CHROME_PREFS_TMP} > {prefs_path}; "
               f"if [ $new = 1 ]; then chown -R $o $d/app_chrome; chmod 700 $d/app_chrome {CHROME_PROFILE}; "
               f"chmod 600 {prefs_path}; chcon -R $(stat -c %C $d) $d/app_chrome; fi; rm -f {CHROME_PREFS_TMP}", check=False)
    return default(read()).get("notifications") == CONTENT_SETTING_ALLOW


GBOARD = "com.google.android.inputmethod.latin"
GBOARD_PREFS = f"/data/user_de/0/{GBOARD}/shared_prefs/{GBOARD}_preferences.xml"
# Gboard > Settings > Physical keyboard > Show on-screen keyboard; found by diffing the file around that switch
# on Gboard 18.4 (2026-11-03). Android's own "Use stylus to write in text fields" (Settings > System > Keyboard).
GBOARD_SHOW_KEYBOARD_KEY = "pk_always_show_vk"
STYLUS_WRITING_SETTING = "stylus_handwriting_enabled"


def keyboard_not_stylus(serial):
    """Keep Gboard's on-screen keyboard, without stylus prompts. The emulator's touchscreen says it takes a pen
    and the emulator has a built-in keyboard, so on the Samsung device profile (relay-device-profile.sh) Gboard
    took touches for a stylus: a "Try out your stylus" sheet over the app, then a small floating toolbar in place
    of the keyboard. Two defaults stop that: Android's stylus handwriting off, and Gboard's "Show on-screen
    keyboard" on. Each is set only while it is still unset, so a choice the user makes later in Settings stays.
    Gboard is stopped for its write, so this runs only when the value is missing (in practice once per phone).
    True when both are in place, False when Gboard's file could not be written (Gboard has never run yet)."""
    if shell(serial, f"settings get secure {STYLUS_WRITING_SETTING}", check=False).strip() == "null":
        shell(serial, f"settings put secure {STYLUS_WRITING_SETTING} 0", check=False)
    if "package:" not in shell(serial, f"pm path {GBOARD}", check=False):
        return True
    read = f"grep -o 'name=\"{GBOARD_SHOW_KEYBOARD_KEY}\"' {GBOARD_PREFS} 2>/dev/null"
    if GBOARD_SHOW_KEYBOARD_KEY in su(serial, read, check=False):
        return True
    su(serial, APP_PREF_WRITE.format(package=GBOARD, prefs=GBOARD_PREFS, key=GBOARD_SHOW_KEYBOARD_KEY,
                                     kind="boolean", value="true"), check=False)
    return GBOARD_SHOW_KEYBOARD_KEY in su(serial, read, check=False)


def keep_pixel_launcher(serial):
    """Switch the Pixel Launcher back on where an earlier Relay switched it off (it draws the nav bar). True when
    it was off: SystemUI connects to it only when SystemUI starts, so the caller restarts SystemUI."""
    if f"package:{PIXEL_LAUNCHER}" not in shell(serial, "pm list packages -d", check=False).split():
        return False
    shell(serial, f"pm enable --user 0 {PIXEL_LAUNCHER}", check=False)
    return True


def home_setup(serial):
    """Make the Relay home app (the agents' search-box launcher, android/home) the phone's home
    screen: the HOME role, which LauncherApps checks before it hands out app shortcuts, and the preferred
    activity, which the Home key opens. Ported from emulation_harness/provision/home.py, without its Home key
    press: this runs on the user's phone, which may be showing anything. The new home shows on the next boot
    or Home press. Idempotent; does nothing when the app is not installed. True when it is the home screen."""
    if "package:" not in shell(serial, f"pm path {HOME_PACKAGE}", check=False):
        return False
    for permission in HOME_PERMISSIONS:
        shell(serial, f"pm grant {HOME_PACKAGE} {permission}", check=False)
    if HOME_PACKAGE not in shell(serial, "cmd role get-role-holders --user 0 android.app.role.HOME", check=False):
        shell(serial, f"cmd role add-role-holder --user 0 android.app.role.HOME {HOME_PACKAGE}", check=False)
    shell(serial, f"cmd package set-home-activity --user 0 {HOME_ACTIVITY}", check=False)
    home_root(serial)
    out_of_doze(serial)
    return HOME_PACKAGE in shell(serial, "cmd role get-role-holders --user 0 android.app.role.HOME", check=False)


# What a look-up asks while the phone may still be coming out of Doze: Gmail's server search (through the home
# app), Google's results in Chrome's hidden tab, and the home app itself.
DOZE_EXEMPT = ("com.google.android.gm", "com.android.chrome", HOME_PACKAGE)


def out_of_doze(serial):
    """Exempt DOZE_EXEMPT from Doze (Android's "battery: unrestricted"): Doze cuts an app off from the network
    and holds its background work, and lets it back only some time after the phone wakes, while a look-up asks
    these apps in the first seconds after a wake. Android keeps the list across restarts. Idempotent; an app the
    phone does not have is skipped. Returns the installed ones still not exempt afterwards."""
    def exempt():
        return {line.split(",")[1] for line in shell(serial, "dumpsys deviceidle whitelist", check=False).splitlines()
                if line.startswith("user,") and line.count(",") >= 2}

    have = exempt()
    wanted = [p for p in DOZE_EXEMPT if p not in have and "package:" in shell(serial, f"pm path {p}", check=False)]
    for package in wanted:
        shell(serial, f"dumpsys deviceidle whitelist +{package}", check=False)
    have = exempt() if wanted else have
    return [p for p in wanted if p not in have]


def home_root(serial):
    """Root for the Relay home app, for its Gmail search: Gmail runs a live search (its servers, not only the
    synced mail) only for callers outside its own process, so the home app runs Android's `content` tool as root
    (home-app Gmail.java). A Magisk policy for the app's uid, with no toast and no log entry, then the global
    setting the app checks before it runs su at all (an ungranted su would put Magisk's prompt on screen).
    Idempotent; the uid is read each time, so a reinstalled app is granted again. True when granted."""
    found = re.search(rf"package:{re.escape(HOME_PACKAGE)} uid:(\d+)",
                      shell(serial, f"pm list packages -U {HOME_PACKAGE}", check=False))
    if not found:
        return False
    su(serial, "magisk --sqlite \"REPLACE INTO policies (uid,policy,until,logging,notification) "
               f"VALUES({found.group(1)},2,0,0,0)\"", check=False)
    shell(serial, f"settings put global {HOME_ROOT_SETTING} 1", check=False)
    return True


# The fixed PIN phones had until 2026-10-25, removed after boot. Relay's own lock for simplified sign-ins
# (gateway/simple_signins.py) is a random PIN kept only on the phone, in PIN_PROP, which relay-bridge reads.
OLD_PIN = "0000"
PIN_PROP = "persist.relay.lock_pin"
VECTOR_CLI = "sh /data/adb/modules/zygisk_vector/cli"


def wait_unlocked(serial, timeout=60.0):
    """Wait until the user's credential-encrypted storage is unlocked (app data, /sdcard). With a
    screen lock that happens only once relay-bridge has entered the PIN in SystemUI after boot."""
    return wait_on_phone(serial, '[ "$(getprop sys.user.0.ce_available)" = true ]', timeout)


# The log line SystemUI's lock screen writes once it is set up (KeyguardViewMediator, Android 15), and the one
# relay-bridge's unlocker writes when it loads into SystemUI (Unlocker.java), which comes before it.
SYSTEMUI_READY = "-s KeyguardViewMediator:D -e 'processing message: SYSTEM_READY$'"
UNLOCKER_LOADED = "-s relay_unlocker:I -e '^installed:'"


def restart_systemui(serial, wait=30):
    """Restart SystemUI so a scope change reaches it, and wait until its lock screen is set up. It comes back at
    once and takes a few seconds more to be ready (longer right after a boot): the wait reads the phone's log from
    just before the restart, so the line cannot be missed. On a build that does not write the line it ends after
    `wait` seconds. True when relay-bridge's unlocker loaded into the new SystemUI."""
    loaded = su(serial, "t=$(date +%s.%N)\n"
                        "pkill -f com.android.systemui\n"
                        f'timeout {wait} logcat -v brief -T "$t" -m 1 {SYSTEMUI_READY} >/dev/null\n'
                        f'logcat -d -v brief -T "$t" {UNLOCKER_LOADED}', check=False, timeout=wait + 30)
    return "installed:" in loaded


def screen_lock_remove(serial, report=print):
    """Take off OLD_PIN, the fixed screen-lock PIN phones had before it was dropped. Runs after wait_boot, once
    relay-bridge has entered the PIN. A phone without a lock, or with Relay's own random PIN for simplified
    sign-ins (PIN_PROP set), is left as is. SystemUI stays scoped to the bridge: its unlocker is idle without a
    PIN, and taking the scope off would cost a SystemUI restart. Returns True when no old lock is left."""
    found = re.search(r"CredentialType:\s*(\w+)", shell(serial, "dumpsys lock_settings", check=False))
    credential = found.group(1).upper() if found else "?"
    if credential == "NONE":
        return True
    if shell(serial, f"getprop {PIN_PROP}", check=False).strip():
        return True  # simplified sign-ins: Relay's own PIN, kept
    if credential != "PIN":
        report(f"warning: the device's screen lock is {credential}, not the PIN; leaving it alone.")
        return False
    shell(serial, f"locksettings clear --old {OLD_PIN}", check=False)
    # Right after the clear, SystemUI may still treat the keyguard as secure: tried again on the phone, in one call.
    shell(serial, "for i in 1 2 3 4 5; do input keyevent 224; wm dismiss-keyguard; "
                  "dumpsys window | grep -q isKeyguardShowing=true || break; sleep 2; done", check=False, timeout=60)
    found = re.search(r"CredentialType:\s*(\w+)", shell(serial, "dumpsys lock_settings", check=False))
    if found and found.group(1).upper() == "NONE":
        return True
    report("warning: the screen-lock PIN could not be removed.")
    return False


def nodes(serial):
    shell(serial, "uiautomator dump /sdcard/relay-ui.xml", check=False)
    xml = shell(serial, "cat /sdcard/relay-ui.xml", check=False)
    found = []
    for m in re.finditer(r'<node [^>]*>', xml):
        attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', m.group(0)))
        box = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", attrs.get("bounds", ""))
        if box:
            x1, y1, x2, y2 = map(int, box.groups())
            attrs["center"] = ((x1 + x2) // 2, (y1 + y2) // 2)
            found.append(attrs)
    return found


def tap_text(serial, *labels, timeout=20.0, when=None, stop=None):
    """Tap the first node whose text or description equals one of `labels` (case-insensitive).
    `when`: tap only while this text is also on screen (the dialog the label belongs to).
    `stop`: a check that ends the wait early, e.g. once the request the dialog answers has finished.
    Returns the label tapped, or None if none appeared before the timeout. On a slow computer one screen
    read takes 10 seconds or more, so timeouts here are minutes, not seconds."""
    wanted = [label.lower() for label in labels]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if stop is not None and stop():
            return None
        found = nodes(serial)
        if when is not None and not any(when.lower() in (n.get("text") or "").lower() for n in found):
            time.sleep(1)
            continue
        for node in found:
            value = (node.get("text") or node.get("content-desc") or "").strip().lower()
            if value in wanted:
                x, y = node["center"]
                shell(serial, f"input tap {x} {y}")
                return value
        time.sleep(1)
    return None


def texts(serial):
    return [n.get("text") or n.get("content-desc") for n in nodes(serial) if n.get("text") or n.get("content-desc")]
