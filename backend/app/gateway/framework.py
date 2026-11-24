"""Relay's hooks stay on the phone: when Android comes back up without them, the phone is restarted once.

Every app Relay reads, and the unlocker that keeps the screen lock out of sight (relay-bridge in SystemUI), get
relay-bridge through Vector, and Vector gets into Android only through Magisk's Zygisk, loaded into zygote. Magisk
leaves Zygisk out when zygote has started three times without Android finishing its boot ("zygote crashed too many
times, rolling-back"): its guard against a Zygisk module that crashes zygote. A busy computer trips it too. When
Android's system server hangs, its watchdog kills it, and every kill starts zygote again (2026-10-28: three in ten
minutes, none of them booted). From then on no app gets relay-bridge and nothing on screen is readable, and a phone
with Relay's screen lock stays on its lock screen.

Only a full restart brings Zygisk back. After a rollback, the boot that completes makes Magisk (30.7) write its
native-bridge setting a second time from a value it has already cleared, so each later zygote start looks for
"libzygisk.so", which does not exist ("dlopen failed"). So restarting zygote alone does not help.

The signal is what Relay depends on, seen as the gateway follows the phone (watch.py): relay-bridge's socket in the
Relay home app (`@relaybridge.app.relay.home`), which is the home screen and always runs. When it has not appeared
HOOK_GRACE seconds after the phone unlocked, Vector is not in Android. A phone with Relay's screen lock that stays
locked LOCK_GRACE seconds after booting means the same (the unlocker never ran). The phone counts as not ready, and
it is restarted. Its storage locks on the restart; relay-bridge's unlocker unlocks it again (24 s from the restart
to relay-bridge in the home app, measured on a test phone).

Never a loop: after MAX_FAILED_RESTARTS restarts in a row that brought no hooks back, it stops trying and logs.
A broken module would otherwise restart the phone forever. The count starts again once hooks are seen."""

import asyncio
import contextlib
import logging

from . import core

log = logging.getLogger("gateway.framework")

HOOK_GRACE = 60  # seconds the unlocked phone may run without relay-bridge in the home app
LOCK_GRACE = 120  # seconds a booted phone with Relay's screen lock may stay locked
MAX_FAILED_RESTARTS = 2
HOME = "app.relay.home"

failed_restarts = {}  # slot -> restarts in a row that brought no hooks back
restarting = {}  # slot -> the restart task


def restart(slot, why):
    if slot in restarting and not restarting[slot].done():
        return
    if failed_restarts.get(slot, 0) >= MAX_FAILED_RESTARTS:
        log.error("%s: %s; not restarting it again (%d restarts brought no hooks back)", slot, why,
                  failed_restarts[slot])
        return
    failed_restarts[slot] = failed_restarts.get(slot, 0) + 1
    log.warning("%s: %s (Magisk drops Zygisk after repeated system restarts); restarting the phone", slot, why)
    restarting[slot] = asyncio.create_task(_reboot(slot))


async def _reboot(slot):
    # The shell may end with the connection as the phone goes down: no reply is expected.
    with contextlib.suppress(Exception):
        await core.adb(slot, "shell", "su -c reboot", timeout=15)
