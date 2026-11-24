# App installation (`install_app`)

Agents install apps with the `install_app` tool first, which needs no Play Store screens:

1. **`install_app`**: the gateway downloads the app from APKPure with
   [apkeep](https://github.com/EFForg/apkeep) and installs it with ADB. No Google account needed.
2. **Play Store**: used if `install_app` fails or the package ID is unknown. The subagent opens
   the listing and installs it with the Google account the user signed in with.
3. **Website**: used in the browser if no suitable app exists.

Before downloading, the gateway asks APKPure which versions it carries (`apkeep -l`). An app it
does not carry fails in about a second instead of after a download attempt.

Every `install_app` failure tells the agent why, in plain language, and to go straight to
Play Store rather than retry. The result has `reason`, `message`, and `next_step`:

| `reason` | Meaning |
|---|---|
| `not_available` | APKPure has no builds of the package. Tool and device are fine. |
| `tool_missing` | `apkeep` was not found on the gateway host. A setup problem. |
| `timeout` | The download exceeded `INSTALL_TIMEOUT_SECONDS`. |
| `no_download` | `apkeep` ran but produced no APK; `detail` has its output. |
| `corrupt` | The downloaded file was not a valid archive. |
| `rejected` | Android refused the install; `message` explains common codes, `detail` has the raw error. |
| `device_error` | The gateway could not be reached, so nothing was installed. |

`apkeep` runs in the **gateway** (the only service that talks to ADB), not in the API or worker.

## What the gateway does

For `install_app` with package `com.dd.doordash`:

1. If the package is already installed, it returns `already_installed` and downloads nothing.
2. It reads the device's CPU ABIs (`getprop ro.product.cpu.abilist`) and downloads for the
   first one: `apkeep -a com.dd.doordash -d apk-pure -o arch=x86_64 <tmp>`. Without
   `arch`, APKPure can return a bundle whose only native split is 32-bit ARM, which a 64-bit
   emulator cannot run.
3. `.apk`: installs it directly. `.xapk`: unzips the `.apk` files and keeps only the native
   split for the best device ABI (`config.x86_64.apk`, etc.), plus the base and density
   splits. OBB data files are not installed.
4. Turns off Play Protect's checks for ADB installs:
   `settings put global verifier_verify_adb_installs 0`, `package_verifier_enable 0`,
   `package_verifier_user_consent -1`.
5. Runs `adb install` or `adb install-multiple` with `-r -g -i com.android.vending`, which
   records Play Store as the installer. If Play Store is missing, `-i` is dropped.
6. Confirms the install with `pm list packages`.

The agent receives `status` (`installed`, `already_installed`, or `failed`) and a `message`.
On failure it also gets Android's error text in `detail` and a `next_step` pointing to Play
Store and then the website. Downloads happen without holding the device lock; only the ADB
steps take it.

Settings (set by the desktop app; `backend/.env` in development):

| Variable | Default | Purpose |
| --- | --- | --- |
| `APKEEP_PATH` | `apkeep` | apkeep executable. If missing, `install_app` fails and agents use Play Store. |
| `INSTALL_TIMEOUT_SECONDS` | `300` | Limit for the download, and separately for the ADB install. |

## Where apkeep comes from

The desktop app ships it: `desktop/scripts/fetch-vendor.js` downloads the release for the build's platform into
`desktop/vendor/bin` (part of `npm run dist`), and the app passes its path to the gateway as `APKEEP_PATH`. In
development (`npm run dev`) the same file is used if it is there; run `node scripts/fetch-vendor.js` in `desktop/`
once. On a platform with no apkeep build, `install_app` reports `tool_missing` and agents use Play Store.

## Troubleshooting

| Result | Meaning |
| --- | --- |
| `reason: tool_missing` | The gateway cannot find apkeep. Check `desktop/vendor/bin`, then restart penrose. |
| `reason: not_available` | APKPure has no build of this package, or the ID is wrong. |
| `INSTALL_FAILED_NO_MATCHING_ABIS` | The app has no native code for the device ABI. |
| `INSTALL_FAILED_UPDATE_INCOMPATIBLE` | A copy signed with a different key is already installed. Uninstall it first. |
| `reason: timeout` | Slow network or a very large app. Raise `INSTALL_TIMEOUT_SECONDS`. |

Sideloaded apps do not auto-update from Play Store. Run `install_app` again (it uses `-r`)
or update through Play Store.
