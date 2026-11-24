# Emulator device profile

Every Electron installer bundles `desktop/assets/android/relay-device-profile.sh` through
the existing Android assets directory. The Android manifest tracks its SHA-256 alongside
the APKs and Magisk modules.

Provisioning writes it to `/data/adb/post-fs-data.d/relay-device-profile.sh`, owned by
root with mode 0755. It normalizes line endings to LF and replaces the file atomically.
Fresh golden images install it after Magisk setup and reboot before app setup and saving
the image. Existing phones receive it through `device-ready`, followed by one reboot;
the applied record is written only after the reboot succeeds. An unchanged script does
not trigger another reboot. Existing golden images can keep their old profile until
rebuilt; their clones receive the script during `device-ready`.

The script presents a Galaxy S25 (SM-S931B, device pa1q, product pa1qxxx; Snapdragon, so
`ro.hardware` and `ro.boot.hardware` are qcom), including partition-specific product
identities and fingerprints. It was the S25 Ultra (SM-S938B) until 2026-11-03: the plain
S25 has no S Pen, so apps have no reason to offer stylus features. The fingerprint is the
Android 15 build S931BXXU1AYA1 (AP3A.240905.015.A2) as listed by a community firmware-dump
channel, not checked against a Samsung source. Hardware properties are overridden too; this
does not change the emulator's actual hardware, kernel, ABI, or HAL.
Changing hardware properties can affect service selection and needs a cold-boot test on
each supported emulator architecture before release.

Magisk runs post-fs-data scripts before Zygote. `resetprop -n` is necessary here to avoid
property-service triggers during the blocking boot stage:
https://topjohnwu.github.io/Magisk/guides.html#boot-scripts

## Camera and integrity status

Since 2026-11-03 every phone has a front camera as well as the back one: `provision.avd_config` writes
`hw.camera.front=emulated` into the AVD's config.ini (new phone images, and existing phones from their next start
after device-ready). Both cameras show the emulator's test scene. Checked on a test phone: two cameras listed
(front, orientation 270; back, 90), and the Camera app opened the front one and showed the scene. The cameras keep
the emulator's own characteristics (about 2.6 MP, 3.3 mm, f/2.8); they do not claim the S25's.

The base image already enables Zygisk and installs pinned Vector, the modern Xposed
framework: https://github.com/JingMatrix/Vector. Do not install a second Xposed framework
alongside it. No camera masking module is bundled or configured by this change, and
camera IDs or vendor metadata have not been altered. A device name alone supplies no
CameraCharacteristics profile. IDs must correspond to cameras the HAL can actually open.

No PlayIntegrityFix module is bundled. The requested upstream URL
https://github.com/chiteroman/PlayIntegrityFix returned HTTP 404 during implementation.
There is no verified module release or attestation configuration in this repository.
The supplied Samsung fingerprint is not an attestation certificate. On Android 13 and
later, Google documents hardware-backed proof for MEETS_DEVICE_INTEGRITY; setting
properties or installing a module does not guarantee that verdict:
https://developer.android.com/google/play/integrity/verdicts#device-integrity-field

Before claiming compatibility, use a disposable phone to check a cold boot, the requested
properties with `getprop`, camera enumeration and opening both cameras, and the actual
Google Play Integrity verdict. These Android checks have not been performed for this
profile. Never run these experiments on the user's phone or the setup emulator.

## Keyboard, not stylus

The emulator's touchscreen reports pen support (BTN_STYLUS, BTN_TOOL_RUBBER), and the emulator always has a
built-in keyboard. With the Samsung identity, Gboard 18.4 took ordinary touches for a stylus: a "Try out your
stylus" sheet over the app, then a small floating toolbar instead of the on-screen keyboard. Without the profile the
same Gboard shows its keyboard. `adbui.keyboard_not_stylus` (in `device_ready` and `clean_golden`) sets two
defaults, each only while unset so the user's later choice in Settings stays:

- `settings secure stylus_handwriting_enabled 0`: Android's "Use stylus to write in text fields". Stops the sheet.
- Gboard's `pk_always_show_vk = true` (Physical keyboard > Show on-screen keyboard). Keeps the keyboard.

Checked on a test phone on 2026-11-03, with touches sent through the emulator's own touchscreen device (`sendevent`
on virtio_input_multi_touch_1): keyboard shown, no sheet or toolbar; with both values removed, the toolbar returned.
