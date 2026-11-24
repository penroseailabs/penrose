#!/system/bin/sh
# Once per boot: bring relay-bridge's Vector scope in line with the apps already installed. Apps
# installed later need nothing from here: the module has Vector's "include new apps" set
# (backend/app/provision include_new_apps), so Vector adds each one to the scope when it is installed, before
# its first launch. Scope changes apply when an app's process starts.
CLI=/data/adb/modules/zygisk_vector/cli
MODULE=app.relay.bridge
# Scope: every app with a launcher icon (the user-facing apps, preinstalled Google ones included:
# Maps, Photos, Gmail, Calendar, Contacts, Messages, YouTube, Files...), every user-installed app,
# and these system apps the phone agent works inside that have no icon of their own: Google sign-in (GMS),
# permission dialogs, the home screen, and the keyboard (Gboard shows Google Password Manager's saved
# sign-ins as chips; the bridge lists them so the agent can pick one), and the system's photo picker and share
# sheet, where an app hands the agent its attach and share steps. SystemUI is scoped for one thing only:
# relay-bridge enters the screen-lock PIN there (Unlocker.java); it is hooked from its next start, i.e. the next boot.
BASE="com.android.chrome com.android.vending com.google.android.gms com.android.settings com.google.android.permissioncontroller com.android.permissioncontroller com.google.android.apps.nexuslauncher com.android.launcher3 com.android.systemui com.google.android.inputmethod.latin com.google.android.providers.media.module com.android.intentresolver"
# Never scoped, icon or not: SIM Toolkit runs inside the telephony process (com.android.phone).
EXCLUDE="com.android.stk"

until [ "$(getprop sys.boot_completed)" = "1" ]; do sleep 2; done
# Vector's daemon starts with the system; wait (at most two minutes) until it answers.
tries=0
until sh "$CLI" status >/dev/null 2>&1; do
  tries=$((tries + 1))
  [ "$tries" -ge 60 ] && exit 1
  sleep 2
done

installed="$( { pm list packages -3 2>/dev/null | sed 's/^package://'
                cmd package query-activities --brief -a android.intent.action.MAIN -c android.intent.category.LAUNCHER 2>/dev/null \
                  | sed -n 's#^ *\([A-Za-z0-9_.]*\)/.*#\1#p'; } | sort -u)"
scoped="$(sh "$CLI" --json scope ls "$MODULE" 2>/dev/null)"
missing=""
for pkg in $BASE $installed; do
  case "$pkg" in "$MODULE"|com.topjohnwu.magisk|org.matrix.*) continue ;; esac
  case " $EXCLUDE " in *" $pkg "*) continue ;; esac
  case "$scoped" in *"\"$pkg\""*) continue ;; esac
  pm path "$pkg" >/dev/null 2>&1 && missing="$missing $pkg/0"
done
# Uninstalled apps leave their scope entry behind, and excluded ones may be left over from an
# older version of this script; drop them.
stale=""
for pkg in $(echo "$scoped" | grep -o '"APP_PACKAGE": *"[^"]*"' | sed 's/.*"\([^"]*\)"$/\1/'); do
  case " $EXCLUDE " in *" $pkg "*) stale="$stale $pkg/0"; continue ;; esac
  pm path "$pkg" >/dev/null 2>&1 || stale="$stale $pkg/0"
done
[ -n "$stale" ] && sh "$CLI" scope rm "$MODULE" $stale >/dev/null 2>&1
if [ -n "$missing" ] && sh "$CLI" scope add "$MODULE" $missing >/dev/null 2>&1; then
  # Apps that start at boot (Messages, the Google app) are already running unhooked on the first
  # boot: `am kill` ends only background processes, so nothing on screen is disturbed, and they
  # come back with the bridge.
  for entry in $missing; do am kill "${entry%/0}" >/dev/null 2>&1; done
fi
