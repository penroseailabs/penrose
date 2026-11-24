package app.relay.bridge;

import android.app.Activity;
import android.content.res.Resources;
import android.os.SystemClock;
import android.view.KeyEvent;
import android.view.MenuItem;
import android.view.View;
import android.view.ViewParent;
import android.widget.AdapterView;
import android.widget.TextView;

import java.lang.reflect.Field;
import java.util.Arrays;
import java.util.HashSet;
import java.util.Set;

/**
 * Actions the phone refuses in an app, whoever asks: the agents never delete email. A page, an email or a
 * message can carry instructions written for the agent (prompt injection), and deleting mail is the kind of
 * damage such an instruction would ask for, so the Gmail app's own process refuses it rather than trusting the
 * agent to say no. In Gmail:
 *
 * - the delete controls (the toolbar and selection bar's Delete, which is "Delete forever" in Trash, Discard
 *   drafts, Discard outbox, Empty trash / spam now), found by their resource id, whether tapped, clicked through
 *   accessibility or chosen in an overflow menu;
 * - the Delete key and '#' (Gmail's delete shortcuts) while no text field has focus;
 * - a swipe set to delete: the setting reads as "None", so the swipe does nothing.
 *
 * Only Android's own classes are hooked (Gmail renames its libraries), and the ids are Gmail's resource names,
 * which updates keep. A refusal is answered in the click's own reply (Server.click), recorded as a `refused`
 * event, and the controls carry `blocked` in every snapshot, so the agent knows before it tries. The
 * notification service refuses Gmail's Delete notification button the same way (Notifier.click).
 */
final class Guard {
    static final String GMAIL = "com.google.android.gm";
    static final String REASON = "This action is blocked as a safeguard against prompt injection: penrose never deletes email. "
        + "If the user wants it deleted, they can delete it themselves.";
    /** What a snapshot says on a blocked control. */
    static final String MARK = "deleting email is blocked as a safeguard against prompt injection";
    private static final Set<String> GMAIL_IDS = new HashSet<>(Arrays.asList(
        "delete", "discard_drafts", "discard_outbox", "empty_trash_spam_action"));
    private static final Set<String> SWIPE_KEYS = new HashSet<>(Arrays.asList("swipe-left-action", "swipe-right-action"));
    /** Gmail's notification button that deletes, by its string resource name. */
    static final String GMAIL_NOTIFICATION_DELETE = "notification_action_delete";

    /** When the last refusal happened (uptime ms), so a click can tell whether it was refused. */
    private static volatile long lastRefused = -1;

    static boolean active() { return GMAIL.equals(Module.pkg); }

    static void install() throws Throwable {
        Module.all(View.class, "performClick", Module.before(p -> {
            String what = blocked((View) p.thisObject);
            if (what != null) { refuse(what); p.setResult(false); }
        }));
        // Overflow menus are list rows whose item is the MenuItem; the row view itself has no id.
        Module.all(AdapterView.class, "performItemClick", Module.before(p -> {
            if (!(p.args[1] instanceof Integer)) return;
            Object item = ((AdapterView<?>) p.thisObject).getItemAtPosition((Integer) p.args[1]);
            String what = item instanceof MenuItem ? blockedItem((MenuItem) item, ((AdapterView<?>) p.thisObject).getResources()) : null;
            if (what != null) { refuse(what); p.setResult(true); }
        }));
        Module.all(Activity.class, "dispatchKeyEvent", Module.before(p -> {
            KeyEvent e = (KeyEvent) p.args[0];
            if (!deleteKey(e) || typing((Activity) p.thisObject)) return;
            if (e.getAction() == KeyEvent.ACTION_DOWN && e.getRepeatCount() == 0) refuse("the " + KeyEvent.keyCodeToString(e.getKeyCode()) + " key");
            p.setResult(true);
        }));
        Module.all(Class.forName("android.app.SharedPreferencesImpl"), "getString", Module.after(p -> {
            if (SWIPE_KEYS.contains(p.args[0]) && "delete".equals(p.getResult())) p.setResult("disable");
        }));
    }

    /** Why a tap on v would be refused, or null: the control's name. */
    static String blocked(View v) {
        if (!active() || v == null) return null;
        String id = v.getId() == View.NO_ID ? null : Dump.resourceName(v, v.getId());
        if (id != null && GMAIL_IDS.contains(id)) return name(v, id);
        MenuItem item = menuItem(v);
        return item != null ? blockedItem(item, v.getResources()) : null;
    }

    private static String blockedItem(MenuItem item, Resources res) {
        if (!active() || item.getItemId() == 0 || item.getItemId() == View.NO_ID) return null;
        String id;
        try {
            id = res.getResourceEntryName(item.getItemId());
        } catch (Resources.NotFoundException e) {
            return null;
        }
        if (!GMAIL_IDS.contains(id)) return null;
        CharSequence title = item.getTitle();
        return title != null && title.length() > 0 ? "\"" + title + "\"" : id;
    }

    /** The MenuItem behind a menu's row or toolbar button: a field of the view, or of an overflow row's list. */
    private static MenuItem menuItem(View v) {
        MenuItem m = fieldOf(v, MenuItem.class);
        if (m != null) return m;
        ViewParent parent = v.getParent();
        if (parent instanceof AdapterView) {
            try {
                Object item = ((AdapterView<?>) parent).getItemAtPosition(((AdapterView<?>) parent).getPositionForView(v));
                if (item instanceof MenuItem) return (MenuItem) item;
            } catch (RuntimeException ignored) {
                // the row is leaving the list
            }
        }
        return null;
    }

    private static <T> T fieldOf(Object o, Class<T> type) {
        for (Class<?> k = o.getClass(); k != null && k != View.class; k = k.getSuperclass()) {
            for (Field f : k.getDeclaredFields()) {
                if (!type.isAssignableFrom(f.getType())) continue;
                try {
                    f.setAccessible(true);
                    Object value = f.get(o);
                    if (value != null) return type.cast(value);
                } catch (Throwable ignored) {
                }
            }
        }
        return null;
    }

    private static String name(View v, String id) {
        CharSequence label = v instanceof TextView ? ((TextView) v).getText() : null;
        if (label == null || label.length() == 0) label = v.getContentDescription();
        return label != null && label.length() > 0 ? "\"" + label + "\"" : id;
    }

    private static boolean deleteKey(KeyEvent e) {
        int k = e.getKeyCode();
        return k == KeyEvent.KEYCODE_DEL || k == KeyEvent.KEYCODE_FORWARD_DEL || k == KeyEvent.KEYCODE_POUND
            || e.getUnicodeChar() == '#';
    }

    /** A text field has focus: the keys edit text there. */
    private static boolean typing(Activity a) {
        View focus = a.getCurrentFocus();
        return focus instanceof TextView && ((TextView) focus).onCheckIsTextEditor();
    }

    private static void refuse(String what) {
        lastRefused = SystemClock.uptimeMillis();
        Events.emit("refused", "app", Module.pkg, "what", what, "reason", REASON);
    }

    /** Whether something was refused at or after `since` (uptime ms). */
    static boolean refusedSince(long since) { return lastRefused >= since; }
}
