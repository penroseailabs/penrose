# Phone-side code

Everything penrose builds to run on the phone. Builds land in `desktop/assets/android`, where they ship with the
app (listed in its `manifest.json`).

| folder | what | ships as |
|---|---|---|
| `bridge/` | relay-bridge, the Xposed module inside each app: reads screens, acts, events (Java) | `relay-bridge.apk` |
| `home/` | the agent-first home screen and its `relayhome` socket ([home/README.md](home/README.md)) | `relay-home.apk` |
| `autoscope/` | Magisk module that puts apps in the bridge's scope at every boot | `relay-autoscope.zip` |
| `device-profile/` | boot script that makes the emulator present as a Pixel | `relay-device-profile.sh` |
| `bridge/testapp/` | a WebView fixture app for testing the bridge; not shipped | |

The page script the bridge runs in WebViews is `backend/app/bridge/web.js`, the same file the gateway uses for
Chrome's tabs: `bridge/build.py` embeds it.

## Build

```
python android/build.py                  # all four into desktop/assets/android
python android/build.py bridge           # only some: bridge, home, autoscope, profile
```

Needs the Android SDK (`D:/Android/Sdk`: build-tools, a platform) and a JDK 17+ (`D:/Android/tools/jdk-17...`), or
`--sdk` / `--jdk` / `ANDROID_HOME` / `JAVA_HOME`. No Gradle: javac, d8, aapt2, zipalign, apksigner.

On its next start the desktop app brings each phone up to date with what changed (`backend/app/provision`,
`reconcile`): it installs the new files and restarts the phone once. A restart from inside Android does not.

## Signing

The APKs are signed with penrose's release key in `android/signing/`, which git ignores, so it is never committed.
`android/toolchain.py` reads `android/signing/signing.json` (or the file `PENROSE_SIGNING` names), which lists:

- `penrose-release.p12`: the release key (RSA 4096, alias `penrose`, valid to 2056); its password is in the json.
- `relay-debug.keystore` and `penrose.lineage`: the key the apps were first signed with ("Relay Bridge Debug")
  and the proof that it handed over to the release key (APK Signature Scheme v3.1, key rotation). Phones that
  installed the old-key apps take new builds as ordinary updates, with no uninstall. Android 13+ checks the
  release key; older versions would see the old one.

Keep a copy of `android/signing` somewhere other than this disk (`git clean -x` would delete it). If the key is lost, no phone can take an update to
these apps without uninstalling them first, which drops the bridge's app scopes and the home app's settings.
