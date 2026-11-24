package app.relay.bridge;

import android.view.View;
import android.widget.TextView;

import org.json.JSONArray;
import org.json.JSONObject;

import java.lang.reflect.Method;
import java.lang.reflect.Modifier;
import java.util.Collection;
import java.util.Map;
import java.util.regex.Pattern;

/**
 * Read-only look at what a view and the objects behind it hold: a chain of zero-argument getters,
 * such as `getAdapter().getItemCount()` or `getTag()`. Only public instance methods named
 * get/is/has/can/should are called; nothing takes arguments, nothing is written. A password field
 * is refused outright.
 */
final class Inspect {
    private static final Pattern GETTER = Pattern.compile("(get|is|has|can|should)[A-Z0-9_]\\w*");
    private static final Pattern STEP = Pattern.compile("\\s*(\\w+)\\(\\)\\s*");
    private static final int MAX_STEPS = 8, MAX_ITEMS = 20;

    static JSONObject run(int h, String path) throws Exception {
        Dump.Target t = Server.target(h);
        if (t == null) return fail("stale handle");
        if (t.isWeb() || t.isVirtual()) return fail("only native views can be inspected");
        Object cur = t.view.get();
        if (cur instanceof TextView && Dump.isPasswordField((TextView) cur)) return fail("a password field is not inspected");
        String[] steps = path.split("\\.");
        if (steps.length > MAX_STEPS) return fail("at most " + MAX_STEPS + " calls in a chain");
        StringBuilder walked = new StringBuilder();
        for (String raw : steps) {
            java.util.regex.Matcher m = STEP.matcher(raw);
            if (!m.matches()) return fail("write each step as name(), like getAdapter().getItemCount(): got " + raw);
            String name = m.group(1);
            if (!GETTER.matcher(name).matches()) return fail(name + "() is not a getter (get/is/has/can/should)");
            if (cur == null) return fail(walked + " returned null");
            Method method = null;
            for (Method k : cur.getClass().getMethods()) {
                if (k.getName().equals(name) && k.getParameterTypes().length == 0 && !Modifier.isStatic(k.getModifiers())) { method = k; break; }
            }
            if (method == null) return fail(cur.getClass().getName() + " has no public " + name + "()");
            method.setAccessible(true);
            cur = method.invoke(cur);
            walked.append(walked.length() > 0 ? "." : "").append(name).append("()");
            if (cur instanceof TextView && Dump.isPasswordField((TextView) cur)) return fail("a password field is not inspected");
        }
        return new JSONObject().put("ok", true).put("path", walked.toString()).put("result", show(cur));
    }

    private static JSONObject fail(String why) throws Exception { return new JSONObject().put("ok", false).put("error", why); }

    private static Object show(Object v) throws Exception {
        if (v == null) return JSONObject.NULL;
        if (v instanceof Number || v instanceof Boolean || v instanceof Enum) return v instanceof Enum ? v.toString() : v;
        if (v instanceof CharSequence) return Events.clip(v.toString(), 500);
        JSONObject o = new JSONObject().put("class", v.getClass().getName());
        if (v instanceof Collection) {
            Collection<?> c = (Collection<?>) v;
            JSONArray items = new JSONArray();
            for (Object x : c) { if (items.length() >= MAX_ITEMS) break; items.put(Events.clip(String.valueOf(x), 300)); }
            return o.put("size", c.size()).put("items", items);
        }
        if (v instanceof Map) return o.put("size", ((Map<?, ?>) v).size()).put("text", Events.clip(String.valueOf(v), 500));
        if (v.getClass().isArray()) return o.put("length", java.lang.reflect.Array.getLength(v));
        if (v instanceof View) return o.put("text", Events.clip(String.valueOf(v), 200));
        return o.put("text", Events.clip(String.valueOf(v), 500));
    }
}
