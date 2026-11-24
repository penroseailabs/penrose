package app.relay.bridge;

import android.app.Notification;
import android.app.PendingIntent;
import android.content.pm.PackageManager;
import android.os.Bundle;
import android.service.notification.NotificationListenerService;
import android.service.notification.StatusBarNotification;

import org.json.JSONArray;
import org.json.JSONObject;

/**
 * Notifications as data: what is in the shade, and each one as it arrives or goes, without opening
 * the shade. This is a real notification listener (the module APK's own process, enabled once with
 * `cmd notification allow_listener app.relay.bridge/app.relay.bridge.Notifier`), not a hook, so it
 * needs no scope and no particular Android version.
 *
 * It answers the same newline-delimited JSON protocol as the app bridges, on the abstract socket
 * `relaybridge.app.relay.bridge:notifications`, and its `events` are read by the host like an app's;
 * with `wait` (milliseconds) the read is held until the next notification, so the host is told of
 * one as it arrives.
 *
 * Each notification says where it is from as exactly as Android knows: the app, the conversation
 * (its title, the app's own id for it, who wrote last) and, for a website's, the site.
 */
public class Notifier extends NotificationListenerService {
    /** The socket's process name. The module also loads in its own process (it is in its own
     * scope), where the app bridge already owns `relaybridge.app.relay.bridge`. */
    static final String PROCESS = "app.relay.bridge:notifications";
    private static volatile Notifier instance;
    private static boolean serving;
    /** The longest one `events` read is held, whatever the host asks. */
    private static final long MAX_WAIT_MS = 60_000;

    @Override public void onCreate() {
        super.onCreate();
        synchronized (Notifier.class) {
            if (serving) return;
            serving = true;
        }
        Thread t = new Thread(() -> Server.listen("relaybridge." + PROCESS, Notifier::handle), "relay-notifier");
        t.setDaemon(true);
        t.start();
    }

    @Override public void onListenerConnected() { instance = this; }
    @Override public void onListenerDisconnected() { instance = null; }

    /** key -> title and text last reported, so an app that re-posts the same notification (progress
     * ticks, ongoing services) is one event, not hundreds. */
    private final java.util.Map<String, String> reported = new java.util.HashMap<>();

    @Override public void onNotificationPosted(StatusBarNotification sbn) {
        try {
            JSONObject n = describe(sbn);
            if ((sbn.getNotification().flags & Notification.FLAG_GROUP_SUMMARY) != 0
                    && (n.isNull("text") || groupShown(sbn))) return;
            String signature = n.opt("title") + "|" + n.opt("text");
            synchronized (reported) {
                if (signature.equals(reported.put(sbn.getKey(), signature))) return;
                if (reported.size() > 500) reported.clear();
            }
            // Ongoing ones (a running service, a download) stay listable but are not news.
            if (sbn.isOngoing()) return;
            n.remove("actions");
            n.remove("tappable");
            n.remove("clearable");
            Events.emit("notification", n);
        } catch (Throwable t) {
            Problems.record("notification_event", "posted", t);
        }
    }

    /** Whether a group summary's group has notifications of its own in the shade. Then the summary only
     * repeats them ("4 messages from 3 chats"), each already reported; and when the others are read it falls
     * back to the one left, an old message re-posted as if new (2026-11-19: WhatsApp's summary re-posted a
     * 10 November "Sent 2 videos" three times on 17 November, and the agent hunted for videos that were never
     * sent). A summary alone in its group is the only notification shown, and is reported. */
    private boolean groupShown(StatusBarNotification summary) {
        StatusBarNotification[] active = getActiveNotifications();
        if (active == null) return false;
        String group = summary.getGroupKey();
        for (StatusBarNotification s : active) {
            if (!s.getKey().equals(summary.getKey()) && group.equals(s.getGroupKey())
                    && (s.getNotification().flags & Notification.FLAG_GROUP_SUMMARY) == 0) return true;
        }
        return false;
    }

    @Override public void onNotificationRemoved(StatusBarNotification sbn) {
        try {
            synchronized (reported) { reported.remove(sbn.getKey()); }
            if (sbn.isOngoing()) return;
            JSONObject n = describe(sbn);
            Events.emit("notification_removed", "key", sbn.getKey(), "app", sbn.getPackageName(), "title", n.opt("title"));
        } catch (Throwable t) {
            Problems.record("notification_event", "removed", t);
        }
    }

    // --- the wire ---------------------------------------------------------------------------

    private static JSONObject ok() throws Exception { return new JSONObject().put("ok", true); }
    private static JSONObject fail(String why) throws Exception { return new JSONObject().put("ok", false).put("error", why); }

    static JSONObject handle(JSONObject r) throws Exception {
        String cmd = r.getString("cmd");
        Notifier n = instance;
        switch (cmd) {
            case "hello":
                return ok().put("version", Module.VERSION).put("pkg", "app.relay.bridge").put("role", "notifications")
                    .put("process", PROCESS).put("pid", android.os.Process.myPid()).put("listening", n != null)
                    .put("hooks", new JSONObject());
            case "diag":
                return ok().put("pkg", "app.relay.bridge").put("pid", android.os.Process.myPid()).put("module_version", Module.VERSION)
                    .put("fired", new JSONObject()).put("problems", Problems.all());
            case "events": {
                // `held` tells the host this listener can hold a read open (`wait`); an older one answers at once.
                long since = r.optLong("since"), wait = Math.min(r.optLong("wait"), MAX_WAIT_MS);
                JSONArray found = wait > 0 ? Events.await(since, wait) : Events.since(since, false);
                return ok().put("events", found).put("seq", Events.lastSeq()).put("held", true);
            }
            default: break;
        }
        if (n == null) {
            return fail("notification access is not enabled for app.relay.bridge; enable it with "
                + "`cmd notification allow_listener app.relay.bridge/app.relay.bridge.Notifier`");
        }
        switch (cmd) {
            case "notifications": return n.list(r.optBoolean("ongoing", false));
            case "notification_click": return n.click(r.getString("key"), r.optInt("action", -1));
            case "notification_dismiss": n.cancelNotification(r.getString("key")); return ok();
            case "notifications_clear": n.cancelAllNotifications(); return ok();
            default: return fail("unknown cmd " + cmd);
        }
    }

    private JSONObject list(boolean includeOngoing) throws Exception {
        JSONArray out = new JSONArray();
        StatusBarNotification[] active = getActiveNotifications();
        if (active != null) {
            for (StatusBarNotification sbn : active) {
                if (!includeOngoing && sbn.isOngoing()) continue;
                out.put(describe(sbn));
            }
        }
        return ok().put("count", out.length()).put("notifications", out);
    }

    private StatusBarNotification find(String key) {
        StatusBarNotification[] active = getActiveNotifications();
        if (active != null) for (StatusBarNotification s : active) if (s.getKey().equals(key)) return s;
        return null;
    }

    /** Tap the notification (its content intent), or its action number `action` (0-based). */
    private JSONObject click(String key, int action) throws Exception {
        StatusBarNotification sbn = find(key);
        if (sbn == null) return fail("no such notification (it may have been dismissed): list them again");
        Notification note = sbn.getNotification();
        PendingIntent pi = action < 0 ? note.contentIntent : (note.actions != null && action < note.actions.length ? note.actions[action].actionIntent : null);
        if (pi == null) return fail(action < 0 ? "the notification has no tap action" : "no action " + action);
        if (action >= 0 && deletesEmail(sbn.getPackageName(), note.actions[action].title)) {
            return fail(Guard.REASON).put("refused", true);
        }
        Bundle options = null;
        try {  // Android 14+: a sender must opt in to let the receiver start an activity from the background.
            Object o = android.app.ActivityOptions.makeBasic();
            o.getClass().getMethod("setPendingIntentBackgroundActivityStartMode", int.class).invoke(o, 1 /* ALLOWED */);
            options = ((android.app.ActivityOptions) o).toBundle();
        } catch (Throwable ignored) {
        }
        try {
            pi.send(this, 0, null, null, null, null, options);
        } catch (PendingIntent.CanceledException e) {
            return fail("the notification's intent was cancelled by its app");
        }
        return ok().put("via", action < 0 ? "contentIntent" : "actionIntent");
    }

    /** Gmail's Delete button on a mail notification (Guard): its title is Gmail's own string, in the phone's language. */
    private boolean deletesEmail(String pkg, CharSequence title) {
        if (!Guard.GMAIL.equals(pkg) || title == null) return false;
        try {
            android.content.res.Resources res = createPackageContext(pkg, 0).getResources();
            int id = res.getIdentifier(Guard.GMAIL_NOTIFICATION_DELETE, "string", pkg);
            return id != 0 && res.getString(id).contentEquals(title);
        } catch (Throwable t) {
            return "Delete".contentEquals(title);  // Gmail's resources unreadable: its English label
        }
    }

    // --- one notification as the host reads it ---------------------------------------------

    private JSONObject describe(StatusBarNotification sbn) throws Exception {
        Notification note = sbn.getNotification();
        Bundle x = note.extras;
        JSONObject n = new JSONObject();
        n.put("key", sbn.getKey()).put("app", sbn.getPackageName()).put("label", label(sbn.getPackageName()));
        n.put("title", clip(x.getCharSequence(Notification.EXTRA_TITLE)));
        n.put("text", clip(x.getCharSequence(Notification.EXTRA_TEXT)));
        String big = clip(x.getCharSequence(Notification.EXTRA_BIG_TEXT));
        if (big != null && !big.equals(n.opt("text"))) n.put("big_text", big);
        // Under the app's name in the shade: the account (Gmail) or, for a website's notification, the site (Chrome).
        String sub = clip(x.getCharSequence(Notification.EXTRA_SUB_TEXT));
        if (sub != null) n.put("sub_text", sub);
        n.put("when", sbn.getPostTime()).put("ongoing", sbn.isOngoing()).put("clearable", sbn.isClearable());
        if (note.category != null) n.put("category", note.category);
        if (note.getChannelId() != null) n.put("channel", note.getChannelId());
        conversation(note, x, n);
        n.put("tappable", note.contentIntent != null);
        JSONArray actions = new JSONArray();
        if (note.actions != null) {
            for (Notification.Action a : note.actions) actions.put(a.title == null ? "" : a.title.toString());
        }
        n.put("actions", actions);
        return n;
    }

    /** A message's conversation, as the app declares it (MessagingStyle and its shortcut): the conversation's
     * title, whether it is a group, who wrote the last message, and the app's own id for the conversation, which
     * stays the same from one notification of it to the next. Each only when the app gives it. */
    private static void conversation(Notification note, Bundle x, JSONObject n) {
        try {
            String thread = note.getShortcutId();
            if (thread != null && !thread.isEmpty()) n.put("thread", thread);
            String title = clip(x.getCharSequence(Notification.EXTRA_CONVERSATION_TITLE));
            if (title != null) n.put("conversation", title);
            if (x.getBoolean(Notification.EXTRA_IS_GROUP_CONVERSATION)) n.put("group", true);
            android.os.Parcelable[] messages = x.getParcelableArray(Notification.EXTRA_MESSAGES);
            if (messages != null && messages.length > 0 && messages[messages.length - 1] instanceof Bundle) {
                Bundle last = (Bundle) messages[messages.length - 1];
                CharSequence sender = last.getCharSequence("sender");
                Object person = last.getParcelable("sender_person");
                if (person instanceof android.app.Person && ((android.app.Person) person).getName() != null) {
                    sender = ((android.app.Person) person).getName();
                }
                if (sender != null) n.put("sender", clip(sender));
            }
        } catch (Throwable ignored) {  // a bundle this process cannot unpack: the title and text still say it
        }
    }

    private String label(String pkg) {
        try {
            PackageManager pm = getPackageManager();
            return pm.getApplicationLabel(pm.getApplicationInfo(pkg, 0)).toString();
        } catch (Throwable t) {
            return pkg;
        }
    }

    private static String clip(CharSequence s) {
        if (s == null) return null;
        String t = s.toString();
        return t.length() <= 500 ? t : t.substring(0, 500) + "…";
    }
}
