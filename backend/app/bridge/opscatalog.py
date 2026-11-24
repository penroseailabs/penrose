"""What the ops lane offers, as data: the names, parameters and one-line descriptions that the
subagent's prompt, the API schema and the playground list are all built from. No dependencies, so
every process (API, worker, gateway, playground) can import it. The implementations are in ops.py."""

# The notification listener (a service in the module's own APK) answers on this "process" name.
NOTIFIER = "app.relay.bridge:notifications"

# name: (parameters, what it does). The prompt and the playground list are built from this.
CATALOG = {
    "state": ("", "One read of the device: wifi, mobile data, airplane, bluetooth, location, dark mode, rotation, brightness, animations, do-not-disturb, battery, locale, time, screen on or locked, foreground app."),
    "wifi": ("value=on|off", "Wi-Fi."),
    "mobile_data": ("value=on|off", "Mobile data."),
    "airplane": ("value=on|off", "Airplane mode."),
    "bluetooth": ("value=on|off", "Bluetooth."),
    "location": ("value=on|off", "The device's Location switch."),
    "dark_mode": ("value=on|off|auto", "System dark theme."),
    "rotation": ("value=auto|portrait|landscape", "Screen rotation (locks it for portrait and landscape)."),
    "brightness": ("value=0..100", "Screen brightness in percent (turns adaptive brightness off)."),
    "animations": ("value=on|off", "System animations; off makes screens finish changing sooner (apps' own animations run at half speed)."),
    "dnd": ("value=on|off", "Do not disturb."),
    "keep_awake": ("value=on|off", "Keep the screen on while charging."),
    "geo": ("lat, lng", "Set the device's GPS position."),
    "launch": ("package [, shortcut]", "Open an installed app by package ID (no home screen, no icon hunting); with shortcut, at one of the shortcuts its icon offers (Orders, Cart, New timer), by its label."),
    "apps": ("query", "Find installed apps (with a launcher icon) by name or by part of the package ID (maps, amazon, twitter): names and package IDs, best match first, user-installed ones flagged."),
    "open": ("uri [, package]", "Open a URL or deep link in an app: http, https, market, geo, mailto, tel, sms, or a scheme an installed app declares for links from outside (google.navigation:, an app's own)."),
    "stop": ("package", "Force-stop an app (its data stays)."),
    "grant": ("package, permissions=[location|camera|microphone|contacts|calendar|notifications|phone|sms|media|storage|nearby_devices or android.permission.X]", "Grant runtime permissions, so no permission dialog appears."),
    "revoke": ("package, permissions=[...]", "Revoke runtime permissions."),
    "shade": ("value=notifications|settings|collapse", "Pull down or close the notification shade / quick settings."),
    "key": ("value=power|wake|sleep|volume_up|volume_down|mute", "A device key (not navigation: the phone agent presses back/home)."),
    "unlock": ("", "Wake the screen and dismiss the keyguard."),
    "hide_keyboard": ("", "Hide the on-screen keyboard."),
    "notifications": ("[ongoing=true]", "What is in the shade, as data: app, title, text, actions."),
    "notification_tap": ("match [, action=0-based index]", "Tap a notification (or one of its action buttons) found by words in its app, title or text."),
    "notification_dismiss": ("match", "Swipe one notification away."),
    "notifications_clear": ("", "Clear every dismissable notification."),
    "sms": ("number, text", "Deliver an incoming SMS to the emulator (emulator console)."),
    "call": ("number", "Ring an incoming phone call on the emulator; end it with call_end (emulator console)."),
    "call_end": ("number", "Hang up the call from number (emulator console)."),
    "battery": ("level=0..100 [, charging=true|false]", "Set the emulated battery (emulator console)."),
    "snapshot": ("action=save|load|delete|list [, name]", "Save or restore the whole emulator state in seconds (emulator console)."),
    "prefs": ("action=list|get|set|remove [, package, name, key, value, type]", "An app's SharedPreferences through the running app, so a change is live. The app must be running. Credential-looking keys read as [redacted]."),
    "set_value": ("target, value", "Set a slider, rating bar, number picker, switch, spinner, date/time picker, pager, tab or bottom-nav item directly. value: on|off, min|max|up|down|mid, a number or N%, next|prev|first|last, an item's text, YYYY-MM-DD, HH:mm."),
    "scroll_to": ("target, pos=top|bottom|row number", "Jump a native list to a row."),
    "inspect": ("target, path", "Read-only getter chain on a view, like getAdapter().getItemCount()."),
    "list_data": ("target [, limit=1..200, start=row]", "The rows of a native list from its adapter's data, also those off screen, each as the app's text for it (default 50 rows, at most 6000 characters)."),
    "read_text": ("target [, start=character]", "The whole text of one native text view or field that Screen shows cut (a note, a long message), at most 6000 characters from `start`."),
    "network": ("[url, contains, limit=1..10, chars=200..4000, package]", "The app's latest OkHttp responses, newest first: method, status, URL and the text or JSON body (compacted, credential-looking values masked), filtered by part of the URL and by words the body holds."),
    "wait_for": ("any of: text, gone, element, absent, activity, foreground, event; timeout=seconds", "Wait until the screen shows what is described (all of it), up to the timeout (at most 5 s)."),
    "check": ("the same fields as wait_for", "wait_for with no waiting: is it true right now?"),
}
# A target names one element by what it is: {"role": "Slider", "text": "Font size", "id": "...", "index": 0}.
TARGET_HELP = ("target: {role?, text?, id?, index?} or {n}: text matches the element's words, id or surrounding text; "
               "index picks among look-alikes (0-based); n is an element's number on the screen.")

READ_ONLY = {"state", "notifications", "inspect", "check", "wait_for", "apps", "list_data", "read_text", "network"}


def is_change(op):
    """Whether an op changed the device or an app (the phone agent is told about those, not about reads)."""
    name = op.get("op")
    if name == "prefs":
        return str(op.get("action")).lower() in ("set", "remove")
    return name not in READ_ONLY


def catalog_text():
    lines = [f"- {name}" + (f" ({params})" if params else "") + f": {text}" for name, (params, text) in CATALOG.items()]
    return "\n".join(lines) + "\n" + TARGET_HELP
