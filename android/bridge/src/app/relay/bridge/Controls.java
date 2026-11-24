package app.relay.bridge;

import android.os.Bundle;
import android.view.View;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.inputmethod.InputMethodManager;
import android.widget.AbsSeekBar;
import android.widget.Adapter;
import android.widget.CompoundButton;
import android.widget.DatePicker;
import android.widget.NumberPicker;
import android.widget.RatingBar;
import android.widget.Spinner;
import android.widget.TimePicker;

import org.json.JSONObject;

import java.lang.reflect.Method;

/**
 * Setting a control to a value directly, instead of dragging, swiping or tapping toward it, and
 * dismissing the keyboard. Runs on the main thread. Platform widgets are handled by type; the
 * AndroidX and Material ones (ViewPager, TabLayout, BottomNavigationView) by reflection on
 * public methods, which survive R8 only when the app keeps them: a miss is an error the host
 * reports, never a guess.
 */
final class Controls {
    private static JSONObject ok() throws Exception { return new JSONObject().put("ok", true); }
    private static JSONObject fail(String why) throws Exception { return new JSONObject().put("ok", false).put("error", why); }

    static JSONObject hideKeyboard() throws Exception {
        InputMethodManager imm = null;
        boolean was = false;
        for (View root : Dump.liveRoots()) {
            if (imm == null) imm = (InputMethodManager) root.getContext().getSystemService(android.content.Context.INPUT_METHOD_SERVICE);
            WindowInsets insets = root.getRootWindowInsets();
            if (insets == null || !insets.isVisible(WindowInsets.Type.ime())) continue;
            was = true;
            WindowInsetsController c = root.getWindowInsetsController();
            if (c != null) c.hide(WindowInsets.Type.ime());
            if (imm != null) imm.hideSoftInputFromWindow(root.getWindowToken(), 0);
        }
        return ok().put("was_visible", was).put("via", "hideSoftInput");
    }

    static JSONObject setValue(int h, String value) throws Exception {
        Dump.Target t = Server.target(h);
        if (t == null) return fail("stale handle");
        if (t.isWeb()) return fail("a web element has no direct setter");
        if (t.isVirtual()) return fail("a Compose or virtual node has no direct setter");
        View v = t.view.get();
        String v0 = value.trim();
        String word = v0.toLowerCase();

        if (v instanceof AbsSeekBar) return seekBar((AbsSeekBar) v, word);
        if (v instanceof NumberPicker) return numberPicker((NumberPicker) v, word);
        if (v instanceof CompoundButton) return compound((CompoundButton) v, word);
        if (v instanceof Spinner) return spinner((Spinner) v, v0);
        if (v instanceof DatePicker) return date((DatePicker) v, v0);
        if (v instanceof TimePicker) return time((TimePicker) v, v0);
        if (named(v, "ViewPager2") || named(v, "ViewPager")) return pager(v, word);
        if (named(v, "TabLayout")) return tabs(v, word);
        if (named(v, "NavigationBarView") || named(v, "BottomNavigationView")) return bottomNav(v, word);
        return fail("no direct setter for " + v.getClass().getName() + ": tap or scroll it instead");
    }

    /** Class or any superclass has this simple name (so an obfuscated subclass still counts). */
    static boolean named(View v, String simple) {
        for (Class<?> k = v.getClass(); k != null; k = k.getSuperclass()) {
            if (k.getSimpleName().equals(simple) || k.getName().endsWith("." + simple)) return true;
        }
        return false;
    }

    // --- sliders, ratings, number pickers --------------------------------------------------

    private static JSONObject seekBar(AbsSeekBar sb, String word) throws Exception {
        if (!sb.isEnabled()) return fail("the control is disabled");
        int min = sb.getMin(), max = sb.getMax(), now = sb.getProgress();
        int step = Math.max(1, sb.getKeyProgressIncrement());
        int to;
        switch (word) {
            case "min": to = min; break;
            case "max": to = max; break;
            case "mid": to = (min + max) / 2; break;
            case "up": to = now + step; break;
            case "down": to = now - step; break;
            default:
                if (word.endsWith("%")) {
                    to = min + Math.round((max - min) * Float.parseFloat(word.substring(0, word.length() - 1)) / 100f);
                } else if (sb instanceof RatingBar) {
                    RatingBar rb = (RatingBar) sb;  // a plain number is stars; progress counts steps of them
                    to = Math.round(Float.parseFloat(word) * max / rb.getNumStars());
                } else {
                    to = Integer.parseInt(word);
                }
        }
        to = Math.max(min, Math.min(max, to));
        Bundle args = new Bundle();
        args.putFloat(AccessibilityNodeInfo.ACTION_ARGUMENT_PROGRESS_VALUE, to);
        // The accessibility action moves it as a user would (listeners see fromUser = true).
        boolean done = sb.performAccessibilityAction(AccessibilityNodeInfo.AccessibilityAction.ACTION_SET_PROGRESS.getId(), args);
        String via = "accessibility_set_progress";
        if (!done || sb.getProgress() != to) {
            sb.setProgress(to);
            via = "setProgress";
        }
        // Controls refuse some values (an alarm volume cannot reach 0): say so, so a caller does not retry.
        return ok().put("via", via).put("from", now).put("to", sb.getProgress()).put("min", min).put("max", max)
            .put("limited", sb.getProgress() != to)
            .put("at_limit", sb.getProgress() == now && (word.equals("min") || word.equals("max") || word.equals("up") || word.equals("down")));
    }

    private static JSONObject numberPicker(NumberPicker np, String word) throws Exception {
        if (!np.isEnabled()) return fail("the control is disabled");
        int now = np.getValue();
        if (word.equals("up") || word.equals("down")) {
            boolean done = np.performAccessibilityAction(word.equals("up")
                ? AccessibilityNodeInfo.ACTION_SCROLL_FORWARD : AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD, null);
            return ok().put("via", "accessibility_scroll").put("ok", done).put("from", now).put("to", np.getValue());
        }
        int to = word.equals("min") ? np.getMinValue() : word.equals("max") ? np.getMaxValue() : Integer.parseInt(word);
        np.setValue(Math.max(np.getMinValue(), Math.min(np.getMaxValue(), to)));
        return ok().put("via", "setValue").put("from", now).put("to", np.getValue())
            .put("note", "the picker's change listener is not called by setValue");
    }

    // --- switches, spinners, date and time -------------------------------------------------

    private static JSONObject compound(CompoundButton cb, String word) throws Exception {
        boolean want;
        switch (word) {
            case "on": case "true": case "checked": case "yes": want = true; break;
            case "off": case "false": case "unchecked": case "no": want = false; break;
            default: return fail("use on or off");
        }
        boolean was = cb.isChecked();
        if (was == want) return ok().put("via", "already").put("checked", was);
        if (!cb.isEnabled()) return fail("the control is disabled");
        boolean handled = cb.performClick();  // as a tap: the app's listener runs
        return ok().put("via", "performClick").put("handled", handled).put("from", was).put("checked", cb.isChecked());
    }

    private static JSONObject spinner(Spinner sp, String value) throws Exception {
        Adapter a = sp.getAdapter();
        if (a == null || a.getCount() == 0) return fail("the spinner has no items");
        int index = -1;
        if (value.matches("\\d+")) index = Integer.parseInt(value);
        else {
            for (int i = 0; i < a.getCount() && index < 0; i++) {
                if (String.valueOf(a.getItem(i)).equalsIgnoreCase(value)) index = i;
            }
            for (int i = 0; i < a.getCount() && index < 0; i++) {
                if (String.valueOf(a.getItem(i)).toLowerCase().contains(value.toLowerCase())) index = i;
            }
        }
        if (index < 0 || index >= a.getCount()) return fail("no item matches " + value + " among " + a.getCount());
        sp.setSelection(index);
        return ok().put("via", "setSelection").put("index", index).put("item", String.valueOf(a.getItem(index)));
    }

    private static JSONObject date(DatePicker dp, String value) throws Exception {
        String[] p = value.split("-");
        if (p.length != 3) return fail("use YYYY-MM-DD");
        dp.updateDate(Integer.parseInt(p[0]), Integer.parseInt(p[1]) - 1, Integer.parseInt(p[2]));
        return ok().put("via", "updateDate").put("date", dp.getYear() + "-" + (dp.getMonth() + 1) + "-" + dp.getDayOfMonth());
    }

    private static JSONObject time(TimePicker tp, String value) throws Exception {
        String[] p = value.split(":");
        if (p.length != 2) return fail("use HH:mm (24 hour)");
        tp.setHour(Integer.parseInt(p[0]));
        tp.setMinute(Integer.parseInt(p[1]));
        return ok().put("via", "setHour/setMinute").put("time", tp.getHour() + ":" + tp.getMinute());
    }

    // --- pages, tabs and bottom navigation (AndroidX / Material, by reflection) -------------

    private static int call(Object o, String name) throws Exception {
        Method m = o.getClass().getMethod(name);
        m.setAccessible(true);
        return ((Number) m.invoke(o)).intValue();
    }

    /** next / prev / first / last / a 0-based index, against the current position and the count. */
    private static int pick(String word, int now, int count) {
        switch (word) {
            case "next": return Math.min(count - 1, now + 1);
            case "prev": case "previous": return Math.max(0, now - 1);
            case "first": return 0;
            case "last": return count - 1;
            default: return Integer.parseInt(word);
        }
    }

    private static JSONObject pager(View pager, String word) throws Exception {
        Object adapter = pager.getClass().getMethod("getAdapter").invoke(pager);
        if (adapter == null) return fail("the pager has no adapter");
        int count;
        try { count = call(adapter, "getItemCount"); } catch (NoSuchMethodException e) { count = call(adapter, "getCount"); }
        int now = call(pager, "getCurrentItem");
        int to = pick(word, now, count);
        if (to < 0 || to >= count) return fail("page " + to + " is outside 0.." + (count - 1));
        pager.getClass().getMethod("setCurrentItem", int.class, boolean.class).invoke(pager, to, true);
        return ok().put("via", "setCurrentItem").put("from", now).put("to", to).put("pages", count);
    }

    private static JSONObject tabs(View layout, String word) throws Exception {
        int count = call(layout, "getTabCount"), now = call(layout, "getSelectedTabPosition");
        int to = pick(word, now, count);
        if (to < 0 || to >= count) return fail("tab " + to + " is outside 0.." + (count - 1));
        Object tab = layout.getClass().getMethod("getTabAt", int.class).invoke(layout, to);
        tab.getClass().getMethod("select").invoke(tab);
        return ok().put("via", "Tab.select").put("from", now).put("to", to).put("tabs", count);
    }

    private static JSONObject bottomNav(View bar, String word) throws Exception {
        Object menu = bar.getClass().getMethod("getMenu").invoke(bar);
        int count = call(menu, "size");
        int selected = call(bar, "getSelectedItemId");
        int now = -1;
        for (int i = 0; i < count; i++) {
            Object item = menu.getClass().getMethod("getItem", int.class).invoke(menu, i);
            if (call(item, "getItemId") == selected) now = i;
        }
        int to = pick(word, Math.max(now, 0), count);
        if (to < 0 || to >= count) return fail("item " + to + " is outside 0.." + (count - 1));
        Object item = menu.getClass().getMethod("getItem", int.class).invoke(menu, to);
        bar.getClass().getMethod("setSelectedItemId", int.class).invoke(bar, call(item, "getItemId"));
        return ok().put("via", "setSelectedItemId").put("from", now).put("to", to).put("items", count);
    }
}
