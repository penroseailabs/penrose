package app.relay.bridge;

import android.content.Context;
import android.net.LocalServerSocket;
import android.net.LocalSocket;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.graphics.Rect;
import android.view.InputDevice;
import android.view.MotionEvent;
import android.view.ViewConfiguration;
import android.view.View;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityNodeProvider;
import android.view.inputmethod.EditorInfo;
import android.view.inputmethod.InputMethodManager;
import android.widget.AbsListView;
import android.widget.AdapterView;
import android.widget.TextView;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.OutputStreamWriter;
import java.io.Writer;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;
import java.util.Collection;
import java.util.concurrent.Callable;
import java.util.concurrent.FutureTask;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;

import de.robv.android.xposed.XposedBridge;

/**
 * Newline-delimited JSON over the abstract unix socket "relaybridge.<process name>". The host
 * reaches it with `adb forward tcp:0 localabstract:relaybridge.<process>`. Only adb (shell) and
 * root may connect; other apps on the device are refused.
 */
final class Server {
    private static final int SHELL_UID = 2000;
    private static final java.util.Set<String> WEB_COMMANDS = new java.util.HashSet<>(java.util.Arrays.asList(
        "click", "long_click", "set_text", "focus", "scroll", "ime_action", "select"));
    private static final Handler MAIN = new Handler(Looper.getMainLooper());

    /** What answers the commands on one socket: the hooked app's {@link #handle}, or the
     * notification service's own. */
    interface Wire { JSONObject handle(JSONObject request) throws Exception; }

    static void start() {
        Thread t = new Thread(() -> listen("relaybridge." + Module.process, Server::handle), "relay-bridge");
        t.setDaemon(true);
        t.start();
    }

    /** Blocks, accepting connections on the abstract socket `name` and answering them with `wire`. */
    static void listen(String name, Wire wire) {
        try (LocalServerSocket server = new LocalServerSocket(name)) {
            while (true) {
                LocalSocket socket = server.accept();
                Thread t = new Thread(() -> serve(socket, wire), "relay-bridge-conn");
                t.setDaemon(true);
                t.start();
            }
        } catch (Throwable t) {
            XposedBridge.log("relay-bridge: socket " + name + " failed: " + t);
        }
    }

    private static void serve(LocalSocket socket, Wire wire) {
        try (LocalSocket s = socket) {
            int uid = s.getPeerCredentials().getUid();
            if (uid != 0 && uid != SHELL_UID) return;
            BufferedReader in = new BufferedReader(new InputStreamReader(s.getInputStream(), StandardCharsets.UTF_8));
            Writer out = new OutputStreamWriter(s.getOutputStream(), StandardCharsets.UTF_8);
            String line;
            while ((line = in.readLine()) != null) {
                JSONObject response;
                Object id = null;
                try {
                    JSONObject request = new JSONObject(line);
                    id = request.opt("id");
                    response = wire.handle(request);
                } catch (TimeoutException e) {
                    Problems.record("main_thread_busy", line.length() > 40 ? line.substring(0, 40) : line, null, null);
                    response = new JSONObject().put("ok", false).put("error", "main thread busy (no answer within the timeout)");
                } catch (Throwable t) {
                    Problems.record("command_error", line.length() > 40 ? line.substring(0, 40) : line, t);
                    response = new JSONObject().put("ok", false).put("error", t.getClass().getSimpleName() + ": " + t.getMessage());
                }
                if (id != null) response.put("id", id);
                out.write(response.toString());
                out.write('\n');
                out.flush();
            }
        } catch (Throwable ignored) {
        }
    }

    static JSONObject handle(JSONObject r) throws Exception {
        String cmd = r.getString("cmd");
        // A DOM element of a WebView's page is acted on through the page, not as a View.
        if (r.has("h") && WEB_COMMANDS.contains(cmd) && Web.owns(r.getInt("h"))) return Web.handle(cmd, r);
        switch (cmd) {
            case "hello": {
                JSONObject hooks = new JSONObject();
                synchronized (Module.hooks) {
                    for (java.util.Map.Entry<String, String> e : Module.hooks.entrySet()) hooks.put(e.getKey(), e.getValue());
                }
                return ok().put("version", Module.VERSION).put("pkg", Module.pkg).put("process", Module.process)
                    .put("pid", android.os.Process.myPid()).put("sdk", Build.VERSION.SDK_INT).put("hooks", hooks)
                    .put("app_version", appVersion());
            }
            case "diag": {
                JSONObject fired = new JSONObject();
                for (java.util.Map.Entry<String, java.util.concurrent.atomic.AtomicInteger> e : Module.fired.entrySet()) {
                    fired.put(e.getKey(), e.getValue().get());
                }
                return ok().put("pkg", Module.pkg).put("pid", android.os.Process.myPid())
                    .put("app_version", appVersion()).put("module_version", Module.VERSION)
                    .put("fired", fired).put("problems", Problems.all());
            }
            case "snapshot": {
                // Optionally: wait for the screen to stop changing first (Settle), and hand back the events since
                // a cursor, so a look after an action is one round trip instead of sleep + snapshot + events.
                JSONObject settle = r.optJSONObject("settle");
                JSONObject settled = settle == null ? null : Settle.await(settle.optLong("quiet", 150), settle.optLong("max", 500), settle.optBoolean("from_call", false));
                // When the read began: a wait for the screen to change (changed) counts from here.
                long uptime = android.os.SystemClock.uptimeMillis();
                JSONObject snapshot = onMain(Dump::snapshot, 3000).put("uptime", uptime);
                Web.attach(snapshot);  // off the main thread: the page answers there
                if (settled != null) snapshot.put("settle", settled);
                if (r.has("events_since")) snapshot.put("events", Events.since(r.getLong("events_since"), false));
                return snapshot.put("ok", true);
            }
            case "changed": return Settle.change(r.getLong("since"), Math.min(r.optLong("max", 2000), 30000)).put("ok", true);
            case "settle": return Settle.await(r.optLong("quiet", 150), r.optLong("max", 500), r.optBoolean("from_call", true)).put("ok", true);
            case "web_page": return Web.page(r.getInt("h"));
            case "capture": return capture(r.getInt("h"));
            case "select": return fail("select is for web <select> elements");
            case "events": return ok().put("events", Events.since(r.optLong("since"), false)).put("seq", Events.lastSeq());
            case "network": return ok().put("calls", Events.since(r.optLong("since"), true));
            case "click": return click(r.getInt("h"), false);
            case "long_click": return click(r.getInt("h"), true);
            case "set_text": return onMain(() -> setText(r.getInt("h"), r.getString("text")), 3000);
            case "insert_text": return onMain(() -> insertText(r.getInt("h"), r.getString("text"), r.optString("at", "")), 3000);
            case "focus": return onMain(() -> focus(r.getInt("h")), 3000);
            case "scroll": {
                // A WebView scrolls its page through the page script; the native action is the fallback.
                if (Web.isRoot(r.getInt("h"))) {
                    JSONObject done = Web.scrollRoot(r.getInt("h"), r.optString("dir", "down"));
                    if (done != null) return done;
                }
                return onMain(() -> scroll(r.getInt("h"), r.optString("dir", "down")), 3000);
            }
            case "ime_action": return onMain(() -> imeAction(r.getInt("h")), 3000);
            case "scroll_to": return onMain(() -> scrollTo(r.getInt("h"), String.valueOf(r.get("pos"))), 3000);
            case "set_value": return onMain(() -> Controls.setValue(r.getInt("h"), String.valueOf(r.get("value"))), 3000);
            case "hide_keyboard": return onMain(Controls::hideKeyboard, 3000);
            case "inspect": return onMain(() -> Inspect.run(r.getInt("h"), r.getString("path")), 3000);
            case "prefs_list": return Prefs.list();
            case "prefs_get": return Prefs.get(r.getString("name"), r.optString("key", null));
            case "prefs_set": return Prefs.set(r.getString("name"), r.getString("key"), r.get("value"), r.optString("type", null));
            case "prefs_remove": return Prefs.remove(r.getString("name"), r.getString("key"));
            case "list_data": return onMain(() -> listData(r.getInt("h"), r.optInt("limit", 200)), 3000);
            case "text": return onMain(() -> fullText(r.getInt("h"), r.optInt("start", 0), r.optInt("limit", 6000)), 3000);
            case "start_activity": return onMain(() -> startActivity(r.getString("class"), r.optJSONObject("extras")), 3000);
            default: return new JSONObject().put("ok", false).put("error", "unknown cmd " + cmd);
        }
    }

    private static JSONObject ok() throws Exception { return new JSONObject().put("ok", true); }

    /** "versionName (versionCode)" of the hooked app: findings are per app version, since an
     * update can rename classes and change which fallback is needed. */
    static String appVersion() {
        try {
            android.app.Activity a = Dump.currentActivity();
            Context c = a != null ? a : (Context) Class.forName("android.app.ActivityThread")
                .getMethod("currentApplication").invoke(null);
            android.content.pm.PackageInfo info = c.getPackageManager().getPackageInfo(Module.pkg, 0);
            return info.versionName + " (" + info.getLongVersionCode() + ")";
        } catch (Throwable t) {
            return null;
        }
    }

    private static JSONObject fail(String why) throws Exception { return new JSONObject().put("ok", false).put("error", why); }

    /**
     * A picture of one element, drawn by the app itself: the view behind the handle is drawn into a bitmap on the
     * main thread, so the bounds and the pixels are from the same instant and nothing drawn over it by another
     * view (a button floating on a photo, the keyboard) is in it. A DOM element of a WebView's page is cut out of
     * the WebView's drawing at the place the page reports now (Web.capture). Answers {"jpeg": base64, "w", "h"}.
     */
    static JSONObject capture(int h) throws Exception {
        Dump.Target t = target(h);
        if (t == null) return fail("stale handle");
        if (t.isWeb()) return Web.capture(t);
        return onMain(() -> {
            View v = t.view.get();
            if (v == null || !v.isAttachedToWindow() || v.getWidth() <= 0 || v.getHeight() <= 0) return fail("the element is not on screen");
            Rect crop = null;
            if (t.isVirtual()) {  // a Compose node: its bounds within the host view, from the accessibility provider
                AccessibilityNodeProvider p = v.getAccessibilityNodeProvider();
                AccessibilityNodeInfo info = p == null ? null : p.createAccessibilityNodeInfo(t.virtualId);
                if (info == null) return fail("the element is not on screen");
                Rect screen = new Rect();
                info.getBoundsInScreen(screen);
                int[] off = new int[2];
                v.getLocationOnScreen(off);
                crop = new Rect(screen.left - off[0], screen.top - off[1], screen.right - off[0], screen.bottom - off[1]);
                info.recycle();
            }
            if (crop == null && v instanceof android.widget.ImageView) {
                // The image as the app loaded it: the decoded bitmap, not the view's scaled, masked drawing of it.
                android.graphics.Bitmap source = loadedBitmap(((android.widget.ImageView) v).getDrawable());
                if (source != null) return ok().put("jpeg", encode(source)).put("w", source.getWidth()).put("h", source.getHeight()).put("source", "bitmap");
            }
            return ok().put("jpeg", encode(draw(v, crop))).put("w", crop == null ? v.getWidth() : crop.width())
                .put("h", crop == null ? v.getHeight() : crop.height());
        }, 4000);
    }

    /** The bitmap behind a drawable, in a software config (a hardware bitmap is copied), or null when there is none. */
    static android.graphics.Bitmap loadedBitmap(android.graphics.drawable.Drawable d) {
        try {
            while (d instanceof android.graphics.drawable.DrawableWrapper) d = ((android.graphics.drawable.DrawableWrapper) d).getDrawable();
            if (d instanceof android.graphics.drawable.LayerDrawable && ((android.graphics.drawable.LayerDrawable) d).getNumberOfLayers() == 1) {
                d = ((android.graphics.drawable.LayerDrawable) d).getDrawable(0);
            }
            android.graphics.Bitmap b = null;
            if (d instanceof android.graphics.drawable.BitmapDrawable) b = ((android.graphics.drawable.BitmapDrawable) d).getBitmap();
            else if (d != null && d.getIntrinsicWidth() > 0 && d.getIntrinsicHeight() > 0 && !(d instanceof android.graphics.drawable.ColorDrawable)) {
                // Rounded, WebP, animated or library drawables: drawn once at their own size.
                b = android.graphics.Bitmap.createBitmap(d.getIntrinsicWidth(), d.getIntrinsicHeight(), android.graphics.Bitmap.Config.ARGB_8888);
                android.graphics.Canvas c = new android.graphics.Canvas(b);
                android.graphics.Rect old = d.copyBounds();
                d.setBounds(0, 0, b.getWidth(), b.getHeight());
                try { d.draw(c); } finally { d.setBounds(old); }
                return b;
            }
            if (b == null || b.getWidth() < 32 || b.getHeight() < 32) return null;
            if (b.getConfig() == android.graphics.Bitmap.Config.HARDWARE) b = b.copy(android.graphics.Bitmap.Config.ARGB_8888, false);
            return b;
        } catch (Throwable t) {
            return null;
        }
    }

    /** The view drawn into a bitmap (its own drawing, children included), cut to `crop` (view coordinates) when given. */
    static android.graphics.Bitmap draw(View v, Rect crop) throws Exception {
        android.graphics.Bitmap whole = android.graphics.Bitmap.createBitmap(v.getWidth(), v.getHeight(), android.graphics.Bitmap.Config.ARGB_8888);
        android.graphics.Canvas canvas = new android.graphics.Canvas(whole);
        canvas.drawColor(android.graphics.Color.WHITE);  // JPEG has no transparency: rounded corners and such go white, not black
        v.draw(canvas);
        if (crop == null) return whole;
        Rect c = new Rect(crop);
        if (!c.intersect(0, 0, v.getWidth(), v.getHeight()) || c.width() <= 0 || c.height() <= 0) throw new Exception("the element is not on screen");
        return android.graphics.Bitmap.createBitmap(whole, c.left, c.top, c.width(), c.height());
    }

    static String encode(android.graphics.Bitmap b) {
        java.io.ByteArrayOutputStream out = new java.io.ByteArrayOutputStream();
        b.compress(android.graphics.Bitmap.CompressFormat.JPEG, 88, out);
        return android.util.Base64.encodeToString(out.toByteArray(), android.util.Base64.NO_WRAP);
    }

    /**
     * Opens one of this app's own activities as the app itself would, exported or not (Chrome's sync consent
     * screen, which `am start` cannot give its nested Bundle). `extras` maps keys to strings, numbers,
     * booleans, or JSON objects for nested Bundles. Android lets an app start activities only while it is in
     * the foreground, so the caller brings the app up first.
     */
    private static JSONObject startActivity(String cls, JSONObject extras) throws Exception {
        android.app.Activity a = Dump.currentActivity();
        Context c = a != null ? a : (Context) Class.forName("android.app.ActivityThread")
            .getMethod("currentApplication").invoke(null);
        android.content.Intent intent = new android.content.Intent().setClassName(c.getPackageName(), cls);
        if (extras != null) intent.putExtras(bundle(extras));
        if (a == null) intent.addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK);
        c.startActivity(intent);
        return ok().put("from_activity", a != null);
    }

    private static android.os.Bundle bundle(JSONObject o) throws Exception {
        android.os.Bundle b = new android.os.Bundle();
        for (java.util.Iterator<String> keys = o.keys(); keys.hasNext(); ) {
            String k = keys.next();
            Object v = o.get(k);
            if (v instanceof JSONObject) b.putBundle(k, bundle((JSONObject) v));
            else if (v instanceof Boolean) b.putBoolean(k, (Boolean) v);
            else if (v instanceof Integer) b.putInt(k, (Integer) v);
            else if (v instanceof Long) b.putLong(k, (Long) v);
            else if (v instanceof Number) b.putDouble(k, ((Number) v).doubleValue());
            else b.putString(k, String.valueOf(v));
        }
        return b;
    }

    static <T> T onMain(Callable<T> work, long timeoutMs) throws Exception {
        if (Looper.myLooper() == Looper.getMainLooper()) return work.call();
        FutureTask<T> task = new FutureTask<>(work);
        MAIN.post(task);
        try {
            return task.get(timeoutMs, TimeUnit.MILLISECONDS);
        } catch (TimeoutException e) {
            // Not run late: a busy app (a cold start) would otherwise work through stale snapshots queued behind
            // its own work, slowing it further just when it is slowest. A cancelled task is skipped when its
            // turn comes; one already running finishes and its result is dropped.
            task.cancel(false);
            throw e;
        } catch (java.util.concurrent.ExecutionException e) {
            Throwable cause = e.getCause();
            throw cause instanceof Exception ? (Exception) cause : new RuntimeException(cause);
        }
    }

    /** The live view (or virtual node host) for a handle, or null when it is gone. */
    static Dump.Target target(int h) {
        Dump.Target t = Dump.targets.get(h);
        if (t == null) return null;
        View v = t.view.get();
        return v != null && v.isAttachedToWindow() ? t : null;
    }

    private static boolean virtual(Dump.Target t, int action, Bundle args) {
        AccessibilityNodeProvider p = t.view.get().getAccessibilityNodeProvider();
        return p != null && p.performAction(t.virtualId, action, args);
    }

    /**
     * A finger tap or long press on the view: touch events sent to the window that shows it, at the
     * middle of the view's visible part, from inside the app. The app's own touch dispatch decides
     * what the touch reaches, exactly as for a finger, so listeners, list rows, React Native and
     * Compose gestures all work the same way. Virtual nodes (Compose, Chromium) take the
     * accessibility click their provider offers.
     */
    static JSONObject click(int h, boolean longPress) throws Exception {
        Dump.Target t = target(h);
        if (t == null) return fail("stale handle");
        View chip = t.view.get();
        if (chip != null && Inline.views.containsKey(chip)) {
            // The chip's content is another process's surface: a touch sent through this window's
            // dispatch never reaches it. The host taps the screen at the point given here.
            return onMain(() -> {
                Rect visible = new Rect();
                if (!chip.isAttachedToWindow() || !chip.getGlobalVisibleRect(visible) || visible.isEmpty()) {
                    return fail("the element is not on screen");
                }
                int[] off = new int[2];
                chip.getRootView().getLocationOnScreen(off);
                return ok().put("via", "host tap").put("tap_at", new JSONArray()
                    .put(visible.centerX() + off[0]).put(visible.centerY() + off[1]));
            }, 3000);
        }
        long asked = SystemClock.uptimeMillis();
        if (t.isVirtual()) {
            return refused(asked, onMain(() -> {
                boolean done = virtual(t, longPress ? AccessibilityNodeInfo.ACTION_LONG_CLICK : AccessibilityNodeInfo.ACTION_CLICK, null);
                return ok().put("ok", done).put("via", "accessibility_action").put("error", done ? JSONObject.NULL : "virtual node refused the action");
            }, 3000));
        }
        final long[] down = new long[1];
        final float[] at = new float[2];
        JSONObject started = onMain(() -> {
            View v = t.view.get();
            Rect visible = new Rect();
            if (v == null || !v.isAttachedToWindow() || !v.getGlobalVisibleRect(visible) || visible.isEmpty()) {
                return fail("the element is not on screen");
            }
            at[0] = visible.exactCenterX();
            at[1] = visible.exactCenterY();
            if (t.area != null) {  // a link inside a text: its own part of the view, in the root's coordinates
                int[] loc = new int[2], rootLoc = new int[2];
                v.getLocationInWindow(loc);
                v.getRootView().getLocationInWindow(rootLoc);
                Rect part = new Rect(t.area);
                part.offset(loc[0] - rootLoc[0], loc[1] - rootLoc[1]);
                if (!Rect.intersects(part, visible)) return fail("the link is not on screen");
                at[0] = part.exactCenterX();
                at[1] = part.exactCenterY();
            }
            down[0] = SystemClock.uptimeMillis();
            boolean taken = touch(v.getRootView(), MotionEvent.ACTION_DOWN, down[0], down[0], at);
            return ok().put("handled", taken);
        }, 3000);
        if (!started.optBoolean("ok")) return started;
        // A finger rests briefly on a tap; a long press outlasts the long-press timeout.
        Thread.sleep(longPress ? ViewConfiguration.getLongPressTimeout() + 150 : 60);
        onMain(() -> {
            View v = t.view.get();
            View root = v != null && v.isAttachedToWindow() ? v.getRootView() : null;
            if (root != null) touch(root, MotionEvent.ACTION_UP, down[0], SystemClock.uptimeMillis(), at);
            return null;
        }, 3000);
        JSONObject out = ok().put("via", longPress ? "touch (long press)" : "touch")
            .put("x", Math.round(at[0])).put("y", Math.round(at[1])).put("handled", started.optBoolean("handled"));
        if (!started.optBoolean("handled")) out.put("ok", false).put("error", "nothing at that point took the touch");
        // The view runs its click after the touch ends, in a message posted behind it: wait for that message,
        // so a click the Guard refused is reported here.
        onMain(() -> null, 3000);
        return refused(asked, out);
    }

    /** `out`, or the refusal when the Guard refused something since `asked`. */
    private static JSONObject refused(long asked, JSONObject out) throws Exception {
        if (!Guard.refusedSince(asked)) return out;
        return out.put("ok", false).put("refused", true).put("error", Guard.REASON);
    }

    /** One single-finger touch event, in the window's coordinates, through the window's own dispatch. */
    private static boolean touch(View root, int action, long downTime, long eventTime, float[] at) {
        MotionEvent.PointerProperties[] props = {new MotionEvent.PointerProperties()};
        props[0].id = 0;
        props[0].toolType = MotionEvent.TOOL_TYPE_FINGER;
        MotionEvent.PointerCoords[] coords = {new MotionEvent.PointerCoords()};
        coords[0].x = at[0];
        coords[0].y = at[1];
        coords[0].pressure = 1f;
        coords[0].size = 1f;
        MotionEvent ev = MotionEvent.obtain(downTime, eventTime, action, 1, props, coords, 0, 0, 1f, 1f, 0, 0,
            InputDevice.SOURCE_TOUCHSCREEN, 0);
        try {
            return root.dispatchTouchEvent(ev);
        } finally {
            ev.recycle();
        }
    }

    /** `text` added at the start or end of what a native field holds, which is left exactly as it is (its line
     * breaks too): retyped whole, a note lost its final newline (2026-11-15). A command of its own, so a bridge
     * or a page that does not know it refuses it rather than replacing the field. */
    static JSONObject insertText(int h, String text, String at) throws Exception {
        Dump.Target t = target(h);
        if (t == null) return fail("stale handle");
        if (!"start".equals(at) && !"end".equals(at)) return fail("at must be start or end");
        if (t.isVirtual() || t.isWeb()) return fail("adding to a field's text needs a native field: type the whole text");
        View v = t.view.get();
        if (!(v instanceof TextView) || !(((TextView) v).getText() instanceof android.text.Editable)) return fail("not an editable text field");
        TextView tv = (TextView) v;
        tv.requestFocus();
        android.text.Editable e = (android.text.Editable) tv.getText();
        if ("start".equals(at)) e.insert(0, text); else e.append(text);
        if (tv instanceof android.widget.EditText) ((android.widget.EditText) tv).setSelection("start".equals(at) ? text.length() : e.length());
        return ok().put("via", "insert").put("now", e.toString());
    }

    static JSONObject setText(int h, String text) throws Exception {
        Dump.Target t = target(h);
        if (t == null) return fail("stale handle");
        if (t.isVirtual()) {
            Bundle args = new Bundle();
            args.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text);
            virtual(t, AccessibilityNodeInfo.ACTION_FOCUS, null);
            boolean done = virtual(t, AccessibilityNodeInfo.ACTION_SET_TEXT, args);
            return ok().put("ok", done).put("via", "accessibility_set_text");
        }
        View v = t.view.get();
        if (!(v instanceof TextView)) return fail("not a text field");
        TextView tv = (TextView) v;
        tv.requestFocus();
        tv.setText(text);
        if (tv instanceof android.widget.EditText) ((android.widget.EditText) tv).setSelection(tv.getText().length());
        return ok().put("via", "setText").put("now", tv.getText().toString());
    }

    static JSONObject focus(int h) throws Exception {
        Dump.Target t = target(h);
        if (t == null) return fail("stale handle");
        if (t.isVirtual()) return ok().put("ok", virtual(t, AccessibilityNodeInfo.ACTION_FOCUS, null)).put("via", "accessibility_focus");
        View v = t.view.get();
        boolean focused = v.requestFocus();
        InputMethodManager imm = (InputMethodManager) v.getContext().getSystemService(Context.INPUT_METHOD_SERVICE);
        if (imm != null) imm.showSoftInput(v, InputMethodManager.SHOW_IMPLICIT);
        return ok().put("ok", focused).put("via", "requestFocus");
    }

    static JSONObject scroll(int h, String dir) throws Exception {
        Dump.Target t = target(h);
        if (t == null) return fail("stale handle");
        boolean forward = dir.equals("down") || dir.equals("right");
        int action = forward ? AccessibilityNodeInfo.ACTION_SCROLL_FORWARD : AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD;
        if (t.isVirtual()) return ok().put("ok", virtual(t, action, null)).put("via", "accessibility_scroll");
        boolean done = t.view.get().performAccessibilityAction(action, null);
        return ok().put("ok", done).put("via", "accessibility_scroll");
    }

    /** The keyboard's action key (Search, Next, Done, Go) as the app's editor listener sees it. */
    static JSONObject imeAction(int h) throws Exception {
        Dump.Target t = target(h);
        if (t == null || t.isVirtual()) return fail(t == null ? "stale handle" : "virtual field: use the enter key");
        View v = t.view.get();
        if (!(v instanceof TextView)) return fail("not a text field");
        TextView tv = (TextView) v;
        int action = tv.getImeOptions() & EditorInfo.IME_MASK_ACTION;
        if (action == EditorInfo.IME_ACTION_UNSPECIFIED || action == EditorInfo.IME_ACTION_NONE) action = EditorInfo.IME_ACTION_DONE;
        tv.onEditorAction(action);
        return ok().put("via", "onEditorAction").put("action", action);
    }

    /** Most pages one scroll_to may scroll through when it has to scroll its way to an end. */
    private static final int MAX_SCROLL_PAGES = 200;

    /**
     * `pos` is a position, or "top" / "bottom" for the first and last row. R8 renames
     * RecyclerView.scrollToPosition in minified apps (one cinema app: one of ten anonymous (int)
     * methods), so: the list's own method by name, else its LayoutManager's (kept in the apps seen
     * so far), else, for an end, scrollBy until the list stops (scrollBy overrides View's, so its
     * name survives). Never a method picked by signature alone: that could call the wrong one.
     */
    static JSONObject scrollTo(int h, String pos) throws Exception {
        Dump.Target t = target(h);
        if (t == null) return fail("stale handle");
        if (t.isVirtual() || t.isWeb()) return fail("not a native list");
        View v = t.view.get();
        if (!Dump.isList(v)) return fail("not a list: " + v.getClass().getName());
        int count = -1;
        try {
            if (v instanceof AdapterView) count = ((AdapterView<?>) v).getCount();
            else {
                Object adapter = v.getClass().getMethod("getAdapter").invoke(v);
                if (adapter != null) count = (Integer) adapter.getClass().getMethod("getItemCount").invoke(adapter);
            }
        } catch (Throwable ignored) {
        }
        if (count < 0) {  // getAdapter() renamed too: the count the list reports to accessibility
            AccessibilityNodeInfo info = v.createAccessibilityNodeInfo();
            AccessibilityNodeInfo.CollectionInfo ci = info.getCollectionInfo();
            if (ci != null) count = Math.max(ci.getRowCount(), ci.getColumnCount());
        }
        boolean top = pos.equals("top"), bottom = pos.equals("bottom");
        int target;
        if (top) target = 0;
        else if (bottom) target = count - 1;  // -1 when unknown: only the scroll loop can reach the end
        else target = Integer.parseInt(pos);
        if (!bottom && count >= 0 && target >= count) return fail("position " + target + " is beyond the list's " + count + " rows");
        if (v instanceof AbsListView) {
            if (target < 0) return fail("cannot tell how many rows the list has");
            ((AbsListView) v).setSelection(target);
            return ok().put("via", "setSelection").put("position", target).put("items", count);
        }
        if (target >= 0) {
            Method m = intMethod(v.getClass(), "scrollToPosition");
            if (m != null) {
                m.invoke(v, target);
                return ok().put("via", "scrollToPosition").put("position", target).put("items", count);
            }
            Object lm = null;
            try {
                lm = v.getClass().getMethod("getLayoutManager").invoke(v);
            } catch (Throwable ignored) {
            }
            Method lmJump = lm == null ? null : intMethod(lm.getClass(), "scrollToPosition");
            if (lmJump != null) {
                Problems.record("fallback", "scroll_to_via_layout_manager", v.getClass().getName(), null);
                lmJump.invoke(lm, target);  // the LayoutManager asks for a layout; the jump shows on the next frame
                return ok().put("via", "layoutManager.scrollToPosition").put("position", target).put("items", count);
            }
        }
        if (!top && !bottom) return fail("the list's scrollToPosition is renamed in this app; only top and bottom work");
        Problems.record("fallback", "scroll_to_via_scroll_loop", v.getClass().getName(), null);
        return scrollToEnd(v, bottom, count);
    }

    /** A one-int instance method named `name` on `c` or a superclass, public or not; null when R8 renamed it. */
    private static Method intMethod(Class<?> c, String name) {
        for (Class<?> k = c; k != null && k != Object.class; k = k.getSuperclass()) {
            try {
                Method m = k.getDeclaredMethod(name, int.class);
                m.setAccessible(true);
                return m;
            } catch (NoSuchMethodException ignored) {
            } catch (Throwable t) {
                return null;
            }
        }
        return null;
    }

    /** Scroll a list to its first or last row by pages, until it cannot scroll further that way. */
    private static JSONObject scrollToEnd(View v, boolean bottom, int count) throws Exception {
        int dir = bottom ? 1 : -1;
        boolean vertical = v.canScrollVertically(1) || v.canScrollVertically(-1) || !(v.canScrollHorizontally(1) || v.canScrollHorizontally(-1));
        int page = Math.max(1, vertical ? v.getHeight() : v.getWidth());
        int pages = 0;
        long until = SystemClock.uptimeMillis() + 2000;  // inside the 3 s main-thread budget
        while (pages < MAX_SCROLL_PAGES && SystemClock.uptimeMillis() < until
            && (vertical ? v.canScrollVertically(dir) : v.canScrollHorizontally(dir))) {
            if (vertical) v.scrollBy(0, dir * page);
            else v.scrollBy(dir * page, 0);
            pages++;
        }
        boolean more = vertical ? v.canScrollVertically(dir) : v.canScrollHorizontally(dir);
        JSONObject out = ok().put("via", "scrollBy").put("pages", pages).put("items", count);
        if (more) out.put("ok", false).put("error", "still not at the " + (bottom ? "last" : "first") + " row after "
            + pages + " pages (the list keeps loading more?)");
        return out;
    }

    /** The data behind a list, not just its visible rows: ListAdapter.getCurrentList(), or the
     * first List field on the adapter. Items are toString()-ed (Kotlin data classes print well). */
    /** The whole text of one view, which a snapshot clips (Dump.MAX_TEXT): a note open in an editor, a long
     * message, an article. From `start`, at most `limit` characters; never a password field's. */
    static JSONObject fullText(int h, int start, int limit) throws Exception {
        Dump.Target t = target(h);
        if (t == null || t.isVirtual() || t.isWeb()) return fail(t != null && t.isWeb() ? "a web element: read_page reads the page" : "stale handle");
        View v = t.view.get();
        if (!(v instanceof TextView)) return fail("not a text view");
        TextView tv = (TextView) v;
        if (Dump.isPasswordField(tv)) return fail("a password field is never read");
        CharSequence all = tv.getText();
        String s = all == null ? "" : all.toString();
        int from = Math.max(0, Math.min(start, s.length()));
        int to = Math.min(s.length(), from + Math.max(1, limit));
        return ok().put("total", s.length()).put("start", from).put("text", s.substring(from, to));
    }

    static JSONObject listData(int h, int limit) throws Exception {
        Dump.Target t = target(h);
        if (t == null || t.isVirtual() || t.isWeb()) return fail(t != null && t.isWeb() ? "not a native list" : "stale handle");
        View v = t.view.get();
        Object adapter;
        try {
            adapter = v.getClass().getMethod("getAdapter").invoke(v);
        } catch (NoSuchMethodException e) {
            return fail("view has no getAdapter()");
        }
        if (adapter == null) return fail("list has no adapter");
        Collection<?> items = null;
        try {
            items = (Collection<?>) adapter.getClass().getMethod("getCurrentList").invoke(adapter);
        } catch (Throwable ignored) {
        }
        String source = "getCurrentList()";
        for (Class<?> k = adapter.getClass(); items == null && k != null && k != Object.class; k = k.getSuperclass()) {
            for (Field f : k.getDeclaredFields()) {
                if (!Collection.class.isAssignableFrom(f.getType())) continue;
                f.setAccessible(true);
                Object value = f.get(adapter);
                if (value != null && !((Collection<?>) value).isEmpty()) {
                    items = (Collection<?>) value;
                    source = k.getSimpleName() + "." + f.getName();
                    break;
                }
            }
        }
        if (items == null) return fail("no collection found on " + adapter.getClass().getName());
        JSONArray out = new JSONArray();
        for (Object item : items) {
            if (out.length() >= limit) break;
            out.put(item == null ? "null" : Events.clip(item.toString(), 500));
        }
        return ok().put("adapter", adapter.getClass().getName()).put("source", source)
            .put("total", items.size()).put("items", out);
    }
}
