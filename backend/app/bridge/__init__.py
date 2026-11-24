"""The phone agent's interaction surface.

The relay-bridge Xposed module runs inside every app on the emulator and answers JSON commands
on an abstract socket (`relaybridge.<process>`): snapshots of the live view tree,
events, and actions performed inside the app. `devices.XposedDevice` speaks to it over adb and
`chrome.ChromePages` reads Chrome's own tabs over DevTools; both run in the gateway, the only
process with adb. `screen` turns a snapshot into the numbered text the phone agent and the subagent read and
into the phone agent's options; it runs in the worker (app/device/driver.py).

This code began in an earlier repository, emulation_harness. The phone-side
sources it talks to (the module's Java, the home app) are in android/ (android/README.md). `web.js` here is the
one page script for both Chrome's tabs and apps' WebViews: android/bridge/build.py embeds it in the module.
"""
