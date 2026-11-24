# penrose desktop app

An installable app (Windows, macOS, Linux) with the whole product inside: the web UI in an Electron window,
the API, worker and device gateway as one bundled program, and the Android emulator on the user's machine.
No Docker, no Python install, no `.env`.

## What the user sees

Everything happens in one window. The services start first (behind a quiet boot screen), then the React app asks
for whatever the first run still needs (`frontend/src/components/Setup.tsx`), one step per screen.

1. **Connect a model.** A DeepSeek, OpenAI or Anthropic API key; at least one. Each key is checked against the
   provider's model list (free) and stored encrypted with the OS keychain (Electron `safeStorage`). The composer's
   model picker offers only providers with a key. ChatGPT and Claude subscriptions cannot be connected: their
   sign-ins are for OpenAI's and Anthropic's own apps.
2. **Build the phone** (once, 15–30 min). The user accepts the Android SDK license, optionally reuses an Android
   SDK already on the machine, and watches four stages with one progress bar: download Android (Java runtime, SDK
   tools, emulator and Google Play image), check hardware acceleration, build the phone image (Magisk, Vector,
   rootAVD, the golden image), make their phone. The provisioner's log is under "Details". When it finishes, the
   services restart with the new adb and the phone starts.
3. **Sign in to Google, on the phone.** The window welcomes the user and opens Google's add-account screen on
   the phone; they sign in themselves from the phone panel (click to tap, type with the keyboard). Tasks stay
   locked until Android's account list (`dumpsys account`) shows an account. There are no Google agents: the
   user's own account is on the phone, and agents choose "Continue with Google" with it. Agents may ask for
   one-time codes (SMS, email, authenticator TOTP) at any time through the private answer card, which types
   them into the phone and keeps them from every model and log; passwords only as a last resort.
4. **The app.** Later launches go straight here: the window opens at once on a quiet boot screen while the
   services start (a few seconds), and the phone resumes beside the conversation. Settings: appearance, location,
   memory, AI models (the same key rows as setup; also in the app menu), reset phone, developer mode. If the phone
   fails its post-boot check, the phone panel says why instead of "starting" forever.

### Location

The phone is always where the computer is. The window reads the computer's position (Windows: the location
service through WinRT, `desktop/src/location-win.ps1`; macOS: Core Location through the window's geolocation,
with the `NSLocation…UsageDescription` keys and location entitlement in the build), or, if that is off or
blocked, a city-level position from the internet connection (ipapi.co, then ipwho.is). It is applied on connect
and every 10 minutes with `adb emu geo fix`, the emulator's own GPS, so apps see a real satellite fix rather
than a mock location. Agents cannot move the phone. Windows needs `inference.location.live.net` reachable to
place a PC without GPS (DNS filters such as NextDNS may block it).

### The window

A hidden title bar in the canvas colour (native buttons drawn over it on Windows, traffic lights on macOS), size and
position remembered between launches, native right-click menus (spelling, cut/copy/paste; Delete on a task) and
native confirmations. A Content-Security-Policy limits the page to penrose's own scripts and service. A view that
crashes shows a calm "Reload" instead of a blank window. The icon is drawn by `desktop/scripts/render-icon.js`
into `desktop/build/` (`icon.png` for macOS and Linux, `icon-win.png` for Windows).

### Phone start-up

The emulator quick-boots: it resumes from the snapshot saved at the last quit in about 2 s (a cold boot is about
18 s), with root and Xposed already running. Quitting (`adb emu kill`) saves the snapshot in under a second. If
penrose or the emulator crashed, the emulator finds the snapshot stale and boots cold instead; nothing written since
is lost (tested by hard-killing a quick-booted clone).

An emulator can also freeze with its process still there (2026-11-09: the computer ran out of virtual memory and
the emulator could not allocate). Nothing exits and adb keeps listing the phone, so the gateway asks it whether it
still answers: every minute, and at once when an adb call runs out of time (`backend/app/gateway/watch.py`). Silent
three times in a row, the phone stops counting as ready and the gateway asks the app to restart it
(`restartFrozenPhone` in `desktop/src/main.js`, over the control server): the app ends the emulator's process and
starts the phone again, cold. Tasks and look-ups wait for it as for any phone that is starting. It is not restarted
twice within ten minutes; a phone that freezes again that soon is reported to the user instead.

The phone runs with `-feature -Vulkan`. With Vulkan inside the phone the emulator refused to save the snapshot
whenever Chrome or a WebView held a Vulkan context ("Not saving state ... UNSUPPORTED_VK_APP") and deleted the old
one, so most launches booted cold (25-32 s). Apps now draw with OpenGL ES on the host GPU; with Chrome open on a
page the snapshot saves in about 1 s and the next launch is ready in about 3 s. Quitting waits up to 30 s
(`EMULATOR_QUIT_MS`) for the emulator, which gives itself 20 s; killing it at 15 s left snapshots that failed to
load. `device-ready` no longer pauses 10 s after boot: it checks that the package, activity and settings services
answer (`wait_services`, one adb call on a running phone), so a launch with nothing to update takes about 2 s.

Updates reboot the phone only when a Magisk module or the Xposed module changed (or the phone has no record yet);
an ordinary app such as the home app is replaced in place.

**Sleep.** When nobody holds the phone (no run, no Claude Code session, no sign-in being driven) and nothing has
used it for 15 s (`phone_sleep_seconds`), the gateway presses Home and turns the screen off; 15 s later
(`phone_doze_seconds`) it puts the phone in Doze. Anything that uses it wakes it first, in about 0.2 s: a lease,
the ops lane, the bridge, the home app's search, a sign-in. The user's click on the sleeping phone only wakes it
(the panel says "Asleep · click to wake"). The phone's own screen timeout stays off, so only the gateway turns the
screen off and never under a task. Measured on a test phone: the emulator used about 30% of a core awake and idle,
about 3% asleep; awake, a page left animating in Chrome had held about a core for days. `gateway/sleep.py`.

**Reading the screen after an action.** A tap or typed text goes through the app's bridge, which also reports
when that app's screen has stopped changing: no layout pass for 150 ms (`settle_quiet_ms`) and no activity opening
or closing (relay-bridge's `Settle.java`, hooks on `ViewRootImpl.performLayout`, `Instrumentation.execStartActivity`
and `Activity.finish`). Drawing alone does not count, so spinners, video and shimmer placeholders still settle. The
wait never exceeds `look_delay_seconds` (0.5 s), the old fixed pause; keys, swipes and launches from adb, which
the app cannot see coming, still wait the whole 0.5 s. Then one gateway call reads the window list and the bridge
sockets in a single adb call, every visible app's snapshot at once, and their events in the same replies.
Measured on the emulator: flipping a switch went from about 700 ms to about 300 ms; opening a Settings screen,
which animates and fills in for longer, from about 800 ms to about 650 ms. Bridge commands that act (tap, type,
scroll) are never sent twice after a timeout; only reads are retried.

## How it fits together

```
Electron main (desktop/src/main.js)
 ├─ relay-server provision ...                first-run setup and post-boot checks (backend/app/provision)
 ├─ emulator -avd relay -port 5570            headless; quit sends `adb emu kill` first
 ├─ relay-server gateway --port <free>        GATEWAY_TOKEN required on every request
 ├─ relay-server api --port <free>            also serves the built UI (FRONTEND_DIR); random per-launch login
 ├─ relay-server worker
 └─ app window → http://127.0.0.1:<api port>  signed in by main with that launch's credentials
```

- `relay-server` is `backend/app/desktop.py` bundled by PyInstaller (one folder, ~55 MB). In development
  Electron runs `.venv`'s Python with `-m app.desktop` instead.
- Everything listens on 127.0.0.1. The gateway token and the per-launch login keep other programs and web pages
  on the machine away from the phone and the API.
- Data: `%LOCALAPPDATA%\Relay` on Windows, `~/Library/Application Support/Relay` on macOS, `~/.config/Relay` on
  Linux: `config.json` (encrypted keys, SDK choice), `data/` (chats, memory), `android/` (SDK, tools, golden image,
  the phone), `logs/`. `RELAY_DATA_DIR` overrides it, then `penrose/data-location.txt` in the per-user app data
  folder (`%LOCALAPPDATA%`, `~/Library/Application Support`, `~/.config`), which holds the folder's full path.
  The Windows installer writes that file from its data page (`desktop/build/installer.nsh`), for new installs only.
  The macOS and Linux builds have no install step, so their first start asks instead (`desktop/src/datafolder.js`,
  same checks and wording), then restarts in the chosen folder; data already in the default folder skips the
  question. Tests: `npm test` in `desktop/`.
- Start at login: penrose starts when the user logs in (so after a reboot or a new boot), in the tray with its
  window closed (`--hidden`; with no tray it opens as usual). Windows: the installer writes
  `HKCU\...\Run\penrose` on a fresh install and removes it on uninstall, not on an update (`installer.nsh`).
  macOS: a login item. Linux: `~/.config/autostart/penrose.desktop` pointing at the AppImage file. The app itself
  turns it on the first time it runs (`desktop/src/autostart.js`, `config.json`'s `startAtLogin`), so macOS and
  Linux, which have no installer, get it too; after that the tray menu's "Start when I log in" is the user's choice
  and is kept. Development builds never register it.
- The live view uses the emulator's own gRPC frame stream (screenshots until it is up; no ffmpeg is shipped);
  `install_app` uses `apkeep` from `desktop/vendor/bin`.

### The provisioner

`backend/app/provision` is `emulation_harness/provision/golden.py`, `new-avd.py`, `include_new_apps.py` and
`adbui.py` ported to run on a user's machine: paths from `--home`, rootAVD from a pinned GitLab archive instead of
`git clone`, Windows/macOS/Linux told apart (`rootAVD.bat` vs `rootAVD.sh`, x86_64 vs arm64 image). Steps are
idempotent, so a failed setup resumes where it stopped.

`relay-bridge.apk`, `relay-autoscope.zip` and `relay-home.apk` ship with the app (`desktop/assets/android`, listed
in its `manifest.json`). They are built from
[android/](../android/README.md) by `python android/build.py` (the APKs signed with penrose's release key, kept
git-ignored in android/signing; the autoscope zip with sorted entries, LF endings and a fixed time, so the same
source gives the same file). After every boot, `device-ready` brings the
phone up to date with them (below, "Releases and updates").

**Home screen.** `relay-home.apk` (`android/home`, package `app.relay.home`, agent socket
`relayhome`) is the phone's home screen: the golden build installs it (`install_home`), phones made before it get
it as an update, and `device-ready` sets it again on every launch (`adbui.home_setup`: the HOME role plus the
preferred home activity). Nothing presses Home or opens an app on the user's phone. Its results list describes
every result to the bridge (up to 50), so the agent's screen lists them all without scrolling. Every search also
asks Gmail in the background, as root: the home app starts a small helper process from its own APK through su
(`GmailHelper`, kept between searches, gone after a minute without one) that runs Gmail's own server search through
its mail contract (UIProvider column names, no Gmail classes), in about a second. `device-ready` grants the root
(`adbui.home_root`: a Magisk policy with no toast, and the `relay_home_root` setting the app checks first).
The emails show as Mail rows beside a row that opens Gmail on the search; details in
`android/home/README.md`.

**No app opened on the user's phone.** The golden build checks the bridge end to end by opening Chrome and asking
it for `hello` (`verify_bridge`). After an update on the user's phone, `device-ready` only checks that Chrome is
in the bridge's scope (`wait_chrome_scoped`). Opening Chrome there put it on top of whatever the user was
doing, such as a Google sign-in, and reopened it for up to four minutes.

**Lighter phone.** The AVD's screen is 720x1600 at 280 dpi (`SCREEN` in `provision/__init__.py`), not the
Pixel 6's 1080x2400: about 55% fewer pixels to draw and capture, and the same 411 dp width, so apps lay out as
before. `create_golden_avd` writes it, `clone_phone` and `device-ready` add it to phones made before (from their next
start, which boots cold once). `adbui.disable_unused_apps` switches off the apps in `adbui.UNUSED_APPS` (`pm disable-user`) in the golden
image and on every start: the Google app (agents search in Chrome), Digital Wellbeing, Speech Services, Android System
Intelligence and Private Compute Services, Personal Safety, Messages (an emulator has no phone number), TalkBack,
Android Auto, the Data Restore Tool and Feedback. The Pixel Launcher stays on although the penrose home app is the
home screen: on this image it draws the three-button nav bar (its Taskbar), so `device-ready` switches it back on
(`adbui.keep_pixel_launcher`, then one SystemUI restart) on phones an earlier penrose turned it off on. `adbui.remove_display_cutout` blanks the display's punch-hole cutout with two
fabricated overlays: the emulator places it for a 1080-wide screen, so at 720 it was a black circle off-centre over a
128 px status bar.

**Test phones.** `scripts/test_phone.py start` runs the golden image read-only (`-read-only`, its writes discarded at
exit) on a port from 5600, so sessions and developers test on a clean rooted phone without touching the user's phone
(5570) or the image build (5580). It refuses while setup is building the image. See `CLAUDE.md`.

**Screen lock and simplified sign-ins.** New phones have no screen lock. After the user's Google sign-in, "Simplify
Sign-ins on this Device" (Settings; on by default, applied once by itself to each new Google account,
`api/devices.py`) makes Google Password Manager's passwords and passkeys usable, `gateway/simple_signins.py`:

- A random 8-digit PIN, made on the phone and kept only in `persist.relay.lock_pin` (Google later trusts it to unlock
  the account's encrypted data, so it is not a fixed one). relay-bridge 0.5.5's unlocker in SystemUI
  (`Unlocker.java` in the harness) works from SystemUI's own events, with no polling and no key presses: while
  the user's storage is unlocked a lock screen is not shown at all (`doKeyguardLocked` skipped); at boot or a
  SystemUI restart it checks the PIN with `LockPatternUtils.checkCredential` once SystemUI is ready and finishes
  the lock screen (`tryKeyguardDone`), within about a second; it fills BiometricPrompt's PIN field for passkeys.
  Setting the PIN takes under a second; SystemUI is restarted first only when the unlocker is not loaded yet.
- Chrome signed in with sync. penrose skips Chrome's first-run screens, which is where Chrome offers it, so Chrome's
  own "Turn on sync?" (`SyncConsentActivity`, not exported, its account in a nested Bundle) is opened from inside
  Chrome by relay-bridge's `start_activity`, and the user presses "Yes, I'm in". Chrome reports it in its
  Preferences (`google.services.consented_to_sync`).
- Once per phone, Google's "Verify it's you" for the encrypted data (the screen lock of another of the user's
  devices, or their password), shown the first time a passkey or password is used: the user's to answer.

Turning it off removes the PIN. Phones set up before 2026-10-25 had the fixed PIN `0000` (`adbui.OLD_PIN`);
`device-ready` removes that one after boot (`screen_lock_remove`) and keeps penrose's own. With either PIN, `wait_boot`
waits for `sys.user.0.ce_available`, as app data stays encrypted until the PIN is checked (`adb logcat -s
relay_unlocker` says what the unlocker did).

### The `relay` command

Other agents on the computer (Claude Code, Cowork) drive the phone through `backend/app/cli.py`, a set of
subcommands of relay-server that the app exposes as a launcher in `<data>/bin` (`relay.cmd`, or `relay` on macOS
and Linux; `writeLauncher` in main.js). The command finds the API through `<data>/data/cli.json`, which the API
writes at start with its port and a durable integration token (`<data>/data/integration_token`, readable by this
user only) that opens the `/api/phone/*` routes and nothing else (`backend/app/api/phone.py`). Each command
leases the phone as `cli:<session>`, so a penrose run and a Claude session never act at once: the one that does
not hold the lease is told who does. The lease is dropped on `relay phone release` or after five idle minutes,
and the settings and location the session changed are put back. Settings › Connect Claude (`frontend/src/components/Claude.tsx`,
over `GET`/`POST /api/phone/setup`) finds Claude Code and the Claude app, puts the launcher on the user's PATH
(Windows: the user's Path in the registry; elsewhere a symlink in `~/.local/bin`) and writes the skill to
`~/.claude/skills/android-phone`; the phone panel shows when a Claude session holds the phone, with Take back.

Cowork's shell runs in a VM that cannot reach 127.0.0.1, so it uses `relay mcp` (`backend/app/mcp.py`): a stdio
MCP server over the same phone API, which the Claude app starts on the computer and bridges into Cowork. The
page's "Add to Claude app" (or `relay setup claude-app`) merges an `mcpServers.relay` entry into the app's
`claude_desktop_config.json` (Windows Store build: `%LOCALAPPDATA%\Claude-Data\claude_<id>`; older builds
`%APPDATA%\Claude`; macOS `~/Library/Application Support/Claude`) that runs relay-server with `mcp` and points
it at `cli.json`. "Export skill" writes `<data>/claude/android-phone.zip` for claude.ai's Customize › Skills.

## Build

```bash
cd desktop
npm install
npm run dist        # web UI, relay-server (PyInstaller), apkeep, then the installer in desktop/dist
npm run dist:dir    # the same, unpacked (quicker to test)
npm start           # the app as built: needs frontend/dist and the repo's .venv
npm run dev         # development with live reload (below)
```

Build on each OS you ship for (PyInstaller does not cross-compile). Development switches: `RELAY_DATA_DIR`
(another profile) and `RELAY_DEV_SKIP_KEY_CHECK=1` (accept any key; unpackaged only).

### Development loop

`npm run dev` (`desktop/scripts/dev.js`) runs the Vite dev server on 127.0.0.1:8000 and Electron pointed at it,
with the API on a fixed port 8001 that Vite proxies `/api` to. No Docker, no rebuilds:

- UI edits (`frontend/src`) appear instantly (hot reload).
- Backend edits (`backend/app/**/*.py`) restart the API, gateway and worker in about two seconds.
- Main-process edits (`desktop/src/*.js`) restart Electron cleanly.
- The phone keeps running across all of these restarts and is picked up again; stop it with
  `adb -s emulator-5570 emu kill`. On Windows it is launched through `Start-Process` so it runs in a hidden console
  (no window for its qemu and netsimd helpers) and holds none of Electron's handles (the DevTools port stays free).
- `?setup=keys,phone` on the window's URL shows those first-run steps whatever is saved (development only).
- A browser tab on http://127.0.0.1:8000 can sign in with this run's dev login, written to
  `desktop/.dev-login.json` (`POST /api/auth/login`). The window is also inspectable on DevTools port 9333.

Put local settings in `desktop/.env.local` (git-ignored), one `KEY=value` per line, e.g. `RELAY_DATA_DIR=D:\penrose-dev`.

The services exit on their own if the app dies without stopping them (`RELAY_PARENT_PID`), and a crashing
service restarts with a back-off from 1 s up to a minute.

## Releases and updates

Everything in a build ships and updates together: the installer carries the services, the UI and the phone-side
pieces, and the phone updates itself from them. The user only ever sees "Check for updates…" in the penrose menu
(or the prompt after a background download) and "Restart now".

**One build ID.** `desktop/scripts/release.js` stamps each build with the UTC time it started, `yyyymmddhhmmss`
(`RELAY_BUILD_ID` pins it for a CI run stamping several platforms). `scripts/dist.js` passes it to
electron-builder as `relayBuildId` in the packaged app's package.json, where `main.js` reads it (`BUILD_ID`:
About, the updates log, and `--build-id` to the provisioner), together with the version derived from it:
`YYYY.MDD.HMM` as plain integers (`2026.1023.1430`, `2026.105.905`), because electron-updater compares semver
and Windows keeps file versions in four 16-bit numbers; it sorts numerically in date order. The repository's
package.json keeps a placeholder version. `npm run release:id` prints what a build started now would get.

**The app.** electron-updater reads the `publish` target in package.json: this repository's GitHub releases
(`penroseailabs/penrose`), read without a token.
`RELAY_UPDATE_URL=https://…/` at build time points the app at a plain HTTPS folder serving `dist/`'s `latest.yml`,
`latest-mac.yml` and installers instead. The app checks 15 s after launch and every six hours, downloads in the
background, then offers "Restart now" (its own shutdown first, so the phone's disk stays consistent) or installs on
the next quit. Windows installs unsigned updates, and so does the Linux AppImage. macOS installs updates from the
`zip` target and only when the app is signed with a Developer ID and notarized; a build without one is signed ad
hoc (`scripts/dist.js`), and instead of installing it tells the user a newer version exists and opens the download
page. `npm run dist` only builds; `npm run release` builds and uploads from your own computer (GitHub needs
`GH_TOKEN`).

**Shipping a release.** `.github/workflows/release.yml` builds Windows, macOS (Apple Silicon) and Linux on GitHub's
runners under one build ID, then publishes them together as one GitHub release, `v<version>`, so no installed copy
ever sees half a release. Start it from the Actions tab ("Release", "Run workflow"), or push the commit to ship to
the `release` branch (`git push origin main:release`). Installed copies pick the release up at their next check.
Signing is switched on by adding repository secrets, with no change to the workflow: `CSC_LINK` and
`CSC_KEY_PASSWORD` (a Developer ID certificate as base64 .p12) with `APPLE_ID`, `APPLE_APP_SPECIFIC_PASSWORD` and
`APPLE_TEAM_ID` for macOS; `WIN_CSC_LINK` and `WIN_CSC_KEY_PASSWORD` for Windows.

**The phone.** `backend/app/provision/manifest.py`: `assets/android/manifest.json` lists each phone-side artifact
(`apk` with its package, or `magisk-module` with its module id) and `golden_generation`. The phone keeps a record
of what was applied to it at `/data/adb/relay/applied.json` (the golden image gets one when it is built, so every
clone starts current). After each boot, `device-ready`'s last step compares the SHA-256 of each shipped file with
the record and, when any differs: installs the changed Magisk modules (`magisk --install-module`, applied at the
next boot) and APKs (`adb install -r`; an Xposed module is re-enabled with "include new apps"), reboots once,
verifies the bridge, rewrites the record. The phone panel shows "Updating your phone (about a minute)" meanwhile.
A phone without a record (made before this) gets everything once. Hashes, not version numbers, decide: a rebuilt
file with the same bytes changes nothing, and the APK's versionCode only has to not go down.

**A new base.** The golden image's base (system image, Magisk, Vector, rootAVD) is not updated in place. Bumping
`golden_generation` in the manifest makes `setup` rebuild the golden before cloning, and a phone from an older
generation gets an `outdated` event: the app offers "Reset phone" and the phone keeps working until then.

Adding a phone-side piece: put the file in `desktop/assets/android`, add it to `manifest.json`, done. A new
kind of artifact is a new branch in `apply_artifact`.

## Status

penrose is developed and used on Windows 11 (x86_64): that is the platform every part has run on. It has also
been run on a MacBook.

Not done yet:

- **Linux:** written for (`rootAVD.sh`, Temurin) and built by the release workflow, but not yet run end to end: a
  preview.
- **Signing:** unsigned builds. Windows shows SmartScreen. macOS needs an Apple Developer ID and notarization to
  open without a warning and to update itself; entitlements for the bundled server and emulator are still to do.
- **Updates:** the phone update after a changed asset was tested with adb faked
  (`tests/test_provision_reconcile.py`). Crash reporting not started.
- **Licensing review:** Magisk and Vector (GPL) are downloaded at setup time, not shipped. The Android SDK and Play image are downloaded from Google after the user accepts its license.
