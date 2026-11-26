package app.relay.bridge;

import android.content.Context;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.view.InputDevice;
import android.view.MotionEvent;
import android.view.View;
import android.view.inputmethod.InputMethodManager;
import android.webkit.ConsoleMessage;
import android.webkit.WebChromeClient;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import org.json.JSONArray;
import org.json.JSONObject;
import org.json.JSONTokener;

import java.lang.ref.WeakReference;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashSet;
import java.util.Iterator;
import java.util.List;
import java.util.Set;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;

import de.robv.android.xposed.XC_MethodHook;
import de.robv.android.xposed.XposedBridge;

/**
 * Pages shown by android.webkit.WebView. Their pixels are not Views, so the view dump sees one
 * opaque box; this reads the DOM instead, by running web.js in the page (evaluateJavascript),
 * and hangs the result under the WebView's node in the snapshot as ordinary nodes: numbered,
 * with roles, labels, state and list rows. Acting on those nodes goes back through the same
 * script, except clicks, which are real touches on the WebView (so the page sees trusted input)
 * and fall back to a script click only when no input event reached the page.
 *
 * All WebView methods must run on the main thread; the methods here run on a bridge connection
 * thread and hop to it only for the call itself, so the page's answer never blocks the UI.
 */
final class Web {
    static final String SCRIPT_VERSION = "11";
    private static final long EVAL_MS = 2500;
    private static final Handler MAIN = new Handler(Looper.getMainLooper());

    /** Every WebView constructed in this process. Settle signals read from here, so they need no tree walk. */
    private static final List<WeakReference<WebView>> views = new CopyOnWriteArrayList<>();
    private static final Set<Class<?>> hookedClients = Collections.synchronizedSet(new HashSet<Class<?>>());

    static void register(WebView v) {
        views.add(new WeakReference<>(v));
        views.removeIf(r -> r.get() == null);
    }

    // ---- talking to the page -----------------------------------------------------------------

    /** The expression to evaluate: the script's API (installed on first use per page), then `call`. */
    static String script(String call) {
        return "(window.__relay&&window.__relay.v==='" + SCRIPT_VERSION + "'?window.__relay:" + WebScript.SOURCE + ")." + call;
    }

    /** Runs `call` on the page's API and returns what it answered (JSONObject, String, ...), or
     * JSONObject.NULL when the page gave nothing (JavaScript disabled, no document yet). Never call
     * from the main thread: the answer arrives there. */
    static Object eval(final WebView v, String call, long timeoutMs) throws Exception {
        if (Looper.myLooper() == Looper.getMainLooper()) throw new IllegalStateException("Web.eval on the main thread");
        final CountDownLatch done = new CountDownLatch(1);
        final AtomicReference<String> value = new AtomicReference<>();
        final AtomicReference<Throwable> failure = new AtomicReference<>();
        final String js = script(call);
        MAIN.post(() -> {
            try {
                v.evaluateJavascript(js, result -> { value.set(result); done.countDown(); });
            } catch (Throwable t) {
                failure.set(t);
                done.countDown();
            }
        });
        if (!done.await(timeoutMs, TimeUnit.MILLISECONDS)) throw new TimeoutException("the page did not answer within " + timeoutMs + " ms");
        if (failure.get() != null) throw new Exception(failure.get());
        String s = value.get();
        if (s == null || s.equals("null")) return JSONObject.NULL;
        return new JSONTokener(s).nextValue();
    }

    private static JSONObject fail(String why) throws Exception {
        return new JSONObject().put("ok", false).put("error", why);
    }

    private static JSONObject ok() throws Exception { return new JSONObject().put("ok", true); }

    /** One action on one DOM element of the page. */
    private static JSONObject act(Dump.Target t, String action, JSONObject args) throws Exception {
        WebView v = (WebView) t.view.get();
        if (v == null) return fail("stale handle");
        String call = "act(" + JSONObject.quote(t.epoch) + "," + t.webId + "," + JSONObject.quote(action) + ","
            + (args == null ? "{}" : args.toString()) + ")";
        Object r;
        try {
            r = eval(v, call, EVAL_MS);
        } catch (TimeoutException e) {
            return fail(e.getMessage());
        }
        return r instanceof JSONObject ? (JSONObject) r : fail("the page returned no answer (navigated away, or JavaScript is disabled)");
    }

    // ---- the snapshot ------------------------------------------------------------------------

    /** CSS px in the page's visual viewport to screen px. */
    private static final class Mapper {
        final double left, top, vx, vy, fx, fy;
        Mapper(int[] box, JSONObject vv) throws Exception {
            left = box[0]; top = box[1];
            vx = vv.getDouble("x"); vy = vv.getDouble("y");
            fx = box[2] / vv.getDouble("w");
            fy = box[3] / vv.getDouble("h");
        }
        int x(double css) { return (int) Math.round(left + (css - vx) * fx); }
        int y(double css) { return (int) Math.round(top + (css - vy) * fy); }
    }

    /** Fills the DOM into every WebView node of the snapshot. A failure leaves that node opaque. */
    static void attach(JSONObject snapshot) {
        try {
            JSONArray windows = snapshot.optJSONArray("windows");
            if (windows == null) return;
            List<JSONObject> found = new ArrayList<>();
            for (int i = 0; i < windows.length(); i++) collect(windows.getJSONObject(i).optJSONArray("nodes"), found);
            for (JSONObject node : found) {
                try {
                    attachOne(node);
                } catch (Throwable t) {
                    Problems.record("web_dump_failed", "attach", t);
                    Dump.markOpaque(node);
                }
            }
        } catch (Throwable t) {
            Problems.record("web_dump_failed", "attach", t);
        }
    }

    private static void collect(JSONArray nodes, List<JSONObject> out) throws Exception {
        if (nodes == null) return;
        for (int i = 0; i < nodes.length(); i++) {
            JSONObject n = nodes.getJSONObject(i);
            if (n.optBoolean("webview")) out.add(n);
            collect(n.optJSONArray("c"), out);
        }
    }

    private static int[] boxOf(final WebView v) throws Exception {
        return Server.onMain(() -> {
            int[] loc = new int[2];
            v.getLocationOnScreen(loc);
            return new int[]{loc[0], loc[1], v.getWidth(), v.getHeight()};
        }, 1000);
    }

    private static void attachOne(JSONObject node) throws Exception {
        Dump.Target t = Dump.targets.get(node.getInt("h"));
        WebView wv = t == null ? null : (WebView) t.view.get();
        if (wv == null) return;
        String where = wv.getClass().getName();
        Object r;
        try {
            r = eval(wv, "dump({scope:'viewport'})", EVAL_MS);
        } catch (TimeoutException e) {
            Problems.record("web_dump_failed", where, e.getMessage(), null);
            Dump.markOpaque(node);
            return;
        }
        if (!(r instanceof JSONObject)) {
            boolean js = Server.onMain(() -> wv.getSettings().getJavaScriptEnabled(), 1000);
            Problems.record(js ? "web_dump_failed" : "web_js_disabled", where,
                js ? "evaluateJavascript returned nothing (no document yet?)" : "this WebView has JavaScript disabled, so its page cannot be read", null);
            Dump.markOpaque(node);
            return;
        }
        JSONObject d = (JSONObject) r;
        if (d.has("error")) {
            Problems.record("web_dump_failed", where, d.optString("error"), d.optString("stack", null));
            Dump.markOpaque(node);
            return;
        }
        JSONObject vv = d.getJSONObject("vv");
        if (vv.getDouble("w") <= 0 || vv.getDouble("h") <= 0) {
            Problems.record("web_dump_failed", where, "the page reports an empty viewport", null);
            return;
        }
        Set<Integer> kept = new HashSet<>();
        JSONArray kids = convertAll(d.getJSONArray("nodes"), new Mapper(boxOf(wv), vv), wv, d.getString("epoch"), kept);
        Dump.retainWeb(wv, kept);
        node.put("c", kids);
        node.put("url", d.optString("url"));
        if (!d.optString("title").isEmpty()) node.put("title", Dump.clip(d.optString("title")));
        node.put("page", new JSONObject().put("scroll", d.get("scroll")).put("ready", d.optString("ready"))
            .put("nodes", d.optInt("count")).put("ms", d.optInt("ms")).put("vv", vv).put("stats", d.opt("stats")));
        report(where, d, kids.length());
    }

    /** What the page told us about its own readability, as problems for the host's diagnostics. */
    private static void report(String where, JSONObject d, int kept) throws Exception {
        JSONObject frames = d.optJSONObject("frames");
        JSONArray blocked = frames == null ? null : frames.optJSONArray("blocked");
        for (int i = 0; blocked != null && i < blocked.length(); i++) {
            Problems.record("web_blocked_frame", blocked.getString(i), "a cross-origin iframe: its content is not readable from the page script", null);
        }
        if (d.optBoolean("truncated")) Problems.record("web_truncated", where, "the page has more elements than the dump budget", null);
        String url = d.optString("url");
        JSONObject sc = d.optJSONObject("scroll");
        // Blank only counts when there is nowhere else to scroll to: the middle of a long page can be empty.
        boolean shortPage = sc == null || sc.optDouble("h") <= d.getJSONObject("vv").getDouble("h") * 1.5;
        if (kept == 0 && shortPage && "complete".equals(d.optString("ready")) && !url.isEmpty() && !url.startsWith("about:")) {
            Problems.record("web_empty", where, "a loaded page with nothing readable: canvas/WebGL content, or all of it off screen"
                + " (" + url + ", page " + (sc == null ? "?" : sc.optInt("w") + "x" + sc.optInt("h")) + ")", null);
        }
    }

    private static JSONArray convertAll(JSONArray in, Mapper m, WebView v, String epoch, Set<Integer> kept) throws Exception {
        JSONArray out = new JSONArray();
        for (int i = 0; i < in.length(); i++) out.put(convert(in.getJSONObject(i), m, v, epoch, kept));
        return out;
    }

    private static JSONObject convert(JSONObject j, Mapper m, WebView v, String epoch, Set<Integer> kept) throws Exception {
        JSONObject n = new JSONObject();
        for (Iterator<String> it = j.keys(); it.hasNext(); ) {
            String k = it.next();
            if (k.equals("i") || k.equals("b") || k.equals("c")) continue;
            n.put(k, j.get(k));
        }
        JSONArray b = j.getJSONArray("b");
        n.put("b", new JSONArray().put(m.x(b.getDouble(0))).put(m.y(b.getDouble(1))).put(m.x(b.getDouble(2))).put(m.y(b.getDouble(3))));
        n.put("dom", true).put("virtual", true);
        if (j.has("i")) {
            int h = Dump.handleWeb(v, epoch, j.getInt("i"));
            kept.add(h);
            n.put("h", h);
        }
        if (j.has("c")) n.put("c", convertAll(j.getJSONArray("c"), m, v, epoch, kept));
        return n;
    }

    /** The whole page, not just the screen: its text and every readable element, off-screen ones
     * flagged "offscreen". For the host's page tool; handles issued here are valid until the next snapshot. */
    static JSONObject page(int h) throws Exception {
        Dump.Target t = Dump.targets.get(h);
        View v = t == null ? null : t.view.get();
        if (!(v instanceof WebView)) return fail("not a WebView");
        WebView wv = (WebView) v;
        Object r = eval(wv, "dump({scope:'page'})", 4000);
        if (!(r instanceof JSONObject) || ((JSONObject) r).has("error")) return fail("the page could not be read");
        JSONObject d = (JSONObject) r;
        Object text = eval(wv, "text(20000)", EVAL_MS);
        JSONArray nodes = convertAll(d.getJSONArray("nodes"), new Mapper(boxOf(wv), d.getJSONObject("vv")), wv, d.getString("epoch"), new HashSet<Integer>());
        return ok().put("url", d.optString("url")).put("title", d.optString("title")).put("scroll", d.get("scroll"))
            .put("truncated", d.optBoolean("truncated")).put("nodes", nodes)
            .put("text", text instanceof JSONObject ? ((JSONObject) text).optString("text") : "");
    }

    // ---- acting ------------------------------------------------------------------------------

    private static Dump.Target webTarget(int h) {
        Dump.Target t = Dump.targets.get(h);
        if (t == null || !t.isWeb()) return null;
        View v = t.view.get();
        return v != null && v.isAttachedToWindow() ? t : null;
    }

    /** True for a handle of a DOM element. */
    /**
     * A picture of a DOM element: the WebView is drawn into a bitmap on the main thread and the element cut out of
     * it at the place the page reports now ("box": CSS px in the viewport, scaled to the view's pixels).
     */
    static JSONObject capture(Dump.Target t) throws Exception {
        JSONObject box = act(t, "box", null);  // off the main thread: the page answers there
        if (!box.optBoolean("ok")) return box;
        return Server.onMain(() -> {
            WebView v = (WebView) t.view.get();
            if (v == null || !v.isAttachedToWindow() || v.getWidth() <= 0 || v.getHeight() <= 0) return fail("the element is not on screen");
            double sx = v.getWidth() / box.getDouble("vw"), sy = v.getHeight() / box.getDouble("vh");
            android.graphics.Rect crop = new android.graphics.Rect(
                (int) Math.round(box.getDouble("vx") * sx), (int) Math.round(box.getDouble("vy") * sy),
                (int) Math.round((box.getDouble("vx") + box.getDouble("w")) * sx), (int) Math.round((box.getDouble("vy") + box.getDouble("h")) * sy));
            android.graphics.Bitmap cut = Server.draw(v, crop);
            return ok().put("jpeg", Server.encode(cut)).put("w", cut.getWidth()).put("h", cut.getHeight());
        }, 4000);
    }

    static boolean owns(int h) {
        Dump.Target t = Dump.targets.get(h);
        return t != null && t.isWeb();
    }

    /** True for the WebView itself (scrolling the page). */
    static boolean isRoot(int h) {
        Dump.Target t = Dump.targets.get(h);
        return t != null && !t.isWeb() && !t.isVirtual() && t.view.get() instanceof WebView;
    }

    static JSONObject scrollRoot(int h, String dir) throws Exception {
        Dump.Target t = Dump.targets.get(h);
        WebView v = t == null ? null : (WebView) t.view.get();
        if (v == null) return fail("stale handle");
        Object r;
        try {
            r = eval(v, "scrollPage(" + JSONObject.quote(dir) + ")", EVAL_MS);
        } catch (TimeoutException e) {
            return fail(e.getMessage());
        }
        return r instanceof JSONObject ? (JSONObject) r : null;  // null: let the caller use the native path
    }

    static JSONObject handle(String cmd, JSONObject r) throws Exception {
        int h = r.getInt("h");
        Dump.Target t = webTarget(h);
        if (t == null) return fail("stale handle");
        switch (cmd) {
            case "click": return click(h, t, false);
            case "long_click": return click(h, t, true);
            case "set_text": return act(t, "set_text", new JSONObject().put("text", r.getString("text")));
            case "select": return act(t, "select", new JSONObject().put("text", r.getString("text")));
            case "scroll": return act(t, "scroll", new JSONObject().put("dir", r.optString("dir", "down")));
            case "ime_action": return act(t, "submit", null);
            case "focus": {
                JSONObject res = act(t, "focus", null);
                if (res.optBoolean("ok") && res.optBoolean("text_field")) showKeyboard((WebView) t.view.get());
                return res;
            }
            default: return fail(cmd + " is not supported on web elements");
        }
    }

    private static void showKeyboard(final WebView v) throws Exception {
        Server.onMain(() -> {
            v.requestFocus();
            InputMethodManager imm = (InputMethodManager) v.getContext().getSystemService(Context.INPUT_METHOD_SERVICE);
            if (imm != null) imm.showSoftInput(v, InputMethodManager.SHOW_IMPLICIT);
            return null;
        }, 1000);
    }

    /**
     * A tap the page experiences as a person's: the script scrolls the element into view and finds
     * a point where it is what a touch would hit; a touch goes in there as a MotionEvent. Then the
     * page says whether any trusted input event arrived. If none did, nothing reached the page, so
     * a script click cannot double up on the action, and it is used instead. A covered element is
     * refused: that is what a person would also find.
     */
    private static JSONObject click(int h, Dump.Target t, boolean longPress) throws Exception {
        final WebView v = (WebView) t.view.get();
        JSONObject p = act(t, "prepare", null);
        if (!p.optBoolean("ok")) return p;
        int[] size = Server.onMain(() -> new int[]{v.getWidth(), v.getHeight()}, 1000);
        JSONObject vv = p.getJSONObject("vv");
        float x = (float) ((p.getDouble("x") - vv.getDouble("x")) * size[0] / vv.getDouble("w"));
        float y = (float) ((p.getDouble("y") - vv.getDouble("y")) * size[1] / vv.getDouble("h"));
        if (x < 0 || y < 0 || x >= size[0] || y >= size[1]) {
            if (longPress) return fail("the element is outside the WebView's visible area");
            return jsClick(t, "its point is outside the WebView's visible area");
        }
        if (p.optBoolean("scrolled")) Thread.sleep(180);  // let the compositor take the new scroll position
        touch(v, x, y, longPress ? 700 : 40);
        Thread.sleep(longPress ? 100 : 140);
        JSONObject d = act(t, "delivered", null);
        if (d.optBoolean("delivered")) {
            return ok().put("via", longPress ? "web_long_press" : "web_tap").put("target_h", h).put("page_saw_click", d.optBoolean("click"));
        }
        Problems.record("web_tap_dropped", v.getClass().getName(), "a touch on the WebView produced no input event in the page", null);
        if (longPress) return fail("the long press did not reach the page");
        return jsClick(t, "the touch did not reach the page");
    }

    private static JSONObject jsClick(Dump.Target t, String why) throws Exception {
        JSONObject res = act(t, "click", null);
        if (res.optBoolean("ok")) res.put("via", "web_js_click (" + why + ")");
        return res;
    }

    private static void touch(final WebView v, final float x, final float y, long holdMs) throws Exception {
        final long down = SystemClock.uptimeMillis();
        final CountDownLatch released = new CountDownLatch(1);
        MAIN.post(() -> dispatch(v, MotionEvent.ACTION_DOWN, down, down, x, y));
        MAIN.postDelayed(() -> {
            dispatch(v, MotionEvent.ACTION_UP, down, SystemClock.uptimeMillis(), x, y);
            released.countDown();
        }, holdMs);
        released.await(holdMs + 1500, TimeUnit.MILLISECONDS);
    }

    private static void dispatch(WebView v, int action, long downTime, long eventTime, float x, float y) {
        MotionEvent e = MotionEvent.obtain(downTime, eventTime, action, x, y, 0);
        e.setSource(InputDevice.SOURCE_TOUCHSCREEN);
        try {
            v.dispatchTouchEvent(e);
        } finally {
            e.recycle();
        }
    }

    // ---- what the page itself tells the app: loads, errors, alert()/confirm() ----------------

    /** Called when an app sets its WebViewClient / WebChromeClient: hook the app's own class (the
     * framework's base methods are empty and apps override without calling super). */
    static void hookClient(Object client, boolean chrome) {
        if (client == null) return;
        Class<?> base = chrome ? WebChromeClient.class : WebViewClient.class;
        for (Class<?> c = client.getClass(); c != null && c != base && c != Object.class; c = c.getSuperclass()) {
            if (!hookedClients.add(c)) continue;
            try {
                if (chrome) {
                    hook(c, "onJsAlert", p -> Events.emit("js_dialog", "kind", "alert", "text", String.valueOf(p.args[2])));
                    hook(c, "onJsConfirm", p -> Events.emit("js_dialog", "kind", "confirm", "text", String.valueOf(p.args[2])));
                    hook(c, "onJsPrompt", p -> Events.emit("js_dialog", "kind", "prompt", "text", String.valueOf(p.args[2])));
                    hook(c, "onConsoleMessage", p -> {
                        if (p.args.length == 1 && p.args[0] instanceof ConsoleMessage) {
                            ConsoleMessage m = (ConsoleMessage) p.args[0];
                            if (m.messageLevel() == ConsoleMessage.MessageLevel.ERROR) Events.logError("console", m.message(), null);
                        }
                    });
                } else {
                    hook(c, "onPageFinished", p -> Events.emit("web_page", "url", String.valueOf(p.args[1]),
                        "title", p.args[0] instanceof WebView ? ((WebView) p.args[0]).getTitle() : null));
                    hook(c, "onReceivedError", p -> {
                        if (p.args.length == 3 && p.args[1] instanceof android.webkit.WebResourceRequest) {
                            android.webkit.WebResourceRequest rq = (android.webkit.WebResourceRequest) p.args[1];
                            if (rq.isForMainFrame()) {
                                android.webkit.WebResourceError e = (android.webkit.WebResourceError) p.args[2];
                                Events.emit("web_error", "url", rq.getUrl().toString(), "error", String.valueOf(e.getDescription()), "code", e.getErrorCode());
                            }
                        } else if (p.args.length == 4) {
                            Events.emit("web_error", "url", String.valueOf(p.args[3]), "error", String.valueOf(p.args[2]), "code", p.args[1]);
                        }
                    });
                    hook(c, "onReceivedHttpError", p -> {
                        android.webkit.WebResourceRequest rq = (android.webkit.WebResourceRequest) p.args[1];
                        android.webkit.WebResourceResponse rs = (android.webkit.WebResourceResponse) p.args[2];
                        if (rq.isForMainFrame()) {
                            Events.emit("web_error", "url", rq.getUrl().toString(), "error", rs.getReasonPhrase(), "code", rs.getStatusCode());
                        }
                    });
                }
            } catch (Throwable t) {
                Problems.record("hook_unavailable", "webview_clients:" + c.getName(), t);
            }
        }
    }

    private static void hook(Class<?> c, String method, final Module.Body0 body) {
        XposedBridge.hookAllMethods(c, method, new XC_MethodHook() {
            @Override protected void beforeHookedMethod(MethodHookParam p) {
                AtomicInteger n = Module.fired.get("webview_clients");
                if (n != null) n.incrementAndGet();
                try { body.run(p); } catch (Throwable t) { Problems.record("hook_callback_error", "webview_clients", t); }
            }
        });
    }
}
