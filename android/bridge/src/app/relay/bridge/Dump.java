package app.relay.bridge;

import android.app.Activity;
import android.graphics.Rect;
import android.graphics.drawable.Drawable;
import android.graphics.drawable.DrawableWrapper;
import android.text.Layout;
import android.text.Spanned;
import android.text.method.PasswordTransformationMethod;
import android.text.InputType;
import android.text.style.ClickableSpan;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowInsets;
import android.view.WindowManager;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityNodeProvider;
import android.widget.AdapterView;
import android.widget.Checkable;
import android.widget.EditText;
import android.widget.ImageView;
import android.widget.ProgressBar;
import android.widget.TextView;

import org.json.JSONArray;
import org.json.JSONObject;

import java.lang.ref.WeakReference;
import java.lang.reflect.Method;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.WeakHashMap;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Reads the live view hierarchy of every window in the process. Must run on the main thread.
 *
 * Each node gets a handle that stays the same while the View lives, so the host can act on
 * "handle 812" even after a redraw. Plain containers are dropped unless they group two or more
 * kept nodes, which keeps cards and list rows intact without the wrapper noise.
 */
final class Dump {
    private static final int MAX_NODES = 1500;
    private static final int MAX_TEXT = 300;

    /** A tappable thing: a real View, a virtual node inside one (Compose), or a DOM element of
     * the page a WebView shows (then `view` is the WebView and `webId` names the element). */
    static final class Target {
        final WeakReference<View> view;
        final int virtualId;  // AccessibilityNodeProvider.HOST_VIEW_ID for real views
        final int webId;      // the page script's id for a DOM element, else -1
        final String epoch;   // which page of the WebView webId belongs to
        final Rect area;      // a part of the view, in its own coordinates (a link in its text), else null
        Target(View v, int virtualId) { this(v, virtualId, -1, null, null); }
        Target(View v, int virtualId, int webId, String epoch) { this(v, virtualId, webId, epoch, null); }
        Target(View v, int virtualId, int webId, String epoch, Rect area) {
            this.view = new WeakReference<>(v);
            this.virtualId = virtualId;
            this.webId = webId;
            this.epoch = epoch;
            this.area = area;
        }
        boolean isVirtual() { return virtualId != AccessibilityNodeProvider.HOST_VIEW_ID; }
        boolean isWeb() { return webId >= 0; }
    }

    private static int nextHandle = 1;
    private static final Map<View, Integer> viewHandles = new WeakHashMap<>();
    private static final Map<String, Integer> virtualHandles = new HashMap<>();
    private static final Map<String, Integer> linkHandles = new HashMap<>();
    private static final Map<String, Integer> webHandles = new HashMap<>();
    private static final Map<Integer, String> webKeys = new HashMap<>();
    /** Web handles issued for each WebView by its latest dump; older ones are dropped. */
    private static final Map<View, java.util.Set<Integer>> webIssued = new WeakHashMap<>();
    static final Map<Integer, Target> targets = Collections.synchronizedMap(new HashMap<>());

    private static int count;

    static synchronized int handle(View v) {
        Integer h = viewHandles.get(v);
        if (h == null) {
            h = nextHandle++;
            viewHandles.put(v, h);
            targets.put(h, new Target(v, AccessibilityNodeProvider.HOST_VIEW_ID));
        }
        return h;
    }

    static synchronized int handle(View host, int virtualId) {
        String key = System.identityHashCode(host) + ":" + virtualId;
        Integer h = virtualHandles.get(key);
        if (h == null) {
            h = nextHandle++;
            virtualHandles.put(key, h);
            targets.put(h, new Target(host, virtualId));
        }
        return h;
    }

    /** A link inside a TextView's text: tapped like a finger at `area` (the view's own coordinates), so the
     * view's own link handling runs. The same link keeps its handle; its area is the latest dump's. */
    static synchronized int handleLink(View tv, int start, int end, Rect area) {
        String key = System.identityHashCode(tv) + ":" + start + "-" + end;
        Integer h = linkHandles.get(key);
        if (h == null) {
            h = nextHandle++;
            linkHandles.put(key, h);
        }
        targets.put(h, new Target(tv, AccessibilityNodeProvider.HOST_VIEW_ID, -1, null, area));
        return h;
    }

    /** A DOM element. The epoch is part of the key: a reloaded page restarts the script's ids,
     * and a handle from the old page must never land on the new page's element. */
    static synchronized int handleWeb(View host, String epoch, int webId) {
        String key = System.identityHashCode(host) + ":" + epoch + ":" + webId;
        Integer h = webHandles.get(key);
        if (h == null) {
            h = nextHandle++;
            webHandles.put(key, h);
            webKeys.put(h, key);
            targets.put(h, new Target(host, AccessibilityNodeProvider.HOST_VIEW_ID, webId, epoch));
        }
        return h;
    }

    /** After a dump of `host`: handles it issued before and did not list again are forgotten,
     * so a long session on a busy page does not accumulate dead elements. */
    static synchronized void retainWeb(View host, java.util.Set<Integer> kept) {
        java.util.Set<Integer> old = webIssued.put(host, kept);
        if (old == null) return;
        for (Integer h : old) {
            if (kept.contains(h)) continue;
            targets.remove(h);
            String key = webKeys.remove(h);
            if (key != null) webHandles.remove(key);
        }
    }

    /** The resumed activity: from the resume hooks, else from the visible activity window's
     * context, which needs no hook at all. */
    static Activity currentActivity() {
        Activity a = Module.resumed.get();
        if (a != null && !a.isFinishing() && !a.isDestroyed()) return a;
        Activity found = null;
        for (WeakReference<View> r : Module.roots) {
            View v = r.get();
            WindowManager.LayoutParams lp = v == null ? null : Module.rootParams.get(v);
            if (v == null || !v.isAttachedToWindow() || v.getWindowVisibility() != View.VISIBLE
                || (lp != null && lp.type != WindowManager.LayoutParams.TYPE_BASE_APPLICATION)) continue;
            android.content.Context c = v.getContext();
            while (c instanceof android.content.ContextWrapper && !(c instanceof Activity)) {
                c = ((android.content.ContextWrapper) c).getBaseContext();
            }
            if (c instanceof Activity && !((Activity) c).isFinishing()) {
                found = (Activity) c;
                if (v.hasWindowFocus()) break;
            }
        }
        if (found != null) Module.resumed = new WeakReference<>(found);
        return found;
    }

    static List<View> liveRoots() {
        List<View> out = new ArrayList<>();
        Activity a = currentActivity();
        View current = a != null && a.getWindow() != null ? a.getWindow().peekDecorView() : null;
        for (WeakReference<View> r : Module.roots) {
            View v = r.get();
            if (v == null || !v.isAttachedToWindow() || v.getVisibility() != View.VISIBLE
                || v.getWindowVisibility() != View.VISIBLE) continue;
            // A finishing activity keeps its window during the exit animation; only the resumed
            // activity's window is the screen.
            WindowManager.LayoutParams lp = Module.rootParams.get(v);
            boolean activityWindow = lp == null || lp.type == WindowManager.LayoutParams.TYPE_BASE_APPLICATION;
            if (activityWindow && current != null && v != current) continue;
            out.add(v);
        }
        if (out.isEmpty() && a != null && a.getWindow() != null) out.add(a.getWindow().getDecorView());
        return out;
    }

    static JSONObject snapshot() throws Exception {
        count = 0;
        // Forget handles whose views were garbage collected.
        synchronized (targets) {
            targets.values().removeIf(t -> t.view.get() == null);
        }
        JSONObject out = new JSONObject();
        out.put("pkg", Module.pkg).put("process", Module.process);
        Activity a = currentActivity();
        out.put("activity", a == null ? JSONObject.NULL : a.getClass().getName());
        JSONArray fragments = new JSONArray();
        synchronized (Module.resumedFragments) {
            for (Object f : Module.resumedFragments.keySet()) fragments.put(f.getClass().getName());
        }
        out.put("fragments", fragments);
        if (a != null) {
            Rect b = a.getWindowManager().getCurrentWindowMetrics().getBounds();
            out.put("screen", new JSONArray().put(b.width()).put(b.height()));
        }
        // Topmost window first, so a huge screen cannot use up the node budget before the
        // popup or dialog over it is read; reported bottom to top as before.
        List<JSONObject> topFirst = new ArrayList<>();
        boolean ime = false;
        List<View> roots = liveRoots();
        Collections.reverse(roots);
        for (View root : roots) {
            WindowManager.LayoutParams lp = Module.rootParams.get(root);
            int[] off = new int[2];
            root.getLocationOnScreen(off);
            JSONObject w = new JSONObject();
            int type = lp == null ? WindowManager.LayoutParams.TYPE_BASE_APPLICATION : lp.type;
            w.put("type", type).put("kind", kind(type))
                .put("title", lp == null ? JSONObject.NULL : String.valueOf(lp.getTitle()))
                .put("focused", root.hasWindowFocus())
                .put("bounds", new JSONArray().put(off[0]).put(off[1])
                    .put(off[0] + root.getWidth()).put(off[1] + root.getHeight()));
            WindowInsets insets = root.getRootWindowInsets();
            if (insets != null && insets.isVisible(WindowInsets.Type.ime())) ime = true;
            w.put("nodes", new JSONArray(visit(root, off, null)));
            topFirst.add(w);
        }
        Collections.reverse(topFirst);
        out.put("windows", new JSONArray(topFirst));
        out.put("ime_visible", ime);
        out.put("truncated", count >= MAX_NODES);
        out.put("events_seq", Events.lastSeq());
        return out;
    }

    static String kind(int type) {
        if (type == WindowManager.LayoutParams.TYPE_BASE_APPLICATION) return "activity";
        if (type == WindowManager.LayoutParams.TYPE_APPLICATION) return "dialog";
        if (type == WindowManager.LayoutParams.TYPE_INPUT_METHOD) return "keyboard";
        if (type >= WindowManager.LayoutParams.FIRST_SUB_WINDOW && type <= WindowManager.LayoutParams.LAST_SUB_WINDOW) return "popup";
        return "other";
    }

    /** Kept nodes of this subtree, each with its kept children under "c". */
    private static List<JSONObject> visit(View v, int[] off, View listParent) throws Exception {
        if (count >= MAX_NODES || v.getVisibility() != View.VISIBLE || v.getAlpha() <= 0.01f) return Collections.emptyList();
        Rect r = new Rect();
        if (!v.getGlobalVisibleRect(r) || r.isEmpty()) return Collections.emptyList();
        r.offset(off[0], off[1]);

        boolean isList = isList(v);
        List<JSONObject> kids = new ArrayList<>();
        if (v instanceof ViewGroup) {
            ViewGroup g = (ViewGroup) v;
            for (int i = 0; i < g.getChildCount(); i++) kids.addAll(visit(g.getChildAt(i), off, isList ? v : null));
        }
        if (v instanceof TextView) kids.addAll(links((TextView) v, off));
        AccessibilityNodeProvider provider = v.getAccessibilityNodeProvider();
        if (provider != null) {
            List<JSONObject> virtual = virtualChildren(v, provider, provider.createAccessibilityNodeInfo(AccessibilityNodeProvider.HOST_VIEW_ID), 0, new HashSet<>());
            // Compose/Chromium hosts whose provider yields nothing are a blind spot for the phone agent.
            if (virtual.isEmpty() && !(v instanceof android.webkit.WebView)) Problems.record("virtual_nodes_empty", v.getClass().getName(), null, null);
            // Compose cells whose text is drawn by hosted TextViews: the selected one, from those views.
            if (!(v instanceof android.webkit.WebView)) Selection.infer(v, off, virtual);
            kids.addAll(virtual);
        } else if (isOpaque(v)) {
            Problems.record("opaque_surface", v.getClass().getName(), "draws content with no views and no accessibility provider", null);
        }

        JSONObject n = describe(v, r);
        if (listParent != null) listItem(n, listParent, v);
        if (isList) list(n, v);
        boolean data = n.length() > 3;  // more than h, cls, b
        if (data || kids.size() >= 2) {
            count++;
            if (!kids.isEmpty()) n.put("c", new JSONArray(kids));
            return Collections.singletonList(n);
        }
        return kids;
    }

    private static JSONObject describe(View v, Rect r) throws Exception {
        JSONObject n = new JSONObject();
        n.put("h", handle(v));
        n.put("cls", v.getClass().getName());
        n.put("b", new JSONArray().put(r.left).put(r.top).put(r.right).put(r.bottom));
        String base = baseClass(v.getClass());
        if (base != null && !base.equals(v.getClass().getName())) n.put("base", base);
        String id = idName(v);
        if (id != null) n.put("id", id);
        if (Guard.blocked(v) != null) n.put("blocked", Guard.MARK);
        if (v instanceof TextView) {
            TextView tv = (TextView) v;
            boolean password = isPassword(tv);
            CharSequence text = tv.getText();
            if (text != null && text.length() > 0) n.put("text", password ? "[redacted]" : clip(text));
            if (!password && text != null && text.length() > MAX_TEXT) n.put("text_len", text.length());  // read_text reads it all
            CharSequence hint = tv.getHint();
            if (hint != null && hint.length() > 0) n.put("hint", clip(hint));
            if (password) flag(n, "password");
            if (tv.onCheckIsTextEditor() && tv.isEnabled()) flag(n, "edit");
            if (v instanceof EditText) flag(n, "edit");
            // A field that takes no keyboard focus is filled by the dialog a tap opens (a date, a time, a list):
            // text set into it shows, but the app keeps its own value (OpenTracks' date filter, 2026-11-15).
            if (v instanceof EditText && tv.isEnabled() && !v.isFocusableInTouchMode()) flag(n, "picker");
        }
        CharSequence desc = v.getContentDescription();
        if (desc != null && desc.length() > 0) n.put("desc", clip(desc));
        if (!n.has("text") && !n.has("desc") && v instanceof ViewGroup) {
            String drawn = drawnText(v);
            if (drawn != null) n.put("text", clip(drawn));
        }
        String icon = iconName(v);
        if (icon != null) n.put("icon", icon);
        if (v instanceof android.webkit.WebView) {
            // Web.attach fills in the page's DOM as this node's children after the snapshot.
            android.webkit.WebView wv = (android.webkit.WebView) v;
            n.put("webview", true);
            String url = wv.getUrl();
            if (url != null) n.put("url", clip(url));
            String title = wv.getTitle();
            if (title != null && !title.isEmpty()) n.put("title", clip(title));
            if (wv.getProgress() < 100) flag(n, "loading");
        }
        JSONObject chip = Inline.views.get(v);
        if (chip != null) {  // a saved-sign-in chip in the keyboard: tappable, but only by the host (Server.click)
            n.put("inline", chip);
            flag(n, "click");
        }
        if (v.hasOnClickListeners()) flag(n, "listener");
        if (v.isClickable()) flag(n, "click");
        if (v.isLongClickable()) flag(n, "long");
        if (v instanceof Checkable) {
            flag(n, "checkable");
            if (((Checkable) v).isChecked()) flag(n, "checked");
        }
        if (v.isSelected() && !isMarquee(v)) flag(n, "selected");
        if (v.isFocused()) flag(n, "focused");
        if (!v.isEnabled()) flag(n, "disabled");
        // TextViews report scrollable whenever their text overflows; only containers really scroll.
        if (v instanceof ViewGroup || v.getClass().getName().contains("WebView")) {
            if (v.canScrollVertically(1)) flag(n, "can_scroll_down");
            if (v.canScrollVertically(-1)) flag(n, "can_scroll_up");
            if (v.canScrollHorizontally(1)) flag(n, "can_scroll_right");
            if (v.canScrollHorizontally(-1)) flag(n, "can_scroll_left");
        }
        if (v instanceof ProgressBar) {
            ProgressBar pb = (ProgressBar) v;
            if (pb.isIndeterminate()) flag(n, "spinner");
            else {
                int low = pb instanceof android.widget.AbsSeekBar ? pb.getMin() : 0;  // the floor, when it is not 0
                n.put("progress", pb.getProgress() + "/" + pb.getMax() + (low != 0 ? " (from " + low + ")" : ""));
            }
        }
        // Controls the host can set to a value directly (Controls.setValue).
        if (v instanceof android.widget.AbsSeekBar && v.isEnabled()) flag(n, "adjust");
        if (v instanceof android.widget.NumberPicker) {
            android.widget.NumberPicker np = (android.widget.NumberPicker) v;
            n.put("progress", np.getValue() + "/" + np.getMaxValue());
            if (v.isEnabled()) flag(n, "adjust");
        }
        if (info(v.getClass()).pager) pageOf(n, v);
        if (v.isClickable() || v.isLongClickable() || v.hasOnClickListeners() || v.isFocusable()) accessibility(v, n);
        if (isOpaque(v)) flag(n, "opaque");
        return n;
    }

    /** "page 2 of 5" of a ViewPager, so the position is known without paging through it. */
    private static void pageOf(JSONObject n, View pager) throws Exception {
        flag(n, "pager");
        try {
            Object adapter = pager.getClass().getMethod("getAdapter").invoke(pager);
            int cur = ((Number) pager.getClass().getMethod("getCurrentItem").invoke(pager)).intValue();
            int count;
            try { count = ((Number) adapter.getClass().getMethod("getItemCount").invoke(adapter)).intValue(); }
            catch (NoSuchMethodException e) { count = ((Number) adapter.getClass().getMethod("getCount").invoke(adapter)).intValue(); }
            n.put("page", (cur + 1) + "/" + count);
        } catch (Throwable ignored) {
        }
    }

    static void flag(JSONObject n, String f) throws Exception {
        JSONArray flags = n.optJSONArray("f");
        if (flags == null) { flags = new JSONArray(); n.put("f", flags); }
        for (int i = 0; i < flags.length(); i++) if (f.equals(flags.optString(i))) return;
        flags.put(f);
    }

    static boolean isPasswordField(TextView tv) { return isPassword(tv); }

    /** A marquee TextView only scrolls while selected, so apps call setSelected(true) on titles
     * that are not a choice of anything; a tappable one may still be a real selection. */
    static boolean isMarquee(View v) {
        return v instanceof TextView && ((TextView) v).getEllipsize() == android.text.TextUtils.TruncateAt.MARQUEE
            && !v.isClickable() && !v.hasOnClickListeners();
    }

    private static boolean isPassword(TextView tv) {
        int variation = tv.getInputType() & (InputType.TYPE_MASK_CLASS | InputType.TYPE_MASK_VARIATION);
        return tv.getTransformationMethod() instanceof PasswordTransformationMethod
            || variation == (InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD)
            || variation == (InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_WEB_PASSWORD)
            || variation == (InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD)
            || variation == (InputType.TYPE_CLASS_NUMBER | InputType.TYPE_NUMBER_VARIATION_PASSWORD);
    }

    /** What a snapshot asks of every view's class, worked out once per class per process: a screen holds
     * thousands of views of a few dozen classes, and these are class-hierarchy walks and reflection lookups
     * (a missing getIcon threw NoSuchMethodException for nearly every view, on the main thread). */
    private static final class ClassInfo {
        final String base;
        final boolean list, opaque, pager;
        final Method icon, childPosition, textContent;

        ClassInfo(Class<?> c) {
            base = baseClassOf(c);
            list = AdapterView.class.isAssignableFrom(c) || extendsNamed(c, "RecyclerView");
            String name = c.getName();
            opaque = !android.webkit.WebView.class.isAssignableFrom(c)
                && (android.view.SurfaceView.class.isAssignableFrom(c) || android.view.TextureView.class.isAssignableFrom(c)
                    || name.contains("WebView") || name.contains("ContentView") || name.contains("AndroidComposeView")
                    || name.contains("FlutterView") || name.contains("UnityPlayer"));
            pager = simpleNamed(c, "ViewPager2") || simpleNamed(c, "ViewPager");
            icon = method(c, "getIcon");
            // Litho draws text on its host views (no TextView): getTextContent().getTextItems() is the only way to it.
            textContent = ViewGroup.class.isAssignableFrom(c) ? method(c, "getTextContent") : null;
            childPosition = list && !AdapterView.class.isAssignableFrom(c) ? method(c, "getChildAdapterPosition", View.class) : null;
        }

        private static Method method(Class<?> c, String name, Class<?>... params) {
            try {
                return c.getMethod(name, params);
            } catch (Throwable t) {
                return null;
            }
        }
    }

    private static final Map<Class<?>, ClassInfo> CLASS_INFO = new ConcurrentHashMap<>();

    private static ClassInfo info(Class<?> c) {
        ClassInfo i = CLASS_INFO.get(c);
        if (i == null) {
            i = new ClassInfo(c);
            CLASS_INFO.put(c, i);
        }
        return i;
    }

    private static boolean extendsNamed(Class<?> c, String suffix) {
        for (Class<?> k = c; k != null; k = k.getSuperclass()) if (k.getName().endsWith(suffix)) return true;
        return false;
    }

    private static boolean simpleNamed(Class<?> c, String simple) {
        for (Class<?> k = c; k != null; k = k.getSuperclass()) {
            if (k.getSimpleName().equals(simple) || k.getName().endsWith("." + simple)) return true;
        }
        return false;
    }

    /** The nearest framework/AndroidX/Material ancestor class, so `com.app.ui.FollowButton`
     * is also known to be a MaterialButton even when the app class name is obfuscated. */
    static String baseClass(Class<?> c) {
        return info(c).base;
    }

    private static String baseClassOf(Class<?> c) {
        for (Class<?> k = c; k != null; k = k.getSuperclass()) {
            String name = k.getName();
            if (name.startsWith("android.") || name.startsWith("androidx.") || name.startsWith("com.google.android.material.")) return name;
        }
        return null;
    }

    static String idName(View v) {
        int id = v.getId();
        if (id == View.NO_ID || (id >>> 24) == 0) return null;  // generated ids have no resource name
        return resourceName(v, id);
    }

    static String iconName(View v) {
        Integer id = Module.viewIcons.get(v);
        String name = null;
        if (v instanceof ImageView) name = drawableName(v, ((ImageView) v).getDrawable());
        if (name == null && id != null) name = resourceName(v, id);
        if (name == null && v instanceof TextView) {
            List<String> names = new ArrayList<>();
            for (Drawable d : ((TextView) v).getCompoundDrawablesRelative()) {
                String n = drawableName(v, d);
                if (n != null) names.add(n);
            }
            if (!names.isEmpty()) name = String.join(",", names);
        }
        if (name == null) {
            Method m = info(v.getClass()).icon;  // MaterialButton, Chip and friends keep their icon outside the compound drawables
            if (m != null) {
                try {
                    Object d = m.invoke(v);
                    if (d instanceof Drawable) name = drawableName(v, (Drawable) d);
                } catch (Throwable ignored) {
                }
            }
        }
        return name;
    }

    static String drawableName(View v, Drawable d) {
        for (int depth = 0; d != null && depth < 4; depth++) {
            Integer id = Module.drawableIds.get(d);
            if (id != null) return resourceName(v, id);
            Drawable current = d.getCurrent();
            if (current != d && (id = Module.drawableIds.get(current)) != null) return resourceName(v, id);
            d = d instanceof DrawableWrapper ? ((DrawableWrapper) d).getDrawable() : null;
        }
        return null;
    }

    static String resourceName(View v, int id) {
        try {
            String name = v.getResources().getResourceEntryName(id);
            // Apps built with resource shrinking + obfuscation rename every entry to this.
            return name.contains("resource_name_obfuscated") ? null : name;
        } catch (Exception e) {
            return null;
        }
    }

    /** Chromium content outside a WebView (Chrome's pages), Compose hosts, SurfaceView/TextureView
     * (maps, video, games, Flutter, Unity): they paint UI the view tree does not describe.
     * A WebView is not opaque: {@link Web} reads its DOM, and marks it opaque only if that fails. */
    static boolean isOpaque(View v) {
        return info(v.getClass()).opaque;
    }

    static void markOpaque(JSONObject n) throws Exception { flag(n, "opaque"); }

    /** RecyclerView (even obfuscated), ListView, GridView and anything scrollable that reports
     * collection info to accessibility. */
    static boolean isList(View v) {
        return info(v.getClass()).list;
    }

    private static void list(JSONObject n, View v) throws Exception {
        flag(n, "list");
        if (v instanceof AdapterView) {
            n.put("items", ((AdapterView<?>) v).getCount());
            return;
        }
        AccessibilityNodeInfo info = v.createAccessibilityNodeInfo();
        AccessibilityNodeInfo.CollectionInfo ci = info.getCollectionInfo();
        // Horizontal lists report one row and many columns.
        if (ci != null) n.put("items", ci.getRowCount() > 1 ? ci.getRowCount() : ci.getColumnCount());
        try {  // RecyclerView.getAdapter().getItemCount() when names survived R8.
            Object adapter = v.getClass().getMethod("getAdapter").invoke(v);
            if (adapter != null) n.put("items", adapter.getClass().getMethod("getItemCount").invoke(adapter));
        } catch (Throwable ignored) {
        }
    }

    private static void listItem(JSONObject n, View list, View child) throws Exception {
        if (list instanceof AdapterView) {
            n.put("li", ((AdapterView<?>) list).getPositionForView(child));
            return;
        }
        Method position = info(list.getClass()).childPosition;
        if (position != null) {
            try {
                Object pos = position.invoke(list, child);
                if (pos instanceof Integer && (Integer) pos >= 0) { n.put("li", pos); return; }
            } catch (Throwable ignored) {
            }
        }
        // getChildAdapterPosition is gone (R8 renamed RecyclerView): accessibility row info instead.
        Problems.record("fallback", "list_position_via_accessibility", list.getClass().getName(), null);
        AccessibilityNodeInfo.CollectionItemInfo item = child.createAccessibilityNodeInfo().getCollectionItemInfo();
        if (item != null) n.put("li", Math.max(item.getRowIndex(), item.getColumnIndex()));
    }

    /** Compose and WebView draw many controls inside one View; their accessibility provider is
     * the framework-level, obfuscation-proof way to enumerate them. */
    /** AccessibilityNodeInfo.getChildId(int), hidden API; null when this Android does not have it. */
    private static final Method CHILD_ID;
    static {
        Method m;
        try {
            m = AccessibilityNodeInfo.class.getMethod("getChildId", int.class);
        } catch (Throwable t) {
            m = null;
        }
        CHILD_ID = m;
    }

    /** `parent` is built by the caller, so each node is created once: Compose builds a node info by walking
     * its semantics tree, and creating it again in the recursion doubled that cost on every screen. */
    private static List<JSONObject> virtualChildren(View host, AccessibilityNodeProvider p, AccessibilityNodeInfo parent, int depth, Set<Integer> seen) {
        List<JSONObject> out = new ArrayList<>();
        if (depth > 40 || count >= MAX_NODES || parent == null) return out;
        for (int i = 0; i < parent.getChildCount(); i++) {
            int id;
            try {
                if (CHILD_ID == null) throw new NoSuchMethodException("getChildId");
                long childId = (long) CHILD_ID.invoke(parent, i);
                id = (int) (childId >> 32);
            } catch (Throwable t) {
                Problems.record("virtual_nodes", "AccessibilityNodeInfo.getChildId", t);
                return out;
            }
            // A child that is a real View (Compose AndroidView interop: HOST_VIEW_ID or the
            // undefined item id) is read by the view walk. Compose can also list a node under
            // more than one parent, which without this guard loops until the node budget is gone.
            if (id == AccessibilityNodeProvider.HOST_VIEW_ID || id == Integer.MAX_VALUE) continue;
            if (!seen.add(id)) {
                Problems.record("virtual_node_cycle", host.getClass().getName(), null, null);
                continue;
            }
            AccessibilityNodeInfo info = p.createAccessibilityNodeInfo(id);
            if (info == null) continue;
            List<JSONObject> kids = virtualChildren(host, p, info, depth + 1, seen);
            if (!info.isVisibleToUser()) { out.addAll(kids); continue; }
            try {
                JSONObject n = describeVirtual(host, id, info);
                boolean data = n.length() > 4;  // more than h, cls, b, virtual
                if (data || kids.size() >= 2) {
                    count++;
                    if (!kids.isEmpty()) n.put("c", new JSONArray(kids));
                    out.add(n);
                } else {
                    out.addAll(kids);
                }
            } catch (Exception ignored) {
            }
        }
        return out;
    }

    /**
     * What a control tells accessibility services (TalkBack): the contract every UI toolkit maps its
     * widgets onto, so a React Native checkbox or a custom toggle reports its role and state here
     * even though its class says nothing. Adds to what the view's own properties gave.
     */
    private static void accessibility(View v, JSONObject n) throws Exception {
        AccessibilityNodeInfo info;
        try {
            info = v.createAccessibilityNodeInfo();
        } catch (Throwable t) {
            Problems.record("accessibility_info", v.getClass().getName(), t);
            return;
        }
        if (info == null) return;
        try {
            CharSequence cls = info.getClassName();
            if (cls != null && cls.length() > 0 && !cls.toString().equals(v.getClass().getName())) n.put("a11y_cls", cls.toString());
            CharSequence role = info.getExtras() == null ? null : info.getExtras().getCharSequence("AccessibilityNodeInfo.roleDescription");
            if (role != null && role.length() > 0) n.put("a11y_role", clip(role));
            CharSequence state = info.getStateDescription();
            if (state != null && state.length() > 0) n.put("state", clip(state));
            // A custom view that draws its own content tells accessibility what it shows and its state (Gmail's
            // rows: "Unread", labels): kept when it says more than the view's own words.
            CharSequence spoken = info.getContentDescription() != null ? info.getContentDescription() : info.getText();
            if (spoken != null && !n.has("desc") && !n.has("text")) {
                String said = spoken.toString().replaceAll("(\\s*,\\s*){2,}", ", ").replaceAll("^[,\\s]+|[,\\s]+$", "");
                if (!said.isEmpty() && addsWords(said, joinTexts(v, 40))) n.put("desc", clip(said));
            } else if (spoken != null && !n.has("state")) {
                // A view with its own words that says more to accessibility after them: a tab's or icon's badge
                // ("Chats" says "Chats, 1 new notification"). The rest is its state.
                String own = n.has("desc") ? n.optString("desc") : n.optString("text");
                String said = spoken.toString().trim();
                if (said.length() > own.length() && said.toLowerCase().startsWith(own.toLowerCase())) {
                    String rest = said.substring(own.length()).replaceAll("^[,;:.\\s·-]+|[,\\s]+$", "");
                    if (!rest.isEmpty() && addsWords(rest, own)) n.put("state", clip(rest));
                }
            }
            if (info.isCheckable()) flag(n, "checkable");
            if (info.isChecked()) flag(n, "checked");
            if (info.isSelected() && !isMarquee(v)) flag(n, "selected");
        } finally {
            info.recycle();
        }
    }

    private static JSONObject describeVirtual(View host, int id, AccessibilityNodeInfo info) throws Exception {
        JSONObject n = new JSONObject();
        Rect r = new Rect();
        info.getBoundsInScreen(r);
        n.put("h", handle(host, id));
        n.put("cls", info.getClassName() == null ? "virtual" : info.getClassName().toString());
        n.put("b", new JSONArray().put(r.left).put(r.top).put(r.right).put(r.bottom));
        n.put("virtual", true);
        if (info.getViewIdResourceName() != null) n.put("id", info.getViewIdResourceName());
        if (info.getText() != null && info.getText().length() > 0) n.put("text", info.isPassword() ? "[redacted]" : clip(info.getText()));
        if (info.getHintText() != null && info.getHintText().length() > 0) n.put("hint", clip(info.getHintText()));
        if (info.getContentDescription() != null && info.getContentDescription().length() > 0) n.put("desc", clip(info.getContentDescription()));
        if (info.getStateDescription() != null && info.getStateDescription().length() > 0) n.put("state", clip(info.getStateDescription()));
        if (info.isClickable()) flag(n, "click");
        if (info.isLongClickable()) flag(n, "long");
        if (info.isEditable()) flag(n, "edit");
        if (info.isPassword()) flag(n, "password");
        if (info.isCheckable()) flag(n, "checkable");
        if (info.isChecked()) flag(n, "checked");
        if (info.isSelected()) flag(n, "selected");
        if (info.isFocused()) flag(n, "focused");
        if (!info.isEnabled()) flag(n, "disabled");
        JSONArray labels = new JSONArray();
        for (AccessibilityNodeInfo.AccessibilityAction a : info.getActionList()) {
            int action = a.getId();
            if (action == AccessibilityNodeInfo.ACTION_SCROLL_FORWARD) flag(n, "can_scroll_down");
            if (action == AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD) flag(n, "can_scroll_up");
            if (a.getLabel() != null) labels.put(a.getLabel().toString());
        }
        if (labels.length() > 0) n.put("actions", labels);
        if (info.getCollectionInfo() != null) n.put("items", info.getCollectionInfo().getRowCount());
        if (info.getCollectionItemInfo() != null) n.put("li", info.getCollectionItemInfo().getRowIndex());
        return n;
    }

    private static final Map<Class<?>, Method> TEXT_ITEMS = new ConcurrentHashMap<>();

    /** Words a Litho host draws itself (Maps' result rows, YouTube Music's song rows), joined with " · ", or null. */
    static String drawnText(View v) {
        Method m = info(v.getClass()).textContent;
        if (m == null) return null;
        try {
            Object content = m.invoke(v);
            if (content == null) return null;
            Method items = TEXT_ITEMS.get(content.getClass());
            if (items == null) {
                items = content.getClass().getMethod("getTextItems");
                items.setAccessible(true);
                TEXT_ITEMS.put(content.getClass(), items);
            }
            Object list = items.invoke(content);
            if (!(list instanceof java.util.Collection)) return null;
            List<String> words = new ArrayList<>();
            for (Object item : (java.util.Collection<?>) list) {
                String w = item == null ? "" : item.toString().trim();
                if (!w.isEmpty() && !w.equals("·") && !w.equals("•")) words.add(w);
            }
            return words.isEmpty() ? null : String.join(" · ", words);
        } catch (Throwable t) {
            Problems.record("drawn_text", v.getClass().getName(), t);
            return null;
        }
    }

    /** The links in a TextView's text (Terms, Privacy policy, "Sign in" inside a sentence): one tappable node each,
     * with its words and where it sits on screen, for the lines of it that are visible. */
    private static List<JSONObject> links(TextView tv, int[] off) {
        CharSequence text = tv.getText();
        Layout layout = tv.getLayout();
        if (!(text instanceof Spanned) || layout == null || isPassword(tv)) return Collections.emptyList();
        Spanned spanned = (Spanned) text;
        ClickableSpan[] spans = spanned.getSpans(0, spanned.length(), ClickableSpan.class);
        if (spans.length == 0) return Collections.emptyList();
        List<JSONObject> out = new ArrayList<>();
        Rect visible = new Rect();
        if (!tv.getLocalVisibleRect(visible)) return out;
        int[] at = new int[2];
        tv.getLocationInWindow(at);
        int[] rootAt = new int[2];
        tv.getRootView().getLocationInWindow(rootAt);
        int dx = tv.getTotalPaddingLeft() - tv.getScrollX(), dy = tv.getTotalPaddingTop() - tv.getScrollY();
        for (ClickableSpan span : spans) {
            int start = spanned.getSpanStart(span), end = spanned.getSpanEnd(span);
            if (start < 0 || end <= start || count >= MAX_NODES) continue;
            int line = layout.getLineForOffset(start);
            int left = (int) layout.getPrimaryHorizontal(start);
            int right = layout.getLineForOffset(end) == line ? (int) layout.getPrimaryHorizontal(end) : (int) layout.getLineRight(line);
            Rect area = new Rect(Math.min(left, right) + dx, layout.getLineTop(line) + dy, Math.max(left, right) + dx,
                layout.getLineBottom(line) + dy);
            if (area.width() <= 0 || !Rect.intersects(area, visible)) continue;
            String words = spanned.subSequence(start, end).toString().trim();
            if (words.isEmpty()) continue;
            try {
                JSONObject n = new JSONObject();
                n.put("h", handleLink(tv, start, end, area));
                n.put("cls", span.getClass().getName());
                n.put("a11y_role", "link");
                // On screen as the view's own bounds are: window coordinates less the root's offset, plus the window's.
                int x = at[0] - rootAt[0] + off[0], y = at[1] - rootAt[1] + off[1];
                n.put("b", new JSONArray().put(area.left + x).put(area.top + y).put(area.right + x).put(area.bottom + y));
                n.put("text", clip(words));
                if (span instanceof android.text.style.URLSpan) {
                    String url = ((android.text.style.URLSpan) span).getURL();
                    if (url != null) n.put("href", clip(url));
                }
                flag(n, "click");
                count++;
                out.add(n);
            } catch (Exception ignored) {
            }
        }
        return out;
    }

    /** Whether `spoken` (what TalkBack says for a view) has words the view's own texts do not: Gmail says
     * "Unread, ..." of a row that only draws the sender and subject. */
    static boolean addsWords(String spoken, String shown) {
        Set<String> seen = new HashSet<>();
        for (String w : shown.toLowerCase().split("[^\\p{L}\\p{N}]+")) if (!w.isEmpty()) seen.add(w);
        for (String w : spoken.toLowerCase().split("[^\\p{L}\\p{N}]+")) if (w.length() > 1 && !seen.contains(w)) return true;
        return false;
    }

    static String joinTexts(View v, int limit) {
        List<String> texts = new ArrayList<>();
        collectTexts(v, texts, limit);
        return String.join(" | ", texts);
    }

    private static void collectTexts(View v, List<String> out, int limit) {
        if (v == null || out.size() >= limit || v.getVisibility() != View.VISIBLE) return;
        if (v instanceof TextView) {
            CharSequence t = ((TextView) v).getText();
            if (t != null && t.length() > 0) out.add(clip(t));
        }
        if (v instanceof ViewGroup) {
            ViewGroup g = (ViewGroup) v;
            for (int i = 0; i < g.getChildCount(); i++) collectTexts(g.getChildAt(i), out, limit);
        }
    }

    static String clip(CharSequence s) {
        String t = s.toString();
        return t.length() <= MAX_TEXT ? t : t.substring(0, MAX_TEXT) + "…";
    }
}
